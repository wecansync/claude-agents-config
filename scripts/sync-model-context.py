#!/usr/bin/env python3
"""Keep CLAUDE_CODE_MAX_CONTEXT_TOKENS aligned with the selected gateway model.

Runs as a PreModelSwitch, PostModelSwitch, or SessionStart hook. Claude Code
applies CLAUDE_CODE_MAX_CONTEXT_TOKENS to every gateway model ID it cannot
resolve, so one static value is wrong for every model whose real context window
differs. The omniroute discovery cache records each model's context_length;
this hook copies the selected model's real limit into the settings env so the
next launch budgets the session at that model's actual window. A PreModelSwitch
run that changes the value prints a systemMessage telling the user to restart
Claude Code, because the override is only read at startup. Fail-open: any error
exits 0 and the session proceeds with the previous value.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    model = payload.get("to_model") or payload.get("model")
    if not isinstance(model, str) or not model:
        return 0
    base = model.split("[", 1)[0].strip()
    if not base or base.lower().startswith("claude-"):
        # Claude Code resolves recognized claude-* IDs itself and the override
        # variable does not apply to them, so leave the setting alone.
        return 0

    home = Path(__file__).resolve().parent.parent
    argv = sys.argv[1:]
    if "--home" in argv:
        index = argv.index("--home")
        if index + 1 < len(argv):
            home = Path(argv[index + 1]).expanduser().resolve()
    cache_path = home / ".claude" / "cache" / "omniroute-models-cache.json"
    settings_path = home / ".claude" / "settings.json"

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return 0
    if not isinstance(cache, list):
        return 0
    context_length = None
    for row in cache:
        if isinstance(row, dict) and row.get("id") == base:
            raw = row.get("context_length", row.get("max_input_tokens"))
            if isinstance(raw, int) and raw > 0:
                context_length = raw
            break
    if context_length is None:
        return 0

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return 0
    if not isinstance(settings, dict):
        return 0
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
        settings["env"] = env
    value = str(context_length)
    if env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == value:
        return 0
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = value

    temp = settings_path.with_name(settings_path.name + ".tmp." + str(os.getpid()))
    wrote = False
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            data = (json.dumps(settings, indent=2) + "\n").encode("utf-8")
            offset = 0
            while offset < len(data):
                offset += os.write(fd, data[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temp, 0o600)
        os.replace(temp, settings_path)
        wrote = True
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
    if wrote and event == "PreModelSwitch":
        # Claude Code reads the override at startup, so the corrected budget
        # only applies to the next launch. PreModelSwitch systemMessage output
        # reaches the user regardless of the switch decision.
        print(json.dumps({
            "systemMessage": (
                f"Context window for {base} set to {value} in settings.json. "
                "Restart Claude Code to apply it to this session."
            )
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
