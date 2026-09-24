#!/usr/bin/env python3
"""Provider profiles: add, switch, and remove gateways without reinstalling,
with each profile's provider policy, exclusions, and tier labels."""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "scripts"))
import provider_catalog  # noqa: E402

A_IDS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku", "codex-5.5"]
B_IDS = ["openai/gpt-5", "anthropic/claude-sonnet-4.5", "deepseek/deepseek-chat"]
C_IDS = ["zen-big", "zen-small"]
TOKENS = {"a": "token-aaaa-1111", "b": "token-bbbb-2222", "c": "token-cccc-3333"}
# An OmniRoute-style policy: named prefix families, nothing namespaced.
OMNI_FAMILIES = [
    {"name": "claude", "prefixes": ["claude-"], "approved": True, "fallbackFamilies": []},
    {"name": "codex", "prefixes": ["codex-"], "approved": True, "fallbackFamilies": []},
]


def gateway(ids: list[str], token: str):
    """A fake gateway with its own state (one class per server)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        mode = "ok"

        def do_GET(self):  # noqa: N802
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return
            if type(self).mode == "401":
                self.send_response(401)
                self.end_headers()
                return
            if type(self).mode == "empty":
                body = b'{"data": []}'
            elif type(self).mode == "malformed":
                body = b"not json"
            else:
                body = json.dumps({"data": [{"id": i, "context_length": 1000000} for i in ids]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Handler, f"http://127.0.0.1:{server.server_address[1]}"


@unittest.skipIf(shutil.which("node") is None, "Node.js is required for the agent generator")
class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = {}
        for key, ids in (("a", A_IDS), ("b", B_IDS), ("c", C_IDS)):
            cls.servers[key] = gateway(ids, TOKENS[key])

    @classmethod
    def tearDownClass(cls):
        for server, _handler, _url in cls.servers.values():
            server.shutdown()
            server.server_close()

    def url(self, key: str) -> str:
        return self.servers[key][2]

    def setUp(self):
        for _server, handler, _url in self.servers.values():
            handler.mode = "ok"
        self.raw = tempfile.TemporaryDirectory(prefix="agentfleet profiles ")
        self.addCleanup(self.raw.cleanup)
        self.home, self.config = Path(self.raw.name) / "home", Path(self.raw.name) / "config"
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
                    "PYTHONDONTWRITEBYTECODE": "1", "AGENTFLEET_NONINTERACTIVE": "1",
                    **{f"TOK_{key.upper()}": value for key, value in TOKENS.items()}}
        install = self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--provider", "gateway", "--gateway-url", self.url("a"),
                                "--allow-insecure-http", "--gateway-token-env", "TOK_A", "--home", str(self.home), "--config-home", str(self.config)])
        self.assertEqual(install.returncode, 0, install.stderr + install.stdout)
        policy = self.read(self.policy_path)
        policy["provider"] = "omniroute"
        policy["families"] = OMNI_FAMILIES
        self.policy_path.write_text(json.dumps(policy, indent=2) + "\n")
        self.omni_policy = self.read(self.policy_path)

    # -- helpers ----------------------------------------------------------------
    @property
    def policy_path(self) -> Path:
        return self.config / "delegate-skills/provider-policy.json"

    @staticmethod
    def read(path: Path) -> dict:
        return json.loads(path.read_text())

    def run_cmd(self, command, **env):
        return subprocess.run(command, cwd=ROOT, env={**self.env, **env}, text=True, capture_output=True, stdin=subprocess.DEVNULL)

    def af(self, *args, **env):
        return self.run_cmd([str(self.home / ".local/bin/agentfleet"), *args], **env)

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def fails(self, result, text: str):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stderr + result.stdout)

    def add(self, name, key, *extra):
        return self.af("add", name, "--gateway-url", self.url(key), "--allow-insecure-http", "--token-env", f"TOK_{key.upper()}", *extra)

    def settings_env(self) -> dict:
        return self.read(self.home / ".claude/settings.json")["env"]

    def lanes(self) -> dict:
        return self.read(self.home / ".claude/fleet.json")["lanes"]

    def gates(self):
        doctor = self.run_cmd([str(self.home / ".local/bin/claude-agents-doctor"), "--check", "--home", str(self.home), "--config-home", str(self.config)])
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        sync = self.run_cmd([str(self.home / ".local/bin/claude-fleet-sync"), "--check"])
        self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
        self.assertEqual(self.read(self.home / ".claude/fleet.json"), self.read(self.config / "delegate-skills/config.json"), "both fleet mirrors match")

    def live_files(self) -> dict:
        return {path: path.read_bytes() for path in (self.home / ".claude/settings.json", self.home / ".claude/fleet.json", self.policy_path)}

    def previous_name(self) -> str:
        listing = self.ok(self.af("profiles")).stdout
        for line in listing.splitlines():
            if self.url("a") in line:
                parts = line.split()
                return parts[1] if parts[0] == "*" else parts[0]
        self.fail(f"no profile for gateway a in:\n{listing}")

    # -- add ----------------------------------------------------------------------
    def test_add_saves_a_profile_without_touching_the_live_setup(self):
        before = self.live_files()
        result = self.ok(self.add("b", "b"))
        self.assertIn("3 models.", result.stdout)
        self.assertIn("Switch with: agentfleet use b", result.stdout)
        profile_path = self.home / ".claude/agentfleet/profiles/b.json"
        token_path = self.home / ".claude/agentfleet/secrets/b.token"
        self.assertEqual(stat.S_IMODE(profile_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(profile_path.parent.stat().st_mode), 0o700)
        profile = self.read(profile_path)
        self.assertEqual(profile["policy"]["provider"], "b")
        self.assertEqual([f["prefixes"] for f in profile["policy"]["families"]], [[""]])
        self.assertEqual(profile["excludedModels"], [])
        self.assertEqual({lane["model"] for lane in profile["lanes"].values()} - set(B_IDS), set(), "every lane uses one of b's models")
        self.assertEqual(self.live_files(), before, "adding without --use changes nothing live")
        self.assertNotIn(TOKENS["b"], result.stdout + result.stderr + profile_path.read_text())

    def test_add_use_switches_and_switching_back_restores_everything(self):
        self.ok(self.run_cmd([str(self.home / ".local/bin/claude-fleet-setup"), "--exclude", "codex-5.5"]))
        self.ok(self.run_cmd([str(self.home / ".local/bin/claude-fleet-setup"), "--prefer", "plan=claude-sonnet-5[1m]"]))
        a_lanes = self.lanes()
        result = self.ok(self.add("b", "b", "--use"))
        self.assertIn("Switched to 'b'", result.stdout)
        env = self.settings_env()
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"], env["ANTHROPIC_API_KEY"]), (self.url("b"), TOKENS["b"], ""))
        self.assertEqual(self.read(self.policy_path), self.read(self.home / ".claude/agentfleet/profiles/b.json")["policy"])
        fleet = self.read(self.home / ".claude/fleet.json")
        self.assertNotIn("excludedModels", fleet, "a's exclusions do not follow to b")
        self.assertNotIn("preferred", fleet["lanes"]["plan"], "a's pins do not follow to b")
        self.assertEqual(self.read(self.home / ".claude/cache/provider-models-cache.json")["endpoint"], provider_catalog.endpoint_scope(self.url("b")))
        self.gates()
        self.ok(self.af("use", self.previous_name()))
        self.assertEqual(self.read(self.policy_path), self.omni_policy, "a's own policy comes back")
        fleet = self.read(self.home / ".claude/fleet.json")
        self.assertEqual(fleet.get("excludedModels"), ["codex-5.5"])
        self.assertEqual(fleet["lanes"]["plan"].get("preferred"), a_lanes["plan"].get("preferred"))
        self.assertEqual(self.settings_env()["ANTHROPIC_AUTH_TOKEN"], TOKENS["a"])
        self.gates()

    def test_round_trip_through_three_gateways_and_native(self):
        a = self.previous_name()
        self.ok(self.add("b", "b"))
        self.ok(self.add("c", "c"))
        for name, url, ids in (("b", self.url("b"), B_IDS), ("c", self.url("c"), C_IDS), ("native", None, ["opus", "sonnet", "haiku"]), (a, self.url("a"), None), ("b", self.url("b"), B_IDS)):
            self.ok(self.af("use", name))
            env = self.settings_env()
            self.assertEqual(env.get("ANTHROPIC_BASE_URL"), url, name)
            if ids:
                models = {provider_catalog.strip_known_suffix(lane["model"]) for lane in self.lanes().values()}
                self.assertEqual(models - set(ids), set(), name)
            if name == "native":
                self.assertNotIn("ANTHROPIC_API_KEY", env, "switching to the Claude login removes the blanked key")
            self.gates()

    def test_reconcile_uses_the_active_profiles_policy(self):
        self.ok(self.add("b", "b", "--use"))
        policy_args = ["--home", str(self.home), "--config-home", str(self.config), "--policy", str(self.policy_path),
                       "--catalog", str(self.home / ".claude/cache/provider-models-cache.json")]
        run = self.ok(self.run_cmd([PYTHON, str(self.home / ".claude/fleet-reconcile.py"), *policy_args]))
        self.assertNotIn("no live approved candidate", run.stdout + run.stderr)
        # Control: with a's policy in place the same catalog cannot be served.
        self.policy_path.write_text(json.dumps(self.omni_policy, indent=2) + "\n")
        control = self.run_cmd([PYTHON, str(self.home / ".claude/fleet-reconcile.py"), *policy_args])
        report = control.stdout + control.stderr
        self.assertTrue("no live approved candidate" in report or "unapproved model family" in report, report)

    def test_audit_never_shows_another_endpoints_cache(self):
        self.ok(self.af("save", "a"))
        self.ok(self.add("b", "b"))
        self.ok(self.af("use", "b"))
        audit = json.loads(self.ok(self.run_cmd([str(self.home / ".local/bin/claude-fleet-setup"), "--audit", "--json"])).stdout)
        self.assertEqual(audit["live_models"]["total"], 0, "a's cached models must not be shown for b")

    # -- refusals --------------------------------------------------------------------
    def test_collisions_and_force(self):
        self.ok(self.add("b", "b"))
        before = (self.home / ".claude/agentfleet/profiles/b.json").read_bytes()
        self.fails(self.add("b", "c"), "already exists")
        self.assertEqual((self.home / ".claude/agentfleet/profiles/b.json").read_bytes(), before)
        self.ok(self.add("b", "c", "--force"))
        self.assertEqual((self.home / ".claude/agentfleet/secrets/b.token").read_text(), TOKENS["c"])
        self.ok(self.af("use", "b"))
        self.fails(self.add("b", "b"), "is active")
        self.ok(self.add("b", "b", "--force"))
        self.assertEqual(self.settings_env()["ANTHROPIC_BASE_URL"], self.url("b"), "replacing the active profile re-applies it")

    def test_names_urls_tokens_and_discovery_are_checked_before_writing(self):
        store = self.home / ".claude/agentfleet/profiles"
        for args, text in (
            (("add", "native", "--gateway-url", self.url("b"), "--allow-insecure-http", "--token-env", "TOK_B"), "reserved"),
            (("add", "con", "--gateway-url", self.url("b"), "--allow-insecure-http", "--token-env", "TOK_B"), "device name"),
            (("add", "x", "--gateway-url", "http://example.com", "--token-env", "TOK_B"), "allowed only for loopback"),
            (("add", "x", "--gateway-url", self.url("b"), "--token-env", "TOK_B"), "--allow-insecure-http"),
            (("add", "x", "--gateway-url", "https://u:p@example.com", "--token-env", "TOK_B"), "without embedded credentials"),
            (("add", "x", "--gateway-url", self.url("b"), "--allow-insecure-http", "--token-env", "UNSET_VAR"), "not set"),
            (("add", "x", "--gateway-url", self.url("b"), "--allow-insecure-http"), "no token"),
            (("add", "x", "--gateway-url", self.url("b"), "--allow-insecure-http", "--token-env", "TOK_A"), "HTTP 401 (check the token)"),
            (("add", "x", "--gateway-url", "http://127.0.0.1:9", "--allow-insecure-http", "--token-env", "TOK_B"), "could not list models"),
            (("remove", "native"), "built in"),
        ):
            self.fails(self.af(*args), text)
        self.fails(self.run_cmd([str(self.home / ".local/bin/agentfleet"), "add", "x", "--gateway-url", self.url("b"), "--allow-insecure-http", "--token-env", "NL"], NL="abc\ndef"), "line break")
        for mode, text in (("empty", "returned no models"), ("malformed", "could not list models")):
            self.servers["b"][1].mode = mode
            self.fails(self.add("x", "b"), text)
        self.assertFalse(list(store.glob("x.json")) if store.is_dir() else [], "nothing was saved")

    def test_remove(self):
        self.ok(self.add("b", "b"))
        self.ok(self.add("c", "c"))
        self.fails(self.af("remove", "b"), "pass --yes")
        self.ok(self.af("remove", "b", "--yes"))
        self.assertFalse((self.home / ".claude/agentfleet/profiles/b.json").exists())
        self.assertFalse((self.home / ".claude/agentfleet/secrets/b.token").exists())
        self.assertTrue((self.home / ".claude/agentfleet/profiles/c.json").exists())
        self.fails(self.af("remove", "b", "--yes"), "no saved profile")
        self.ok(self.af("use", "c"))
        self.fails(self.af("remove", "c", "--yes"), "is the active profile")
        self.gates()

    # -- reinstalling with another gateway -----------------------------------------------
    def install(self, key, *extra):
        return self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--provider", "gateway", "--gateway-url", self.url(key), "--allow-insecure-http",
                             "--gateway-token-env", f"TOK_{key.upper()}", "--home", str(self.home), "--config-home", str(self.config), *extra])

    def test_reinstall_to_another_port_uses_the_generic_policy(self):
        # b runs on the same host as a; only the port differs.
        self.ok(self.run_cmd([str(self.home / ".local/bin/claude-fleet-setup"), "--exclude", "codex-5.5"]))
        self.ok(self.run_cmd([str(self.home / ".local/bin/claude-fleet-setup"), "--prefer", "plan=claude-sonnet-5[1m]"]))
        result = self.ok(self.install("b"))
        self.assertIn(f"Gateway changed ({self.url('a')} -> {self.url('b')})", result.stdout)
        self.assertEqual(self.read(self.policy_path), self.read(ROOT / "config/provider-policy.json"), "a's families are not carried to b")
        fleet = self.read(self.home / ".claude/fleet.json")
        self.assertNotIn("excludedModels", fleet)
        self.assertNotIn("preferred", fleet["lanes"]["plan"])
        models = {provider_catalog.strip_known_suffix(lane["model"]) for lane in fleet["lanes"].values()}
        self.assertEqual(models - set(B_IDS), set(), "every lane uses one of b's models")
        self.assertEqual(self.settings_env()["ANTHROPIC_API_KEY"], "")
        self.gates()

    def test_reinstall_to_the_same_endpoint_keeps_the_policy_and_choices(self):
        self.ok(self.run_cmd([str(self.home / ".local/bin/claude-fleet-setup"), "--exclude", "codex-5.5"]))
        result = self.ok(self.install("a"))
        self.assertNotIn("Gateway changed", result.stdout)
        self.assertEqual(self.read(self.policy_path), self.omni_policy)
        self.assertEqual(self.read(self.home / ".claude/fleet.json").get("excludedModels"), ["codex-5.5"])
        self.gates()

    def test_reinstall_uses_the_saved_profiles_policy_and_choices(self):
        self.ok(self.add("b", "b"))
        path = self.home / ".claude/agentfleet/profiles/b.json"
        profile = self.read(path)
        profile["excludedModels"] = ["deepseek/deepseek-chat"]
        profile["lanes"]["plan"]["preferred"] = ["anthropic/claude-sonnet-4.5"]
        path.write_text(json.dumps(profile))
        saved = path.read_bytes()
        result = self.ok(self.install("b"))
        self.assertIn("agentfleet profile 'b'", result.stdout)
        self.assertNotIn("Gateway changed", result.stdout)
        self.assertEqual(self.read(self.policy_path), profile["policy"])
        fleet = self.read(self.home / ".claude/fleet.json")
        self.assertEqual(fleet.get("excludedModels"), ["deepseek/deepseek-chat"])
        self.assertEqual(fleet["lanes"]["plan"].get("preferred"), ["anthropic/claude-sonnet-4.5"])
        self.assertNotIn("deepseek/deepseek-chat", {provider_catalog.strip_known_suffix(lane["model"]) for lane in fleet["lanes"].values()})
        self.assertEqual(path.read_bytes(), saved, "the installer never writes profiles")
        self.gates()

    def test_reinstall_with_a_legacy_saved_profile_keeps_the_policy(self):
        self.ok(self.add("b", "b"))
        path = self.home / ".claude/agentfleet/profiles/b.json"
        profile = self.read(path)
        del profile["policy"]
        path.write_text(json.dumps(profile))
        result = self.install("b")
        self.assertNotIn("Gateway changed", result.stdout)
        self.assertEqual(self.read(self.policy_path), self.omni_policy)

    def test_gateway_installs_blank_the_api_key_and_uninstall_removes_it(self):
        journal = self.read(self.home / ".claude/.claude-agents-config-install.json")
        self.assertEqual(self.settings_env()["ANTHROPIC_API_KEY"], "")
        self.assertIn("value:env:ANTHROPIC_API_KEY", json.dumps(journal))
        self.ok(self.install("a"))
        self.assertEqual(self.settings_env()["ANTHROPIC_API_KEY"], "")
        uninstall = self.ok(self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(self.home), "--config-home", str(self.config)]))
        settings_path = self.home / ".claude/settings.json"
        env = self.read(settings_path).get("env", {}) if settings_path.is_file() else {}
        self.assertNotIn("ANTHROPIC_API_KEY", env, uninstall.stdout)

    def test_reinstall_keeps_a_user_api_key(self):
        settings_path = self.home / ".claude/settings.json"
        settings = self.read(settings_path)
        settings["env"]["ANTHROPIC_API_KEY"] = "user-owned-key-value"
        settings_path.write_text(json.dumps(settings))
        result = self.ok(self.install("a"))
        self.assertIn("may send it instead of the gateway token", result.stderr)
        self.assertEqual(self.settings_env()["ANTHROPIC_API_KEY"], "user-owned-key-value")
        self.ok(self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(self.home), "--config-home", str(self.config)]))
        self.assertEqual(self.read(settings_path)["env"].get("ANTHROPIC_API_KEY"), "user-owned-key-value", "uninstall keeps the user's key")

    # -- compatibility and safety ------------------------------------------------------
    def test_legacy_profile_keeps_the_current_policy(self):
        self.ok(self.af("save", "a"))
        self.ok(self.add("b", "b"))
        path = self.home / ".claude/agentfleet/profiles/b.json"
        profile = self.read(path)
        del profile["policy"], profile["excludedModels"]
        path.write_text(json.dumps(profile))
        before = self.policy_path.read_bytes()
        result = self.ok(self.af("use", "b"))
        self.assertIn("saved by an older version without a provider policy", result.stdout)
        self.assertEqual(self.policy_path.read_bytes(), before)
        self.ok(self.af("use", "a"))
        self.assertIn("policy", self.read(path), "switching away saves it with a policy")

    def test_lock_contention_changes_nothing(self):
        self.ok(self.add("b", "b"))
        before = self.live_files()
        fd, lock = provider_catalog.acquire_lock(self.home, timeout=2.0)
        self.assertIsNotNone(fd)
        try:
            self.fails(self.af("--lock-timeout", "0.5", "use", "b"), "busy")
        finally:
            provider_catalog.release_lock(fd, lock)
        self.assertEqual(self.live_files(), before)
        self.ok(self.af("use", "b"))

    def test_force_reapplies_the_saved_copy(self):
        self.ok(self.add("b", "b", "--use"))
        saved = self.read(self.home / ".claude/agentfleet/profiles/b.json")
        self.ok(self.run_cmd([str(self.home / ".local/bin/claude-fleet-setup"), "--exclude", "deepseek/deepseek-chat"]))
        self.ok(self.af("use", "b", "--force"))
        self.assertEqual(self.read(self.home / ".claude/agentfleet/profiles/b.json"), saved, "the saved copy is not overwritten first")
        self.assertNotIn("excludedModels", self.read(self.home / ".claude/fleet.json"), "the saved copy is what gets applied")

    def test_active_marker_follows_the_live_gateway(self):
        self.ok(self.af("save", "omni"))
        install = self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--provider", "gateway", "--gateway-url", self.url("b"),
                                "--allow-insecure-http", "--gateway-token-env", "TOK_B", "--home", str(self.home), "--config-home", str(self.config)])
        self.assertEqual(install.returncode, 0, install.stderr + install.stdout)
        self.ok(self.af("use", "native"))
        omni = self.read(self.home / ".claude/agentfleet/profiles/omni.json")
        self.assertEqual(omni["env"]["ANTHROPIC_BASE_URL"], self.url("a"), "the reinstall to b did not overwrite 'omni'")
        self.assertEqual((self.home / ".claude/agentfleet/secrets/omni.token").read_text(), TOKENS["a"])

    def test_user_owned_api_key_is_kept(self):
        settings_path = self.home / ".claude/settings.json"
        settings = self.read(settings_path)
        settings["env"]["ANTHROPIC_API_KEY"] = "user-owned-key-value"
        settings_path.write_text(json.dumps(settings))
        result = self.ok(self.add("b", "b", "--use"))
        self.assertIn("may send it instead of the gateway token", result.stderr)
        self.assertEqual(self.settings_env()["ANTHROPIC_API_KEY"], "user-owned-key-value")

    def test_generic_policy_matches_the_shipped_file(self):
        shipped = json.loads((ROOT / "config/provider-policy.json").read_text())
        self.assertEqual(provider_catalog.generic_policy(shipped["provider"]), shipped)

    def test_tokens_never_leak_into_other_files(self):
        self.ok(self.add("b", "b", "--use"))
        self.ok(self.add("c", "c"))
        self.ok(self.af("use", "native"))
        allowed = {self.home / ".claude/settings.json"}
        for root in (self.home, self.config):
            for path in root.rglob("*"):
                if not path.is_file() or path in allowed or path.suffix == ".token" or "backups" in path.parts:
                    continue
                data = path.read_bytes()
                for key, token in TOKENS.items():
                    self.assertNotIn(token.encode(), data, f"{key} token found in {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
