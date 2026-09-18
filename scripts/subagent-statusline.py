#!/usr/bin/env python3
# claude-agents-config:managed

import json
import sys
from datetime import datetime, timezone


def compact_number(value):
    if not isinstance(value, (int, float)):
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def elapsed(start):
    if not start:
        return ""
    try:
        stamp = str(start).replace("Z", "+00:00")
        dt = datetime.fromisoformat(stamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def clip(text, width):
    if width <= 1:
        return text[: max(width, 0)]
    return text if len(text) <= width else text[: width - 1] + "…"


try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

columns = payload.get("columns")
width = max(24, int(columns)) if isinstance(columns, (int, float)) else 120
icons = {
    "running": "▶",
    "completed": "✓",
    "succeeded": "✓",
    "failed": "✗",
    "error": "✗",
    "pending": "…",
    "queued": "…",
    "stopped": "■",
}

for task in payload.get("tasks") or []:
    if not isinstance(task, dict) or not task.get("id"):
        continue
    status = str(task.get("status") or "pending")
    icon = icons.get(status.lower(), "•")
    name = str(task.get("name") or task.get("label") or task.get("type") or "agent")
    model = str(task.get("model") or "model pending")
    effort = task.get("effort")
    effort_text = f" · {effort}" if effort not in (None, "") else ""
    tokens = task.get("tokenCount")
    context = task.get("contextWindowSize")
    token_text = ""
    if isinstance(tokens, (int, float)):
        token_text = f" · {compact_number(tokens)} tok"
        if isinstance(context, (int, float)) and context > 0:
            token_text += f"/{tokens / context:.0%}"
    elapsed_text = elapsed(task.get("startTime"))
    elapsed_suffix = f" · {elapsed_text}" if elapsed_text else ""
    row = f"{icon} {name} · {model}{effort_text} · {status}{token_text}{elapsed_suffix}"
    print(json.dumps({"id": str(task["id"]), "content": clip(row, width)}, ensure_ascii=False))
