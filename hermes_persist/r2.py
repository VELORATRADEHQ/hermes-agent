"""Minimal Cloudflare R2 client over the S3-compatible API (stdlib urllib + sigv4).

Endpoint shape: https://<account_id>.r2.cloudflarestorage.com/<bucket>/<key>
(path-style). Region "auto" per R2 convention. Scope: this module is used ONLY by
ops tooling (hermes-ops), never by the hot gateway path.
"""
from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from . import sigv4

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class R2Error(RuntimeError):
    def __init__(self, op: str, key: str, status: int, body: bytes):
        body_s = body[:300].decode("utf-8", "replace")
        super().__init__(f"{op} {key}: HTTP {status}: {body_s}")
        self.status = status


class R2Client:
    def __init__(self, *, endpoint: str, bucket: str, access_key_id: str,
                 secret_access_key: str, region: str = "auto", timeout: float = 30.0):
        endpoint = endpoint.rstrip("/")
        if "://" in endpoint:
            self._base = endpoint
        else:
            self._base = "https://" + endpoint
        self.bucket = bucket
        self._ak = access_key_id
        self._sk = secret_access_key
        self._region = region
        self._timeout = timeout

    # ── core signed request ────────────────────────────────────────────────
    def _request(self, method: str, key: str, payload: bytes = b"",
                 extra_headers: Optional[Dict[str, str]] = None,
                 query: Optional[List] = None) -> "tuple[int, Dict[str, str], bytes]":
        path = f"/{self.bucket}/{key}"
        host = self._base.split("://", 1)[1]
        headers: Dict[str, str] = {
            "Host": host,
            "X-Amz-Content-Sha256": sigv4.payload_hash(payload),
        }
        if extra_headers:
            headers.update(extra_headers)
        _, signed = sigv4.sign(method, path, query or [], headers, payload,
                               access_key=self._ak, secret_key=self._sk,
                               region=self._region, service="s3")
        req = urllib.request.Request(f"{self._base}{path}", data=(payload if method != "HEAD" else None),
                                     method=method, headers=signed)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                low = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, low, resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            if exc.code in (404, 304, 412):
                low = {k.lower(): v for k, v in (exc.headers or {}).items()}
                return exc.code, low, body
            raise R2Error(method, key, exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise R2Error(method, key, -1, str(exc).encode()) from exc

    # ── public object API ──────────────────────────────────────────────────
    def put_object(self, key: str, data: bytes, *, if_none_match_star: bool = False) -> None:
        hdrs = {"If-None-Match": "*"} if if_none_match_star else None
        status, _, _ = self._request("PUT", key, data, extra_headers=hdrs)
        if status == 412:
            raise PreconditionFailed(key)
        if status >= 300:
            raise R2Error("PUT", key, status, b"")

    def get_object(self, key: str) -> Optional[bytes]:
        status, _, body = self._request("GET", key)
        if status == 404:
            return None
        if status >= 300:
            raise R2Error("GET", key, status, body)
        return body

    def head_object(self, key: str) -> Optional[Dict[str, str]]:
        status, headers, _ = self._request("HEAD", key)
        if status == 404:
            return None
        return headers

    def delete_object(self, key: str) -> None:
        status, _, body = self._request("DELETE", key)
        if status >= 300 and status != 404:
            raise R2Error("DELETE", key, status, body)

    def list_objects(self, prefix: str = "", limit: int = 1000) -> List[str]:
        q = [("list-type", "2"), ("max-keys", str(limit))]
        if prefix:
            q.append(("prefix", prefix))
        status, _, body = self._request("GET", "", query=q)
        if status >= 300:
            raise R2Error("LIST", prefix, status, body)
        root = ET.fromstring(body or b"<ListBucketResult/>")
        return [el.text for el in root.iter(_S3_NS + "Key") if el.text]


class PreconditionFailed(RuntimeError):
    def __init__(self, key: str):
        super().__init__(f"object already exists (precondition failed): {key}")
