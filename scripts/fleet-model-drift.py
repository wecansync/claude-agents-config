#!/usr/bin/env python3
# claude-agents-config:managed
"""Fleet model drift detector.

Invoked synchronously as a child process of sync-omniroute-models.mjs right
after that script refreshes the omniroute discovery cache on disk, so this
always reads a cache that is at least as fresh as the current SessionStart
run. It is deliberately not registered as its own SessionStart hook: separate
hooks for the same event give no ordering guarantee, and a standalone
detector hook previously raced the cache write and could read a stale cache
(see project history). Running it as a synchronous subprocess of the script
that writes the cache removes the race by construction.

Compares the gateway models the cache advertises against the models the
delegate fleet lanes reference and writes ~/.claude/fleet-model-proposal.json
(atomic write). When the availability set changed since the last run (or the
proposal file did not exist yet), prints a single-line plain-text notice on
stdout; the caller folds that into a SessionStart hookSpecificOutput
additionalContext value. Prints nothing when there is nothing new to report,
so repeated SessionStart runs stay silent. The in-session LLM is expected to
propose lane remappings from the written proposal file and apply them only
after the user explicitly approves. Fail-open: any error exits 0 silently.

Model identity uses the same effective spelling sync-omniroute-models.mjs
computes for the picker: a leading vendor namespace ("wecansync/foo") is
collapsed to the bare id before comparison, and a "[1m]" suffix is appended
whenever context_length >= 872000 (matching the mjs threshold exactly),
regardless of whatever suffix the gateway's raw id happened to carry. Fleet
lane models are already configured in that same spelling (e.g.
"codex-sol[1m]"), so comparison is a direct string match with no stripping
on the lane side -- this is what keeps "missing"/"unused" sets aligned with
what would actually land in the modelPicker.

Delivery note: the notice text is written to stdout, flushed, and only then
is the proposal file (carrying the dedup hash) persisted. If this process is
killed after printing but before persisting, the next run simply reprints an
unchanged notice (safe, redundant) instead of silently losing it because the
hash was already marked seen. If killed before printing, nothing is
persisted either, so the next run retries from scratch. This does not (and
cannot, from inside this subprocess) confirm the notice actually reached the
user inside the Claude Code session -- only that this process did everything
in its power to emit it before recording that it did. A second confirmation
channel (a parent-written acknowledgement) was considered and rejected here
as unwarranted complexity for a single-line, idempotent, at-least-once
notice.
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# Same threshold sync-omniroute-models.mjs uses to decide whether a discovered
# model earns a "[1m]" picker suffix.
ONE_M_CONTEXT_THRESHOLD = 872000
# Cap how many items each notice category lists on stdout so the SessionStart
# additionalContext stays a bounded single line; the proposal JSON written to
# disk is never truncated.
MAX_NOTICE_ITEMS = 8


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def parse_arg(argv, name):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def resolve_home(argv):
    value = parse_arg(argv, "--home")
    if value:
        return Path(value).expanduser().resolve()
    env_home = os.environ.get("CLAUDE_FLEET_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


_TRAILING_SUFFIX = re.compile(r"\[\d+[kmKM]?\]\s*$")


def effective_spelling(raw_id, context_length):
    """Mirror sync-omniroute-models.mjs's picker-id derivation exactly:
    collapse a leading "vendor/" namespace, strip whatever bracket suffix the
    gateway happened to spell, and append "[1m]" only when context_length
    clears the same 872000 threshold the mjs script uses. Returns
    (effective_id, base) where base is the pre-suffix name used for the
    claude-*/speech-*/tts-* exclusion checks.
    """
    raw_id = (raw_id or "").strip()
    if not raw_id:
        return "", ""
    collapsed = raw_id.split("/")[-1]
    base = _TRAILING_SUFFIX.sub("", collapsed).strip()
    if not base:
        return "", ""
    try:
        ctx = int(context_length) if context_length else 0
    except (TypeError, ValueError):
        ctx = 0
    effective_id = base + "[1m]" if ctx >= ONE_M_CONTEXT_THRESHOLD else base
    return effective_id, base


def resolve_config_home(argv, home):
    value = parse_arg(argv, "--config-home")
    if value:
        return Path(value).expanduser().resolve()
    env_value = os.environ.get("CLAUDE_FLEET_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return home / ".config"


def main() -> int:
    try:
        return _main()
    except Exception:
        # Fail-open per module contract: any unexpected error (malformed
        # cache/fleet-config row, unexpected type, etc.) must not surface as
        # a non-zero exit or traceback, since the caller (sync-omniroute-
        # models.mjs) treats a non-zero status as "no drift notice" and
        # would otherwise silently lose the notice while looking crashed.
        return 0


def _main() -> int:
    argv = sys.argv[1:]
    home = resolve_home(argv)
    config_home = resolve_config_home(argv, home)

    cache = read_json(home / ".claude" / "cache" / "omniroute-models-cache.json")
    fleet = read_json(config_home / "delegate-skills" / "config.json")
    if not isinstance(cache, list) or not isinstance(fleet, dict) or not isinstance(fleet.get("lanes"), dict):
        return 0

    available = {}
    for row in cache:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id") or ""
        effective_id, base = effective_spelling(raw_id, row.get("context_length") or row.get("max_input_tokens"))
        if not base or base.lower().startswith("claude-") or base.startswith("speech-") or base.startswith("tts-"):
            continue
        raw_context = row.get("context_length") or row.get("max_input_tokens") or 200000
        try:
            context_length = int(raw_context)
        except (TypeError, ValueError):
            context_length = 200000
        # Namespace collapse means two raw ids ("wecansync/foo", "foo") can
        # land on the same effective_id; keep the first seen so the notice
        # does not enumerate the same model twice under different vendor
        # prefixes.
        available.setdefault(effective_id, {
            "id": effective_id,
            "context_length": context_length,
            "description": (row.get("description") or "")[:120],
        })

    lanes = fleet["lanes"]
    lane_candidates = {}
    for lane, config in lanes.items():
        if not isinstance(config, dict):
            continue
        candidates = [config.get("model"), *(config.get("fallbacks") or [])]
        candidates = [model.strip() for model in candidates if isinstance(model, str) and model.strip()]
        if candidates:
            lane_candidates[lane] = candidates

    resolved = {
        lane: next((model for model in candidates if model in available or model.lower().startswith("claude-")), candidates[0])
        for lane, candidates in lane_candidates.items()
    }
    fallback_resolutions = {
        lane: {"preferred": candidates[0], "resolved": resolved[lane], "candidates": candidates}
        for lane, candidates in lane_candidates.items()
        if resolved[lane] != candidates[0]
    }
    missing = {
        lane: candidates
        for lane, candidates in lane_candidates.items()
        if not any(model in available or model.lower().startswith("claude-") for model in candidates)
    }
    used = set(resolved.values())
    unused = {base: info for base, info in sorted(available.items()) if base not in used}

    # Hash both the gateway's available model set and the fleet's lane->model
    # mapping. Hashing only the available set would let a fleet_config-only
    # change (e.g. a lane remapped onto a model that was already unavailable
    # under the same gateway snapshot) go unreported whenever it happens to
    # coincide with a models_hash already recorded from an earlier run.
    signature = "\n".join(
        f"{model}:{info['context_length']}" for model, info in sorted(available.items())
    ) + "\n--\n" + "\n".join(
        f"{lane}={'|'.join(candidates)}=>{resolved[lane]}" for lane, candidates in sorted(lane_candidates.items())
    )
    models_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    proposal_path = home / ".claude" / "fleet-model-proposal.json"
    previous = read_json(proposal_path)
    if isinstance(previous, dict) and previous.get("models_hash") == models_hash:
        return 0

    proposal = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models_hash": models_hash,
        "fleet_config": str(config_home / "delegate-skills" / "config.json"),
        # The proposal file is always exhaustive; only the stdout notice
        # below is length-bounded.
        "missing_lane_models": missing,
        "fallback_resolutions": fallback_resolutions,
        "available_models_unused_by_fleet": unused,
    }

    def bounded(items, formatter):
        rendered = [formatter(item) for item in items]
        if len(rendered) > MAX_NOTICE_ITEMS:
            shown = rendered[:MAX_NOTICE_ITEMS]
            return ", ".join(shown) + f", and {len(rendered) - MAX_NOTICE_ITEMS} more"
        return ", ".join(rendered)

    parts = []
    if missing:
        parts.append("lanes with unavailable models: " + bounded(
            sorted(missing.items()), lambda pair: f"{pair[0]} -> {' then '.join(pair[1])}"
        ))
    if unused:
        parts.append("gateway models unused by any lane: " + bounded(
            sorted(unused.items()), lambda pair: f"{pair[0]} ({pair[1]['context_length'] // 1000}K)"
        ))

    # Emit before persist: print (and flush) the notice first, and only mark
    # this signature "seen" in the proposal file afterward. See the module
    # docstring for the delivery-window rationale. When there is nothing to
    # report, still persist the proposal (an always-current, exhaustive
    # snapshot) since there is no notice to protect against loss.
    if parts:
        notice = (
            "Fleet model drift detected. Review "
            f"{proposal_path} and propose lane remappings to the user; "
            "apply only after explicit approval, then run claude-fleet-sync and remind the user to restart. "
            + "; ".join(parts) + "."
        )
        sys.stdout.write(notice + "\n")
        sys.stdout.flush()

    temp = proposal_path.with_name(proposal_path.name + ".tmp." + str(os.getpid()))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            data = (json.dumps(proposal, indent=2) + "\n").encode("utf-8")
            offset = 0
            while offset < len(data):
                offset += os.write(fd, data[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temp, 0o600)
        os.replace(temp, proposal_path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        # The notice (if any) already reached stdout above; only the "seen"
        # bookkeeping failed to persist, so the next run harmlessly reprints
        # an unchanged notice instead of silently losing this one.
    return 0


if __name__ == "__main__":
    sys.exit(main())
