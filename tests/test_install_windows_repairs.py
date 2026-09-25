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
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

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
        self.assertNotIn("(", body.split("\r\n", 1)[0], "no parenthesized errorlevel block")

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
            self.assertIn("backup snapshot", reinstall.stdout.lower())

            settings_after = json.loads(settings_path.read_text())
            command_after = find_hook(settings_after, "SessionStart", "model-sync")["command"]
            self.assertEqual(command_after, refreshed, "a hook pointing at a retired script is replaced even though it was genuinely edited")

            meta = json.loads((home / ".claude/.claude-agents-config-install.json").read_text())
            self.assertEqual(journal_entry(meta, "SessionStart", "model-sync")["installed"], refreshed)

            uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            settings_uninstalled = json.loads(settings_path.read_text())
            self.assertFalse(hook_present(settings_uninstalled, "SessionStart", "model-sync"), "uninstall removes the refreshed hook it installed")

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

    def test_retired_hook_script_for_kind_matches_the_reported_incident(self):
        fn = self.installer.retired_hook_script_for_kind
        self.assertEqual(fn("model-sync", "node /home/u/.claude/sync-omniroute-models.mjs --quiet"), "sync-omniroute-models.mjs")
        self.assertEqual(fn("model-sync", r"node C:\u\.claude\sync-omniroute-models.mjs --quiet"), "sync-omniroute-models.mjs")
        self.assertEqual(fn("model-sync", "python3 /home/u/.claude/fleet-model-proposal.py"), "fleet-model-proposal.py")
        self.assertIsNone(fn("model-sync", "node /home/u/.claude/sync-provider-models.mjs --quiet"), "the current script is not retired")
        self.assertIsNone(fn("route", "python3 /home/u/.claude/route-to-fleet.py"), "an unrelated kind never matches")


if __name__ == "__main__":
    unittest.main()
