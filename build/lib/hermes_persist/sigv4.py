"""AWS Signature Version 4 signer (stdlib-only) — used to talk to Cloudflare R2's
S3-compatible API without pulling in boto3 on a bare runtime.

Validated against the official AWS SigV4 test-suite vector ``get-vanilla``
(see tests/hermes_state/test_sigv4.py): the well-known iam.amazonaws.com example
whose expected signature is 5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

_UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"


def _uri_encode(s: str, *, encode_slash: bool = True) -> str:
    out = []
    for ch in s:
        if ch in _UNRESERVED or (ch == "/" and not encode_slash):
            out.append(ch)
        else:
            for b in ch.encode("utf-8"):
                out.append(f"%{b:02X}")
    return "".join(out)


def payload_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def canonical_request(method: str, path: str, query: Iterable[Tuple[str, str]],
                      headers: Dict[str, str], payload: bytes) -> Tuple[str, str]:
    """Returns (canonical_request, signed_headers)."""
    canon_uri = _uri_encode(path if path.startswith("/") else "/" + path, encode_slash=False) or "/"
    q = sorted((k, v) for k, v in query)
    canon_query = "&".join(f"{_uri_encode(str(k))}={_uri_encode(str(v))}" for k, v in q)
    low = {k.strip().lower(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(low)
    canon_headers = "".join(f"{k}:{low[k]}\n" for k in names)
    signed = ";".join(names)
    cr = "\n".join([method.upper(), canon_uri, canon_query, canon_headers, signed,
                    payload_hash(payload)])
    return cr, signed


def sign(method: str, path: str, query: Iterable[Tuple[str, str]], headers: Dict[str, str],
         payload: bytes, *, access_key: str, secret_key: str, region: str, service: str,
         amz_date: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """Sign a request. Returns (signature, headers_with_auth_added).

    ``headers`` must already include host (and x-amz-content-sha256 when signing body)."""
    now = amz_date or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = now[:8]
    hdrs = dict(headers)
    hdrs.setdefault("X-Amz-Date", now)
    cr, signed = canonical_request(method, path, query, hdrs, payload)
    scope = f"{date}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", now, scope, hashlib.sha256(cr.encode()).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region, service),
                         sts.encode(), hashlib.sha256).hexdigest()
    # SignedHeader list must match canonical ordering (lowercased, sorted)
    low_sorted = sorted({k.strip().lower() for k in hdrs})
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={';'.join(low_sorted)}, Signature={signature}")
    out = dict(hdrs)
    out["Authorization"] = auth
    return signature, out
