#!/usr/bin/env python3
# claude-agents-config:managed
"""Advisory provider drift detection.

This is invoked after the serialized SessionStart catalog refresh. It validates
the scoped cache again for safe direct invocation, resolves exact provider IDs
through provider_catalog.py, and writes an exhaustive mode-0600 proposal. It
never edits the fleet map. Notification deduplication is separate from the
proposal so an unapproved/pending decision remains visible to setup.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_catalog import (  # noqa: E402
    available_rows,
    cache_rows,
    candidate_list,
    family_approved,
    load_policy,
)

# Windows pipes and consoles default to a legacy encoding while drift notices
# may quote non-ASCII provider text; emit stdout as UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MAX_NOTICE_ITEMS = 8


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def parse_arg(argv: list[str], name: str):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def resolve_home(argv: list[str]) -> Path:
    value = parse_arg(argv, "--home") or os.environ.get("CLAUDE_FLEET_HOME")
    return Path(value).expanduser().resolve() if value else Path(__file__).resolve().parent.parent


def resolve_config_home(argv: list[str], home: Path) -> Path:
    value = parse_arg(argv, "--config-home") or os.environ.get("CLAUDE_FLEET_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
    return Path(value).expanduser().resolve() if value else home / ".config"


def bounded(items, formatter) -> str:
    rendered = [formatter(item) for item in items]
    if len(rendered) > MAX_NOTICE_ITEMS:
        return ", ".join(rendered[:MAX_NOTICE_ITEMS]) + f", and {len(rendered) - MAX_NOTICE_ITEMS} more"
    return ", ".join(rendered)


def _main() -> int:
    argv = sys.argv[1:]
    home = resolve_home(argv)
    config_home = resolve_config_home(argv, home)
    settings = read_json(home / ".claude" / "settings.json")
    policy = read_json(config_home / "delegate-skills" / "provider-policy.json")
    claude_fleet = home / ".claude" / "fleet.json"
    legacy_fleet = config_home / "delegate-skills" / "config.json"
    if claude_fleet.is_file() and legacy_fleet.is_file():
        try:
            if legacy_fleet.stat().st_mtime > claude_fleet.stat().st_mtime:
                fleet_path = legacy_fleet
            else:
                fleet_path = claude_fleet
        except Exception:
            fleet_path = claude_fleet
    elif claude_fleet.is_file():
        fleet_path = claude_fleet
    else:
        fleet_path = legacy_fleet
    fleet = read_json(fleet_path)
    if not isinstance(settings, dict) or not isinstance(policy, dict) or not isinstance(fleet, dict) or not isinstance(fleet.get("lanes"), dict):
        return 0
    try:
        load_policy(config_home / "delegate-skills" / "provider-policy.json")
    except Exception:
        return 0
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    endpoint = env.get("ANTHROPIC_BASE_URL")
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not isinstance(endpoint, str) or not isinstance(token, str) or not endpoint or not token:
        return 0
    cache_path = home / ".claude" / "cache" / "omniroute-models-cache.json"
    cached = read_json(cache_path)
    rows = cache_rows(cached, endpoint, token, int(policy.get("cacheTtlSeconds", 21600)))
    if rows is None:
        return 0
    available = available_rows(rows, policy)
    lane_candidates: dict[str, list[str]] = {}
    for lane, config in fleet["lanes"].items():
        candidates = candidate_list(config)
        if candidates:
            lane_candidates[lane] = candidates

    resolved: dict[str, str] = {}
    fallback_resolutions: dict[str, dict] = {}
    missing: dict[str, list[str]] = {}
    for lane, candidates in lane_candidates.items():
        approved = [model for model in candidates if family_approved(model, policy)]
        chosen = next((model for model in approved if model in available), None)
        if chosen is None:
            resolved[lane] = candidates[0]
            missing[lane] = candidates
        else:
            resolved[lane] = chosen
            if chosen != candidates[0]:
                fallback_resolutions[lane] = {
                    "preferred": candidates[0], "resolved": chosen, "candidates": candidates,
                }

    used = set(resolved.values())
    unused = {model: info for model, info in sorted(available.items()) if model not in used}
    signature = "\n".join(
        f"{model}:{info.get('context_length')}" for model, info in sorted(available.items())
    ) + "\n--\n" + "\n".join(
        f"{lane}={'|'.join(candidates)}=>{resolved[lane]}" for lane, candidates in sorted(lane_candidates.items())
    )
    models_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    proposal_path = home / ".claude" / "fleet-model-proposal.json"
    previous = read_json(proposal_path)
    notice_state_path = home / ".claude" / "fleet-model-notice.json"
    notice_state = read_json(notice_state_path)
    auto_approved = policy.get("autoApproveProposals") is True
    decision = "approved" if auto_approved else (
        previous.get("decision", "pending") if isinstance(previous, dict) and previous.get("models_hash") == models_hash else "pending"
    )
    proposal = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models_hash": models_hash,
        "decision": decision,
        "auto_approved": auto_approved,
        "fleet_config": str(fleet_path),
        "missing_lane_models": missing,
        "fallback_resolutions": fallback_resolutions,
        "available_models_unused_by_fleet": unused,
    }
    parts = []
    if missing:
        parts.append("lanes with unavailable approved candidates: " + bounded(
            sorted(missing.items()), lambda pair: f"{pair[0]} -> {' then '.join(pair[1])}"
        ))
    if fallback_resolutions:
        parts.append("eligible fallback resolutions: " + bounded(
            sorted(fallback_resolutions.items()), lambda pair: f"{pair[0]} {pair[1]['preferred']} -> {pair[1]['resolved']}"
        ))
    if unused:
        parts.append("gateway models unused by any lane: " + bounded(
            sorted(unused.items()), lambda pair: f"{pair[0]} ({(pair[1].get('context_length') or 0) // 1000}K)"
        ))

    notice_hash = notice_state.get("models_hash") if isinstance(notice_state, dict) else None
    should_notice = bool(parts) and notice_hash != models_hash
    if should_notice:
        if auto_approved:
            notice = (
                "Fleet models auto-reconciled; proposals auto-approved per policy. "
                + "; ".join(parts) + "."
            )
        else:
            decision_hint = "Run claude-fleet-setup with proposalHash and proposalDecision=approve/reject/supersede; no remapping is applied automatically."
            notice = (
                "Fleet model drift detected. Review "
                f"{proposal_path}; {decision_hint} "
                "Then run claude-fleet-sync and restart Claude Code. " + "; ".join(parts) + "."
            )
        sys.stdout.write(notice + "\n")
        sys.stdout.flush()
        notice_payload = {"models_hash": models_hash, "notified_at": time.time()}
        temp_notice = notice_state_path.with_name(notice_state_path.name + ".tmp." + str(os.getpid()) + "." + secrets.token_hex(6))
        try:
            temp_notice.write_text(json.dumps(notice_payload) + "\n", encoding="utf-8")
            os.chmod(temp_notice, 0o600)
            os.replace(temp_notice, notice_state_path)
        except OSError:
            try:
                temp_notice.unlink()
            except OSError:
                pass

    temp = proposal_path.with_name(proposal_path.name + ".tmp." + str(os.getpid()) + "." + secrets.token_hex(6))
    try:
        temp.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, proposal_path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
    return 0


def main() -> int:
    try:
        return _main()
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
