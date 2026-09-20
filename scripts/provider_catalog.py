#!/usr/bin/env python3
"""Authoritative provider identity, catalog, cache, and writer-lock helpers.

Provider rows are availability data, not picker labels.  This module is the
single Python resolution boundary used by install-time reconciliation, startup
reconciliation, drift detection, and context updates.  Node discovery invokes
this file in ``--normalize`` mode so its presentation path uses the same rules.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.dont_write_bytecode = True
import time
import urllib.error
import urllib.parse
import urllib.request

CACHE_FORMAT = "claude-agents-config.provider-cache.v1"
POLICY_FORMAT = "provider-policy.v1"
DECISIONS_FORMAT = "provider-policy-decisions.v1"
PROVIDER_NAME = "omniroute"
DEFAULT_CACHE_TTL = 6 * 60 * 60
MAX_FALLBACK_CANDIDATES = 3
MAX_CATALOG_PAGES = 16
MAX_CATALOG_BYTES = 4 * 1024 * 1024
_SUFFIX = re.compile(r"\[\d+[kmKM]?\]\s*$")


class CatalogError(ValueError):
    """Raised when provider data cannot be trusted for reconciliation."""


def load_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def load_policy(path: Path) -> dict:
    value = load_json(path)
    if not isinstance(value, dict) or value.get("version") != POLICY_FORMAT:
        raise CatalogError(f"invalid provider policy: {path}")
    if value.get("provider") != PROVIDER_NAME:
        raise CatalogError(f"provider policy is not for {PROVIDER_NAME}")
    if not isinstance(value.get("families"), list):
        raise CatalogError("provider policy has no family list")
    return value


def policy_with_decisions(policy: dict, decisions: object) -> dict:
    """Apply explicit machine-readable interview decisions without prompting."""
    result = copy.deepcopy(policy)
    if not isinstance(decisions, dict) or decisions.get("format") not in {None, DECISIONS_FORMAT}:
        return result
    approved = decisions.get("approvedFamilies")
    approved_set = {value for value in approved if isinstance(value, str)} if isinstance(approved, list) else set()
    for family in result.get("families", []):
        if not isinstance(family, dict) or not isinstance(family.get("name"), str):
            continue
        if family["name"] in approved_set:
            family["approved"] = True
    selected = decisions.get("fallbacks")
    if isinstance(selected, dict):
        result["fallbacks"] = {
            key: [value for value in values if isinstance(value, str)][:MAX_FALLBACK_CANDIDATES]
            for key, values in selected.items()
            if isinstance(key, str) and isinstance(values, list)
        }
    if decisions.get("allowDiscovery") is True:
        result["discoveryApproved"] = True
    return result


def policy_aliases(policy: dict) -> dict[str, str]:
    aliases = policy.get("runtimeAliases")
    if not isinstance(aliases, dict):
        return {}
    return {
        key: value
        for key, value in aliases.items()
        if isinstance(key, str) and key and isinstance(value, str) and value
    }


def namespace_aliases(policy: dict) -> set[str]:
    values = policy.get("namespaceAliases")
    return {value for value in values if isinstance(value, str) and value} if isinstance(values, list) else set()


def strip_known_suffix(value: str) -> str:
    return _SUFFIX.sub("", value.strip()).strip()


def _has_suffix(value: str) -> bool:
    return bool(_SUFFIX.search(value.strip()))


def _context_suffix(policy: dict) -> str:
    value = policy.get("contextSuffix")
    return value if isinstance(value, str) and re.fullmatch(r"\[\d+[kmKM]?\]", value) else "[1m]"


def _context_threshold(policy: dict) -> int:
    value = policy.get("oneMContextThreshold")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 872000


def effective_model_id(raw_id: object, policy: dict, context: int | None = None) -> str:
    """Resolve one advertised ID without inventing an unapproved namespace.

    A suffix already advertised by the provider is retained only when the row
    has no contradictory context value.  A bare approved ID gets the Claude
    Code capacity suffix only when the provider row explicitly reports enough
    context.  Unknown IDs remain exact; only namespaces and aliases named in
    the policy may collapse or rename them.
    """
    if not isinstance(raw_id, str):
        return ""
    raw = raw_id.strip()
    if not raw:
        return ""
    aliases = policy_aliases(policy)
    had_suffix = _has_suffix(raw)
    base = strip_known_suffix(raw)
    direct = aliases.get(raw)
    if direct:
        resolved = direct
    else:
        candidate = base
        if "/" in candidate:
            namespace, remainder = candidate.split("/", 1)
            if namespace not in namespace_aliases(policy):
                return raw
            candidate = remainder.strip()
        resolved = aliases.get(candidate, candidate)
        if not resolved:
            return raw

    resolved = resolved.strip()
    if not resolved:
        return ""
    resolved_has_suffix = _has_suffix(resolved)
    threshold = _context_threshold(policy)
    if context is not None and isinstance(context, int) and context > 0:
        if context < threshold and resolved_has_suffix:
            resolved = strip_known_suffix(resolved)
        elif context >= threshold and not resolved_has_suffix:
            resolved += _context_suffix(policy)
    elif had_suffix and not resolved_has_suffix:
        # Preserve an exact provider suffix when the provider gave no usable
        # capacity value.  Do not infer a suffix from a display description.
        resolved += _SUFFIX.search(raw).group(0).strip()  # type: ignore[union-attr]
    return resolved


def context_length(row: dict) -> int | None:
    raw = row.get("context_length", row.get("max_input_tokens"))
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _catalog_page(payload: object) -> tuple[list[dict], bool, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CatalogError("provider catalog data is not an array")
    rows: list[dict] = []
    for row in payload["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise CatalogError("provider catalog contains a malformed model row")
        rows.append(row)
    has_more = payload.get("has_more") is True
    cursor = payload.get("last_id") or payload.get("next_cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
        raise CatalogError("provider catalog has an invalid pagination cursor")
    return rows, has_more, cursor.strip() if isinstance(cursor, str) else None


def parse_catalog_payload(payload: object) -> tuple[list[dict], bool]:
    """Validate one provider response and report whether it is complete.

    Empty ``data`` is valid.  A response with ``has_more`` is incomplete even
    when it supplies a cursor; ``fetch_catalog`` is responsible for walking
    that cursor and callers must never destructively reconcile an incomplete
    response.
    """
    rows, has_more, _ = _catalog_page(payload)
    return rows, not has_more


def endpoint_scope(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise CatalogError("provider endpoint is missing")
    parsed = urllib.parse.urlparse(url.strip().rstrip("/"))
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise CatalogError("provider endpoint is malformed")
    if parsed.scheme == "http" and hostname.lower().strip("[]") not in {"localhost", "127.0.0.1", "::1"}:
        raise CatalogError("provider endpoint must use HTTPS outside loopback")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CatalogError("provider endpoint has an invalid port") from exc
    netloc = hostname.lower()
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunparse((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", "", ""))


def account_scope(token: object) -> str:
    if not isinstance(token, str) or not token or any(char in token for char in "\r\n"):
        raise CatalogError("provider credential is missing or malformed")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def now_seconds() -> int:
    return int(time.time())


def cache_payload(rows: list[dict], endpoint: str, token: str, fetched_at: int | None = None) -> dict:
    if not isinstance(rows, list):
        raise CatalogError("cache rows must be a list")
    # Validate before writing, including successful empty catalogs.
    parse_catalog_payload({"data": rows})
    return {
        "format": CACHE_FORMAT,
        "provider": PROVIDER_NAME,
        "endpoint": endpoint_scope(endpoint),
        "account": account_scope(token),
        "fetched_at": int(fetched_at if fetched_at is not None else now_seconds()),
        "models": rows,
    }


def cache_rows(value: object, endpoint: str, token: str, ttl: int = DEFAULT_CACHE_TTL, now: int | None = None) -> list[dict] | None:
    """Return validated rows, including ``[]`` for a fresh empty catalog."""
    if not isinstance(value, dict):
        return None
    if value.get("format") != CACHE_FORMAT or value.get("provider") != PROVIDER_NAME:
        return None
    try:
        expected_endpoint = endpoint_scope(endpoint)
        expected_account = account_scope(token)
        fetched_at = int(value.get("fetched_at"))
    except (CatalogError, TypeError, ValueError):
        return None
    if value.get("endpoint") != expected_endpoint or value.get("account") != expected_account:
        return None
    current = now_seconds() if now is None else now
    if fetched_at < 0 or fetched_at > current + 60:
        return None
    if not isinstance(ttl, int) or ttl < 0 or current - fetched_at > ttl:
        return None
    models = value.get("models")
    if not isinstance(models, list):
        return None
    try:
        rows, complete = parse_catalog_payload({"data": models})
    except CatalogError:
        return None
    return rows if complete else None


def fetch_catalog(endpoint: str, token: str, timeout: float = 2.5) -> tuple[list[dict], bool, str | None]:
    """Fetch bounded pages without exposing the credential.

    ``complete`` is false for a missing cursor, malformed pagination, timeout,
    or any failed page.  The caller may retain an older cache in that case.
    """
    base = endpoint_scope(endpoint)
    rows: list[dict] = []
    cursor: str | None = None
    deadline = time.monotonic() + max(0.1, float(timeout))
    for _ in range(MAX_CATALOG_PAGES):
        query = {"limit": "1000"}
        if cursor:
            query["after"] = cursor
        url = base.rstrip("/") + "/v1/models?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={
            "x-api-key": token,
            "authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
        })
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], False, "provider catalog timed out"
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                body = response.read(MAX_CATALOG_BYTES + 1)
            if len(body) > MAX_CATALOG_BYTES:
                return [], False, "provider catalog response is too large"
            payload = json.loads(body.decode("utf-8"))
            page, has_more, next_cursor = _catalog_page(payload)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except OSError:
                pass
            return [], False, f"provider returned HTTP {exc.code}"
        except (OSError, UnicodeError, json.JSONDecodeError, CatalogError, ValueError) as exc:
            return [], False, f"provider catalog unavailable: {type(exc).__name__}"
        rows.extend(page)
        if not has_more:
            return rows, True, None
        if not next_cursor:
            return [], False, "provider catalog pagination is incomplete"
        cursor = next_cursor
    return [], False, "provider catalog exceeded the page limit"


def available_rows(rows: list[dict], policy: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id")
        context = context_length(row)
        runtime_id = effective_model_id(raw_id, policy, context)
        if not runtime_id:
            continue
        # The first exact provider row wins a namespace collision. Raw identity
        # remains available for diagnostics without leaking credentials.
        result.setdefault(runtime_id, {
            "id": runtime_id,
            "advertised_id": str(raw_id),
            "context_length": context,
            "description": str(row.get("description") or "")[:160],
        })
    return result


def normalized_rows(rows: object, policy: dict) -> list[dict]:
    """Return stable normalized rows for Node presentation and tests."""
    if not isinstance(rows, list):
        raise CatalogError("catalog rows must be an array")
    result = available_rows(rows, policy)
    return list(result.values())


def model_family(model: object, policy: dict) -> str | None:
    if not isinstance(model, str) or not model.strip():
        return None
    base = strip_known_suffix(model.strip())
    if "/" in base:
        namespace, candidate = base.split("/", 1)
        if namespace in namespace_aliases(policy):
            base = candidate
    for family in policy.get("families", []):
        if not isinstance(family, dict):
            continue
        name = family.get("name")
        exact = family.get("exact") if isinstance(family.get("exact"), list) else []
        prefixes = family.get("prefixes") if isinstance(family.get("prefixes"), list) else []
        if isinstance(name, str) and (base in exact or any(isinstance(prefix, str) and base.startswith(prefix) for prefix in prefixes)):
            return name
    return None


def family_approved(model: object, policy: dict) -> bool:
    family_name = model_family(model, policy)
    approved_families = policy.get("approvedFamilies")
    if isinstance(approved_families, list) and family_name in approved_families:
        return True
    return any(
        isinstance(family, dict) and family.get("name") == family_name and family.get("approved") is True
        for family in policy.get("families", [])
    )


def candidate_list(config: object) -> list[str]:
    if not isinstance(config, dict):
        return []
    values = [config.get("model")]
    fallbacks = config.get("fallbacks")
    if isinstance(fallbacks, list):
        values.extend(fallbacks)
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def eligible_candidates(config: object, available: dict[str, dict], policy: dict) -> tuple[list[str], list[str]]:
    """Return live approved candidates and candidates needing a decision."""
    candidates = candidate_list(config)[:MAX_FALLBACK_CANDIDATES]
    eligible = [model for model in candidates if family_approved(model, policy) and model in available]
    pending = [model for model in candidates if not family_approved(model, policy)]
    return eligible, pending


def lock_path(home: Path) -> Path:
    """Path shared by installer, Node hooks, context updates, reconcile, and sync."""
    try:
        canonical = str(home.resolve(strict=True))
    except OSError:
        canonical = str(home.resolve(strict=False))
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"claude-agents-config-{key}.lock"


def acquire_lock(home: Path, timeout: float | None = 4.0):
    """Acquire the portable O_EXCL writer lock.

    A bounded timeout is used by hooks so a contended startup remains usable;
    ``None`` blocks for installer operations. Stale recovery is conservative.
    """
    path = lock_path(home)
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd, path
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 60:
                    path.unlink()
                    continue
            except OSError:
                pass
            if deadline is not None and time.monotonic() >= deadline:
                return None, path
            time.sleep(0.05)


def release_lock(fd: int | None, path: Path) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        path.unlink()
    except OSError:
        pass


def _main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Provider catalog helper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--normalize", action="store_true")
    mode.add_argument("--fetch", action="store_true")
    mode.add_argument("--cache-read", action="store_true")
    mode.add_argument("--cache-payload", action="store_true")
    parser.add_argument("--policy", required=False)
    parser.add_argument("--endpoint")
    parser.add_argument("--ttl", type=int, default=DEFAULT_CACHE_TTL)
    parser.add_argument("--timeout", type=float, default=2.5)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SystemExit("catalog input must be valid JSON")
    if args.normalize:
        if not args.policy:
            parser.error("--normalize requires --policy")
        policy = load_policy(Path(args.policy).resolve())
        print(json.dumps(normalized_rows(payload, policy), ensure_ascii=False, separators=(",", ":")))
        return 0
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
        raise SystemExit("catalog input must contain a token")
    endpoint = args.endpoint or payload.get("endpoint")
    if not isinstance(endpoint, str):
        raise SystemExit("catalog operation requires an endpoint")
    if args.fetch:
        rows, complete, error = fetch_catalog(endpoint, payload["token"], args.timeout)
        result = {"rows": rows, "complete": complete, "error": error}
        if complete:
            result["endpoint"] = endpoint_scope(endpoint)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if complete else 1
    if args.cache_read:
        rows = cache_rows(payload.get("cache"), endpoint, payload["token"], args.ttl)
        print(json.dumps({"valid": rows is not None, "rows": rows or []}, ensure_ascii=False, separators=(",", ":")))
        return 0 if rows is not None else 1
    if not isinstance(payload.get("rows"), list):
        raise SystemExit("--cache-payload input must contain rows")
    result = cache_payload(payload["rows"], endpoint, payload["token"])
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
