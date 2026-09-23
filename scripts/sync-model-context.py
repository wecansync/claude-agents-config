#!/usr/bin/env python3
"""Synchronize a verified model context budget without racing other writers.

The provider cache is accepted only when its endpoint, account scope, schema,
and freshness match the current gateway settings.  The resulting budget is a
conservative 90 percent of the advertised context, capped at the installer
budget; a provider suffix or display label never inflates capacity.  Fail-open
is intentional for hook use: malformed payloads, stale/offline discovery, lock
contention, or malformed settings leave the existing file unchanged.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

# Set before importing provider_catalog: CPython writes a module's .pyc
# during import, before the module body (and its own identical guard) can
# run. Without this, importing it here creates a __pycache__ directory next
# to the managed scripts -- which the installer's own bundle-inventory
# preflight then rejects as an unlisted path, and which leaves stray
# generated files in an installed home.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_catalog import (  # noqa: E402
    acquire_lock,
    available_rows,
    cache_rows,
    load_policy,
    release_lock,
    strip_known_suffix,
)

# Windows pipes and consoles default to a legacy encoding while provider
# payloads may contain non-ASCII text; emit stdout as UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MIN_CONTEXT = 100_000
MAX_CONTEXT = 800_000
HEADROOM_NUMERATOR = 9
HEADROOM_DENOMINATOR = 10


def arg_path(argv: list[str], name: str, default: Path) -> Path:
    try:
        index = argv.index(name)
        if index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser().resolve()
    except (ValueError, OSError, RuntimeError):
        pass
    return default


def safe_context(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < MIN_CONTEXT:
        return None
    return min(MAX_CONTEXT, max(MIN_CONTEXT, number * HEADROOM_NUMERATOR // HEADROOM_DENOMINATOR))


def compact_control(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if MIN_CONTEXT <= number <= 1_000_000 else None


def update_context(payload: object, home: Path, config_home: Path) -> dict | None:
    if not isinstance(payload, dict):
        return None
    settings_path = home / ".claude" / "settings.json"
    cache_path = home / ".claude" / "cache" / "provider-models-cache.json"
    policy_path = config_home / "delegate-skills" / "provider-policy.json"
    try:
        policy = load_policy(policy_path)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    model = payload.get("to_model") or payload.get("model") or settings.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    event = payload.get("hook_event_name")
    env = settings.get("env")
    if not isinstance(env, dict):
        return None
    endpoint = env.get("ANTHROPIC_BASE_URL")
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not isinstance(endpoint, str) or not isinstance(token, str) or not endpoint or not token:
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        rows = cache_rows(cached, endpoint, token)
    except (OSError, UnicodeError, ValueError):
        return None
    if rows is None:
        return None
    catalog = available_rows(rows, policy)
    model_strip = model.strip()
    clean = strip_known_suffix(model_strip)
    model_info = catalog.get(model_strip)
    if not isinstance(model_info, dict):
        if clean in catalog:
            model_info = catalog[clean]
        elif f"{clean}[1m]" in catalog:
            model_info = catalog[f"{clean}[1m]"]
        else:
            model_info = next((row for runtime_id, row in catalog.items() if strip_known_suffix(runtime_id) == clean), None)
    if not isinstance(model_info, dict):
        return None
    budget = safe_context(model_info.get("context_length"))
    if budget is None:
        return None
    value = str(budget)
    compact_budget = budget

    fd, lock_path = acquire_lock(home, timeout=0.75)
    if fd is None:
        return None
    temp = settings_path.with_name(settings_path.name + ".tmp." + str(os.getpid()) + "." + secrets.token_hex(6))
    try:
        try:
            latest = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(latest, dict):
            return None
        latest_env = latest.get("env")
        if not isinstance(latest_env, dict):
            return None
        changed = (
            latest_env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") != value
            or latest.get("autoCompactWindow") != compact_budget
            or latest_env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") != value
        )
        if changed:
            latest_env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = value
            latest["autoCompactWindow"] = compact_budget
            latest_env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = value
            data = (json.dumps(latest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            fd_temp = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd_temp, view)
                    if written <= 0:
                        raise OSError("short settings write")
                    view = view[written:]
                os.fsync(fd_temp)
            finally:
                os.close(fd_temp)
            os.chmod(temp, 0o600)
            os.replace(temp, settings_path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return None
    finally:
        release_lock(fd, lock_path)

    if event in {"PreModelSwitch", "PostModelSwitch"}:
        return {
            "systemMessage": (
                f"Verified context and paired compaction budget for {model.strip()} set to {compact_budget} tokens in settings.json. "
                "Restart Claude Code to apply these startup-only settings."
            )
        }
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        argv = sys.argv[1:]
        script_home = Path(__file__).resolve().parent.parent
        home = arg_path(argv, "--home", script_home)
        config_home = arg_path(argv, "--config-home", home / ".config")
        output = update_context(payload, home, config_home)
        if output:
            print(json.dumps(output, ensure_ascii=False))
    except (OSError, UnicodeError, ValueError, TypeError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
