#!/usr/bin/env python3
"""Provider profiles: add, switch, and remove gateways without reinstalling,
with each profile's provider policy, exclusions, and tier labels."""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import runpy
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
        redirect_to = ""
        seen: list = []

        def do_GET(self):  # noqa: N802
            type(self).seen.append(self.headers.get("Authorization"))
            if type(self).mode == "redirect":
                self.send_response(302)
                self.send_header("Location", type(self).redirect_to + self.path)
                self.end_headers()
                return
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
            handler.seen = []
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

    def add(self, name, key, *extra, **env):
        return self.af("add", name, "--gateway-url", self.url(key), "--allow-insecure-http", "--token-env", f"TOK_{key.upper()}", *extra, **env)

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
    def update(self):
        return self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--home", str(self.home), "--config-home", str(self.config)])

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
        result = self.ok(self.install("b"))
        self.assertNotIn("Gateway changed", result.stdout)
        self.assertEqual(self.read(self.policy_path), self.omni_policy)

    def test_a_saved_policy_wins_over_an_older_legacy_profile(self):
        self.ok(self.add("b", "b"))
        profile = self.read(self.home / ".claude/agentfleet/profiles/b.json")
        legacy = {key: value for key, value in profile.items() if key != "policy"}
        legacy.update(name="aaa-old", savedAt="2020-01-01T00:00:00Z")
        (self.home / ".claude/agentfleet/profiles/aaa-old.json").write_text(json.dumps(legacy))
        result = self.ok(self.install("b"))
        self.assertIn("agentfleet profile 'b'", result.stdout)
        self.assertEqual(self.read(self.policy_path), profile["policy"])

    def test_reinstall_after_an_agentfleet_switch_keeps_the_live_gateway(self):
        self.ok(self.add("b", "b", "--use"))
        b_policy = self.read(self.home / ".claude/agentfleet/profiles/b.json")["policy"]
        # Re-running the original install command (gateway a) must not revert
        # the switch, nor mix a's policy or lanes into b's live setup.
        result = self.ok(self.install("a"))
        self.assertIn("Keeping the live gateway", result.stdout)
        env = self.settings_env()
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]), (self.url("b"), TOKENS["b"]))
        self.assertEqual(self.read(self.policy_path), b_policy)
        self.assertEqual({provider_catalog.strip_known_suffix(lane["model"]) for lane in self.lanes().values()} - set(B_IDS), set())
        self.gates()
        self.ok(self.af("use", "native"))
        saved = self.read(self.home / ".claude/agentfleet/profiles/b.json")
        self.assertEqual((saved["env"]["ANTHROPIC_BASE_URL"], saved["policy"]), (self.url("b"), b_policy), "b's profile keeps b's setup")

    def test_dry_run_never_sends_the_live_token_to_another_gateway(self):
        dry = self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--dry-run", "--provider", "gateway", "--gateway-url", self.url("c"),
                            "--allow-insecure-http", "--home", str(self.home), "--config-home", str(self.config)])
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        self.assertNotIn(f"Bearer {TOKENS['a']}", self.servers["c"][1].seen)

    def test_update_keeps_discovery_after_a_native_install_switched_to_a_gateway(self):
        self.ok(self.af("use", "native"))
        self.ok(self.update())
        self.ok(self.add("c", "c", "--use"))
        self.assertEqual(self.settings_env()["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.ok(self.update())
        self.assertEqual(self.settings_env()["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.gates()

    def test_lan_gateways_are_told_apart(self):
        # Plain-HTTP LAN URLs are not valid catalog endpoints; two of them
        # still count as different gateways, and the installer copes.
        self.ok(self.af("save", "lan1"))
        path = self.home / ".claude/agentfleet/profiles/lan1.json"
        profile = self.read(path)
        profile["env"]["ANTHROPIC_BASE_URL"] = "http://192.168.1.10:4000"
        path.write_text(json.dumps(profile))
        (self.home / ".claude/agentfleet/active").write_text("lan1\n")
        settings_path = self.home / ".claude/settings.json"
        settings = self.read(settings_path)
        settings["env"]["ANTHROPIC_BASE_URL"] = "http://192.168.1.11:4000"
        settings_path.write_text(json.dumps(settings))
        self.ok(self.af("use", "native"))
        self.assertEqual(self.read(path)["env"]["ANTHROPIC_BASE_URL"], "http://192.168.1.10:4000", "lan1 was not overwritten")
        self.ok(self.install("b"))

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

    def test_uninstall_removes_the_api_key_entry_when_env_existed_before(self):
        # A fresh home whose settings already have an env map, so the
        # installer journals the key itself rather than the whole env.
        home, config = Path(self.raw.name) / "home2", Path(self.raw.name) / "config2"
        (home / ".claude").mkdir(parents=True)
        settings_path = home / ".claude/settings.json"
        settings_path.write_text(json.dumps({"env": {"USER_SETTING": "1"}}))
        paths = ["--home", str(home), "--config-home", str(config)]
        self.ok(self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--provider", "gateway", "--gateway-url", self.url("a"), "--allow-insecure-http",
                              "--gateway-token-env", "TOK_A", *paths], HOME=str(home), XDG_CONFIG_HOME=str(config)))
        self.assertEqual(self.read(settings_path)["env"]["ANTHROPIC_API_KEY"], "")
        self.ok(self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", *paths], HOME=str(home), XDG_CONFIG_HOME=str(config)))
        env = self.read(settings_path)["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env, "the per-key entry removes the mask")
        self.assertEqual(env.get("USER_SETTING"), "1")

    def test_reinstall_keeps_a_user_api_key(self):
        settings_path = self.home / ".claude/settings.json"
        settings = self.read(settings_path)
        settings["env"]["ANTHROPIC_API_KEY"] = "user-owned-key-value"
        settings_path.write_text(json.dumps(settings))
        result = self.ok(self.install("a"))
        self.assertIn("may send it to the gateway instead of the gateway token", result.stderr)
        self.assertEqual(self.settings_env()["ANTHROPIC_API_KEY"], "user-owned-key-value")
        self.ok(self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(self.home), "--config-home", str(self.config)]))
        self.assertEqual(self.read(settings_path)["env"].get("ANTHROPIC_API_KEY"), "user-owned-key-value", "uninstall keeps the user's key")

    def test_uninstall_keeps_the_api_key_mask_while_a_gateway_remains(self):
        self.ok(self.add("b", "b", "--use"))
        self.ok(self.run_cmd([PYTHON, str(ROOT / "bin/install.py"), "--uninstall", "--apply", "--home", str(self.home), "--config-home", str(self.config)]))
        env = self.read(self.home / ".claude/settings.json")["env"]
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), self.url("b"), "the gateway the user switched to stays")
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "", "so does the mask that keeps a shell API key away from it")

    def test_update_redacts_tokens_older_versions_journaled(self):
        path = self.home / ".claude/.claude-agents-config-install.json"
        meta = self.read(path)
        for entry in meta["settingsJournal"]:
            if entry.get("id") == "value:env:ANTHROPIC_AUTH_TOKEN":
                entry["installed"] = TOKENS["a"]
        meta["settingsJournal"].append({"id": "value:env", "kind": "value", "path": ["env"], "beforePresent": False, "before": None,
                                        "installedPresent": True, "installed": {"ANTHROPIC_AUTH_TOKEN": TOKENS["a"], "OTHER": "1"}})
        path.write_text(json.dumps(meta))
        self.ok(self.install("a"))
        self.assertNotIn(TOKENS["a"], path.read_text())
        entry = next(e for e in self.read(path)["settingsJournal"] if e.get("id") == "value:env")
        self.assertEqual(set(entry["installed"]["ANTHROPIC_AUTH_TOKEN"]), {"redactedSha256"})

    # -- redirects and proxies ---------------------------------------------------------
    def test_redirects_are_refused_and_never_carry_the_token(self):
        self.servers["b"][1].mode = "redirect"
        self.servers["b"][1].redirect_to = self.url("c")
        self.fails(self.add("x", "b"), "redirects are not followed")
        self.assertTrue(self.servers["b"][1].seen, "the request reached b")
        self.assertNotIn(f"Bearer {TOKENS['b']}", self.servers["c"][1].seen, "b's token never reached the redirect target")
        self.assertFalse((self.home / ".claude/agentfleet/profiles/x.json").exists())

    def test_loopback_gateways_bypass_proxies(self):
        dead_proxy = "http://127.0.0.1:9"
        self.ok(self.add("b", "b", http_proxy=dead_proxy, HTTP_PROXY=dead_proxy))

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
        self.assertIn("may send it to the gateway instead of the gateway token", result.stderr)
        self.assertEqual(self.settings_env()["ANTHROPIC_API_KEY"], "user-owned-key-value")

    def test_token_env_value_is_never_echoed(self):
        result = self.af("add", "x", "--gateway-url", self.url("b"), "--allow-insecure-http", "--token-env", "LooksLikeAToken_1234")
        self.fails(result, "--token-env is not set")
        self.assertNotIn("LooksLikeAToken_1234", result.stdout + result.stderr)

    def test_default_names_avoid_windows_device_names(self):
        agentfleet = runpy.run_path(str(ROOT / "bin/agentfleet"))
        self.assertEqual(agentfleet["gateway_name"]("https://aux.example.com"), "aux-gateway")
        self.assertEqual(agentfleet["gateway_name"]("https://openrouter.ai/api"), "openrouter")

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
