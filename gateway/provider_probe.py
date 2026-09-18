"""Generic provider probe engine (task-7 unified provider architecture).

Replaces the hardcoded "every provider is OpenAI-compatible, has GET {base}/models,
and answers to the Bearer header" assumption with a data-driven contract:

  - ``build_provider_probe_request(entry)`` derives an HTTP request from the provider
    config's ``probe:`` block (or OpenAI-style defaults via {base}/models + Bearer).
  - ``probe_provider_entry(entry)`` executes it safely and classifies the outcome:
    SUCCESS | AUTH | QUOTA | NOT_FOUND | PROVIDER_ERROR | TIMEOUT | NETWORK |
    MALFORMED | NO_KEY.

Security posture (fail-closed):
  * Only https:// endpoints. No file:///gopher://, no userinfo URLs, no credential
    substitution inside URLs, and no redirect chain that ever leaves the provider's
    origin (scheme+host+port). ``follow_redirects`` with per-hop origin validation.
  * No client-side URL templating ever receives a secret (secrets are inlined into
    outgoing request headers by httpx, never logged).
  * Hostname-level IP allowlisting still applies upstream; here we only ensure no
    redirect reaches an http:// hop and that redirect targets stay same-origin.
  * The provider's marketing ``website`` field is metadata: it is NEVER fetched.

Tests mock the transport layer; the engine never calls a real provider in tests.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification taxonomy
# ---------------------------------------------------------------------------

SUCCESS = "SUCCESS"
AUTH = "AUTH"            # credentials rejected (401/403)
QUOTA = "QUOTA"          # rate-limit (429 / explicit quota shapes)
NOT_FOUND = "NOT_FOUND"  # endpoint missing (404) => feature not supported by provider
PROVIDER_ERROR = "PROVIDER_ERROR"  # 5xx or semantic error in a completed request
TIMEOUT = "TIMEOUT"      # network timeout -> show as connectivity, fail-soft
NETWORK = "NETWORK"      # DNS/connect/TLS issues
MALFORMED = "MALFORMED"  # garbage body/JSON/HTML verdict; success contract unsatisfied
NO_KEY = "NO_KEY"        # auth can't even be attempted (missing secret for non-"none" auth)

QUOTA_STATUSES = (429,)
AUTH_STATUSES = (401, 403)
PROVIDER_ERROR_STATUSES = range(500, 600)

_CATEGORY_FOR = {
    SUCCESS: "success",
    AUTH: "auth",
    QUOTA: "quota",
    NOT_FOUND: "not_found",
    PROVIDER_ERROR: "provider_error",
    TIMEOUT: "timeout",
    NETWORK: "network",
    MALFORMED: "malformed",
    NO_KEY: "no_key",
}
# All categories are STRING categories (stable for UI + tests); the verdict object
# itself is a small immutably-shaped namespace.

_PROBE_TIMEOUT = 12.0
_MAX_REDIRECTS = 5
_MAX_BODY_FOR_SUCCESS = 131_072  # only read what's needed for classification
_INFERENCE_CAPABILITIES = frozenset({"chat", "completion", "embeddings"})


def category_of(verdict: str) -> str:
    return _CATEGORY_FOR.get(str(verdict or "").upper(), "provider_error")


def _caps(entry: Dict[str, Any]) -> frozenset:
    caps = entry.get("provider_capabilities")
    if isinstance(caps, (list, tuple, set, frozenset)):
        return frozenset(str(c).strip().lower() for c in caps if str(c).strip())
    return frozenset()


def _normalized_auth(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults keyed on declared structure; legacy entry ⇒ legacy bearer assumption."""
    auth = entry.get("auth")
    if isinstance(auth, dict) and auth.get("type"):
        return dict(auth)
    # api_key == present → Bearer default; explicit key_env-only → Bearer too (legacy).
    return {"type": "bearer"}


def _has_secret(entry: Dict[str, Any]) -> bool:
    env_name = str(entry.get("key_env") or "").strip()
    return bool(
        str(entry.get("api_key") or "").strip()
        or (os.environ.get(env_name, "").strip() if env_name else "")
        or entry.get("_probe_secret")
    )


def _auth_headers(entry: Dict[str, Any]) -> Dict[str, str]:
    auth = _normalized_auth(entry)
    secret = str(entry.get("_probe_secret") or "").strip() or str(entry.get("api_key") or "").strip()
    if not secret:
        env = str(entry.get("key_env") or "").strip()
        if env:
            secret = str(os.environ.get(env) or "").strip()
    if not secret:
        return {}
    atype = str(auth.get("type") or "bearer").strip().lower()
    if atype == "none":
        return {}
    if atype == "api_key_header":
        header = str(auth.get("header") or "X-API-Key").strip() or "X-API-Key"
        return {header: secret}
    if atype == "native":
        # Provider-native pair: the runtime owns signing; the probe has no opinion.
        return {}
    # default: bearer (with an optional explicit prefix like "Token ").
    prefix = str(auth.get("prefix") or "").strip()
    return {"Authorization": f"{prefix}{secret}" if prefix else f"Bearer {secret}"}


def _origin(url: str) -> Tuple[str, str, int]:
    parts = urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80))


def validate_probe_url(url: Any) -> Optional[Tuple[bool, str]]:
    """Return (ok, reason). Hard refuse anything that modern SSRF hygiene wouldn't allow."""
    if not isinstance(url, str) or not url.strip():
        return (False, "missing")
    url = url.strip()
    if "@" in url:
        return (False, "userinfo")
    parts = urlsplit(url)
    if parts.scheme != "https":
        return (False, "scheme")
    if not parts.hostname:
        return (False, "host")
    if "${" in url or "`" in url:
        return (False, "substitution")
    return None  # all good


def _probe_block(entry: Dict[str, Any]) -> Dict[str, Any]:
    probe = entry.get("probe")
    return dict(probe) if isinstance(probe, dict) else {}


def build_provider_probe_request(entry: Dict[str, Any]) -> Tuple[str, str, Dict[str, str], Optional[str], Optional[str]]:
    """(method, url, headers, body, rejection_reason). Pure; no I/O."""
    base_url = str(entry.get("base_url") or entry.get("api") or entry.get("url") or "").strip().rstrip("/")
    verdict_bad = validate_probe_url(base_url)
    if verdict_bad is not None:
        return ("GET", base_url or "https://(missing)", {}, None, f"base_url_rejected:{verdict_bad}")

    auth = _normalized_auth(entry)
    if str(auth.get("type") or "bearer").lower() != "none" and not _has_secret(entry):
        return ("GET", base_url, {}, None, "no_key")

    auth_headers = _auth_headers(entry)
    block = _probe_block(entry)

    if block.get("path"):
        # Host-relative path against the base ORIGIN — /api/v3/... + base .../api/v4 resolves
        # to <scheme>://<host>/api/v3/... (api-version paths may differ on the same origin).
        path = str(block.get("path")).strip()
        if not path.startswith("/"):
            return ("GET", base_url, {}, None, "path_rejected:not_relative")
        parts = urlsplit(base_url)
        query, _, _frag = path.partition("#")
        rel_path, _, q = query.partition("?")
        url = urlunsplit((parts.scheme, parts.netloc, rel_path, q, ""))
    else:
        # OpenAI-style legacy default: {base}/models.
        url = urljoin(base_url + "/", "models")

    method = str(block.get("method") or "GET").strip().upper()
    if method not in ("GET", "POST", "HEAD"):
        method = "GET"
    headers = dict(auth_headers)
    extra = block.get("headers")
    if isinstance(extra, str) and extra.strip():
        try:
            parsed = json.loads(extra)
            if isinstance(parsed, dict):
                extra = parsed
        except Exception:
            extra = None
        else:
            extra = {str(k): str(v) for k, v in extra.items()} if isinstance(extra, dict) else None
    if isinstance(extra, dict):
        for k, v in extra.items():
            k, v = str(k).strip(), str(v).strip()
            if k and v and k.lower() not in {h.lower() for h in headers}:
                headers[k] = v
    body = block.get("body")
    if isinstance(body, (dict, list, bool, int, float)):
        body = json.dumps(body)
    elif body is not None and not isinstance(body, str):
        body = None
    return (method, url, headers, body, None)


def classify_status(status: int, body_excerpt: str = "") -> str:
    if 200 <= status < 300:
        return SUCCESS
    if status in QUOTA_STATUSES:
        return QUOTA
    if status in AUTH_STATUSES:
        return AUTH
    if status == 404:
        return NOT_FOUND
    if status in PROVIDER_ERROR_STATUSES:
        return PROVIDER_ERROR
    # Anything else (3xx leak, 4xx oddities): treat as a provider-side rejection.
    return PROVIDER_ERROR


def _looks_like_json_models(body_text: str) -> bool:
    try:
        data = json.loads(body_text)
    except Exception:
        return False
    if isinstance(data, dict):
        return isinstance(data.get("data"), list) or bool(
            data.get("projectId") or data.get("credits") is not None or data.get("rateLimit") is not None
        )
    return isinstance(data, list)


_QUOTA_BODY_HINTS = ("quota exceeded", "insufficient_quota", "rate_limit", "too many requests")


def _body_quota_hint(body_text: str) -> bool:
    return any(hint in body_text.lower() for hint in _QUOTA_BODY_HINTS)


def apply_success_contract(verdict: str, body_text: str, entry: Dict[str, Any]) -> str:
    """A bare 200 doesn't prove anything. JSON contract must be present; otherwise MALFORMED."""
    if verdict != SUCCESS:
        return verdict
    touched = body_text.strip()
    if not touched:
        return MALFORMED
    if _body_quota_hint(touched):
        return QUOTA
    if touched[:1] not in "[{" or not _looks_like_json_models(touched):
        return MALFORMED
    return SUCCESS


def _bounded_json_models_parse(text: str) -> List[str]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    ids: List[str] = []
    source = data.get("data") if isinstance(data, dict) else data
    if isinstance(source, list):
        for row in source:
            model_id = row.get("id") if isinstance(row, dict) else row
            if isinstance(model_id, str) and model_id.strip():
                ids.append(model_id.strip())
    return ids


async def _run_probe(
    method: str, url: str, headers: Dict[str, str], body: Optional[str], origin,
    *, transport=None,
) -> Tuple[str, int, str]:
    """One guarded request. Redirect payloads re-validated per hop (same origin, https)."""
    seen = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(_PROBE_TIMEOUT), follow_redirects=False, transport=transport) as client:
        current_url, current_method, current_body = url, method, body
        while True:
            resp = await client.request(current_method, current_url, headers=headers,
                                        content=current_body.encode("utf-8", "replace") if current_body else None)
            if resp.is_redirect:
                seen += 1
                if seen > _MAX_REDIRECTS:
                    return (MALFORMED, 0, "")
                target = str(resp.headers.get("location") or "")
                if not target:
                    return (MALFORMED, resp.status_code, "")
                next_url = urljoin(current_url, target)
                verdict_bad = validate_probe_url(next_url)
                if verdict_bad is not None or _origin(next_url) != origin:
                    return (MALFORMED, resp.status_code, "")
                current_url = next_url
                if resp.status_code in (301, 302, 303):
                    current_method, current_body = "GET", None
                continue
            return (classify_status(resp.status_code, resp.text[:4096]), resp.status_code, resp.text)


async def probe_provider_entry(entry: Dict[str, Any], *, transport=None) -> Dict[str, Any]:
    """Run a data-driven probe and return a classification dict for the UI to paint.

    Result shape: ``verdict``/``category``/``http_status``/``url``/``auth_used``/
    ``model_ids`` (only for SUCCESS-with-models-data)/capabilities (declared mirrored).
    Never raises for providerine issues; honest verdicts only.
    """
    method, url, headers, body, rejection = build_provider_probe_request(entry)
    origin = _origin(url) if url and "://" in url else None
    if rejection:
        reason = str(rejection)
        if reason.startswith("no_key"):
            verdict, status, text = NO_KEY, 0, ""
        elif reason.startswith("path_rejected") or reason.startswith("base_url_rejected"):
            verdict, status, text = MALFORMED, 0, ""
        else:
            verdict, status, text = NETWORK, 0, ""
        auth_used = False if reason.startswith("no_key") else bool(headers)
        return _result(verdict, status, url, auth_used, entry, [])

    try:
        verdict, status, text = await _run_probe(method, url, headers, body, origin, transport=transport)
    except httpx.TimeoutException:
        return _result(TIMEOUT, 0, url, bool(headers), entry, [])
    except httpx.TransportError:
        return _result(NETWORK, 0, url, bool(headers), entry, [])
    except Exception:
        return _result(NETWORK, 0, url, bool(headers), entry, [])

    if verdict == SUCCESS:
        verdict = apply_success_contract(verdict, text, entry)
    model_ids = _bounded_json_models_parse(text) if verdict == SUCCESS else []
    return _result(verdict, status, url, bool(headers), entry, model_ids)


def _result(verdict: str, status: int, url: str, auth_used: bool, entry: Dict[str, Any], model_ids: List[str]) -> Dict[str, Any]:
    caps = _caps(entry)
    declared = sorted(caps) if caps else ["chat", "completion"]
    task_only = bool(caps and caps.isdisjoint(_INFERENCE_CAPABILITIES))
    return {
        "verdict": verdict,
        "category": category_of(verdict),
        "ok": verdict == SUCCESS,
        "http_status": int(status or 0),
        "url": url,
        "auth_used": bool(auth_used),
        "capabilities": declared,
        "task_only": task_only,
        "model_ids": list(model_ids or []),
        "notes": [] if verdict == SUCCESS else [f"{category_of(verdict)}:{status or 0}"],
    }
