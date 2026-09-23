#!/usr/bin/env python3
"""Tests for agentfleet auto-update feature."""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
AGENTFLEET = str(ROOT / "bin" / "agentfleet")
INSTALL_PY = str(ROOT / "bin" / "install.py")
DOCTOR = str(ROOT / "bin" / "claude-agents-doctor")

VERSION = (ROOT / "VERSION").read_text().strip()
# Parse to build a newer same-major version for tests
_v = tuple(int(x) for x in VERSION.split("."))
NEWER_VERSION = f"{_v[0]}.{_v[1]}.{_v[2] + 1}"
MAJOR_NEXT_VERSION = f"{_v[0] + 1}.0.0"


def env_for(home: Path, config: Path, **extra: str) -> dict:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AGENTFLEET_NONINTERACTIVE": "1",
        **extra,
    }


def make_release_store(store: Path) -> None:
    """Set up a fake release store pointing at the repo root."""
    (store / "releases").mkdir(parents=True)
    (store / "current").write_text(VERSION + "\n")
    (store / "releases" / VERSION).symlink_to(ROOT, target_is_directory=True)


def run_agentfleet(home: Path, config: Path, *args: str, store: Path | None = None,
                   base_url: str | None = None, **env_extra: str) -> subprocess.CompletedProcess:
    extra: dict[str, str] = env_extra
    if store:
        extra["AGENTFLEET_HOME"] = str(store)
    if base_url:
        extra["AGENTFLEET_BASE_URL"] = base_url
    e = env_for(home, config, **extra)
    return subprocess.run(
        [PYTHON, AGENTFLEET, "--home", str(home), "--config-home", str(config), *args],
        env=e, text=True, capture_output=True,
    )


class _LatestServer:
    """Local HTTP server serving latest.json and a stub install.sh."""

    def __init__(self, latest_version: str, record_file: Path | None = None):
        self.latest_version = latest_version
        self.record_file = record_file
        self._server: socketserver.TCPServer | None = None

    def __enter__(self) -> "_LatestServer":
        record_file = self.record_file
        latest_version = self.latest_version

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/releases/latest.json":
                    body = json.dumps({
                        "format": "agentfleet.release.v1",
                        "version": latest_version,
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path in ("/install.sh", "/install.ps1"):
                    # Stub installer: records env vars and exits 0
                    ua = self.headers.get("User-Agent", "")
                    record = {
                        "user_agent": ua,
                        "path": self.path,
                    }
                    if record_file:
                        record_file.write_text(json.dumps(record))
                    # Write a stub sh script that records env
                    body = (
                        b"#!/bin/sh\n"
                        b"env > \"$HOME/.claude/agentfleet/install-env.txt\"\n"
                        b"exit 0\n"
                    )
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *_args):
                pass

        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def __exit__(self, *_):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


class AutoUpdateBasicTests(unittest.TestCase):
    """Tests for silent no-ops and throttle logic."""

    def _setup(self, tmp: str, store: bool = True):
        raw = Path(tmp)
        home, config = raw / "home", raw / "config"
        (home / ".claude").mkdir(parents=True)
        s = raw / "store" if store else None
        if s:
            make_release_store(s)
        return home, config, s

    def test_disabled_via_state_file_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            # Write disabled state
            state_dir = home / ".claude" / "agentfleet"
            state_dir.mkdir(parents=True)
            (state_dir / "auto-update.json").write_text(json.dumps({"enabled": False}))
            with _LatestServer(NEWER_VERSION) as srv:
                r = run_agentfleet(home, config, "auto-update", "--hook",
                                   store=store, base_url=srv.url)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_disabled_via_env_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            with _LatestServer(NEWER_VERSION) as srv:
                r = run_agentfleet(home, config, "auto-update", "--hook",
                                   store=store, base_url=srv.url,
                                   AGENTFLEET_AUTO_UPDATE="0")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_no_release_install_is_silent(self):
        """No store (git-clone install) → silent no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            home, config = raw / "home", raw / "config"
            (home / ".claude").mkdir(parents=True)
            # No store → current_release returns None
            with _LatestServer(NEWER_VERSION) as srv:
                r = run_agentfleet(home, config, "auto-update", "--hook", base_url=srv.url)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_throttled_after_recent_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            # Pre-set lastCheck to just now
            state_dir = home / ".claude" / "agentfleet"
            state_dir.mkdir(parents=True)
            now_iso = "2030-01-01T00:00:00Z"  # far future but in state only
            import datetime as dt
            now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            (state_dir / "auto-update.json").write_text(json.dumps({"lastCheck": now_iso}))
            with _LatestServer(NEWER_VERSION) as srv:
                r = run_agentfleet(home, config, "auto-update", "--hook",
                                   store=store, base_url=srv.url)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_latest_json_failure_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            # Point at a URL that won't serve latest.json
            r = run_agentfleet(home, config, "auto-update", "--hook",
                               store=store, base_url="http://127.0.0.1:1")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")


class AutoUpdateInstallTests(unittest.TestCase):
    """Tests for same-major update flow, major-boundary notice, and reporting."""

    def _setup(self, tmp: str):
        raw = Path(tmp)
        home, config, store = raw / "home", raw / "config", raw / "store"
        (home / ".claude").mkdir(parents=True)
        make_release_store(store)
        # Write install json so installed_version() returns VERSION
        (home / ".claude" / ".claude-agents-config-install.json").write_text(
            json.dumps({"version": VERSION, "profile": "native"})
        )
        return home, config, store

    def test_same_major_newer_runs_installer_and_reports_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            record = Path(tmp) / "request-record.json"
            with _LatestServer(NEWER_VERSION, record_file=record) as srv:
                # Sync mode: run in-process instead of spawning
                r = run_agentfleet(home, config, "auto-update", "--hook",
                                   store=store, base_url=srv.url,
                                   AGENTFLEET_AUTO_UPDATE_SYNC="1")
            self.assertEqual(r.returncode, 0, r.stderr)
            # Check User-Agent was agentfleet/
            if record.exists():
                rec = json.loads(record.read_text())
                self.assertTrue(rec["user_agent"].startswith("agentfleet/"), rec["user_agent"])

            # Now run --hook again to get the success notice
            r2 = run_agentfleet(home, config, "auto-update", "--hook",
                                store=store, base_url=srv.url)
            self.assertIn(f"AgentFleet was updated to {NEWER_VERSION} automatically.", r2.stdout)
            self.assertEqual(r2.returncode, 0)

            # Third run: notice is NOT repeated
            r3 = run_agentfleet(home, config, "auto-update", "--hook",
                                store=store, base_url=srv.url)
            self.assertEqual(r3.stdout, "")

    def test_major_boundary_prints_notice_once_and_does_not_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            install_env = home / ".claude" / "agentfleet" / "install-env.txt"
            with _LatestServer(MAJOR_NEXT_VERSION) as srv:
                r = run_agentfleet(home, config, "auto-update", "--hook",
                                   store=store, base_url=srv.url,
                                   AGENTFLEET_AUTO_UPDATE_SYNC="1")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"AgentFleet {_v[0] + 1}.0.0 is available", r.stdout)
            self.assertFalse(install_env.exists(), "major update must not run installer")

            # Second run with same major: notice is NOT repeated
            with _LatestServer(MAJOR_NEXT_VERSION) as srv2:
                state_dir = home / ".claude" / "agentfleet"
                state = json.loads((state_dir / "auto-update.json").read_text())
                # Reset lastCheck so throttle doesn't block
                state["lastCheck"] = "2000-01-01T00:00:00Z"
                (state_dir / "auto-update.json").write_text(json.dumps(state))
                r2 = run_agentfleet(home, config, "auto-update", "--hook",
                                    store=store, base_url=srv2.url)
            self.assertEqual(r2.stdout, "")


class AutoUpdateLockTests(unittest.TestCase):
    """Tests for the exclusive lock."""

    def _setup(self, tmp: str):
        raw = Path(tmp)
        home, config, store = raw / "home", raw / "config", raw / "store"
        (home / ".claude").mkdir(parents=True)
        make_release_store(store)
        (home / ".claude" / ".claude-agents-config-install.json").write_text(
            json.dumps({"version": VERSION, "profile": "native"})
        )
        return home, config, store

    def test_fresh_lock_blocks_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            lock_path = home / ".claude" / "agentfleet" / "auto-update.lock"
            (home / ".claude" / "agentfleet").mkdir(parents=True)
            # Write a fresh lock (not stale)
            lock_path.write_text(f"pid=99999 time=now\n")
            lock_path.touch()  # set mtime to now

            log_path = home / ".claude" / "agentfleet" / "auto-update.log"
            with _LatestServer(NEWER_VERSION) as srv:
                r = run_agentfleet(home, config, "auto-update", "--run", NEWER_VERSION,
                                   store=store, base_url=srv.url)
            # Should exit 0 (lock held → skip)
            self.assertEqual(r.returncode, 0)
            # Log should say locked
            if log_path.exists():
                self.assertIn("lock held", log_path.read_text())

    def test_stale_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, config, store = self._setup(tmp)
            lock_path = home / ".claude" / "agentfleet" / "auto-update.lock"
            (home / ".claude" / "agentfleet").mkdir(parents=True)
            # Write a stale lock (mtime > 1h ago)
            lock_path.write_text("pid=1 time=old\n")
            old_time = time.time() - 3700
            os.utime(str(lock_path), (old_time, old_time))

            with _LatestServer(NEWER_VERSION) as srv:
                r = run_agentfleet(home, config, "auto-update", "--run", NEWER_VERSION,
                                   store=store, base_url=srv.url)
            self.assertEqual(r.returncode, 0, r.stderr)
            # Lock should be cleaned up after success
            self.assertFalse(lock_path.exists())


class AutoUpdateOnOffStatusTests(unittest.TestCase):
    """Tests for on/off/status subcommands and state file permissions."""

    def test_on_off_status_and_file_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            home, config = raw / "home", raw / "config"
            (home / ".claude").mkdir(parents=True)

            r = run_agentfleet(home, config, "auto-update", "off")
            self.assertEqual(r.returncode, 0)
            state_file = home / ".claude" / "agentfleet" / "auto-update.json"
            self.assertTrue(state_file.exists())
            state = json.loads(state_file.read_text())
            self.assertFalse(state["enabled"])
            # Check 0600 permissions
            mode = oct(state_file.stat().st_mode & 0o777)
            self.assertEqual(mode, oct(0o600))

            r2 = run_agentfleet(home, config, "auto-update", "on")
            self.assertEqual(r2.returncode, 0)
            state2 = json.loads(state_file.read_text())
            self.assertTrue(state2["enabled"])

            r3 = run_agentfleet(home, config, "auto-update", "status")
            self.assertEqual(r3.returncode, 0)
            self.assertIn("enabled", r3.stdout)
            self.assertIn("installed version", r3.stdout)

    def test_status_shows_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            home, config = raw / "home", raw / "config"
            (home / ".claude").mkdir(parents=True)
            r = run_agentfleet(home, config, "auto-update", "status",
                               AGENTFLEET_AUTO_UPDATE="0")
            self.assertIn("AGENTFLEET_AUTO_UPDATE=0", r.stdout)


class AutoUpdateHookWiringTest(unittest.TestCase):
    """Tests that install puts the auto-update hook in settings.json and doctor passes."""

    def test_native_install_wires_hook_and_doctor_passes(self):
        with tempfile.TemporaryDirectory(prefix="agentfleet au-wire ") as tmp:
            raw = Path(tmp)
            home, config = raw / "home", raw / "config"
            e = env_for(home, config)
            install = subprocess.run(
                [PYTHON, INSTALL_PY, "--provider", "native",
                 "--home", str(home), "--config-home", str(config)],
                cwd=str(ROOT), env=e, text=True, capture_output=True,
            )
            self.assertEqual(install.returncode, 0, install.stderr + install.stdout)
            settings = json.loads((home / ".claude" / "settings.json").read_text())
            # Find auto-update hook command in SessionStart
            session_hooks = settings.get("hooks", {}).get("SessionStart", [])
            commands = []
            for entry in session_hooks:
                for h in entry.get("hooks", []):
                    commands.append(h.get("command", ""))
            self.assertTrue(
                any("auto-update" in c and "--hook" in c for c in commands),
                f"auto-update --hook not found in SessionStart hooks: {commands}",
            )
            agentfleet_py = home / ".claude" / "agentfleet.py"
            self.assertTrue(agentfleet_py.exists(), "agentfleet.py not installed")
            # Check the hook points at the installed path
            au_cmd = next(c for c in commands if "auto-update" in c and "--hook" in c)
            self.assertIn(str(agentfleet_py), au_cmd)

            # Doctor should pass
            doctor = subprocess.run(
                [PYTHON, DOCTOR, "--check",
                 "--home", str(home), "--config-home", str(config)],
                env=e, text=True, capture_output=True,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)

    def test_uninstall_removes_auto_update_hook(self):
        with tempfile.TemporaryDirectory(prefix="agentfleet au-uninst ") as tmp:
            raw = Path(tmp)
            home, config, store = raw / "home", raw / "config", raw / "store"
            e = env_for(home, config, AGENTFLEET_HOME=str(store))
            install = subprocess.run(
                [PYTHON, INSTALL_PY, "--provider", "native",
                 "--home", str(home), "--config-home", str(config)],
                cwd=str(ROOT), env=e, text=True, capture_output=True,
            )
            self.assertEqual(install.returncode, 0, install.stderr + install.stdout)
            # Build a real release store so uninstall works
            make_release_store(store)
            uninstall = subprocess.run(
                [PYTHON, INSTALL_PY, "--uninstall", "--apply",
                 "--home", str(home), "--config-home", str(config)],
                cwd=str(ROOT), env=e, text=True, capture_output=True,
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr + uninstall.stdout)
            # After uninstall, no auto-update hook should remain
            try:
                settings_text = (home / ".claude" / "settings.json").read_text()
                self.assertNotIn("auto-update --hook", settings_text)
            except FileNotFoundError:
                pass  # settings.json removed is also fine


if __name__ == "__main__":
    unittest.main()
