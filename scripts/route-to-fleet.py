#!/usr/bin/env python3
# claude-agents-config:managed
"""UserPromptSubmit hook: classify the request and name the fleet lane to dispatch.

Runs on every prompt. Emits additionalContext naming one lane when the request
has delegable shape; stays silent otherwise.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Ordered: the first lane whose pattern matches wins. Specific before general.
LANES = [
    ("fleet-security-review", r"\b(security|vulnerab|exploit|auth[nz]?|authoriz|authentic|secret|credential|token leak|injection|xss|csrf|sql ?inject|permission model|trust boundar)\w*"),
    ("fleet-tests",           r"\b(test|tests|testing|spec suite|unit test|integration test|e2e|coverage|flaky|failing test|pytest|jest|vitest|phpunit|pest)\w*"),
    ("fleet-docs",            r"\b(readme|changelog|documentation|document this|write docs|api reference|runbook|guide|docstring)\w*"),
    ("fleet-ui",             r"\b(ui|ux|component|css|styling|layout|responsive|accessib|a11y|frontend|screen|button|modal|form design|tailwind|design system)\w*"),
    ("fleet-review",          r"\b(review|critique|audit|code smell|look over|sanity.?check|check (?:my|the|this) (?:diff|change|branch|pr|pull request|code))\w*"),
    ("fleet-diagnose-static", r"\b(debug|diagnos|root ?cause|why (?:is|does|did|won'?t|isn'?t)|traceback|stack ?trace|crash|broken|failing|regression|bug in)\w*"),
    ("fleet-plan",            r"\b(plan|design|architect|approach|strategy|how should (?:i|we)|propose|options for|trade.?offs?|refactor plan)\w*"),
    ("fleet-research-web",     r"https?://|\b(?:search the web|look ?up|google it|evaluate (?:this|the) (?:tool|library|service|api|package|vendor)|worth (?:using|adopting|paying)|should we (?:use|adopt|buy|switch)|found this (?:tool|library|service|skill|product)|latest version of|current (?:docs|documentation)|docs for|release notes|changelog for|compare (?:it |them )?(?:to|with|against) (?:other|alternatives))\w*"),
    ("fleet-research-codebase", r"\b(how does|how do|explain (?:the|this|how)|understand (?:the|this|how)|walk me through|trace (?:the|how)|survey|research)\w*"),
    ("fleet-explore-narrow",  r"\b(where is|where are|find (?:the|all|every)|locate|which file|search for|grep for|list all)\w*"),
    ("fleet-triage-static",   r"\b(triage|prioriti[sz]|who owns|scope (?:of|this)|next step|what should we do about)\w*"),
    ("fleet-implement",       r"\b(implement|build|add|create|write|fix|refactor|migrate|rename|extract|wire up|hook up|support for|make it|change the)\w*"),
]

SKIP = re.compile(
    r"^(y|n|yes|no|ok|okay|sure|thanks|thank you|go|go ahead|do it|continue|resume|stop|wait|"
    r"proceed|approved?|lgtm|nice|great|perfect|got it|hmm+|\?+|/\w+.*)$",
    re.IGNORECASE,
)
ALREADY_ROUTED = re.compile(r"\bfleet-[a-z0-9-]+|@[a-z0-9-]*fleet", re.IGNORECASE)
INLINE = re.compile(
    r"\b(inline|yourself|don'?t delegate|no subagent|without delegat|just (?:tell|show|answer)|"
    r"quick question|what (?:is|are|does) (?:the|this|that)\b)",
    re.IGNORECASE,
)

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

CONFIG_HOME = Path(os.environ.get("CLAUDE_FLEET_HOME", Path(__file__).resolve().parent.parent)).expanduser()
CLAUDE_DIR = CONFIG_HOME / ".claude"
LOG_PATH = str(CLAUDE_DIR / "routing-log.jsonl")
LOG_MAX_BYTES = 2_000_000
PROMPT_EXCERPT = 160
# Two tiers: the redacted excerpts are the near-term tuning surface and expire
# first; the bare counts are cheap and survive longer so trends stay readable.
PROMPT_RETENTION_DAYS = 14
ENTRY_RETENTION_DAYS = 90
PURGE_STAMP = str(CLAUDE_DIR / ".routing-log-purged")
PURGE_INTERVAL_SECONDS = 86_400

# Anything that looks like a credential is masked before it can reach the log.
SECRETS = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{12,}"
    r"|[A-Za-z0-9._%%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b(?:\d[ -]*?){13,19}\b"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*)\S+)"
)


def _purge_file(path, now):
    """Drop entries past the entry horizon and strip text past the text horizon."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return

    kept = []
    changed = False
    for line in lines:
        line = line.strip()
        if not line:
            changed = True
            continue
        try:
            entry = json.loads(line)
            age = (now - datetime.fromisoformat(entry["ts"])).total_seconds()
        except (ValueError, KeyError, TypeError):
            kept.append(line)          # unreadable: keep rather than destroy
            continue
        if age > ENTRY_RETENTION_DAYS * 86_400:
            changed = True
            continue
        if "prompt" in entry and age > PROMPT_RETENTION_DAYS * 86_400:
            del entry["prompt"]
            changed = True
            line = json.dumps(entry, ensure_ascii=False)
        kept.append(line)

    if not changed:
        return
    if not kept:
        try:
            os.unlink(path)
        except OSError:
            pass
        return

    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(kept) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def maybe_purge():
    """Apply retention at most once a day. Cost on other prompts is one stat call."""
    try:
        now = datetime.now().astimezone()
        try:
            if now.timestamp() - os.path.getmtime(PURGE_STAMP) < PURGE_INTERVAL_SECONDS:
                return
        except OSError:
            pass
        for path in (LOG_PATH, LOG_PATH + ".1"):
            if os.path.exists(path):
                _purge_file(path, now)
        os.close(os.open(PURGE_STAMP, os.O_WRONLY | os.O_CREAT, 0o600))
        os.utime(PURGE_STAMP, None)
    except Exception:
        pass


def record(reason, prompt, lane=None):
    """Append one routing decision. Never raises: a logging fault must not break the prompt."""
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        maybe_purge()
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": reason,
            "lane": lane,
            "chars": len(prompt),
        }
        # Only the silent prompts are the tuning surface, so only they keep text.
        if reason == "no-match":
            entry["prompt"] = SECRETS.sub("[redacted]", prompt[:PROMPT_EXCERPT])
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(LOG_PATH, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def leave(reason, prompt, lane=None):
    record(reason, prompt, lane)
    sys.exit(0)


prompt = (payload.get("prompt") or "").strip()
if not prompt:
    sys.exit(0)
# System-injected envelopes (task notifications, reminders) are not user requests.
if prompt.startswith("<"):
    leave("system-envelope", prompt)
if len(prompt) < 12:
    leave("too-short", prompt)
if SKIP.match(prompt):
    leave("skip-phrase", prompt)
if ALREADY_ROUTED.search(prompt):
    leave("already-named", prompt)
if INLINE.search(prompt):
    leave("inline-requested", prompt)

# A leading "add/write tests" is test work; otherwise the earliest signal in the
# prompt wins, so "add X and add tests" routes to implement, not tests.
TEST_FIRST = re.compile(r"^\W*(add|write|create|fix|update)\s+(some |a |the )?"
                        r"(unit |integration |e2e |failing |flaky )?tests?\b", re.IGNORECASE)

if TEST_FIRST.match(prompt):
    lane = "fleet-tests"
else:
    hits = []
    for index, (name, pattern) in enumerate(LANES):
        match = re.search(pattern, prompt, re.IGNORECASE)
        if match:
            hits.append((match.start(), index, name))
    if not hits:
        leave("no-match", prompt)
    lane = min(hits)[2]

context = (
    f"Routing: this request has {lane.replace('fleet-', '')} shape. "
    f"Dispatch `{lane}` via the Agent tool rather than doing it yourself, and say in one line that you did. "
    f"Give it a brief carrying the goal, scope, exclusions, the project instructions it needs "
    f"(it does not load CLAUDE.md), verification commands, and what to report back. "
    f"Then verify its work yourself before reporting done. "
    f"Override only when the task is a single obvious edit, already answered by context, or a command "
    f"you need output from — say why in one line if you do."
)

record("routed", prompt, lane)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }
}))
