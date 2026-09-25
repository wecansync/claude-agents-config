#!/usr/bin/env python3
"""Regression tests for two Windows installer bugs:

1. The python-runtime .cmd/.ps1 shims (agentfleet, claude-agents-doctor,
   claude-fleet-setup) only ever tried "py -3" then bare "python". On a
   machine whose only Python is off PATH (e.g. uv-managed), bare "python" is
   the Microsoft Store stub and the shim exits 9009 -- even though the hooks
   this same installer wires up work fine there, because they pin
   sys.executable. The shims must try the pinned interpreter first.

2. clean_hook_groups() compared a marker-bearing hook's live command with the
   install journal's recorded "installed" command byte-for-byte. A command
   that only differs by quoting or marker-comment convention (e.g. something
   outside the installer re-quoted settings.json, or the journal predates the
   "# marker" convention) was therefore misclassified as a real user edit and
   frozen verbatim -- including, in one real incident, a stale reference to a
   script this bundle has since retired and deletes on the same run, which
   left a SessionStart hook that failed every session.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def load_installer():
    spec = importlib.util.spec_from_file_location("agentfleet_install_windows_repairs", ROOT / "bin/install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_for(home: Path, config: Path) -> dict:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1"}


def find_hook(settings: dict, event: str, kind: str) -> dict:
    """The single inner hook dict under settings["hooks"][event] whose command
    carries the claude-agents-config:<kind> marker."""
    for group in settings.get("hooks", {}).get(event, []):
        for hook in group.get("hooks", []):
            if f"claude-agents-config:{kind}" in hook.get("command", ""):
                return hook
    raise AssertionError(f"no {kind} hook found under {event}")


def hook_present(settings: dict, event: str, kind: str) -> bool:
    for group in settings.get("hooks", {}).get(event, []):
        for hook in group.get("hooks", []):
            if f"claude-agents-config:{kind}" in hook.get("command", ""):
                return True
    return False


def journal_entry(meta: dict, event: str, kind: str) -> dict:
    identity = f"hook:{event}:{kind}"
    for entry in meta.get("settingsJournal", []):
        if entry.get("id") == identity:
            return entry
    raise AssertionError(f"no journal entry {identity!r} in {sorted(e.get('id') for e in meta.get('settingsJournal', []))}")


def alternate_marker_form(command: str) -> str:
    """A hook command that names the same script and arguments but differs in
    both quoting convention and marker-comment style -- e.g. the current
    double-quoted-arguments " # ..." form versus the pre-2.0 unquoted
    " & rem ..." form (see hook_marker's comment in bin/install.py)."""
    match = re.search(r"\s*#\s*claude-agents-config:([a-z0-9-]+)\s*$", command)
    assert match, f"expected a '#'-marker command, got: {command!r}"
    kind = match.group(1)
    body = command[: match.start()]
    tokens = shlex.split(body, posix=True)
    if '"' in body:
        rebuilt = " ".join(tokens)
    else:
        rebuilt = " ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    return rebuilt + " & rem claude-agents-config:" + kind


def path_dirs_without_python() -> str:
    """The current PATH with every directory that holds python.exe or
    py.exe removed, so a shim can only find an interpreter by the pinned
    absolute path this installer wrote into it."""
    kept = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        if (directory / "python.exe").is_file() or (directory / "py.exe").is_file():
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


class ShimGenerationTests(unittest.TestCase):
    """Unit-level: the generated shim bytes, without running an install."""

    def setUp(self):
        self.installer = load_installer()

    def test_cmd_shim_tries_pinned_interpreter_before_py_and_python(self):
        script = Path("C:/fleet/claude-agents-doctor.py")
        body = self.installer.cmd_dispatch(script, "python").decode("utf-8")
        pinned = '"' + sys.executable.replace('"', '""') + '"'
        self.assertIn(pinned, body, "the shim must reference the installer's own interpreter")
        self.assertIn("if not exist", body)
        self.assertIn("where py", body, "the py launcher fallback must still be present")
        self.assertIn("python ", body, "the bare python fallback must still be present")
        self.assertLess(body.index(pinned), body.index("where py"), "the pinned interpreter must be tried first")
        if_lines = [line for line in body.split("\r\n") if line.lower().startswith("if ")]
        self.assertTrue(if_lines, "expected at least one if line")
        for line in if_lines:
            # The pinned path itself may contain "(" (e.g. "Program Files (x86)").
            self.assertNotIn("(", line.replace(pinned, ""), f"no parenthesized errorlevel block: {line!r}")

    def test_ps1_shim_tries_pinned_interpreter_before_py_and_python(self):
        script = Path("C:/fleet/claude-agents-doctor.py")
        body = self.installer.powershell_dispatch(script, "python").decode("utf-8")
        pinned = "'" + sys.executable.replace("'", "''") + "'"
        self.assertIn(pinned, body)
        self.assertIn("Test-Path -LiteralPath", body)
        self.assertIn("Get-Command py", body, "the py launcher fallback must still be present")
        self.assertIn("Get-Command python", body, "the bare python fallback must still be present")
        # The pinned interpreter is tried first at runtime: it is the "if",
        # the py launcher is "elseif", and bare python is the final "else".
        if_elseif_else = re.search(r"if \(Test-Path.*?\belseif\b.*?\belse\b", body)
        self.assertIsNotNone(if_elseif_else, "expected a single if/elseif/else testing Test-Path before py before python")
        self.assertIn("exit $LASTEXITCODE", body)
        self.assertNotIn('\\"', body, "PowerShell 5.1 strips a double quote embedded in a native-command argument")

    def test_node_shims_are_unchanged(self):
        script = Path("fleet") / "generate-claude-agents.mjs"
        cmd_body = self.installer.cmd_dispatch(script, "node").decode("utf-8")
        self.assertEqual(
            cmd_body,
            '@echo off\r\nsetlocal\r\nwhere node >nul 2>nul\r\nif errorlevel 1 (echo Node.js ^>=18 is required 1^>^&2 & exit /b 1)\r\n'
            f'node "{script}" %*\r\nexit /b %errorlevel%\r\n',
        )
        ps1_body = self.installer.powershell_dispatch(script, "node").decode("utf-8")
        self.assertEqual(
            ps1_body,
            "$node = Get-Command node -ErrorAction SilentlyContinue\nif (-not $node) { throw 'Node.js >=18 is required' }\n"
            f"& $node.Source '{script}' @args\nexit $LASTEXITCODE\n",
        )

    def test_reinstall_updates_an_unmodified_old_shim_without_conflict(self):
        # Managed prefix files: a shim an older release installed, unmodified
        # since, is a routine update, never a conflict requiring --force-owned.
        with tempfile.TemporaryDirectory(prefix="af-shim-refresh-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            first = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            cmd_path = home / ".local/bin/agentfleet.cmd"
            self.assertTrue(cmd_path.is_file())

            # Put back the shim the previous release generated (py -3, then
            # bare python, no pinned interpreter) and record it as installed.
            script_arg = '"' + str(home / ".claude" / "agentfleet.py").replace('"', '""') + '"'
            args = "".join(f' --{flag} "' + str(value).replace('"', '""') + '"' for flag, value in (("home", home), ("prefix", home / ".local"), ("config-home", config)))
            old_body = (
                f"@echo off\r\nsetlocal EnableExtensions DisableDelayedExpansion\r\nwhere py >nul 2>nul\r\nif not errorlevel 1 goto run_py\r\n"
                f"python {script_arg}{args} %*\r\nexit /b %errorlevel%\r\n:run_py\r\npy -3 {script_arg}{args} %*\r\nexit /b %errorlevel%\r\n"
            ).encode("utf-8")
            cmd_path.write_bytes(old_body)
            meta_path = home / ".claude/.claude-agents-config-install.json"
            meta = json.loads(meta_path.read_text())
            entries = [entry for entry in meta["managed"] if entry.get("id") == "prefix:agentfleet.cmd"]
            self.assertEqual(len(entries), 1, "the shim is a managed file")
            entries[0]["sha256"] = hashlib.sha256(old_body).hexdigest()
            meta_path.write_text(json.dumps(meta, indent=2))

            second = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn(f"update: {cmd_path}", second.stdout)
            self.assertNotIn("force-owned", second.stdout + second.stderr)
            body = cmd_path.read_text(encoding="utf-8")
            pinned = '"' + sys.executable.replace('"', '""') + '"'
            self.assertIn(pinned, body)


@unittest.skipUnless(os.name == "nt", "exercises the generated .cmd/.ps1 shims directly")
class ShimExecutionTests(unittest.TestCase):
    def test_cmd_and_ps1_shim_use_pinned_interpreter_with_python_off_path(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory(prefix="af-shim-exec-") as raw:
            root = Path(raw)
            probe = root / "probe.py"
            probe.write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
            env = {**os.environ, "PATH": path_dirs_without_python(), "PYTHONDONTWRITEBYTECODE": "1"}

            cmd_path = root / "probe.cmd"
            cmd_path.write_bytes(installer.cmd_dispatch(probe, "python"))
            cmd_result = subprocess.run(["cmd.exe", "/c", str(cmd_path)], env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(cmd_result.returncode, 0, cmd_result.stdout + cmd_result.stderr)
            self.assertEqual(cmd_result.stdout.strip(), sys.executable)

            ps1_path = root / "probe.ps1"
            ps1_path.write_bytes(installer.powershell_dispatch(probe, "python"))
            ps1_result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1_path)],
                env=env, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(ps1_result.returncode, 0, ps1_result.stdout + ps1_result.stderr)
            self.assertEqual(ps1_result.stdout.strip(), sys.executable)

    def test_shim_falls_back_to_python_when_the_pinned_interpreter_is_missing(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory(prefix="af-shim-missing-pin-") as raw:
            root = Path(raw)
            probe = root / "probe.py"
            probe.write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
            with unittest.mock.patch.object(installer.sys, "executable", r"C:\nonexistent\python.exe"):
                cmd_bytes = installer.cmd_dispatch(probe, "python")
                ps1_bytes = installer.powershell_dispatch(probe, "python")
            # The pinned path in the shim is now nonexistent, but the real
            # interpreter's directory is back on PATH, so "python" resolves.
            real_dir = str(Path(sys.executable).parent)
            env = {**os.environ, "PATH": real_dir + os.pathsep + path_dirs_without_python(), "PYTHONDONTWRITEBYTECODE": "1"}

            cmd_path = root / "probe.cmd"
            cmd_path.write_bytes(cmd_bytes)
            cmd_result = subprocess.run(["cmd.exe", "/c", str(cmd_path)], env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(cmd_result.returncode, 0, cmd_result.stdout + cmd_result.stderr)
            self.assertEqual(cmd_result.stdout.strip(), sys.executable)

            ps1_path = root / "probe.ps1"
            ps1_path.write_bytes(ps1_bytes)
            ps1_result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1_path)],
                env=env, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(ps1_result.returncode, 0, ps1_result.stdout + ps1_result.stderr)
            self.assertEqual(ps1_result.stdout.strip(), sys.executable)

    def test_shim_forwards_exit_code_and_arguments(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory(prefix="af-shim-args-") as raw:
            root = Path(raw)
            probe = root / "probe.py"
            probe.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(3)\n", encoding="utf-8")
            env = {**os.environ, "PATH": path_dirs_without_python(), "PYTHONDONTWRITEBYTECODE": "1"}

            cmd_path = root / "probe.cmd"
            cmd_path.write_bytes(installer.cmd_dispatch(probe, "python"))
            cmd_result = subprocess.run(["cmd.exe", "/c", str(cmd_path), "alpha", "beta"], env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(cmd_result.returncode, 3, cmd_result.stdout + cmd_result.stderr)
            self.assertEqual(json.loads(cmd_result.stdout), ["alpha", "beta"])

            ps1_path = root / "probe.ps1"
            ps1_path.write_bytes(installer.powershell_dispatch(probe, "python"))
            ps1_result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1_path), "alpha", "beta"],
                env=env, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(ps1_result.returncode, 3, ps1_result.stdout + ps1_result.stderr)
            self.assertEqual(json.loads(ps1_result.stdout), ["alpha", "beta"])


class MarkerBearingHookRepairTests(unittest.TestCase):
    """Marker-bearing hooks frozen as a false "user edit" (bin/install.py
    clean_hook_groups), across the requoting/retired-script/genuine-edit cases."""

    def test_equivalent_form_in_settings_is_replaced_with_the_refreshed_command(self):
        with tempfile.TemporaryDirectory(prefix="af-hook-equiv-a-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            original = find_hook(settings, "UserPromptSubmit", "route")["command"]

            swapped = alternate_marker_form(original)
            self.assertNotEqual(swapped, original, "the rewritten form must actually differ byte-for-byte")
            find_hook(settings, "UserPromptSubmit", "route")["command"] = swapped
            settings_path.write_text(json.dumps(settings, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            settings_after = json.loads(settings_path.read_text())
            self.assertEqual(find_hook(settings_after, "UserPromptSubmit", "route")["command"], original, "an equivalent (requoted/re-marked) hook is replaced with the refreshed command, not frozen")

            meta = json.loads((home / ".claude/.claude-agents-config-install.json").read_text())
            self.assertEqual(journal_entry(meta, "UserPromptSubmit", "route")["installed"], original, "the journal entry is refreshed too, so a later install still sees an exact match")

    def test_equivalent_form_recorded_only_in_the_journal_is_also_replaced(self):
        # The journal-side direction of the same incident: the journal holds
        # the pre-marker "& rem" form (as an install performed by an older
        # bundle version would have recorded it) while settings.json already
        # holds the exact refreshed command.
        with tempfile.TemporaryDirectory(prefix="af-hook-equiv-b-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"
            meta_path = home / ".claude" / ".claude-agents-config-install.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            original = find_hook(settings, "UserPromptSubmit", "route")["command"]

            meta = json.loads(meta_path.read_text())
            entry = journal_entry(meta, "UserPromptSubmit", "route")
            entry["installed"] = alternate_marker_form(original)
            meta_path.write_text(json.dumps(meta, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            settings_after = json.loads(settings_path.read_text())
            self.assertEqual(find_hook(settings_after, "UserPromptSubmit", "route")["command"], original, "settings.json already matched the refreshed command and must keep doing so")

            meta_after = json.loads(meta_path.read_text())
            self.assertEqual(journal_entry(meta_after, "UserPromptSubmit", "route")["installed"], original, "the stale journal-only legacy form is refreshed to match")

    def test_retired_script_hook_is_replaced_even_though_it_was_genuinely_edited(self):
        with tempfile.TemporaryDirectory(prefix="af-hook-retired-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            refreshed = find_hook(settings, "SessionStart", "model-sync")["command"]
            self.assertIn("sync-provider-models.mjs", refreshed)

            # A genuine user edit (a changed flag) that also still points at
            # the script this bundle retired in favor of sync-provider-models.mjs.
            edited = refreshed.replace("sync-provider-models.mjs", "sync-omniroute-models.mjs").replace("--quiet", "--quiet --changed-by-user")
            self.assertNotEqual(edited, refreshed)
            find_hook(settings, "SessionStart", "model-sync")["command"] = edited
            settings_path.write_text(json.dumps(settings, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            self.assertIn("sync-omniroute-models.mjs", reinstall.stdout, "the notice must name the retired script")
            self.assertIn("hook:SessionStart:model-sync", reinstall.stdout, "the notice must name the hook")
            self.assertIn("replacing it with the current hook", reinstall.stdout)
            self.assertIn("backed up", reinstall.stdout.lower())

            settings_after = json.loads(settings_path.read_text())
            command_after = find_hook(settings_after, "SessionStart", "model-sync")["command"]
            self.assertEqual(command_after, refreshed, "a hook pointing at a retired script is replaced even though it was genuinely edited")

            meta = json.loads((home / ".claude/.claude-agents-config-install.json").read_text())
            self.assertEqual(journal_entry(meta, "SessionStart", "model-sync")["installed"], refreshed)

            # Idempotency: a third install with nothing left to repair prints
            # no further notice and leaves settings.json and the journal alone.
            settings_bytes_before = settings_path.read_bytes()
            meta_bytes_before = (home / ".claude/.claude-agents-config-install.json").read_bytes()
            third = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertNotIn("refresh:", third.stdout)
            self.assertEqual(settings_path.read_bytes(), settings_bytes_before, "settings.json is unchanged once nothing needs repair")
            self.assertEqual((home / ".claude/.claude-agents-config-install.json").read_bytes(), meta_bytes_before, "the journal entry is unchanged once nothing needs repair")

            uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            settings_uninstalled = json.loads(settings_path.read_text())
            self.assertFalse(hook_present(settings_uninstalled, "SessionStart", "model-sync"), "uninstall removes the refreshed hook it installed")

    def test_hook_chained_to_a_user_owned_retired_script_name_survives_reinstall(self):
        # The retired-script name appears at the *exact* home/.claude path,
        # but the file is the user's own (never installer-managed): it will
        # not disappear this run, so the hook must be left alone.
        with tempfile.TemporaryDirectory(prefix="af-hook-retired-userowned-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            refreshed = find_hook(settings, "SessionStart", "model-sync")["command"]

            proposal = home / ".claude" / "fleet-model-proposal.py"
            proposal.write_text("# the user's own prototype, not installer-managed\n", encoding="utf-8")
            match = re.search(r"\s*#\s*claude-agents-config:model-sync\s*$", refreshed)
            self.assertIsNotNone(match)
            edited = refreshed[: match.start()] + f" && {sys.executable} {proposal}" + match.group(0)
            self.assertNotEqual(edited, refreshed)
            find_hook(settings, "SessionStart", "model-sync")["command"] = edited
            settings_path.write_text(json.dumps(settings, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            self.assertNotIn("refresh:", reinstall.stdout, "a user-owned file that will still exist must not be treated as retired")
            settings_after = json.loads(settings_path.read_text())
            self.assertEqual(find_hook(settings_after, "SessionStart", "model-sync")["command"], edited)

    def test_hook_referencing_a_retired_script_name_outside_home_survives_reinstall(self):
        # Same retired filename, but under a different project's .claude
        # directory entirely -- never a candidate for this home's repair.
        with tempfile.TemporaryDirectory(prefix="af-hook-retired-outside-") as raw:
            root = Path(raw)
            home, config = root / "home", root / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            refreshed = find_hook(settings, "SessionStart", "model-sync")["command"]

            outside = root / "proj" / ".claude" / "sync-omniroute-models.mjs"
            match = re.search(r"\s*#\s*claude-agents-config:model-sync\s*$", refreshed)
            self.assertIsNotNone(match)
            edited = refreshed[: match.start()] + f" && {sys.executable} {outside}" + match.group(0)
            self.assertNotEqual(edited, refreshed)
            find_hook(settings, "SessionStart", "model-sync")["command"] = edited
            settings_path.write_text(json.dumps(settings, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            self.assertNotIn("refresh:", reinstall.stdout, "a retired filename outside home/.claude must never match")
            settings_after = json.loads(settings_path.read_text())
            self.assertEqual(find_hook(settings_after, "SessionStart", "model-sync")["command"], edited)

    def test_uninstall_removes_a_hook_whose_journal_entry_is_in_the_legacy_marker_form(self):
        with tempfile.TemporaryDirectory(prefix="af-hook-uninstall-legacy-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"
            meta_path = home / ".claude" / ".claude-agents-config-install.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            original = find_hook(settings, "UserPromptSubmit", "route")["command"]

            # Simulate a 2.0.5 install broken exactly as in the real incident:
            # both journal fields recorded in the legacy "& rem" form while
            # settings.json (untouched here) still holds the current form.
            meta = json.loads(meta_path.read_text())
            entry = journal_entry(meta, "UserPromptSubmit", "route")
            legacy_form = alternate_marker_form(original)
            entry["installed"] = legacy_form
            entry["command"] = legacy_form
            meta_path.write_text(json.dumps(meta, indent=2))

            uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            settings_after = json.loads(settings_path.read_text())
            self.assertFalse(hook_present(settings_after, "UserPromptSubmit", "route"), "uninstall removes the hook even though the journal recorded it in the legacy marker form")

    def test_retired_hook_beside_an_edited_one_is_removed_not_replaced(self):
        # Two model-sync hooks for one event: A still runs the retired script,
        # B is a genuine edit of the current one. A goes; B is kept, so the
        # current hook is not installed and the notice must not claim it is.
        with tempfile.TemporaryDirectory(prefix="af-hook-retired-beside-edit-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            hook = find_hook(settings, "SessionStart", "model-sync")
            refreshed = hook["command"]
            retired = refreshed.replace("sync-provider-models.mjs", "sync-omniroute-models.mjs")
            edited = refreshed.replace("--quiet", "--quiet --b-flag")
            self.assertNotEqual(retired, refreshed)
            self.assertNotEqual(edited, refreshed)
            hook["command"] = retired
            settings["hooks"]["SessionStart"].append({"hooks": [dict(hook, command=edited)]})
            settings_path.write_text(json.dumps(settings, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            self.assertIn("hook:SessionStart:model-sync", reinstall.stdout)
            self.assertIn("removing it", reinstall.stdout)
            self.assertNotIn("replacing it with the current hook", reinstall.stdout)
            commands = [h["command"] for g in json.loads(settings_path.read_text())["hooks"]["SessionStart"] for h in g["hooks"] if "claude-agents-config:model-sync" in h["command"]]
            self.assertEqual(commands, [edited], "the retired hook is dropped and the edited one kept verbatim")

    def test_current_command_over_a_stale_journal_entry_is_adopted(self):
        # An older release froze a hook and its journal kept an old command
        # with different arguments; the user then pasted the current command.
        # It equals what this run installs, so it is the bundle's again.
        with tempfile.TemporaryDirectory(prefix="af-hook-adopt-current-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"
            meta_path = home / ".claude" / ".claude-agents-config-install.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            original = find_hook(json.loads(settings_path.read_text()), "UserPromptSubmit", "route")["command"]

            meta = json.loads(meta_path.read_text())
            entry = journal_entry(meta, "UserPromptSubmit", "route")
            stale = alternate_marker_form(original.replace(" --home ", " --legacy-arg --home "))
            self.assertNotEqual(stale, alternate_marker_form(original), "the stale entry must differ by more than quoting")
            entry["installed"] = entry["command"] = stale
            meta_path.write_text(json.dumps(meta, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            self.assertEqual(find_hook(json.loads(settings_path.read_text()), "UserPromptSubmit", "route")["command"], original)
            self.assertEqual(journal_entry(json.loads(meta_path.read_text()), "UserPromptSubmit", "route")["installed"], original, "the journal adopts the current command")

            uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertFalse(hook_present(json.loads(settings_path.read_text()), "UserPromptSubmit", "route"), "uninstall removes the adopted hook")

    def test_uninstall_keeps_an_unmarked_user_copy_of_a_managed_hook(self):
        with tempfile.TemporaryDirectory(prefix="af-hook-uninstall-unmarked-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            managed = find_hook(settings, "PreModelSwitch", "model-context")
            unmarked = re.sub(r"\s*#\s*claude-agents-config:[a-z0-9-]+\s*$", "", managed["command"])
            self.assertNotEqual(unmarked, managed["command"])
            settings["hooks"]["PreModelSwitch"].append({"hooks": [dict(managed, command=unmarked)]})
            settings_path.write_text(json.dumps(settings, indent=2))

            uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            after = json.loads(settings_path.read_text())
            commands = [h["command"] for g in after.get("hooks", {}).get("PreModelSwitch", []) for h in g["hooks"]]
            self.assertEqual(commands, [unmarked], "the managed hook goes; the user's unmarked copy stays")

    def test_edited_hook_with_a_current_script_survives_reinstall_verbatim(self):
        with tempfile.TemporaryDirectory(prefix="af-hook-edited-") as raw:
            home, config = Path(raw) / "home", Path(raw) / "config"
            env = env_for(home, config)
            settings_path = home / ".claude" / "settings.json"

            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            settings = json.loads(settings_path.read_text())
            original = find_hook(settings, "UserPromptSubmit", "route")["command"]

            match = re.search(r"\s*#\s*claude-agents-config:route\s*$", original)
            self.assertIsNotNone(match)
            edited = original[: match.start()] + " --extra-user-flag" + match.group(0)
            self.assertNotEqual(edited, original)
            find_hook(settings, "UserPromptSubmit", "route")["command"] = edited
            settings_path.write_text(json.dumps(settings, indent=2))

            reinstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            settings_after = json.loads(settings_path.read_text())
            self.assertEqual(find_hook(settings_after, "UserPromptSubmit", "route")["command"], edited, "a genuine edit that still names the current script is kept verbatim")


class HookRepairUnitTests(unittest.TestCase):
    """Direct unit coverage of the helper functions clean_hook_groups relies on."""

    def setUp(self):
        self.installer = load_installer()

    def test_normalized_hook_command_ignores_quoting_and_marker_style(self):
        posix = 'python3 "/home/u/.claude/route-to-fleet.py" # claude-agents-config:route'
        windows_unquoted = "python3 /home/u/.claude/route-to-fleet.py & rem claude-agents-config:route"
        self.assertEqual(self.installer.normalized_hook_command(posix), self.installer.normalized_hook_command(windows_unquoted))
        self.assertNotEqual(
            self.installer.normalized_hook_command(posix),
            self.installer.normalized_hook_command('python3 "/home/u/.claude/route-to-fleet.py" --extra # claude-agents-config:route'),
        )

    def test_retired_hook_script_for_kind_only_fires_on_the_exact_home_path_when_truly_gone(self):
        fn = self.installer.retired_hook_script_for_kind
        with tempfile.TemporaryDirectory(prefix="af-retired-script-unit-") as raw:
            home = Path(raw) / "home"
            claude = home / ".claude"
            claude.mkdir(parents=True)
            omniroute = claude / "sync-omniroute-models.mjs"

            # Absent: the script is simply gone.
            self.assertEqual(fn("model-sync", f"node {omniroute} --quiet", home, {}), "sync-omniroute-models.mjs")
            self.assertEqual(fn("model-sync", f"node {omniroute.as_posix()} --quiet", home, {}), "sync-omniroute-models.mjs")

            # Present, and a previously managed record proves it is unchanged
            # since install (obsolete_paths would retire it too).
            data = b"// old omniroute sync\n"
            omniroute.write_bytes(data)
            item = {"path": str(omniroute), "sha256": hashlib.sha256(data).hexdigest(), "mode": "0o755"}
            self.assertEqual(fn("model-sync", f"node {omniroute} --quiet", home, {str(omniroute): item}), "sync-omniroute-models.mjs")

            # Present, but not a previously managed record at all: a user's
            # own file by that name, which will still exist after this run.
            self.assertIsNone(fn("model-sync", f"node {omniroute} --quiet", home, {}), "an unmanaged file that will still exist is not retired")

            # Present and "managed", but its content no longer matches the
            # recorded sha256 and carries no marker: the user changed it, so
            # it is not the file obsolete_paths would remove.
            omniroute.write_bytes(b"// user's own edits\n")
            self.assertIsNone(fn("model-sync", f"node {omniroute} --quiet", home, {str(omniroute): item}), "a user-edited file is not the one obsolete_paths would retire")

            # Same filename, different home entirely: never a match.
            other_home = Path(raw) / "other-project"
            outside = other_home / ".claude" / "sync-omniroute-models.mjs"
            self.assertIsNone(fn("model-sync", f"node {outside} --quiet", home, {}), "a path outside this home never matches")

            self.assertIsNone(fn("model-sync", f"node {claude / 'sync-provider-models.mjs'} --quiet", home, {}), "the current script is not retired")
            self.assertIsNone(fn("route", f"python3 {claude / 'route-to-fleet.py'}", home, {}), "an unrelated kind never matches")

            # Whole path arguments only: a longer path that starts or ends
            # with the retired one is a different file.
            omniroute.unlink()
            self.assertIsNone(fn("model-sync", f"node {omniroute}.bak --quiet", home, {}), "a .bak copy is a different file")
            self.assertIsNone(fn("model-sync", f"node /backup{omniroute} --quiet", home, {}), "a path that merely ends with it is a different file")
            # Other spellings of the same file are recognized.
            for spelled in ("~/.claude/sync-omniroute-models.mjs", '"$HOME/.claude/sync-omniroute-models.mjs"', "--script=${HOME}/.claude/sync-omniroute-models.mjs"):
                self.assertEqual(fn("model-sync", f"node {spelled} --quiet", home, {}), "sync-omniroute-models.mjs", spelled)

    def test_names_home_path_recognizes_windows_spellings(self):
        fn = self.installer.names_home_path
        home = PureWindowsPath("C:/Users/u")
        target = home / ".claude" / "sync-omniroute-models.mjs"
        for command in (
            r'node "C:\Users\u\.claude\sync-omniroute-models.mjs" --quiet',
            "node C:/Users/u/.claude/sync-omniroute-models.mjs --quiet",
            "node /c/Users/u/.claude/sync-omniroute-models.mjs --quiet",
            r'node "%USERPROFILE%\.claude\sync-omniroute-models.mjs"',
            r"node $env:USERPROFILE\.claude\sync-omniroute-models.mjs",
        ):
            self.assertTrue(fn(command, target, home), command)
        self.assertFalse(fn(r"node C:\Users\u\.claude\sync-omniroute-models.mjs.bak", target, home))
        self.assertFalse(fn(r"node D:\Users\u\.claude\sync-omniroute-models.mjs", target, home))
        with unittest.mock.patch.object(self.installer.os, "name", "nt"):
            self.assertTrue(fn(r"node c:\users\U\.CLAUDE\sync-omniroute-models.mjs", target, home), "paths are case-insensitive on Windows")


if __name__ == "__main__":
    unittest.main()
