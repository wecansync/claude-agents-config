#!/usr/bin/env python3
"""Authoritative provider identity, catalog, cache, and writer-lock helpers.

Provider rows are availability data, not picker labels.  This module is the
single Python resolution boundary used by install-time reconciliation, startup
reconciliation, drift detection, and context updates.  Node discovery invokes
this file in ``--normalize`` mode so its presentation path uses the same rules.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys

sys.dont_write_bytecode = True
import time
import urllib.error
import urllib.parse
import urllib.request

# Windows pipes and consoles default to a legacy encoding while provider
# payloads routinely contain non-ASCII text; emit stdout as UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CACHE_FORMAT = "claude-agents-config.provider-cache.v1"
POLICY_FORMAT = "provider-policy.v1"
# Default provider label for a freshly installed generic gateway. The policy's
# own "provider" value is authoritative once installed; it is a label only and
# never gates which endpoint may be used (endpoint/account scoping does that).
PROVIDER_NAME = "gateway"
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


def validate_policy(value: object) -> dict:
    """Raise CatalogError unless ``value`` is a well-formed provider policy."""
    if not isinstance(value, dict) or value.get("version") != POLICY_FORMAT:
        raise CatalogError("invalid provider policy")
    provider = value.get("provider")
    if not isinstance(provider, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", provider):
        raise CatalogError("provider policy has an invalid provider label")
    if not isinstance(value.get("families"), list):
        raise CatalogError("provider policy has no family list")
    return value


def load_policy(path: Path) -> dict:
    value = load_json(path)
    try:
        return validate_policy(value)
    except CatalogError:
        raise CatalogError(f"invalid provider policy: {path}") from None


def generic_policy(provider: str = PROVIDER_NAME) -> dict:
    """The shipped catch-all policy (every model approved), labeled for the
    given provider. Matches config/provider-policy.json apart from the label."""
    label = provider if isinstance(provider, str) and provider else PROVIDER_NAME
    return {
        "version": POLICY_FORMAT,
        "provider": label,
        "cacheTtlSeconds": 21600,
        "oneMContextThreshold": 872000,
        "contextSuffix": "[1m]",
        "discoveryApproved": False,
        "autoApproveProposals": True,
        "namespaceAliases": [],
        "runtimeAliases": {},
        "modelTiers": {},
        "families": [
            {
                "name": "gateway",
                "prefixes": [""],
                "approved": True,
                "fallbackFamilies": [],
            }
        ],
    }


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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def endpoint_identity(url: object) -> str:
    """``scheme://host:port`` with the default port filled in and any path
    ignored. Two URLs that resolve to the same endpoint_identity are the same
    provider account boundary regardless of path (e.g. differing API bases
    on one host); a different port is a different provider."""
    scoped = endpoint_scope(url)
    parsed = urllib.parse.urlparse(scoped)
    hostname = parsed.hostname or ""
    netloc = hostname.lower()
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    port = parsed.port if parsed.port is not None else _DEFAULT_PORTS.get(parsed.scheme)
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunparse((parsed.scheme, netloc, "", "", "", ""))


def account_scope(token: object) -> str:
    if not isinstance(token, str) or not token or any(char in token for char in "\r\n"):
        raise CatalogError("provider credential is missing or malformed")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def now_seconds() -> int:
    return int(time.time())


def cache_payload(rows: list[dict], endpoint: str, token: str, fetched_at: int | None = None, provider: str = PROVIDER_NAME) -> dict:
    if not isinstance(rows, list):
        raise CatalogError("cache rows must be a list")
    # Validate before writing, including successful empty catalogs.
    parse_catalog_payload({"data": rows})
    return {
        "format": CACHE_FORMAT,
        "provider": provider if isinstance(provider, str) and provider else PROVIDER_NAME,
        "endpoint": endpoint_scope(endpoint),
        "account": account_scope(token),
        "fetched_at": int(fetched_at if fetched_at is not None else now_seconds()),
        "models": rows,
    }


def cache_rows(value: object, endpoint: str, token: str, ttl: int = DEFAULT_CACHE_TTL, now: int | None = None) -> list[dict] | None:
    """Return validated rows, including ``[]`` for a fresh empty catalog."""
    if not isinstance(value, dict):
        return None
    # The provider label is informational; endpoint and account scoping below
    # are what bind a cache to the credentials that fetched it.
    if value.get("format") != CACHE_FORMAT or not isinstance(value.get("provider"), str):
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


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the same endpoint (scheme, host, and
    port). urllib copies every request header, credentials included, onto
    the redirect target, which may be another host or plain HTTP; any other
    redirect surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if _origin(newurl) is not None and _origin(newurl) == _origin(req.full_url):
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        return None


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or _DEFAULT_PORTS.get(parsed.scheme.lower())
    except ValueError:
        return None


def _credential_opener(url: str) -> urllib.request.OpenerDirector:
    """An opener for requests that carry the provider credential: no
    redirects, and no proxy for a loopback gateway (urllib does not bypass
    proxies for localhost by itself, and that hop is plain HTTP)."""
    handlers: list[urllib.request.BaseHandler] = [_RefuseRedirect()]
    if (urllib.parse.urlparse(url).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


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
            # Anthropic's Models API pages with after_id; some gateways use
            # after. Sending both is harmless to either.
            query["after_id"] = cursor
            query["after"] = cursor
        url = base.rstrip("/") + "/v1/models?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={
            "x-api-key": token,
            "authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
            "user-agent": "claude-agents-config/1.0.0",
        })
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], False, "provider catalog timed out"
        try:
            with _credential_opener(url).open(request, timeout=remaining) as response:
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
            if 300 <= exc.code < 400:
                return [], False, f"provider redirected to another endpoint (HTTP {exc.code}); use the final gateway URL, such redirects are not followed"
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


# Capability tiers describe what a lane needs, never which vendor serves it.
# Catalogs report context size and capability flags but no price or latency,
# so a model's tier comes from, in order: an explicit label (the setup wizard
# writes these), keywords in the model id, keywords in the catalog
# description, and finally "balanced".
TIERS = ("cheap", "fast", "balanced", "deep")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
TIER_ID_KEYWORDS = (
    ("cheap", ("free", "cheap", "budget", "auto")),
    # "turbo" is deliberately absent: gpt-4-turbo is a strong model.
    ("fast", ("haiku", "flash", "mini", "lite", "nano", "fast", "small", "instant")),
    ("deep", ("opus", "pro", "max", "ultra", "large", "sol", "astra", "o1", "o3", "reasoner")),
)
TIER_DESCRIPTION_KEYWORDS = (
    ("cheap", ("free", "cheapest", "budget", "low cost", "low-cost")),
    ("fast", ("fastest", "low latency", "low-latency", "quick", "fast")),
    ("deep", ("strongest", "deep reasoning", "deeper reasoning", "frontier", "most capable")),
)
# Native Claude Code aliases resolve against the user's own login (subscription
# or API key), so a direct profile never needs catalog discovery.
NATIVE_TIER_MODELS = {"deep": "opus", "balanced": "sonnet", "fast": "haiku", "cheap": "haiku"}
NATIVE_MODELS = ("default", "opus", "sonnet", "haiku")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def supports_reasoning(row: object) -> bool:
    """Read a reasoning/thinking flag from either catalog capability shape.

    Gateways commonly report ``{"thinking": true}``; Anthropic reports
    ``{"thinking": {"supported": true}}``.
    """
    caps = row.get("capabilities") if isinstance(row, dict) else None
    if isinstance(row, dict) and isinstance(row.get("reasoning"), bool) and caps is None:
        return row["reasoning"]
    if not isinstance(caps, dict):
        return False
    for key in ("thinking", "reasoning", "supportsThinking"):
        value = caps.get(key)
        if value is True or (isinstance(value, dict) and value.get("supported") is True):
            return True
    return False


def tier_overrides(*sources: object) -> dict[str, str]:
    """Merge ``modelTiers`` maps; later sources win. Invalid entries are ignored."""
    merged: dict[str, str] = {}
    for source in sources:
        values = source.get("modelTiers") if isinstance(source, dict) else None
        if not isinstance(values, dict):
            continue
        for key, tier in values.items():
            if isinstance(key, str) and key.strip() and tier in TIER_RANK:
                merged[key.strip()] = tier
    return merged


def model_tier(model: object, row: object = None, overrides: dict[str, str] | None = None) -> str:
    if not isinstance(model, str) or not model.strip():
        return "balanced"
    model = model.strip()
    base = strip_known_suffix(model)
    if overrides:
        for key in (model, base):
            if overrides.get(key) in TIER_RANK:
                return overrides[key]
    tokens = [token for token in _TOKEN_SPLIT.split(base.lower().rsplit("/", 1)[-1]) if token]
    for tier, keywords in TIER_ID_KEYWORDS:
        for keyword in keywords:
            if keyword in tokens or (len(keyword) >= 5 and any(keyword in token for token in tokens)):
                return tier
    description = str(row.get("description") or "").lower() if isinstance(row, dict) else ""
    for tier, keywords in TIER_DESCRIPTION_KEYWORDS:
        if any(keyword in description for keyword in keywords):
            return tier
    return "balanced"


def native_lane_model(tier: object, avoid: object = None) -> str:
    """Pick the native alias for a lane tier, stepping to a neighbour when the
    natural choice must be avoided (an alternate's sibling, or excluded
    models). ``avoid`` is one alias or a collection of them."""
    blocked = {avoid} if isinstance(avoid, str) else {item for item in avoid or () if isinstance(item, str)}
    chosen = NATIVE_TIER_MODELS.get(tier if isinstance(tier, str) else "", "sonnet")
    if chosen in blocked:
        for alternative in ("sonnet", "opus", "haiku"):
            if alternative not in blocked:
                return alternative
    return chosen


def excluded_models(fleet: object) -> set[str]:
    """Models the user excluded from every lane, suffix-insensitive."""
    values = fleet.get("excludedModels") if isinstance(fleet, dict) else None
    return {strip_known_suffix(item) for item in values or () if isinstance(item, str) and item.strip()}


def is_excluded(model: object, excluded: set[str]) -> bool:
    return isinstance(model, str) and strip_known_suffix(model) in excluded


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
            "reasoning": supports_reasoning(row),
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


def model_family_specific(model: object, policy: dict) -> str | None:
    """Like model_family, but ignores catch-all families (an empty prefix).

    A generic gateway policy approves every model through one catch-all
    family, which says nothing about whether a picker row came from the
    provider or was added by hand, so it must not be used for that decision.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    base = strip_known_suffix(model.strip())
    if "/" in base:
        namespace, candidate = base.split("/", 1)
        if namespace in namespace_aliases(policy):
            base = candidate
    for family in policy.get("families", []):
        if not isinstance(family, dict) or not isinstance(family.get("name"), str):
            continue
        exact = family.get("exact") if isinstance(family.get("exact"), list) else []
        prefixes = [value for value in (family.get("prefixes") or []) if isinstance(value, str) and value]
        if base in exact or any(base.startswith(prefix) for prefix in prefixes):
            return family["name"]
    return None


def family_approved(model: object, policy: dict) -> bool:
    # An explicit approvedFamilies list (recorded by claude-fleet-setup, even
    # when empty) is authoritative and must never be OR'd with the shipped
    # per-family "approved" defaults: those defaults ship as true for every
    # family, so falling back to them here would silently re-approve a
    # family the user explicitly revoked. The per-family "approved" flag is
    # consulted only before any explicit decision has ever been recorded.
    family_name = model_family(model, policy)
    approved_families = policy.get("approvedFamilies")
    if "approvedFamilies" in policy:
        return (
            isinstance(approved_families, list)
            and all(isinstance(name, str) for name in approved_families)
            and family_name is not None
            and family_name in approved_families
        )
    return any(
        isinstance(family, dict) and family.get("name") == family_name and family.get("approved") is True
        for family in policy.get("families", [])
    )


def candidate_list(config: object) -> list[str]:
    if not isinstance(config, dict):
        return []
    values: list[str] = []
    preferred = config.get("preferred")
    if isinstance(preferred, list):
        for val in preferred:
            if isinstance(val, str) and val.strip():
                values.append(val.strip())
    primary = config.get("model")
    if isinstance(primary, str) and primary.strip() and primary.strip() not in values:
        values.append(primary.strip())
    fallbacks = config.get("fallbacks")
    if isinstance(fallbacks, list):
        for fb in fallbacks:
            if isinstance(fb, str) and fb.strip() and fb.strip() not in values:
                values.append(fb.strip())
    return list(dict.fromkeys(values))


def eligible_candidates(config: object, available: dict[str, dict], policy: dict) -> tuple[list[str], list[str]]:
    """Return live approved candidates and candidates needing a decision."""
    candidates = candidate_list(config)
    eligible = [model for model in candidates if family_approved(model, policy) and model in available]
    pending = [model for model in candidates if not family_approved(model, policy)]
    return eligible, pending


LOCK_STALE_SECONDS = 60


def lock_path(home: Path) -> Path:
    """Path shared by installer, Node hooks, context updates, reconcile, and sync.

    The lock lives under the resolved home itself rather than the system temp
    directory. ``tempfile.gettempdir()`` / Node's ``os.tmpdir()`` honor
    ``TMPDIR``/``TMP``/``TEMP``, which two writers targeting the very same
    home can have set differently (distinct sandboxes, distinct shells); that
    let two "different" lock files serialize nothing. A location scoped
    inside home needs no per-home hash or environment agreement: every writer
    that resolves the same home lands on the same file. This function never
    creates anything; it is a pure path computation so it stays safe to call
    for diagnostics or dry-run reporting.
    """
    try:
        resolved = home.resolve(strict=True)
    except OSError:
        resolved = home.resolve(strict=False)
    return resolved / ".claude" / ".claude-agents-config.lock"


def _ensure_no_symlink_parent(path: Path) -> None:
    """Refuse to create a lock through a symlinked ancestor directory."""
    current = path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    while True:
        if current.is_symlink():
            raise OSError(f"refusing to lock through symlinked directory: {current}")
        if current == current.parent:
            return
        current = current.parent


def _process_alive(pid: int) -> bool:
    """Conservative liveness check: unknown or unparseable pids count as alive.

    A false "alive" merely leaves a genuinely stale lock in place a little
    longer (bounded by an operator or a later run once the pid is gone); a
    false "dead" would let a second writer through, mutations races that this
    lock exists to prevent. Windows lacks POSIX signal-0 semantics, so it is
    treated as always alive; orphaned locks there require operator cleanup.
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _lock_owner_pid(path: Path) -> int:
    try:
        content = path.read_text(encoding="ascii", errors="strict")
    except (OSError, UnicodeError):
        return -1
    try:
        return int(content.split(":", 1)[0].strip())
    except ValueError:
        return -1


def acquire_lock(home: Path, timeout: float | None = 4.0, create_parent: bool = False):
    """Acquire the portable O_EXCL writer lock.

    A bounded timeout is used by hooks so a contended startup remains usable;
    ``None`` blocks for installer operations. ``create_parent`` must only be
    set by the installer's apply path (after its own symlink preflight),
    which may be racing to create ``home/.claude`` for the very first time;
    every other caller (hooks, context updates, reconcile, setup) treats a
    missing home or a missing ``.claude`` directory as "cannot lock right
    now" and defers rather than fabricating the directory, so a not-yet or
    no-longer installed home never gets written into by a background writer.
    Eviction of a contended lock requires both an age past
    ``LOCK_STALE_SECONDS`` and a dead owning pid, so a live long-running
    installer is never evicted merely for running past the age bound.
    """
    path = lock_path(home)
    if create_parent:
        try:
            _ensure_no_symlink_parent(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return None, path
    elif not path.parent.is_dir():
        return None, path
    identity = f"{os.getpid()}:{secrets.token_hex(8)}".encode("ascii")
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if path.is_symlink():
                return None, path
        except OSError:
            return None, path
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, identity)
            return fd, path
        except FileExistsError:
            try:
                if not path.is_symlink():
                    stale_age = time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS
                    if stale_age and not _process_alive(_lock_owner_pid(path)):
                        path.unlink()
                        continue
            except OSError:
                pass
            if deadline is not None and time.monotonic() >= deadline:
                return None, path
            time.sleep(0.05)
        except OSError:
            return None, path


def release_lock(fd: int | None, path: Path) -> None:
    """Release a lock acquired by this process, deleting only if still ours.

    A lock reclaimed as stale and recreated by another writer while this
    process held the fd open must never be deleted here: the fd's own
    content (read back through the same descriptor, which still points at
    the original inode even if the directory entry was replaced) is compared
    against whatever currently sits at ``path`` before unlinking anything.
    """
    if fd is None:
        return
    owned_identity = None
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        owned_identity = os.read(fd, 128)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    if not owned_identity:
        return
    try:
        if path.is_symlink():
            return
        current = path.read_bytes()
    except OSError:
        return
    if current == owned_identity:
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
