#!/usr/bin/env python3
"""Provider-aware fleet reconciliation for setup and SessionStart.

This command never prompts.  Explicit setup/reconfigure commands own policy
interviews; startup only applies already-approved, live candidates and reports
pending decisions.  It is also the single Python writer for the fleet map,
provider picker rows, and startup context budget.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from contextlib import contextmanager

# Set before importing provider_catalog: CPython writes a module's .pyc
# during import, before the module body (and its own identical guard) can
# run. Without this, importing it here creates a __pycache__ directory next
# to the managed scripts -- which the installer's own bundle-inventory
# preflight then rejects as an unlisted path, and which leaves stray
# generated files in an installed home.
sys.dont_write_bytecode = True

from provider_catalog import (
    MAX_FALLBACK_CANDIDATES,
    CatalogError,
    acquire_lock,
    available_rows,
    cache_rows,
    candidate_list,
    eligible_candidates,
    family_approved,
    load_policy,
    model_family,
    release_lock,
)

MIN_CONTEXT = 100_000
MAX_CONTEXT = 800_000
HEADROOM_NUMERATOR = 9
HEADROOM_DENOMINATOR = 10
TRANSACTION_MARKER = ".fleet-reconcile-pending.json"
TRANSACTION_FORMAT = "claude-agents-config.reconcile-transaction.v1"
GATEWAY_MODEL_PREFIXES = ("agy-", "claude-", "codex-", "cursor-", "omniroute-", "openclaw-", "deepseek-", "qwen-")

CANONICAL_LANE_PREFERRED: dict[str, list[str]] = {
    "plan": ["claude-opus-5[1m]", "codex-sol[1m]", "agy-claude-opus[1m]"],
    "plan-alt": ["codex-sol[1m]", "claude-opus-5[1m]", "agy-claude-opus[1m]"],
    "implement": ["codex-luna[1m]", "claude-sonnet-5[1m]", "agy-gemini-flash[1m]"],
    "implement-02-gemini-flash": ["agy-gemini-flash[1m]", "claude-sonnet-5[1m]"],
    "implement-03-agy-opus": ["agy-claude-opus[1m]", "claude-sonnet-5[1m]"],
    "implement-04-free-1m": ["omniroute-free-1m-ctx[1m]", "omniroute-free-256k-ctx"],
    "implement-05-agy-sonnet": ["agy-claude-sonnet[1m]", "claude-sonnet-5[1m]"],
    "implement-06-free-256k": ["omniroute-free-256k-ctx", "omniroute-free-1m-ctx[1m]"],
    "implement-07-auto-128k": ["custom-auto", "qwen-3.8-128k-ctx", "omniroute-free-256k-ctx"],
    "implement-08-atria-experimental": ["Atria", "claude-sonnet-5[1m]"],
    "implement-09-codex-5-5": ["codex-5.5", "claude-sonnet-5[1m]"],
    "implement-10-sonnet": ["claude-sonnet-5[1m]", "agy-gemini-flash[1m]"],
    "implement-11-terra": ["codex-terra[1m]", "claude-sonnet-5[1m]"],
    "implement-12-openclaw-free": ["openclaw-free", "omniroute-free-1m-ctx[1m]", "omniroute-free-256k-ctx"],
    "implement-13-grok-no-cache": ["cursor-grok", "claude-sonnet-5[1m]"],
    "review": ["codex-sol[1m]", "claude-opus-5[1m]", "agy-claude-opus[1m]"],
    "review-02-opus": ["claude-opus-5[1m]", "codex-sol[1m]", "agy-claude-opus[1m]"],
    "review-03-terra": ["codex-terra[1m]", "claude-opus-5[1m]", "codex-sol[1m]"],
    "review-04-gemini": ["agy-gemini-pro[1m]", "claude-opus-5[1m]"],
    "review-05-grok": ["cursor-grok", "claude-opus-5[1m]"],
    "review-06-astra": ["codex-astra[1m]", "codex-sol[1m]", "claude-opus-5[1m]"],
    "diagnose-static": ["codex-sol-max[1m]", "codex-sol[1m]", "claude-opus-5[1m]"],
    "security-review": ["claude-opus-5[1m]", "agy-claude-opus[1m]", "codex-sol-max[1m]"],
    "ui": ["agy-claude-opus[1m]", "claude-sonnet-5[1m]"],
    "tests": ["agy-gemini-flash[1m]", "claude-sonnet-5[1m]"],
    "docs": ["agy-claude-sonnet[1m]", "claude-sonnet-5[1m]"],
    "explore-narrow": ["claude-haiku", "cursor-auto", "qwen-3.8-128k-ctx"],
    "research-codebase": ["agy-gemini-pro[1m]", "claude-sonnet-5[1m]"],
    "research-web": ["agy-gemini-pro[1m]", "claude-sonnet-5[1m]"],
    "triage-static": ["cursor-auto", "qwen-3.8-128k-ctx", "claude-haiku"],
}


def read_json(path: Path, default: object = None) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(raw)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp.unlink()
        except OSError:
            pass


def _transaction_dir(home: Path) -> Path:
    return home / ".claude" / ".fleet-reconcile-txn"


def _safe_transaction_path(home: Path, raw: object, trusted_roots: list[Path] | None = None) -> Path:
    if not isinstance(raw, str) or not raw:
        raise OSError("transaction path is missing")
    path = Path(raw)
    roots = trusted_roots or [home.resolve(strict=False), (home / ".claude").resolve(strict=False)]
    roots = [root.resolve(strict=False) for root in roots]
    resolved = path.resolve(strict=False)
    if not path.is_absolute() or not any(resolved == root or root in resolved.parents for root in roots):
        raise OSError(f"transaction target escapes home: {path}")
    return path


def _validate_transaction(
    home: Path,
    transaction: Path,
    trusted_roots: list[Path] | None = None,
) -> tuple[dict, list[tuple[Path, bool, bytes | None, int | None]]]:
    if transaction.name != ".fleet-reconcile-txn" or transaction.parent != (home / ".claude"):
        raise OSError("transaction directory is outside the installed home")
    manifest_path = transaction / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("format") != TRANSACTION_FORMAT:
        raise OSError("transaction manifest format is invalid")
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise OSError("transaction manifest has no records")
    prepared = []
    seen_targets: set[str] = set()
    seen_payloads: set[str] = set()
    transaction_root = transaction.resolve(strict=False)
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise OSError(f"transaction record {index} is malformed")
        target = _safe_transaction_path(home, record.get("path"), trusted_roots)
        if str(target) in seen_targets:
            raise OSError(f"transaction target repeats: {target}")
        seen_targets.add(str(target))
        existed = record.get("existed")
        if not isinstance(existed, bool):
            raise OSError(f"transaction existed flag is invalid: {target}")
        if existed:
            payload_name = record.get("backup")
            digest = record.get("sha256")
            mode = record.get("mode")
            if not isinstance(payload_name, str) or not payload_name or payload_name in seen_payloads:
                raise OSError(f"transaction backup name is invalid: {target}")
            if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise OSError(f"transaction backup hash is invalid: {target}")
            if not isinstance(mode, int) or stat.S_IMODE(mode) != mode or not 0o600 <= mode <= 0o777:
                raise OSError(f"transaction backup mode is invalid: {target}")
            payload = transaction / payload_name
            if payload.is_symlink() or not payload.is_file() or transaction_root not in payload.resolve(strict=False).parents:
                raise OSError(f"transaction backup payload is invalid: {payload}")
            data = payload.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise OSError(f"transaction backup checksum mismatch: {payload}")
            prepared.append((target, True, data, mode))
            seen_payloads.add(payload_name)
        else:
            prepared.append((target, False, None, None))
    return manifest, prepared


def _remove_transaction_evidence(transaction: Path) -> None:
    for child in transaction.iterdir():
        if child.is_symlink() or not child.is_file():
            raise OSError(f"refusing to remove unexpected transaction child: {child}")
        child.unlink()
    transaction.rmdir()


def _restore_transaction(
    home: Path,
    transaction: Path,
    *,
    committed: bool = False,
    trusted_roots: list[Path] | None = None,
) -> None:
    manifest, prepared = _validate_transaction(home, transaction, trusted_roots)
    if committed or manifest.get("state") == "committed":
        _remove_transaction_evidence(transaction)
        return
    for target, existed, data, mode in prepared:
        if existed:
            assert data is not None and mode is not None
            atomic_write(target, data, mode)
        elif os.path.lexists(target):
            if target.is_symlink() or target.is_dir():
                raise OSError(f"refusing to remove unexpected target: {target}")
            target.unlink()
    _remove_transaction_evidence(transaction)


def recover_transaction(home: Path, trusted_roots: list[Path] | None = None) -> None:
    marker = home / ".claude" / TRANSACTION_MARKER
    transaction_value = read_json(marker)
    if not isinstance(transaction_value, dict) or not isinstance(transaction_value.get("directory"), str):
        if marker.exists():
            raise OSError(f"malformed transaction marker preserved at {marker}")
        return
    directory = Path(transaction_value["directory"])
    try:
        _restore_transaction(
            home,
            directory,
            committed=transaction_value.get("state") == "committed",
            trusted_roots=trusted_roots,
        )
    except Exception as exc:
        # Never delete evidence after a malformed or partial recovery. The next
        # startup can report the exact transaction path for operator repair.
        raise OSError(f"transaction recovery failed; evidence preserved at {directory}: {exc}") from exc
    marker.unlink()


def transactional_write(
    home: Path,
    updates: dict[Path, tuple[bytes, int]],
    trusted_roots: list[Path] | None = None,
) -> None:
    """Write related settings on their own filesystems with recoverable state.

    ``trusted_roots`` must come from the caller's resolved --home/--config-home
    arguments, never from the transaction marker or manifest on disk: reading
    the trust boundary out of the recovered file would let a damaged marker
    authorize a write anywhere. All records are validated against that
    boundary before any target file is mutated, so a manifest naming a path
    outside the trusted roots (for example an external --config-home fleet
    path that a stale default would have rejected) is caught before any write
    happens rather than being discovered by the post-write restore path,
    which would otherwise leave the just-created prepared marker poisoned.
    """
    recover_transaction(home, trusted_roots)
    transaction = _transaction_dir(home)
    if transaction.exists():
        _restore_transaction(home, transaction, trusted_roots=trusted_roots)
    transaction.mkdir(parents=True, mode=0o700)
    records = []
    for index, path in enumerate(sorted(updates, key=str)):
        existed = os.path.lexists(path)
        record = {"path": str(path), "existed": existed}
        if existed:
            if path.is_symlink() or path.is_dir():
                raise OSError(f"refusing to snapshot symlink or directory: {path}")
            payload = transaction / f"{index:04d}.bin"
            data = path.read_bytes()
            payload.write_bytes(data)
            os.chmod(payload, 0o600)
            record.update({
                "backup": payload.name,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        records.append(record)
    atomic_write(transaction / "manifest.json", json_bytes({"format": TRANSACTION_FORMAT, "state": "prepared", "records": records}), 0o600)
    marker = home / ".claude" / TRANSACTION_MARKER
    atomic_write(marker, json_bytes({"format": TRANSACTION_FORMAT, "state": "prepared", "directory": str(transaction)}), 0o600)
    try:
        # Validate every record against the trusted roots before writing any
        # target. Nothing has been mutated yet, so a rejected transaction can
        # be discarded outright instead of leaving recovery evidence behind.
        _validate_transaction(home, transaction, trusted_roots)
    except Exception:
        _remove_transaction_evidence(transaction)
        marker.unlink(missing_ok=True)
        raise
    try:
        for path, (data, mode) in sorted(updates.items(), key=lambda item: str(item[0])):
            atomic_write(path, data, mode)
        manifest, _ = _validate_transaction(home, transaction, trusted_roots)
        manifest["state"] = "committed"
        atomic_write(transaction / "manifest.json", json_bytes(manifest), 0o600)
        atomic_write(marker, json_bytes({"format": TRANSACTION_FORMAT, "state": "committed", "directory": str(transaction)}), 0o600)
        _remove_transaction_evidence(transaction)
        marker.unlink()
    except BaseException:
        try:
            _restore_transaction(home, transaction, trusted_roots=trusted_roots)
            marker.unlink(missing_ok=True)
        except BaseException as recovery_error:
            raise OSError(f"transaction failed and recovery evidence was preserved at {transaction}: {recovery_error}") from recovery_error
        raise


@contextmanager
def home_lock(home: Path, already_locked: bool = False):
    if already_locked:
        yield True
        return
    fd, path = acquire_lock(home)
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        release_lock(fd, path)


def gateway_credentials(settings: dict) -> tuple[str, str] | None:
    env = settings.get("env")
    if not isinstance(env, dict):
        return None
    endpoint = env.get("ANTHROPIC_BASE_URL")
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    if isinstance(endpoint, str) and endpoint and isinstance(token, str) and token:
        return endpoint, token
    return None


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


def _row_for_picker(model: str, row: dict) -> dict:
    advertised = row.get("advertised_id") or model
    description = row.get("description") or "Gateway model"
    context = row.get("context_length")
    if isinstance(context, int) and context > 0 and "context" not in description.lower():
        description = f"{description} ({context // 1000}K gateway context)"
    return {"model": model, "label": str(advertised), "description": str(description)[:240]}


def rank_live_candidates(
    lane: str,
    config: dict,
    current: str | None,
    approved_live: list[str],
    catalog: dict[str, dict],
    policy: dict,
) -> list[str]:
    if not approved_live:
        return []
    curr_lower = (current or "").lower()
    lane_lower = lane.lower()
    curr_fam = model_family(current, policy) if current else None
    fallback_fams: list[str] = []
    if curr_fam:
        for f in policy.get("families", []):
            if isinstance(f, dict) and f.get("name") == curr_fam:
                fb = f.get("fallbackFamilies")
                if isinstance(fb, list):
                    fallback_fams = [x for x in fb if isinstance(x, str)]
                break

    tokens = [
        "opus", "sonnet", "haiku", "gemini", "flash", "pro",
        "codex", "sol", "luna", "astra", "terra", "grok",
        "free", "auto", "atria", "openclaw"
    ]
    effort = str(config.get("effort", "medium")).lower()
    is_ro = config.get("readOnly") is True

    scored: list[tuple[int, str]] = []
    for cand in approved_live:
        score = 0
        cand_lower = cand.lower()
        cand_fam = model_family(cand, policy)

        for tok in tokens:
            if tok in curr_lower or tok in lane_lower:
                if tok in cand_lower:
                    score += 500

        if curr_fam and cand_fam == curr_fam:
            score += 150
        elif cand_fam in fallback_fams:
            score += 100

        row_info = catalog.get(cand, {})
        ctx = row_info.get("context_length") or 0
        caps = row_info.get("capabilities") if isinstance(row_info.get("capabilities"), dict) else {}
        thinking = bool(caps.get("thinking") or caps.get("reasoning") or caps.get("supportsThinking"))

        if effort in ("xhigh", "max") or is_ro:
            if thinking:
                score += 80
            if ctx >= 800000:
                score += 50
            if any(k in cand_lower for k in ("opus", "sonnet", "pro", "sol")):
                score += 60
        elif effort == "high":
            if thinking:
                score += 40
            if ctx >= 800000:
                score += 40
            if any(k in cand_lower for k in ("sonnet", "flash", "codex", "luna")):
                score += 50
        else:
            if ctx >= 200000:
                score += 30

        if current and current.endswith("[1m]") and cand.endswith("[1m]"):
            score += 20

        scored.append((score, cand))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [cand for _, cand in scored]


def select_live_fallback(
    lane: str,
    config: dict,
    current: str | None,
    approved_live: list[str],
    catalog: dict[str, dict],
    policy: dict,
) -> str | None:
    ranked = rank_live_candidates(lane, config, current, approved_live, catalog, policy)
    return ranked[0] if ranked else None


def resolve_fleet(
    fleet: dict,
    settings: dict,
    rows: list[dict],
    policy: dict,
    *,
    allow_new_families: bool = False,
) -> tuple[dict, dict, list[str], dict[str, dict]]:
    """Resolve live approved candidates while preserving custom lanes/settings."""
    result = copy.deepcopy(fleet)
    result.pop("_reconcile", None)
    catalog = available_rows(rows, policy)
    pending: list[str] = []
    changes: dict[str, dict] = {}
    approved_live = [m for m in catalog if family_approved(m, policy)]
    for lane, config in result.get("lanes", {}).items():
        if not isinstance(config, dict):
            pending.append(f"{lane}: malformed lane configuration")
            continue
        candidates = candidate_list(config)
        for model in candidates:
            if not family_approved(model, policy) and not allow_new_families:
                pending.append(f"{lane}: unapproved model family {model}")
        current = config.get("model")
        if isinstance(current, str) and not family_approved(current, policy) and not allow_new_families:
            # A new family requires an explicit interview; do not silently
            # replace the user's chosen custom lane at startup.
            continue
        eligible, _ = eligible_candidates(config, catalog, policy)
        if not eligible:
            selected = select_live_fallback(lane, config, current, approved_live, catalog, policy)
            if not selected:
                pending.append(f"{lane}: no live approved candidate")
                continue
        else:
            selected = eligible[0]

        if selected != current:
            changes[lane] = {"from": current, "to": selected}
            config["model"] = selected

        fallback_values = [
            model for model in candidates
            if model != selected
            and family_approved(model, policy)
            and model in catalog
        ]
        fallback_values = fallback_values[: MAX_FALLBACK_CANDIDATES - 1]
        if fallback_values:
            config["fallbacks"] = fallback_values
        else:
            config.pop("fallbacks", None)
    picker: dict[str, dict] = {}
    for model, row in catalog.items():
        picker[model] = _row_for_picker(model, row)
    # Only catalog models go in the picker — stale lane fallbacks (models the
    # provider no longer returns) must not be resurrected as picker entries.
    result["_reconcile"] = {"changes": changes, "catalog_models": sorted(catalog)}
    return result, {"options": list(picker.values()), "replaceBuiltInOptions": True}, list(dict.fromkeys(pending)), catalog


def reconcile_settings(
    settings: dict,
    picker: dict,
    fleet: dict | None = None,
    policy: dict | None = None,
) -> dict:
    result = copy.deepcopy(settings)
    current = result.get("modelPicker") if isinstance(result.get("modelPicker"), dict) else {}
    existing = current.get("options") if isinstance(current.get("options"), list) else []
    incoming = picker.get("options") if isinstance(picker.get("options"), list) else []
    incoming_by_model = {row.get("model"): row for row in incoming if isinstance(row, dict) and isinstance(row.get("model"), str)}

    options = []
    seen: set[str] = set()
    for row in existing:
        if not isinstance(row, dict):
            continue
        model = row.get("model")
        if not isinstance(model, str) or not model:
            continue
        if model in incoming_by_model:
            options.append(copy.deepcopy(incoming_by_model[model]))
            seen.add(model)
        else:
            desc = str(row.get("description", "")).lower()
            is_gateway = (
                "gateway context" in desc
                or "gateway model" in desc
                or any(model.startswith(p) for p in GATEWAY_MODEL_PREFIXES)
                or (policy is not None and model_family(model, policy) is not None)
            )
            if not is_gateway:
                options.append(copy.deepcopy(row))
                seen.add(model)
    for model, row in incoming_by_model.items():
        if model not in seen:
            options.append(copy.deepcopy(row))
            seen.add(model)
    result["modelPicker"] = {**current, "options": options, "replaceBuiltInOptions": True}

    # If the user's active model was a gateway model removed from provider, fall back to live model
    active_model = result.get("model")
    if isinstance(active_model, str) and active_model and active_model not in incoming_by_model:
        desc = str(next((r.get("description", "") for r in existing if isinstance(r, dict) and r.get("model") == active_model), "")).lower()
        active_is_gateway = (
            "gateway context" in desc
            or "gateway model" in desc
            or any(active_model.startswith(p) for p in GATEWAY_MODEL_PREFIXES)
            or (policy is not None and model_family(active_model, policy) is not None)
        )
        if active_is_gateway and incoming:
            fallback_active = incoming[0].get("model")
            for inc in incoming:
                m_name = str(inc.get("model", "")).lower()
                if "sonnet" in m_name or "flash" in m_name or "opus" in m_name:
                    fallback_active = inc.get("model")
                    break
            if isinstance(fallback_active, str) and fallback_active:
                result["model"] = fallback_active

    # Check ANTHROPIC_DEFAULT_OPUS_MODEL / ANTHROPIC_DEFAULT_SONNET_MODEL in env
    env = result.get("env")
    if isinstance(env, dict) and incoming_by_model:
        opus_model = env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
        if isinstance(opus_model, str) and opus_model not in incoming_by_model:
            alt_opus = next((m for m in incoming_by_model if "opus" in m.lower()), None)
            if alt_opus:
                env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = alt_opus
        sonnet_model = env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        if isinstance(sonnet_model, str) and sonnet_model not in incoming_by_model:
            alt_sonnet = next((m for m in incoming_by_model if "sonnet" in m.lower()), None)
            if alt_sonnet:
                env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = alt_sonnet

    return result


def reconcile_context(settings: dict, catalog: dict[str, dict]) -> tuple[dict, str | None, int | None]:
    result = copy.deepcopy(settings)
    active = result.get("model")
    if not isinstance(active, str) or active not in catalog:
        return result, None, None
    budget = safe_context(catalog[active].get("context_length"))
    if budget is None:
        return result, None, None
    env = result.get("env")
    if not isinstance(env, dict):
        return result, None, None
    top = compact_control(result.get("autoCompactWindow"))
    env_value = compact_control(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    existing = [value for value in (top, env_value) if value is not None]
    # Never inflate a user-selected lower budget. A pair owned by this bundle
    # may be reduced to the active route's conservative budget, but both controls
    # stay equal so startup cannot choose conflicting limits.
    target = min([budget, *existing]) if existing else budget
    changed = top != target or env_value != target
    if changed:
        result["autoCompactWindow"] = target
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(target)
    if not changed:
        return result, None, target
    return result, f"Verified paired compaction controls for {active}: {target} tokens. Restart Claude Code to apply startup-only limits.", target


def reconcile_home(home: Path, config_home: Path, policy_path: Path, rows: list[dict], *, catalog_valid: bool = True, already_locked: bool = False) -> dict:
    settings_path = home / ".claude" / "settings.json"
    fleet_path = config_home / "delegate-skills" / "config.json"
    # Trusted roots must come from the resolved --home/--config-home argv so a
    # damaged transaction marker can never expand its own write boundary. An
    # external --config-home (a fleet path outside home/.claude) is legitimate
    # and must be trusted explicitly, not merely tolerated by an accident of
    # path resolution.
    trusted_roots = [home.resolve(strict=False), config_home.resolve(strict=False)]
    with home_lock(home, already_locked) as locked:
        if not locked:
            return {"status": "deferred", "pending": ["another fleet writer holds the reconciliation lock"]}
        recover_transaction(home, trusted_roots)
        settings = read_json(settings_path, {})
        fleet = read_json(fleet_path, {})
        if not isinstance(settings, dict) or not isinstance(fleet, dict):
            return {"status": "deferred", "pending": ["settings or fleet configuration is malformed"]}
        try:
            policy = load_policy(policy_path)
            if not catalog_valid:
                return {"status": "offline", "pending": ["provider catalog is stale, unavailable, or incomplete; existing fleet was preserved"], "lanes": len(fleet.get("lanes", {}))}
            lanes = fleet.get("lanes")
            if isinstance(lanes, dict):
                for lane_name, lane_cfg in lanes.items():
                    if isinstance(lane_cfg, dict) and "preferred" not in lane_cfg:
                        if lane_name in CANONICAL_LANE_PREFERRED:
                            lane_cfg["preferred"] = list(CANONICAL_LANE_PREFERRED[lane_name])
                        else:
                            cur = lane_cfg.get("model")
                            fbs = lane_cfg.get("fallbacks") if isinstance(lane_cfg.get("fallbacks"), list) else []
                            lane_cfg["preferred"] = [x for x in ([cur] + fbs) if isinstance(x, str) and x]
            resolved, picker, pending, catalog = resolve_fleet(fleet, settings, rows, policy)
        except CatalogError as exc:
            return {"status": "deferred", "pending": [str(exc)]}
        lane_changes = resolved.get("_reconcile", {}).get("changes", {}) if isinstance(resolved.get("_reconcile"), dict) else {}
        resolved.pop("_reconcile", None)
        new_settings = reconcile_settings(settings, picker, resolved, policy)
        new_settings, context_notice, context_budget = reconcile_context(new_settings, catalog)
        changed = resolved != fleet or new_settings != settings
        if changed:
            updates: dict[Path, tuple[bytes, int]] = {}
            if resolved != fleet:
                updates[fleet_path] = (json_bytes(resolved), 0o644)
            if new_settings != settings:
                updates[settings_path] = (json_bytes(new_settings), 0o600)
            transactional_write(home, updates, trusted_roots)
        changes = {"fleet": resolved != fleet, "settings": new_settings != settings}
        return {
            "status": "applied" if changed else "unchanged",
            "pending": pending,
            "changes": changes,
            "lane_changes": lane_changes,
            "context_budget": context_budget,
            "context_notice": context_notice,
            "lanes": len(resolved.get("lanes", {})),
        }


def summary(result: dict) -> str:
    status = result.get("status", "deferred")
    pending = result.get("pending") if isinstance(result.get("pending"), list) else []
    lane_changes = result.get("lane_changes") if isinstance(result.get("lane_changes"), dict) else {}
    parts = []
    if lane_changes:
        changes_str = ", ".join(f"{lane} ({change['from']} -> {change['to']})" for lane, change in sorted(lane_changes.items()))
        parts.append(f"adjusted {len(lane_changes)} lane(s): {changes_str}")
    if pending:
        shown = ", ".join(str(item) for item in pending[:5])
        if len(pending) > 5:
            shown += f", and {len(pending) - 5} more"
        parts.append(f"pending decisions: {shown}. Run claude-fleet-setup to review or record an explicit provider/proposal decision; startup will not approve it automatically.")
    if parts:
        return f"Fleet reconciliation {status}; " + "; ".join(parts)
    return f"Fleet reconciliation {status}; approved provider models are in sync."


def catalog_input(catalog_path: Path, settings: dict, policy: dict, raw_catalog: bool = False) -> tuple[list[dict], bool, str | None]:
    value = read_json(catalog_path)
    if raw_catalog and isinstance(value, list):
        return value, True, None
    credentials = gateway_credentials(settings)
    if not credentials:
        return [], False, "gateway credentials are unavailable"
    rows = cache_rows(value, credentials[0], credentials[1], int(policy.get("cacheTtlSeconds", 21600)))
    return (rows or [], rows is not None, None if rows is not None else "provider cache is stale or scoped to another endpoint/account")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reconcile an installed Claude fleet without prompting")
    parser.add_argument("--home", required=True)
    parser.add_argument("--config-home", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--session-start", action="store_true")
    parser.add_argument("--lock-held", action="store_true")
    parser.add_argument("--raw-catalog", action="store_true")
    args = parser.parse_args()
    home = Path(args.home).resolve()
    config_home = Path(args.config_home).resolve()
    policy_path = Path(args.policy).resolve()
    settings = read_json(home / ".claude" / "settings.json", {})
    policy = read_json(policy_path, {})
    if not isinstance(settings, dict) or not isinstance(policy, dict):
        print(json.dumps({"status": "deferred", "pending": ["settings or policy is malformed"]}))
        return 0
    rows, valid, error = catalog_input(Path(args.catalog), settings, policy, args.raw_catalog)
    result = reconcile_home(home, config_home, policy_path, rows, catalog_valid=valid, already_locked=args.lock_held)
    if error and error not in result.get("pending", []):
        result.setdefault("pending", []).append(error)
    result["summary"] = summary(result)
    if result.get("context_notice"):
        result["systemMessage"] = result["context_notice"]
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
