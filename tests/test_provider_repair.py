#!/usr/bin/env python3
"""Stdlib regression tests for provider identity and installed reconciliation."""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
POLICY = json.loads((ROOT / "config/provider-policy.json").read_text())


def load_catalog():
    namespace = {}
    exec(compile((ROOT / "scripts/provider_catalog.py").read_text(), "provider_catalog.py", "exec"), namespace)
    return namespace


class FakeProvider(http.server.BaseHTTPRequestHandler):
    rows = []
    mode = "ok"
    page = 0
    requests = 0
    last_headers = None

    def setup(self):
        super().setup()
        type(self).requests += 1

    def do_GET(self):  # noqa: N802
        type(self).last_headers = self.headers
        if self.path.startswith("/v1/models"):
            if self.mode == "unauthorized":
                self.send_response(401)
                self.end_headers()
                return
            if self.mode == "malformed":
                body = b'{"data":[{"bad":true}]}'
            elif self.mode == "empty":
                body = b'{"data":[]}'
            elif self.mode == "page1":
                body = json.dumps({"data": self.rows[:1], "has_more": True, "last_id": "cursor-1"}).encode()
            else:
                body = json.dumps({"data": self.rows}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        pass


class ProviderRepairTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog()

    def test_exact_identity_namespace_and_context(self):
        effective = self.catalog["effective_model_id"]
        self.assertEqual(effective("wecansync/codex-luna", POLICY, 872000), "codex-luna[1m]")
        self.assertEqual(effective("other/codex-luna", POLICY, 872000), "other/codex-luna")
        self.assertEqual(effective("codex-luna", POLICY, 200000), "codex-luna")
        self.assertEqual(effective("codex-luna[1m]", POLICY, 200000), "codex-luna")

    def test_cache_scope_and_empty_catalog(self):
        cache_payload = self.catalog["cache_payload"]
        cache_rows = self.catalog["cache_rows"]
        value = cache_payload([], "https://gateway.test/api", "fake-token", fetched_at=100)
        self.assertEqual(cache_rows(value, "https://gateway.test/api", "fake-token", now=100), [])
        self.assertIsNone(cache_rows(value, "https://other.test", "fake-token", now=100))
        self.assertIsNone(cache_rows(value, "https://gateway.test/api", "other-token", now=100))
        self.assertIsNone(cache_rows(value, "https://gateway.test/api", "fake-token", ttl=1, now=102))

    def test_malformed_and_pagination_are_conservative(self):
        parse = self.catalog["parse_catalog_payload"]
        with self.assertRaises(ValueError):
            parse({"data": [{"missing": "id"}]})
        rows, complete = parse({"data": [{"id": "codex-luna"}], "has_more": True})
        self.assertEqual(rows[0]["id"], "codex-luna")
        self.assertFalse(complete)

    def test_family_approved_rejects_malformed_explicit_allowlist_without_shipped_fallback(self):
        # "codex" ships with families[].approved == True in the fixture
        # policy, and no top-level approvedFamilies key. Once an explicit
        # approvedFamilies key is present at all, a malformed value (null, a
        # bare string, or a list containing a non-string entry) must never
        # fall back to that shipped per-family default -- doing so would
        # silently re-approve a family the user may have deliberately
        # revoked. Only an absent approvedFamilies key may use the legacy
        # per-family default.
        family_approved = self.catalog["family_approved"]
        self.assertNotIn("approvedFamilies", POLICY, "fixture assumption: shipped policy has no explicit decision yet")

        # Absent key: legacy per-family "approved": true default applies.
        self.assertTrue(family_approved("codex-luna", POLICY), "absent approvedFamilies must still honor the shipped per-family default")

        # Key present but null.
        null_policy = {**POLICY, "approvedFamilies": None}
        self.assertFalse(family_approved("codex-luna", null_policy), "a null approvedFamilies must not fall back to the shipped default")

        # Key present but a bare string, not a list.
        string_policy = {**POLICY, "approvedFamilies": "codex"}
        self.assertFalse(family_approved("codex-luna", string_policy), "a string approvedFamilies must not fall back to the shipped default")

        # Key present as a list containing a non-string entry.
        non_string_policy = {**POLICY, "approvedFamilies": ["codex", 123]}
        self.assertFalse(family_approved("codex-luna", non_string_policy), "a list with a non-string entry must not fall back to the shipped default")

        # Key present as an empty list: an explicit denial, not "no decision yet".
        empty_policy = {**POLICY, "approvedFamilies": []}
        self.assertFalse(family_approved("codex-luna", empty_policy), "an empty approvedFamilies list must deny every family")

        # Key present and well-formed: only the named families are approved.
        claude_only_policy = {**POLICY, "approvedFamilies": ["claude"]}
        self.assertTrue(family_approved("claude-sonnet-5", claude_only_policy))
        self.assertFalse(family_approved("codex-luna", claude_only_policy), "a well-formed allowlist must still exclude families it does not name")

    def test_load_policy_rejects_invalid_documents(self):
        load_policy = self.catalog["load_policy"]
        CatalogError = self.catalog["CatalogError"]
        with tempfile.TemporaryDirectory(prefix="fleet load policy ") as raw:
            path = Path(raw) / "provider-policy.json"

            path.write_text(json.dumps(["not", "a", "dict"]))
            with self.assertRaises(CatalogError):
                load_policy(path)

            path.write_text(json.dumps({"version": "provider-policy.v0", "provider": "omniroute", "families": []}))
            with self.assertRaises(CatalogError):
                load_policy(path)

            path.write_text(json.dumps({"version": "provider-policy.v1", "provider": "not-omniroute", "families": []}))
            with self.assertRaises(CatalogError):
                load_policy(path)

            path.write_text(json.dumps({"version": "provider-policy.v1", "provider": "omniroute"}))
            with self.assertRaises(CatalogError):
                load_policy(path)

            path.write_bytes(b"{not valid json")
            with self.assertRaises(CatalogError):
                load_policy(path)

            path.write_text(json.dumps({"version": "provider-policy.v1", "provider": "omniroute", "families": []}))
            loaded = load_policy(path)
            self.assertEqual(loaded["families"], [])

    def test_fake_provider_fetch_and_error_classes(self):
        FakeProvider.rows = [{"id": "codex-luna", "context_length": 872000}]
        server = socketserver.TCPServer(("127.0.0.1", 0), FakeProvider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_address[1]}"
            fetch = self.catalog["fetch_catalog"]
            rows, complete, error = fetch(endpoint, "fake-token")
            self.assertTrue(complete)
            self.assertIsNone(error)
            self.assertEqual(rows[0]["id"], "codex-luna")
            self.assertEqual(FakeProvider.last_headers.get("user-agent"), "claude-agents-config/1.0.0")
            FakeProvider.mode = "unauthorized"
            rows, complete, error = fetch(endpoint, "fake-token")
            self.assertFalse(complete)
            self.assertIn("HTTP 401", error)
            FakeProvider.mode = "malformed"
            rows, complete, error = fetch(endpoint, "fake-token")
            self.assertFalse(complete)
            self.assertIn("CatalogError", error)
        finally:
            server.shutdown()
            server.server_close()
            FakeProvider.mode = "ok"

    def test_installed_direct_round_trip_and_setup_command(self):
        with tempfile.TemporaryDirectory(prefix="fleet repair space ") as raw:
            root = Path(raw)
            home = root / "home ☃"
            config = root / "config space"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1"}
            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            doctor = subprocess.run([str(home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            sync = subprocess.run([str(home / ".local/bin/claude-fleet-sync")], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(sync.returncode, 0, sync.stderr)
            synced_doctor = subprocess.run([str(home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(synced_doctor.returncode, 0, synced_doctor.stderr)
            settings = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(settings["permissions"]["allow"], [])
            self.assertEqual(settings["enabledPlugins"], {})
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps({"format": "provider-policy-decisions.v1", "approvedFamilies": ["claude", "codex"], "allowDiscovery": False}))
            setup = subprocess.run([str(home / ".local/bin/claude-fleet-setup"), "--decisions", str(decisions)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            self.assertFalse("fake-token" in setup.stdout or "fake-token" in setup.stderr)
            doctor_after_setup = subprocess.run([str(home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(doctor_after_setup.returncode, 0, doctor_after_setup.stderr)
            uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)

    def test_family_revocation_survives_reinstall_update(self):
        # A user who explicitly approves only "claude" must have that
        # revocation of every other shipped family (all of which ship with
        # "approved": true) survive a later reinstall/update. Before the
        # fix, install.py's merge only preserved the top-level
        # approvedFamilies list while restoring the shipped families array
        # verbatim, and family_approved() OR'd the stale per-family
        # "approved": true flag back in, silently re-enabling revoked
        # families and their fallback candidates.
        with tempfile.TemporaryDirectory(prefix="fleet revocation ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1"}
            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            policy_path = config / "delegate-skills" / "provider-policy.json"

            decisions = root / "decisions-claude-only.json"
            decisions.write_text(json.dumps({"format": "provider-policy-decisions.v1", "approvedFamilies": ["claude"], "allowDiscovery": False}))
            setup = subprocess.run([str(home / ".local/bin/claude-fleet-setup"), "--decisions", str(decisions)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(setup.returncode, 0, setup.stderr)

            family_approved = self.catalog["family_approved"]
            eligible_candidates = self.catalog["eligible_candidates"]
            available = {"claude-sonnet-5": {}, "codex-luna": {}}
            fallback_config = {"model": "claude-sonnet-5", "fallbacks": ["codex-luna"]}

            policy = json.loads(policy_path.read_text())
            self.assertFalse(family_approved("codex-luna", policy), "codex must not be approved right after setup")
            self.assertTrue(family_approved("claude-sonnet-5", policy))
            eligible, pending = eligible_candidates(fallback_config, available, policy)
            self.assertEqual(eligible, ["claude-sonnet-5"])
            self.assertIn("codex-luna", pending)

            # Reinstall/update: this must not resurrect the shipped
            # "approved": true default for the revoked "codex" family.
            update = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(update.returncode, 0, update.stderr)
            policy_after_update = json.loads(policy_path.read_text())
            self.assertEqual(policy_after_update.get("approvedFamilies"), ["claude"])
            self.assertFalse(family_approved("codex-luna", policy_after_update), "reinstall must not silently re-approve a revoked family")
            self.assertTrue(family_approved("claude-sonnet-5", policy_after_update))
            eligible_after_update, pending_after_update = eligible_candidates(fallback_config, available, policy_after_update)
            self.assertEqual(eligible_after_update, ["claude-sonnet-5"], "the live fallback selection must still exclude the revoked family after an update")
            self.assertIn("codex-luna", pending_after_update)
            codex_family = next(family for family in policy_after_update["families"] if family.get("name") == "codex")
            self.assertFalse(codex_family.get("approved"), "the per-family approved flag must also stay revoked on disk, not just the top-level list")
            doctor_after_update = subprocess.run([str(home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(doctor_after_update.returncode, 0, doctor_after_update.stderr)

            # An empty approval set (everything revoked) must also remain
            # empty, rather than falling back to "no explicit decisions yet".
            decisions_empty = root / "decisions-empty.json"
            decisions_empty.write_text(json.dumps({"format": "provider-policy-decisions.v1", "approvedFamilies": [], "allowDiscovery": False}))
            setup_empty = subprocess.run([str(home / ".local/bin/claude-fleet-setup"), "--decisions", str(decisions_empty)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(setup_empty.returncode, 0, setup_empty.stderr)
            update_empty = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(update_empty.returncode, 0, update_empty.stderr)
            policy_final = json.loads(policy_path.read_text())
            self.assertEqual(policy_final.get("approvedFamilies"), [])
            self.assertFalse(family_approved("claude-sonnet-5", policy_final), "an empty approval set must stay empty, not fall back to shipped defaults")

    def test_install_upgrade_rejects_corrupt_or_malformed_installed_policy_without_mutation(self):
        # An installed policy that already holds an explicit revocation is
        # the sole record of that decision (see family_approved() and the
        # merge in install.py's managed_specs()). If a later update finds
        # that file corrupted or its approvedFamilies allowlist malformed,
        # it must abort before touching any target -- settings.json, the
        # fleet config, and the damaged policy bytes themselves -- rather
        # than silently reinstalling the shipped all-approved defaults. The
        # shared home lock must still be released (not left held) once the
        # aborted update unwinds.
        with tempfile.TemporaryDirectory(prefix="fleet policy corruption ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1"}
            install_args = [PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)]
            install = subprocess.run(install_args, cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)

            policy_path = config / "delegate-skills" / "provider-policy.json"
            settings_path = home / ".claude" / "settings.json"
            fleet_path = config / "delegate-skills" / "config.json"
            lock_file = self.catalog["lock_path"](home)

            # Revoke every family but "claude" before corrupting anything, so
            # a fall-back-to-shipped-defaults bug would be visible as a
            # resurrected approval rather than a merely absent one.
            decisions = root / "decisions-claude-only.json"
            decisions.write_text(json.dumps({"format": "provider-policy-decisions.v1", "approvedFamilies": ["claude"], "allowDiscovery": False}))
            setup = subprocess.run([str(home / ".local/bin/claude-fleet-setup"), "--decisions", str(decisions)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            known_family_names = {family["name"] for family in json.loads(policy_path.read_text())["families"] if isinstance(family, dict) and isinstance(family.get("name"), str)}
            self.assertIn("codex", known_family_names, "fixture assumption: a revocable family other than claude must exist")

            def snapshot():
                return policy_path.read_bytes(), settings_path.read_bytes(), fleet_path.read_bytes() if fleet_path.is_file() else None

            good_policy_bytes = policy_path.read_bytes()
            baseline = snapshot()

            def assert_update_aborts_without_mutation(bad_bytes: bytes, label: str):
                policy_path.write_bytes(bad_bytes)
                update = subprocess.run(install_args, cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertNotEqual(update.returncode, 0, f"{label}: update must refuse to proceed")
                self.assertIn("resolve or restore it before installing", update.stderr, f"{label}: must abort for the installed-policy reason, not an unrelated failure")
                self.assertEqual(policy_path.read_bytes(), bad_bytes, f"{label}: damaged/malformed policy bytes must be preserved untouched")
                self.assertEqual(settings_path.read_bytes(), baseline[1], f"{label}: settings.json must not be mutated by an aborted update")
                if baseline[2] is not None:
                    self.assertEqual(fleet_path.read_bytes(), baseline[2], f"{label}: fleet config must not be mutated by an aborted update")
                self.assertFalse(lock_file.exists(), f"{label}: shared home lock must not remain held after an aborted update")

            # Unreadable/invalid JSON bytes.
            assert_update_aborts_without_mutation(b"{not valid json at all", "corrupt bytes")

            # approvedFamilies present but null.
            null_allowlist = json.loads(good_policy_bytes)
            null_allowlist["approvedFamilies"] = None
            assert_update_aborts_without_mutation(json.dumps(null_allowlist).encode("utf-8"), "null approvedFamilies")

            # approvedFamilies present but a string, not a list.
            string_allowlist = json.loads(good_policy_bytes)
            string_allowlist["approvedFamilies"] = "claude"
            assert_update_aborts_without_mutation(json.dumps(string_allowlist).encode("utf-8"), "string approvedFamilies")

            # approvedFamilies containing a non-string element.
            non_string_entry = json.loads(good_policy_bytes)
            non_string_entry["approvedFamilies"] = ["claude", 123]
            assert_update_aborts_without_mutation(json.dumps(non_string_entry).encode("utf-8"), "non-string approvedFamilies entry")

            # approvedFamilies naming a family the policy does not define.
            unknown_name = json.loads(good_policy_bytes)
            unknown_name["approvedFamilies"] = ["claude", "not-a-real-family"]
            assert_update_aborts_without_mutation(json.dumps(unknown_name).encode("utf-8"), "unknown approvedFamilies name")

            # Restore the known-good, revoked policy and confirm an update
            # still succeeds and preserves the empty-list-is-a-denial case.
            policy_path.write_bytes(good_policy_bytes)
            recovered = subprocess.run(install_args, cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(json.loads(policy_path.read_text()).get("approvedFamilies"), ["claude"])
            self.assertFalse(lock_file.exists(), "lock must not remain held after a successful update")

    def test_installed_fake_gateway_sessionstart_update_uninstall(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is required for gateway SessionStart integration")
        fleet = json.loads((ROOT / "config/delegate-fleet.json").read_text())
        rows_by_id = {}
        for config in fleet["lanes"].values():
            for model in [config.get("model"), *(config.get("fallbacks") or [])]:
                if not isinstance(model, str):
                    continue
                base = model.rsplit("[", 1)[0] if model.endswith("]") else model
                rows_by_id[base] = {"id": base, "context_length": 272000 if base == "codex-5.5" else (872000 if model.endswith("]") else 200000)}
        FakeProvider.rows = list(rows_by_id.values())
        FakeProvider.mode = "ok"
        FakeProvider.requests = 0
        server = socketserver.TCPServer(("127.0.0.1", 0), FakeProvider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="fleet gateway ") as raw:
                root = Path(raw)
                home = root / "home"
                config = root / "config"
                endpoint = f"http://127.0.0.1:{server.server_address[1]}"
                env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1", "FAKE_GATEWAY_TOKEN": "fake-token"}
                install_args = [PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config), "--gateway-url", endpoint, "--allow-insecure-http", "--gateway-token-env", "FAKE_GATEWAY_TOKEN", "--enable-model-discovery"]
                install = subprocess.run(install_args, cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(install.returncode, 0, install.stderr)
                installed_settings = json.loads((home / ".claude/settings.json").read_text())
                installed_settings["model"] = "codex-5.5"
                (home / ".claude/settings.json").write_text(json.dumps(installed_settings, indent=2) + "\n")
                sync = subprocess.run([shutil.which("node"), str(home / ".claude/sync-omniroute-models.mjs"), "--home", str(home), "--config-home", str(config), "--quiet", "--drift"], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(sync.returncode, 0, sync.stderr)
                settings_after_start = json.loads((home / ".claude/settings.json").read_text())
                self.assertEqual(settings_after_start["autoCompactWindow"], 244800)
                self.assertEqual(settings_after_start["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "244800")
                self.assertGreaterEqual(FakeProvider.requests, 1)
                self.assertNotIn("fake-token", sync.stdout + sync.stderr)
                post_switch = subprocess.run([PYTHON, str(home / ".claude/sync-model-context.py"), "--home", str(home), "--config-home", str(config)], input=json.dumps({"hook_event_name": "PostModelSwitch", "to_model": "codex-5.5"}), cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(post_switch.returncode, 0, post_switch.stderr)
                switched = json.loads((home / ".claude/settings.json").read_text())
                self.assertEqual(switched["autoCompactWindow"], 244800)
                self.assertEqual(switched["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "244800")
                self.assertIn("Restart Claude Code", post_switch.stdout)
                cache = json.loads((home / ".claude/cache/omniroute-models-cache.json").read_text())
                self.assertEqual(cache["endpoint"], endpoint)
                self.assertEqual(cache["account"], hashlib.sha256(b"fake-token").hexdigest()[:24])
                first_agent = home / ".claude/agents/fleet-implement.md"
                self.assertIn('model: "codex-luna[1m]"', first_agent.read_text())
                manifest_before = json.loads((home / ".claude/.claude-agents-config-manifest.json").read_text())
                repeat = subprocess.run([shutil.which("node"), str(home / ".claude/sync-omniroute-models.mjs"), "--home", str(home), "--config-home", str(config), "--quiet", "--drift"], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(repeat.returncode, 0, repeat.stderr)
                doctor = subprocess.run([str(home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(doctor.returncode, 0, doctor.stderr)
                manifest_after = json.loads((home / ".claude/.claude-agents-config-manifest.json").read_text())
                self.assertEqual(manifest_after["fleetSha256"], hashlib.sha256((config / "delegate-skills/config.json").read_bytes()).hexdigest())
                update = subprocess.run(install_args, cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(update.returncode, 0, update.stderr)
                uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        finally:
            server.shutdown()
            server.server_close()
            FakeProvider.mode = "ok"

    def test_reconcile_rejects_damaged_transaction_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory(prefix="fleet transaction ") as raw:
            home = Path(raw) / "home"
            txn = home / ".claude" / ".fleet-reconcile-txn"
            txn.mkdir(parents=True)
            marker = home / ".claude" / ".fleet-reconcile-pending.json"
            marker.write_text(json.dumps({"format": "claude-agents-config.reconcile-transaction.v1", "state": "prepared", "directory": str(txn)}))
            (txn / "manifest.json").write_text(json.dumps({"format": "claude-agents-config.reconcile-transaction.v1", "state": "prepared", "records": [{"path": str(home / "target.json"), "existed": True, "backup": "0000.bin", "mode": 0o600, "sha256": "0" * 64}]}))
            (txn / "0000.bin").write_bytes(b"damaged")
            namespace = {}
            sys.path.insert(0, str(ROOT / "scripts"))
            exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
            with self.assertRaises(OSError):
                namespace["recover_transaction"](home)
            self.assertTrue(marker.exists())
            self.assertTrue(txn.exists())

    def test_hooks_execute_malformed_input_and_redact_full_secret(self):
        with tempfile.TemporaryDirectory(prefix="fleet hooks ") as raw:
            home = Path(raw) / "home"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "CLAUDE_FLEET_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
            malformed_route = subprocess.run([PYTHON, str(ROOT / "scripts/route-to-fleet.py")], input="[]", text=True, env=env, capture_output=True)
            self.assertEqual(malformed_route.returncode, 0)
            malformed_status = subprocess.run([PYTHON, str(ROOT / "scripts/subagent-statusline.py")], input="[]", text=True, env=env, capture_output=True)
            self.assertEqual(malformed_status.returncode, 0)
            secret = "sk-" + "x" * 40
            prompt = "ordinary words " + secret + " after secret crossing the excerpt boundary " + ("z" * 260)
            prompt = "opaque request " + secret + " and no recognizable routing keyword " + ("z" * 260)
            routed = subprocess.run([PYTHON, str(ROOT / "scripts/route-to-fleet.py")], input=json.dumps({"prompt": prompt}), text=True, env=env, capture_output=True)
            self.assertEqual(routed.returncode, 0)
            log = home / ".claude/routing-log.jsonl"
            self.assertTrue(log.exists())
            content = log.read_text()
            self.assertNotIn(secret, content)
            self.assertIn("[redacted]", content)
            entries = [json.loads(line) for line in content.splitlines() if line]
            self.assertLessEqual(len(entries[-1].get("prompt", "")), 160)

    def test_compaction_pair_reduces_to_smaller_active_model_and_preserves_lower_budget(self):
        namespace = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
        settings = {"model": "codex-5.5", "autoCompactWindow": 800000, "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}}
        updated, notice, budget = namespace["reconcile_context"](settings, {"codex-5.5": {"context_length": 272000}})
        self.assertEqual(budget, 244800)
        self.assertEqual(updated["autoCompactWindow"], 244800)
        self.assertEqual(updated["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "244800")
        self.assertIn("Restart Claude Code", notice)
        context_script = (ROOT / "scripts/sync-model-context.py").read_text()
        self.assertIn('latest["autoCompactWindow"] = compact_budget', context_script)
        lower = {"model": "codex-5.5", "autoCompactWindow": 180000, "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "180000"}}
        preserved, _, lower_budget = namespace["reconcile_context"](lower, {"codex-5.5": {"context_length": 272000}})
        self.assertEqual(lower_budget, 180000)
        self.assertEqual(preserved["autoCompactWindow"], 180000)
        self.assertEqual(preserved["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "180000")

    def test_proposal_path_follows_resolved_home_not_policy_directory(self):
        with tempfile.TemporaryDirectory(prefix="fleet proposal ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "external config"  # deliberately not under home
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)
            shutil.copy(ROOT / "scripts/provider_catalog.py", claude_dir / "provider_catalog.py")
            setup_script = claude_dir / "claude-fleet-setup.py"
            shutil.copy(ROOT / "bin/claude-fleet-setup", setup_script)
            policy_dir = config / "delegate-skills"
            policy_dir.mkdir(parents=True)
            (policy_dir / "provider-policy.json").write_text((ROOT / "config/provider-policy.json").read_text())
            proposal_file = claude_dir / "fleet-model-proposal.json"
            proposal_file.write_text(json.dumps({"models_hash": "abc123"}))
            decisions_ok = root / "decisions-approve.json"
            decisions_ok.write_text(json.dumps({"format": "provider-policy-decisions.v1", "proposalHash": "abc123", "proposalDecision": "approve"}))
            approve = subprocess.run([PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(decisions_ok)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(approve.returncode, 0, approve.stderr)
            recorded = json.loads(proposal_file.read_text())
            self.assertEqual(recorded["decision"], "approve")
            # A decision file whose hash no longer matches the pending proposal
            # must be rejected without touching the recorded evidence.
            decisions_bad = root / "decisions-bad-hash.json"
            decisions_bad.write_text(json.dumps({"format": "provider-policy-decisions.v1", "proposalHash": "does-not-match", "proposalDecision": "reject"}))
            reject = subprocess.run([PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(decisions_bad)], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(reject.returncode, 0)
            unchanged = json.loads(proposal_file.read_text())
            self.assertEqual(unchanged["decision"], "approve")

    def test_setup_rejects_malformed_decision_documents_without_any_side_effect(self):
        # An unsupported format, a document mixing proposal-decision fields
        # with family-approval fields, or an unknown key must all be
        # rejected before any write happens. Before the fix, a mixed
        # document was accepted: the early-return proposal branch resolved
        # the pending proposal (a real write) and silently dropped the
        # approvedFamilies fields, and format validation never ran for it.
        with tempfile.TemporaryDirectory(prefix="fleet setup validation ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)
            shutil.copy(ROOT / "scripts/provider_catalog.py", claude_dir / "provider_catalog.py")
            setup_script = claude_dir / "claude-fleet-setup.py"
            shutil.copy(ROOT / "bin/claude-fleet-setup", setup_script)
            policy_dir = config / "delegate-skills"
            policy_dir.mkdir(parents=True)
            policy_path = policy_dir / "provider-policy.json"
            policy_path.write_text((ROOT / "config/provider-policy.json").read_text())
            proposal_file = claude_dir / "fleet-model-proposal.json"
            proposal_file.write_text(json.dumps({"models_hash": "abc123"}))

            def digests():
                return (hashlib.sha256(policy_path.read_bytes()).hexdigest(), hashlib.sha256(proposal_file.read_bytes()).hexdigest())

            baseline = digests()

            wrong_format = root / "decisions-wrong-format.json"
            wrong_format.write_text(json.dumps({"format": "bogus-format.v1", "approvedFamilies": ["claude"]}))
            result_wrong_format = subprocess.run([PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(wrong_format)], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(result_wrong_format.returncode, 0)
            self.assertEqual(digests(), baseline, "an unsupported format must not write anything")

            mixed = root / "decisions-mixed.json"
            mixed.write_text(json.dumps({
                "format": "provider-policy-decisions.v1",
                "proposalDecision": "approve",
                "proposalHash": "abc123",
                "approvedFamilies": ["claude"],
            }))
            result_mixed = subprocess.run([PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(mixed)], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(result_mixed.returncode, 0, result_mixed.stdout)
            self.assertEqual(digests(), baseline, "a mixed proposal/family-approval document must not resolve the proposal or touch the policy")

            unknown_key = root / "decisions-unknown-key.json"
            unknown_key.write_text(json.dumps({"format": "provider-policy-decisions.v1", "approvedFamilies": ["claude"], "fallbacks": {"fleet-implement": ["claude-sonnet-5"]}}))
            result_unknown = subprocess.run([PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(unknown_key)], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(result_unknown.returncode, 0)
            self.assertEqual(digests(), baseline, "an unsupported/unknown decision key must not write anything")

            # A genuinely valid, unmixed document must still succeed, proving
            # the new validation is not simply rejecting everything.
            valid = root / "decisions-valid.json"
            valid.write_text(json.dumps({"format": "provider-policy-decisions.v1", "approvedFamilies": ["claude"], "allowDiscovery": False}))
            result_valid = subprocess.run([PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(valid)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result_valid.returncode, 0, result_valid.stderr)
            self.assertEqual(json.loads(policy_path.read_text())["approvedFamilies"], ["claude"])

    def test_setup_defers_under_lock_contention_without_success_or_mutation(self):
        # claude-fleet-setup writes both the provider policy and the fleet
        # model proposal, both of which are also written by other fleet
        # writers (the installer, fleet-model-drift) under the same shared
        # home lock. Before the fix, setup never acquired that lock at all,
        # so a concurrent writer's changes could be silently clobbered. This
        # exercises an *actual* contended holder (not just a unit check of
        # the lock primitive) against both setup decision types and asserts
        # that contention produces neither a success message nor a mutation.
        with tempfile.TemporaryDirectory(prefix="fleet setup lock ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)
            shutil.copy(ROOT / "scripts/provider_catalog.py", claude_dir / "provider_catalog.py")
            setup_script = claude_dir / "claude-fleet-setup.py"
            shutil.copy(ROOT / "bin/claude-fleet-setup", setup_script)
            policy_dir = config / "delegate-skills"
            policy_dir.mkdir(parents=True)
            policy_path = policy_dir / "provider-policy.json"
            policy_path.write_text((ROOT / "config/provider-policy.json").read_text())
            proposal_file = claude_dir / "fleet-model-proposal.json"
            proposal_file.write_text(json.dumps({"models_hash": "abc123"}))

            def digests():
                return (hashlib.sha256(policy_path.read_bytes()).hexdigest(), hashlib.sha256(proposal_file.read_bytes()).hexdigest())

            baseline = digests()
            hold_script = (
                "import sys, time; sys.path.insert(0, sys.argv[1]);"
                "from provider_catalog import acquire_lock, release_lock;"
                "from pathlib import Path;"
                "fd, path = acquire_lock(Path(sys.argv[2]), timeout=2.0);"
                "print('ACQUIRED' if fd is not None else 'FAILED', flush=True);"
                "time.sleep(1.5);"
                "release_lock(fd, path)"
            )
            holder_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            holder = subprocess.Popen([PYTHON, "-c", hold_script, str(ROOT / "scripts"), str(home)], env=holder_env, text=True, stdout=subprocess.PIPE)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ACQUIRED")

                family_decisions = root / "decisions-family.json"
                family_decisions.write_text(json.dumps({"format": "provider-policy-decisions.v1", "approvedFamilies": ["claude"], "allowDiscovery": False}))
                family_result = subprocess.run(
                    [PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(family_decisions), "--lock-timeout", "0.2"],
                    cwd=ROOT, text=True, capture_output=True,
                )
                self.assertNotEqual(family_result.returncode, 0)
                self.assertNotIn("Saved explicit fleet policy decisions", family_result.stdout)
                self.assertEqual(digests(), baseline, "a contended family-approval write must not mutate anything")

                proposal_decisions = root / "decisions-proposal.json"
                proposal_decisions.write_text(json.dumps({"format": "provider-policy-decisions.v1", "proposalHash": "abc123", "proposalDecision": "approve"}))
                proposal_result = subprocess.run(
                    [PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(proposal_decisions), "--lock-timeout", "0.2"],
                    cwd=ROOT, text=True, capture_output=True,
                )
                self.assertNotEqual(proposal_result.returncode, 0)
                self.assertNotIn("Recorded proposal decision", proposal_result.stdout)
                self.assertEqual(digests(), baseline, "a contended proposal-decision write must not mutate anything")
            finally:
                holder.wait(timeout=5)
                holder.stdout.close()

            # Once the lock clears, both decision types must still succeed normally.
            family_after = subprocess.run(
                [PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(family_decisions)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(family_after.returncode, 0, family_after.stderr)
            proposal_after = subprocess.run(
                [PYTHON, str(setup_script), "--home", str(home), "--config-home", str(config), "--decisions", str(proposal_decisions)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(proposal_after.returncode, 0, proposal_after.stderr)

    def test_context_sync_repairs_stale_compaction_pair_when_max_context_already_matches(self):
        with tempfile.TemporaryDirectory(prefix="fleet context repair ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            (home / ".claude" / "cache").mkdir(parents=True)
            (config / "delegate-skills").mkdir(parents=True)
            (config / "delegate-skills" / "provider-policy.json").write_text((ROOT / "config/provider-policy.json").read_text())
            settings = {
                "model": "codex-5.5",
                # CLAUDE_CODE_MAX_CONTEXT_TOKENS is already correct (244800), but
                # the paired compaction controls were never repaired and remain
                # at the old 800000 ceiling; a defect that short-circuits on the
                # context value alone must not skip fixing this pair.
                "autoCompactWindow": 800000,
                "env": {
                    "ANTHROPIC_BASE_URL": "https://gateway.test/api",
                    "ANTHROPIC_AUTH_TOKEN": "fake-token",
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "244800",
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000",
                },
            }
            (home / ".claude" / "settings.json").write_text(json.dumps(settings))
            cache = self.catalog["cache_payload"]([{"id": "codex-5.5", "context_length": 272000}], "https://gateway.test/api", "fake-token", fetched_at=int(__import__("time").time()))
            (home / ".claude" / "cache" / "omniroute-models-cache.json").write_text(json.dumps(cache))
            result = subprocess.run(
                [PYTHON, str(ROOT / "scripts/sync-model-context.py"), "--home", str(home), "--config-home", str(config)],
                input=json.dumps({"hook_event_name": "PostModelSwitch", "to_model": "codex-5.5"}),
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = json.loads((home / ".claude" / "settings.json").read_text())
            self.assertEqual(updated["autoCompactWindow"], 244800)
            self.assertEqual(updated["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "244800")
            self.assertEqual(updated["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "244800")
            self.assertIn("Restart Claude Code", result.stdout)

    def test_reconcile_removed_primary_with_external_config_home_leaves_no_poisoned_marker(self):
        namespace = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
        with tempfile.TemporaryDirectory(prefix="fleet external config ") as raw:
            root = Path(raw)
            # config_home is deliberately a sibling of home, not nested under
            # it: an installed-layout XDG_CONFIG_HOME or --config-home is
            # legitimately external to --home.
            home = root / "home"
            config_home = root / "external-config"
            (home / ".claude").mkdir(parents=True)
            (config_home / "delegate-skills").mkdir(parents=True)
            policy_path = config_home / "delegate-skills" / "provider-policy.json"
            policy_path.write_text((ROOT / "config/provider-policy.json").read_text())
            fleet_path = config_home / "delegate-skills" / "config.json"
            fleet_path.write_text(json.dumps({
                "version": "delegate-fleet.v1",
                "lanes": {
                    "implement": {
                        "implementer": "claude",
                        "model": "codex-old-removed[1m]",
                        "fallbacks": ["codex-luna[1m]"],
                        "effort": "high",
                    }
                },
            }))
            settings_path = home / ".claude" / "settings.json"
            settings_path.write_text(json.dumps({"modelPicker": {"options": []}}))
            rows = [{"id": "codex-luna", "context_length": 872000}]

            marker = home / ".claude" / ".fleet-reconcile-pending.json"

            def run_once():
                return namespace["reconcile_home"](home, config_home, policy_path, rows)

            result = run_once()
            self.assertEqual(result["status"], "applied", result)
            self.assertEqual(result["lane_changes"]["implement"]["to"], "codex-luna[1m]")
            self.assertFalse(marker.exists(), "no leftover transaction marker after a successful external-config-home reconcile")
            updated_fleet = json.loads(fleet_path.read_text())
            self.assertEqual(updated_fleet["lanes"]["implement"]["model"], "codex-luna[1m]")

            # Repeat reconciliation (doctor-style re-check) must stay clean and
            # idempotent: no poisoned marker should ever have been written, so
            # a second pass finds nothing to recover and reports unchanged.
            second = run_once()
            self.assertEqual(second["status"], "unchanged", second)
            self.assertFalse(marker.exists())

    def test_reconcile_settings_prunes_stale_provider_models_while_preserving_first_party_and_custom(self):
        namespace = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
        policy = json.loads((ROOT / "config/provider-policy.json").read_text())
        settings = {
            "model": "agy-gemini-flash[1m]",
            "modelPicker": {
                "options": [
                    {"model": "claude-opus-5[1m]", "label": "Opus 5", "description": "Gateway Claude Opus"},
                    {"model": "deepseek-openrouter[1m]", "label": "DeepSeek", "description": "Gateway model (1000K gateway context)"},
                    {"model": "codex-astra[1m]", "label": "Codex Astra", "description": "Gateway model (872K gateway context)"},
                    {"model": "my-custom-model", "label": "Custom Local", "description": "User local model"},
                ],
                "replaceBuiltInOptions": True,
            },
        }
        picker = {
            "options": [
                {"model": "agy-gemini-flash[1m]", "label": "Gemini Flash", "description": "Gateway model (1000K gateway context)"},
            ],
            "replaceBuiltInOptions": True,
        }
        fleet = {
            "version": "delegate-fleet.v1",
            "lanes": {
                "tests": {"implementer": "claude", "model": "agy-gemini-flash[1m]"},
            },
        }
        reconciled = namespace["reconcile_settings"](settings, picker, fleet, policy)
        models = [row["model"] for row in reconciled["modelPicker"]["options"]]
        self.assertIn("agy-gemini-flash[1m]", models, "live catalog model must be in picker")
        self.assertIn("my-custom-model", models, "custom non-gateway user rows must be preserved")
        self.assertNotIn("deepseek-openrouter[1m]", models, "removed gateway model must be pruned")
        self.assertNotIn("codex-astra[1m]", models, "removed gateway model must be pruned")
        self.assertNotIn("claude-opus-5[1m]", models, "removed gateway model must be pruned")

    def test_resolve_fleet_prunes_removed_candidates_from_lane_fallbacks(self):
        namespace = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
        policy = json.loads((ROOT / "config/provider-policy.json").read_text())
        fleet = {
            "version": "delegate-fleet.v1",
            "lanes": {
                "review-06-astra": {
                    "implementer": "claude",
                    "model": "codex-astra[1m]",
                    "fallbacks": ["claude-opus-5[1m]"],
                },
            },
        }
        # Provider returns agy-gemini-flash and claude-opus-5; codex-astra was removed
        rows = [
            {"id": "agy-gemini-flash", "context_length": 1000000},
            {"id": "claude-opus-5", "context_length": 1000000},
        ]
        resolved, picker, pending, catalog = namespace["resolve_fleet"](fleet, {}, rows, policy)
        lane = resolved["lanes"]["review-06-astra"]
        self.assertEqual(lane["model"], "claude-opus-5[1m]")
        self.assertNotIn("fallbacks", lane, "stale codex-astra must not remain as a fallback")

    def test_resolve_fleet_dynamically_assigns_live_candidate_when_all_lane_models_removed(self):
        namespace = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
        policy = json.loads((ROOT / "config/provider-policy.json").read_text())
        fleet = {
            "version": "delegate-fleet.v1",
            "lanes": {
                "plan": {
                    "implementer": "claude",
                    "model": "claude-opus-5[1m]",
                    "effort": "xhigh",
                    "readOnly": True,
                },
            },
        }
        # Provider only has agy-claude-opus and agy-gemini-flash; claude-opus-5 was removed
        rows = [
            {"id": "agy-claude-opus", "context_length": 1000000, "capabilities": {"thinking": True}},
            {"id": "agy-gemini-flash", "context_length": 1000000},
        ]
        resolved, picker, pending, catalog = namespace["resolve_fleet"](fleet, {}, rows, policy)
        self.assertEqual(len(pending), 0, f"dynamic fallback must resolve lane without pending errors: {pending}")
        lane = resolved["lanes"]["plan"]
        self.assertEqual(lane["model"], "agy-claude-opus[1m]", "plan should match agy-claude-opus by token/capability")

    def test_omniroute_sync_surfaces_bounded_reconcile_child_failure(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is required for the omniroute sync hook")
        with tempfile.TemporaryDirectory(prefix="fleet reconcile failure ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            claude_dir = home / ".claude"
            (claude_dir / "cache").mkdir(parents=True)
            (config / "delegate-skills").mkdir(parents=True)
            (config / "delegate-skills" / "provider-policy.json").write_text(json.dumps({"version": "provider-policy.v1", "provider": "omniroute"}))
            (claude_dir / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}}))
            (claude_dir / "cache" / "omniroute-models-cache.json").write_text("{}")
            # A reconcile child that always fails; the hook must not swallow
            # this into a silent null response.
            (claude_dir / "fleet-reconcile.py").write_text("import sys\nsys.exit(3)\n")
            shutil.copy(ROOT / "scripts/sync-omniroute-models.mjs", claude_dir / "sync-omniroute-models.mjs")
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
            result = subprocess.run(
                [shutil.which("node"), str(claude_dir / "sync-omniroute-models.mjs"), "--home", str(home), "--config-home", str(config), "--quiet"],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip(), "the hook must report the reconcile failure instead of emitting nothing")
            output = json.loads(result.stdout)
            self.assertIn("Fleet reconciliation failed", output.get("systemMessage", ""))
            self.assertIn("status 3", output["systemMessage"])

    def test_generator_recovers_interrupted_transaction_preserves_damaged_evidence_and_defers_to_live_lock(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is required for the fleet-sync generator")
        with tempfile.TemporaryDirectory(prefix="fleet generator recovery ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            # The writer lock lives under home/.claude, not under tmpdir(), so
            # a distinct TMPDIR for this child proves the lock location does
            # not depend on it (see test_lock_path_is_home_scoped_not_tmpdir).
            child_tmp = root / "child-tmp"
            child_tmp.mkdir()
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(child_tmp)}
            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            sync_bin = home / ".local/bin/claude-fleet-sync"
            doctor_bin = home / ".local/bin/claude-agents-doctor"
            agent_file = home / ".claude/agents/fleet-implement.md"
            original = agent_file.read_text()
            txn_dir = home / ".claude" / ".claude-fleet-sync-txn"
            marker = home / ".claude" / ".claude-fleet-sync-pending.json"

            def run_sync():
                return subprocess.run([str(sync_bin)], cwd=ROOT, env=env, text=True, capture_output=True)

            # --- Phase 1: an interrupted *valid* prepared transaction. A prior
            # run crashed after partially overwriting the target but before
            # committing; the next run must roll the target back to its
            # recorded prior content before treating the fleet as fresh input,
            # then regenerate normally and leave no evidence behind.
            txn_dir.mkdir(parents=True)
            (txn_dir / "manifest.json").write_text(json.dumps({
                "format": "claude-agents-config.sync-transaction.v1",
                "state": "prepared",
                "records": [{
                    "path": str(agent_file),
                    "existed": True,
                    "previous": base64.b64encode(original.encode()).decode(),
                    "previousSha256": hashlib.sha256(original.encode()).hexdigest(),
                    "payload": None,
                    "mode": 0o644,
                }],
            }))
            marker.write_text(json.dumps({"format": "claude-agents-config.sync-transaction.v1", "state": "prepared", "directory": str(txn_dir)}))
            agent_file.write_text("CORRUPTED MID WRITE")
            recovered = run_sync()
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertNotIn("CORRUPTED", agent_file.read_text())
            self.assertFalse(marker.exists())
            self.assertFalse(txn_dir.exists())
            doctor_after_recovery = subprocess.run([str(doctor_bin), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(doctor_after_recovery.returncode, 0, doctor_after_recovery.stderr)

            # --- Phase 2: a damaged transaction (record path escapes the
            # managed agents directory). It must be rejected, and rejection
            # must preserve the evidence rather than silently discarding it.
            good_content = agent_file.read_text()
            txn_dir.mkdir(parents=True)
            (txn_dir / "manifest.json").write_text(json.dumps({
                "format": "claude-agents-config.sync-transaction.v1",
                "state": "prepared",
                "records": [{"path": "/etc/passwd", "existed": True, "previous": "AAAA", "payload": None, "mode": 0o644}],
            }))
            marker.write_text(json.dumps({"format": "claude-agents-config.sync-transaction.v1", "state": "prepared", "directory": str(txn_dir)}))
            damaged = run_sync()
            self.assertNotEqual(damaged.returncode, 0)
            self.assertTrue(marker.exists(), "damaged transaction evidence must be preserved, not deleted")
            self.assertTrue(txn_dir.exists())
            self.assertEqual(agent_file.read_text(), good_content, "a rejected recovery must not mutate any target")

            # Clean up the damaged fixture directly (simulating manual
            # operator repair) before exercising the live-lock scenario.
            shutil.rmtree(txn_dir)
            marker.unlink()

            # --- Phase 3: a canonical writer lock owned by a still-live pid
            # must defer the generator without mutating anything even once it
            # is older than the stale-age bound; only age plus a dead owning
            # pid may evict a lock. The lock is planted with this test
            # process's own pid (guaranteed alive) and an mtime pushed well
            # past the stale bound, isolating the pid-liveness check from the
            # age check.
            lock_path = home.resolve() / ".claude" / ".claude-agents-config.lock"
            lock_path.write_text(f"{os.getpid()}:deadbeefdeadbeef")
            old = time.time() - 3600
            os.utime(lock_path, (old, old))
            try:
                before = agent_file.read_text()
                deferred = run_sync()
                self.assertNotEqual(deferred.returncode, 0)
                self.assertEqual(agent_file.read_text(), before, "a live-owner lock must defer without mutating any target")
                self.assertTrue(lock_path.exists(), "a live-owner lock must not be evicted merely for its age")
            finally:
                lock_path.unlink(missing_ok=True)

            # A lock older than the stale bound whose owning pid is provably
            # dead (an unused, unparseable-as-live pid) must be reclaimed so
            # the fleet does not wedge forever behind a crashed writer.
            dead_pid = 999999999
            lock_path.write_text(f"{dead_pid}:deadbeefdeadbeef")
            os.utime(lock_path, (old, old))
            reclaimed = run_sync()
            self.assertEqual(reclaimed.returncode, 0, reclaimed.stderr)

            # The generator must still work normally once the live lock clears.
            final = run_sync()
            self.assertEqual(final.returncode, 0, final.stderr)

    def test_generator_transaction_fault_regressions(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is required for the fleet-sync generator")
        with tempfile.TemporaryDirectory(prefix="fleet generator faults ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1"}
            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            sync_bin = home / ".local/bin/claude-fleet-sync"
            agent_file = home / ".claude/agents/fleet-implement.md"
            live_content = agent_file.read_text()
            txn_dir = home / ".claude" / ".claude-fleet-sync-txn"
            marker = home / ".claude" / ".claude-fleet-sync-pending.json"

            def run_sync():
                return subprocess.run([str(sync_bin)], cwd=ROOT, env=env, text=True, capture_output=True)

            # --- A corrupted "previous" backup payload (previousSha256 no
            # longer matches the decoded bytes) must be rejected -- and must
            # preserve the evidence and every target -- rather than restoring
            # the wrong bytes over a live target undetected.
            txn_dir.mkdir(parents=True)
            stale_previous = "some prior agent content that predates this backup"
            (txn_dir / "manifest.json").write_text(json.dumps({
                "format": "claude-agents-config.sync-transaction.v1",
                "state": "prepared",
                "records": [{
                    "path": str(agent_file),
                    "existed": True,
                    "previous": base64.b64encode(stale_previous.encode()).decode(),
                    "previousSha256": hashlib.sha256(b"different bytes than what was actually encoded").hexdigest(),
                    "payload": None,
                    "mode": 0o644,
                }],
            }))
            marker.write_text(json.dumps({"format": "claude-agents-config.sync-transaction.v1", "state": "prepared", "directory": str(txn_dir)}))
            corrupted = run_sync()
            self.assertNotEqual(corrupted.returncode, 0)
            self.assertIn("backup is corrupted", corrupted.stderr)
            self.assertTrue(marker.exists(), "corrupted-backup evidence must be preserved, not deleted")
            self.assertTrue(txn_dir.exists())
            self.assertEqual(agent_file.read_text(), live_content, "a rejected corrupted backup must not mutate the target")
            shutil.rmtree(txn_dir)
            marker.unlink()

            # --- A "committed" marker whose transaction directory is already
            # gone (a prior cleanup crashed after removing the directory but
            # before unlinking the marker) must be recovered by simply
            # dropping the stale marker, without touching any target.
            marker.write_text(json.dumps({"format": "claude-agents-config.sync-transaction.v1", "state": "committed", "directory": str(txn_dir)}))
            self.assertFalse(txn_dir.exists())
            recovered_missing_dir = run_sync()
            self.assertEqual(recovered_missing_dir.returncode, 0, recovered_missing_dir.stderr)
            self.assertFalse(marker.exists(), "a committed marker pointing at a missing directory must be dropped")
            doctor = subprocess.run([str(home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            live_content = agent_file.read_text()

            # --- A "committed" transaction whose bookkeeping cleanup itself
            # fails (here: an unexpected subdirectory left inside the
            # transaction directory makes the unlinkSync loop throw) must
            # leave every target exactly as committed -- never rolled back to
            # the pre-commit "previous" payload -- since a committed write
            # already landed for real. "previous" is deliberately different
            # from the live target content so a wrongful rollback would be
            # detectable.
            txn_dir.mkdir(parents=True)
            pre_commit_content = "pre-commit content that must never be restored"
            self.assertNotEqual(pre_commit_content, live_content)
            (txn_dir / "manifest.json").write_text(json.dumps({
                "format": "claude-agents-config.sync-transaction.v1",
                "state": "committed",
                "records": [{
                    "path": str(agent_file),
                    "existed": True,
                    "previous": base64.b64encode(pre_commit_content.encode()).decode(),
                    "previousSha256": hashlib.sha256(pre_commit_content.encode()).hexdigest(),
                    "payload": None,
                    "mode": 0o644,
                }],
            }))
            marker.write_text(json.dumps({"format": "claude-agents-config.sync-transaction.v1", "state": "committed", "directory": str(txn_dir)}))
            (txn_dir / "leftover-directory").mkdir()
            cleanup_failure = run_sync()
            self.assertNotEqual(cleanup_failure.returncode, 0, "an unlinkSync failure during committed-transaction cleanup must not be silently swallowed")
            self.assertNotIn("evidence preserved at", cleanup_failure.stderr, "this must fail from the cleanup loop itself, not from an earlier validation guard")
            self.assertEqual(agent_file.read_text(), live_content, "a committed transaction must never roll a target back to its pre-commit content, even when cleanup itself fails")
            self.assertTrue(marker.exists() or txn_dir.exists(), "cleanup-failure evidence must be preserved, not silently discarded")
            shutil.rmtree(txn_dir, ignore_errors=True)
            marker.unlink(missing_ok=True)
            # The crashed child raised past its own writer-lock release, so
            # the lock outlives it (by design: an unattended lock is only
            # ever reclaimed once it is provably stale, exactly as exercised
            # in test_generator_recovers_interrupted_transaction_...). Clear
            # it here as the manual operator repair that scenario documents.
            lock_path = home.resolve() / ".claude" / ".claude-agents-config.lock"
            lock_path.unlink(missing_ok=True)

            # The generator must still work normally afterward.
            final = run_sync()
            self.assertEqual(final.returncode, 0, final.stderr)

    def test_shared_lock_contention_defers_reconciliation(self):
        import time
        catalog = self.catalog
        with tempfile.TemporaryDirectory(prefix="fleet lock ") as raw:
            home = Path(raw) / "home"
            (home / ".claude").mkdir(parents=True)
            fd, lock = catalog["acquire_lock"](home, timeout=0.1)
            self.assertIsNotNone(fd)
            try:
                self.assertIsNone(catalog["acquire_lock"](home, timeout=0.05)[0])
            finally:
                catalog["release_lock"](fd, lock)

    def test_lock_path_is_home_scoped_not_tmpdir(self):
        # Two processes resolving the same home must land on the identical
        # lock file even when their TMPDIR/TMP/TEMP differ, since that is an
        # environment value each caller (shell, sandbox, test harness) can
        # set independently for the very same target home.
        with tempfile.TemporaryDirectory(prefix="fleet lock path ") as raw:
            home = Path(raw) / "home"
            (home / ".claude").mkdir(parents=True)
            tmp_a = Path(raw) / "tmp-a"
            tmp_b = Path(raw) / "tmp-b"
            tmp_a.mkdir()
            tmp_b.mkdir()
            script = (
                "import sys; sys.path.insert(0, sys.argv[1]);"
                "from provider_catalog import lock_path;"
                "from pathlib import Path;"
                "print(lock_path(Path(sys.argv[2])))"
            )

            def resolved_path(tmp_dir: Path) -> str:
                env = {**os.environ, "TMPDIR": str(tmp_dir), "TMP": str(tmp_dir), "TEMP": str(tmp_dir), "PYTHONDONTWRITEBYTECODE": "1"}
                result = subprocess.run([PYTHON, "-c", script, str(ROOT / "scripts"), str(home)], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            path_a = resolved_path(tmp_a)
            path_b = resolved_path(tmp_b)
            self.assertEqual(path_a, path_b, "the writer lock must not depend on TMPDIR/TMP/TEMP")
            self.assertTrue(path_a.startswith(str(home.resolve())), "the writer lock must live under the resolved home")
            self.assertNotIn(str(tmp_a), path_a)
            self.assertNotIn(str(tmp_b), path_a)

    def test_shared_lock_contention_defers_across_different_tmpdir_envs(self):
        # Two genuinely separate subprocess environments -- distinct TMPDIR
        # values, no shared env dict -- must still serialize on one lock.
        # Aligning TMPDIR between the two sides would hide exactly the bug
        # this regression targets.
        with tempfile.TemporaryDirectory(prefix="fleet lock cross-env ") as raw:
            home = Path(raw) / "home"
            (home / ".claude").mkdir(parents=True)
            tmp_a = Path(raw) / "tmp-a"
            tmp_b = Path(raw) / "tmp-b"
            tmp_a.mkdir()
            tmp_b.mkdir()
            hold_script = (
                "import sys, time; sys.path.insert(0, sys.argv[1]);"
                "from provider_catalog import acquire_lock, release_lock;"
                "from pathlib import Path;"
                "fd, path = acquire_lock(Path(sys.argv[2]), timeout=2.0);"
                "print('ACQUIRED' if fd is not None else 'FAILED', flush=True);"
                "time.sleep(1.5);"
                "release_lock(fd, path)"
            )
            env_a = {**os.environ, "TMPDIR": str(tmp_a), "TMP": str(tmp_a), "TEMP": str(tmp_a), "PYTHONDONTWRITEBYTECODE": "1"}
            holder = subprocess.Popen([PYTHON, "-c", hold_script, str(ROOT / "scripts"), str(home)], env=env_a, text=True, stdout=subprocess.PIPE)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ACQUIRED")
                probe_script = (
                    "import sys; sys.path.insert(0, sys.argv[1]);"
                    "from provider_catalog import acquire_lock;"
                    "from pathlib import Path;"
                    "fd, path = acquire_lock(Path(sys.argv[2]), timeout=0.2);"
                    "print('ACQUIRED' if fd is not None else 'DEFERRED')"
                )
                env_b = {**os.environ, "TMPDIR": str(tmp_b), "TMP": str(tmp_b), "TEMP": str(tmp_b), "PYTHONDONTWRITEBYTECODE": "1"}
                probe = subprocess.run([PYTHON, "-c", probe_script, str(ROOT / "scripts"), str(home)], env=env_b, text=True, capture_output=True, timeout=5)
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertEqual(probe.stdout.strip(), "DEFERRED", "a second writer in a different TMPDIR must not also acquire the lock")
            finally:
                holder.wait(timeout=5)
                holder.stdout.close()


    def test_entry_points_never_write_bytecode_caches(self):
        """The installer's own bundle-inventory preflight rejects any unlisted
        directory, so a __pycache__ written next to the managed scripts makes
        install.py fail against its own source tree. Every other test in this
        file passes PYTHONDONTWRITEBYTECODE=1 to the child, which is exactly
        the environment variable a real user does not have set -- so this test
        deliberately omits it and asserts the in-process guards hold on their
        own, both in the bundle and in an installed home."""
        cache_roots = [ROOT / "scripts", ROOT / "bin", ROOT / "tests"]
        for stale in cache_roots:
            shutil.rmtree(stale / "__pycache__", ignore_errors=True)
        with tempfile.TemporaryDirectory(prefix="fleet bytecode ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            # No PYTHONDONTWRITEBYTECODE: this is the real-user environment.
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config)}
            install = subprocess.run(
                [PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            for source_dir in cache_roots:
                self.assertFalse(
                    (source_dir / "__pycache__").exists(),
                    f"{source_dir}/__pycache__ was created; install.py would fail its own bundle inventory preflight",
                )
            switch = subprocess.run(
                [PYTHON, str(home / ".claude/sync-model-context.py"), "--home", str(home), "--config-home", str(config)],
                cwd=ROOT, env=env, text=True, capture_output=True,
                input=json.dumps({"hook_event_name": "PostModelSwitch", "to_model": "codex-5.5"}),
            )
            self.assertEqual(switch.returncode, 0, switch.stderr)
            self.assertFalse(
                (home / ".claude/__pycache__").exists(),
                "the installed model-switch hook left a __pycache__ in the user's home",
            )
            setup = subprocess.run(
                [str(home / ".local/bin/claude-fleet-setup"), "--show"],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            self.assertFalse((home / ".claude/__pycache__").exists(), "claude-fleet-setup left a __pycache__ in the user's home")


    def test_fleet_sync_updates_manifest_fleet_hash_and_doctor_passes(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is required for fleet-sync generator")
        with tempfile.TemporaryDirectory(prefix="fleet sync test ") as raw:
            root = Path(raw)
            home = root / "home"
            config = root / "config"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1"}
            install = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)

            # Modify a lane in the installed config.json
            fleet_path = config / "delegate-skills/config.json"
            fleet_data = json.loads(fleet_path.read_text())
            fleet_data["lanes"]["implement"]["model"] = "claude-sonnet-5[1m]"
            fleet_path.write_text(json.dumps(fleet_data, indent=2) + "\n")
            new_fleet_hash = hashlib.sha256(fleet_path.read_bytes()).hexdigest()

            # Run installed claude-fleet-sync
            sync = subprocess.run([str(home / ".local/bin/claude-fleet-sync")], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(sync.returncode, 0, sync.stderr)

            # Verify claude-agents-doctor --check passes
            doctor = subprocess.run([str(home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)

            # Verify manifest.managed has the updated sha256 for config:delegate-fleet.json
            manifest = json.loads((home / ".claude/.claude-agents-config-manifest.json").read_text())
            self.assertEqual(manifest["fleetSha256"], new_fleet_hash)
            fleet_record = next(r for r in manifest["managed"] if r["id"] == "config:delegate-fleet.json")
            self.assertEqual(fleet_record["sha256"], new_fleet_hash)

    def test_resolve_fleet_bidirectional_auto_promotion_cycle(self):
        namespace = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
        policy = json.loads((ROOT / "config/provider-policy.json").read_text())
        fleet = {
            "version": "delegate-fleet.v1",
            "lanes": {
                "implement": {
                    "implementer": "claude",
                    "model": "codex-luna[1m]",
                    "effort": "xhigh",
                    "timeout": "2h",
                    "preferred": ["codex-luna[1m]", "claude-sonnet-5[1m]", "agy-gemini-flash[1m]"],
                    "fallbacks": ["claude-sonnet-5[1m]", "agy-gemini-flash[1m]"],
                },
            },
        }

        # Step 1: codex-luna is dropped from provider -> lane auto-downgrades to claude-sonnet-5
        rows_degraded = [
            {"id": "claude-sonnet-5", "context_length": 1000000},
            {"id": "agy-gemini-flash", "context_length": 1000000},
        ]
        resolved, picker, pending, catalog = namespace["resolve_fleet"](fleet, {}, rows_degraded, policy)
        lane = resolved["lanes"]["implement"]
        self.assertEqual(lane["model"], "claude-sonnet-5[1m]")
        self.assertEqual(lane["fallbacks"], ["agy-gemini-flash[1m]"])
        self.assertEqual(lane["preferred"], ["codex-luna[1m]", "claude-sonnet-5[1m]", "agy-gemini-flash[1m]"], "preferred hierarchy must be retained across dropouts")
        changes = resolved.get("_reconcile", {}).get("changes", {})
        self.assertEqual(changes.get("implement"), {"from": "codex-luna[1m]", "to": "claude-sonnet-5[1m]"})

        # Step 2: codex-luna is restored to provider -> lane auto-promotes back to codex-luna
        rows_restored = [
            {"id": "codex-luna", "context_length": 872000},
            {"id": "claude-sonnet-5", "context_length": 1000000},
            {"id": "agy-gemini-flash", "context_length": 1000000},
        ]
        promoted, picker2, pending2, catalog2 = namespace["resolve_fleet"](resolved, {}, rows_restored, policy)
        lane_promoted = promoted["lanes"]["implement"]
        self.assertEqual(lane_promoted["model"], "codex-luna[1m]", "lane must auto-promote to preferred model")
        self.assertEqual(lane_promoted["fallbacks"], ["claude-sonnet-5[1m]", "agy-gemini-flash[1m]"])
        self.assertEqual(lane_promoted["preferred"], ["codex-luna[1m]", "claude-sonnet-5[1m]", "agy-gemini-flash[1m]"])
        changes2 = promoted.get("_reconcile", {}).get("changes", {})
        self.assertEqual(changes2.get("implement"), {"from": "claude-sonnet-5[1m]", "to": "codex-luna[1m]"})

    def test_reconcile_home_seeds_canonical_preferred_hierarchy(self):
        namespace = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        exec(compile((ROOT / "scripts/fleet-reconcile.py").read_text(), "fleet-reconcile.py", "exec"), namespace)
        policy_path = ROOT / "config/provider-policy.json"
        with tempfile.TemporaryDirectory(prefix="fleet-seed-test-") as temp_dir:
            temp_root = Path(temp_dir)
            home = temp_root / "home"
            config_home = temp_root / "config"
            (home / ".claude").mkdir(parents=True, mode=0o700)
            (config_home / "delegate-skills").mkdir(parents=True, mode=0o755)

            # Degraded fleet without 'preferred' field
            stale_fleet = {
                "version": "delegate-fleet.v1",
                "lanes": {
                    "implement": {
                        "implementer": "claude",
                        "model": "claude-sonnet-5[1m]",
                        "effort": "xhigh",
                        "fallbacks": ["agy-gemini-flash[1m]"],
                    },
                    "review": {
                        "implementer": "claude",
                        "model": "codex-5.5",
                        "effort": "xhigh",
                        "readOnly": True,
                    },
                },
            }
            (config_home / "delegate-skills" / "config.json").write_text(json.dumps(stale_fleet, indent=2))
            (home / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-sonnet-5[1m]", "env": {}}, indent=2))

            rows = [
                {"id": "codex-luna", "context_length": 872000},
                {"id": "codex-sol", "context_length": 872000},
                {"id": "claude-sonnet-5", "context_length": 1000000},
                {"id": "agy-gemini-flash", "context_length": 1000000},
                {"id": "codex-5.5", "context_length": 272000},
            ]
            result = namespace["reconcile_home"](home, config_home, policy_path, rows, catalog_valid=True)
            self.assertEqual(result["status"], "applied")
            reconciled_fleet = json.loads((config_home / "delegate-skills" / "config.json").read_text())
            # implement must have promoted to codex-luna and retained preferred
            self.assertEqual(reconciled_fleet["lanes"]["implement"]["model"], "codex-luna[1m]")
            self.assertIn("preferred", reconciled_fleet["lanes"]["implement"])
            # review must have promoted to codex-sol and retained preferred
            self.assertEqual(reconciled_fleet["lanes"]["review"]["model"], "codex-sol[1m]")
            self.assertIn("preferred", reconciled_fleet["lanes"]["review"])

    def test_fleet_model_drift_auto_approves_when_policy_enabled(self):
        with tempfile.TemporaryDirectory(prefix="fleet-drift-test-") as temp_dir:
            temp_root = Path(temp_dir)
            home = temp_root / "home"
            config_home = temp_root / "config"
            (home / ".claude" / "cache").mkdir(parents=True, mode=0o700)
            (config_home / "delegate-skills").mkdir(parents=True, mode=0o755)

            policy = json.loads((ROOT / "config/provider-policy.json").read_text())
            policy["autoApproveProposals"] = True
            (config_home / "delegate-skills" / "provider-policy.json").write_text(json.dumps(policy, indent=2))

            fleet = {
                "version": "delegate-fleet.v1",
                "lanes": {
                    "implement": {
                        "implementer": "claude",
                        "model": "codex-luna[1m]",
                    },
                },
            }
            (config_home / "delegate-skills" / "config.json").write_text(json.dumps(fleet, indent=2))
            (home / ".claude" / "settings.json").write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://omniroute.example.com",
                    "ANTHROPIC_AUTH_TOKEN": "secret-test-token",
                }
            }, indent=2))

            cache = {
                "format": "claude-agents-config.provider-cache.v1",
                "provider": "omniroute",
                "endpoint": "https://omniroute.example.com",
                "account": hashlib.sha256(b"secret-test-token").hexdigest()[:24],
                "fetched_at": int(time.time()),
                "models": [
                    {"id": "codex-luna", "context_length": 872000},
                    {"id": "codex-sol", "context_length": 872000},
                ],
            }
            (home / ".claude" / "cache" / "omniroute-models-cache.json").write_text(json.dumps(cache, indent=2))

            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            drift_script = ROOT / "scripts/fleet-model-drift.py"
            proc = subprocess.run(
                [sys.executable, str(drift_script), "--home", str(home), "--config-home", str(config_home)],
                capture_output=True, text=True, env=env, check=False
            )
            self.assertEqual(proc.returncode, 0)
            proposal = json.loads((home / ".claude" / "fleet-model-proposal.json").read_text())
            self.assertEqual(proposal["decision"], "approved")
            self.assertTrue(proposal.get("auto_approved"))


class FleetSetupComprehensiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="fleet-setup-comp-")
        self.home = Path(self.temp_dir)
        self.claude_dir = self.home / ".claude"
        self.claude_dir.mkdir(parents=True)
        self.cache_dir = self.claude_dir / "cache"
        self.cache_dir.mkdir(parents=True)
        self.agents_dir = self.claude_dir / "agents"
        self.agents_dir.mkdir(parents=True)
        self.skills_dir = self.claude_dir / "skills"
        self.skills_dir.mkdir(parents=True)
        self.config_dir = self.home / ".config" / "delegate-skills"
        self.config_dir.mkdir(parents=True)

        self.catalog_data = {
            "format": "claude-agents-config.provider-cache.v1",
            "provider": "omniroute",
            "models": [
                {"id": "claude-opus-5", "display_name": "Opus 5", "context_length": 1000000},
                {"id": "claude-sonnet-5", "display_name": "Sonnet 5", "context_length": 1000000},
                {"id": "claude-haiku", "display_name": "Haiku", "context_length": 200000},
                {"id": "agy-gemini-flash", "display_name": "Gemini Flash", "context_length": 1000000},
                {"id": "codex-5.5", "display_name": "Codex 5.5", "context_length": 272000},
            ]
        }
        (self.cache_dir / "omniroute-models-cache.json").write_text(json.dumps(self.catalog_data, indent=2))

        self.settings_data = {
            "model": "agy-gemini-flash[1m]",
            "advisorModel": "codex-sol-max[1m]",
            "autoCompactWindow": 235929,
            "env": {
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "235929",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "dead-opus-model",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5[1m]",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku"
            },
            "hooks": {
                "PostToolUse": [
                    {
                        "hooks": [
                            {"type": "command", "command": f"{sys.executable} -c 'pass' # comment"}
                        ]
                    }
                ]
            }
        }
        (self.claude_dir / "settings.json").write_text(json.dumps(self.settings_data, indent=2))

        self.policy_data = {
            "format": "provider-policy.v1",
            "approvedFamilies": ["anthropic", "google", "openai"]
        }
        (self.config_dir / "provider-policy.json").write_text(json.dumps(self.policy_data, indent=2))

        self.fleet_data = {
            "format": "claude-agents-config.fleet.v1",
            "lanes": {
                "fleet-plan": {"model": "claude-opus-5[1m]"},
                "fleet-implement": {"model": "claude-sonnet-5[1m]"},
                "fleet-tests": {"model": "agy-gemini-flash[1m]"},
            }
        }
        (self.claude_dir / "fleet.json").write_text(json.dumps(self.fleet_data, indent=2))

        (self.agents_dir / "fleet-plan.md").write_text("# fleet plan agent\n")
        (self.agents_dir / "fleet-implement.md").write_text("# fleet implement agent\n")
        (self.agents_dir / "engineering-database-optimizer.md").write_text("# db optimizer\n")
        (self.agents_dir / "testing-accessibility-auditor.md").write_text("# a11y auditor\n")
        (self.agents_dir / "unity-architect.md").write_text("# unity architect\n")
        (self.agents_dir / "unreal-systems-engineer.md").write_text("# unreal engineer\n")
        (self.agents_dir / "engineering-wechat-mini-program-developer.md").write_text("# wechat dev\n")
        (self.agents_dir / "design-ux-architect.md").write_text("# ux architect\n")

        (self.skills_dir / "fleet-setup").mkdir()
        (self.skills_dir / "code-review").mkdir()
        (self.skills_dir / "cline-delegate").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_fleet_setup(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(ROOT / "bin" / "claude-fleet-setup"), "--home", str(self.home), *args]
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    def test_audit_json_output(self) -> None:
        proc = self.run_fleet_setup("--audit", "--json")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        data = json.loads(proc.stdout)

        self.assertEqual(data.get("format"), "claude-fleet-audit.v1")
        self.assertEqual(data["live_models"]["total"], 5)

        stale = data["settings"]["stale_models"]
        self.assertIn("advisorModel", stale)
        self.assertEqual(stale["advisorModel"]["current"], "codex-sol-max[1m]")
        self.assertIn("env.ANTHROPIC_DEFAULT_OPUS_MODEL", stale)
        self.assertEqual(stale["env.ANTHROPIC_DEFAULT_OPUS_MODEL"]["current"], "dead-opus-model")

        self.assertFalse(data["settings"]["compaction"]["optimal"])
        self.assertEqual(data["settings"]["compaction"]["current"], "235929")

        self.assertTrue(data["hooks"]["all_valid"])

        agents_audit = data["agents"]
        self.assertEqual(agents_audit["fleet_active_lanes"], 2)
        self.assertEqual(agents_audit["non_fleet_active_count"], 6)
        cats = agents_audit["non_fleet_by_category"]
        self.assertEqual(len(cats["engineering"]), 2)
        self.assertEqual(len(cats["game_dev"]), 2)
        self.assertEqual(len(cats["niche_ops"]), 1)
        self.assertEqual(len(cats["other"]), 1)

    def test_audit_text_summary(self) -> None:
        proc = self.run_fleet_setup("--audit")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        self.assertIn("Claude Fleet & Context Audit", proc.stdout)
        self.assertIn("codex-sol-max[1m]", proc.stdout)
        self.assertIn("Engineering & QA tools: 2 active", proc.stdout)
        self.assertIn("Game Dev & 3D: 2 active", proc.stdout)

    def test_fix_settings_auto(self) -> None:
        proc = self.run_fleet_setup("--fix-settings")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        self.assertIn("advisorModel: replaced stale 'codex-sol-max[1m]' with live 'claude-opus-5[1m]'", proc.stdout)
        self.assertIn("env.ANTHROPIC_DEFAULT_OPUS_MODEL: replaced stale 'dead-opus-model' with live 'claude-opus-5[1m]'", proc.stdout)
        self.assertIn("235929 -> 800000", proc.stdout)

        updated = json.loads((self.claude_dir / "settings.json").read_text())
        self.assertEqual(updated["advisorModel"], "claude-opus-5[1m]")
        self.assertEqual(updated["autoCompactWindow"], 800000)
        self.assertEqual(updated["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "800000")
        self.assertEqual(updated["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-5[1m]")

    def test_fix_settings_advisor_override(self) -> None:
        proc = self.run_fleet_setup("--fix-settings", "--advisor", "codex-5.5")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        updated = json.loads((self.claude_dir / "settings.json").read_text())
        self.assertEqual(updated["advisorModel"], "codex-5.5")

    def test_archive_and_restore_agents_by_category(self) -> None:
        proc = self.run_fleet_setup("--archive-agents", "game_dev,niche_ops")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        self.assertIn("Archived 3 agents", proc.stdout)

        archived = sorted([f.name for f in (self.claude_dir / "agents-archived").glob("*.md")])
        self.assertEqual(archived, [
            "engineering-wechat-mini-program-developer.md",
            "unity-architect.md",
            "unreal-systems-engineer.md"
        ])

        active = sorted([f.name for f in self.agents_dir.glob("*.md")])
        self.assertEqual(active, [
            "design-ux-architect.md",
            "engineering-database-optimizer.md",
            "fleet-implement.md",
            "fleet-plan.md",
            "testing-accessibility-auditor.md"
        ])

        proc_restore = self.run_fleet_setup("--restore-agents", "game_dev")
        self.assertEqual(proc_restore.returncode, 0, f"Error: {proc_restore.stderr}")
        self.assertIn("Restored 2 agents", proc_restore.stdout)

        active_after = sorted([f.name for f in self.agents_dir.glob("*.md")])
        self.assertIn("unity-architect.md", active_after)
        self.assertIn("unreal-systems-engineer.md", active_after)
        self.assertNotIn("engineering-wechat-mini-program-developer.md", active_after)

    def test_archive_agents_all_preserves_fleet(self) -> None:
        proc = self.run_fleet_setup("--archive-agents", "all")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")

        active = sorted([f.name for f in self.agents_dir.glob("*.md")])
        self.assertEqual(active, ["fleet-implement.md", "fleet-plan.md"])

    def test_archive_and_restore_skills(self) -> None:
        proc = self.run_fleet_setup("--archive-skills", "delegates")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        self.assertIn("Archived 1 skills", proc.stdout)

        archived = [s.name for s in (self.claude_dir / "skills-archived").iterdir()]
        self.assertEqual(archived, ["cline-delegate"])

        active = sorted([s.name for s in self.skills_dir.iterdir()])
        self.assertEqual(active, ["code-review", "fleet-setup"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
