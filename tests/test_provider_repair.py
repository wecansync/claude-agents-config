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

    def setup(self):
        super().setup()
        type(self).requests += 1

    def do_GET(self):  # noqa: N802
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
            uninstall = subprocess.run([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(home), "--config-home", str(config)], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)

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
            # The writer lock lives under tmpdir(); node's os.tmpdir() falls
            # back to "/tmp" when TMPDIR is absent, so the child must see the
            # same tmpdir this test uses below to plant the phase-3 lock.
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": tempfile.gettempdir()}
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

            # --- Phase 3: a live (recently created) canonical writer lock
            # must defer the generator without mutating anything, and must
            # not be evicted merely for being contended.
            digest = hashlib.sha256(str(home.resolve()).encode()).hexdigest()
            lock_path = Path(tempfile.gettempdir()) / f"claude-agents-config-{digest}.lock"
            lock_path.write_text("999999999")
            try:
                before = agent_file.read_text()
                deferred = run_sync()
                self.assertNotEqual(deferred.returncode, 0)
                self.assertEqual(agent_file.read_text(), before, "a live lock must defer without mutating any target")
                self.assertTrue(lock_path.exists(), "a live lock must not be evicted")
            finally:
                lock_path.unlink(missing_ok=True)

            # The generator must still work normally once the live lock clears.
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
