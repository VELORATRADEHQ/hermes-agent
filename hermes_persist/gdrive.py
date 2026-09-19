"""Google Drive persistence backend — REAL integration, official Drive API v3.

- Storage root: Drive `appDataFolder` (application-private, hidden, cannot be
  shared or made public: inherently satisfies "never public"). Scope requested is
  exactly ONE: `drive.appdata` (minimum necessary; no whole-drive access).
- Auth: OAuth2 installed-application flow with PKCE (S256) — a "Desktop app"
  OAuth client needs NO client secret. For headless runtimes the short-lived
  authorization CODE is exchanged via the loopback/paste flow; the user never
  pastes a token, only the one-time code from the Google authorize redirect.
  An optional web-app flow (client secret + redirect URI from env) is supported
  for hosted panels; the secret lives in the runtime env only.
- Key model: Drive has no object paths; provider maps hermes_persist keys to flat
  appDataFolder files named `h:<key-with-/->^>` and byte-echoes listing through
  in-memory prefix filtering. Listing is exact (returns the mapped key names).
- No mocks: every method performs real HTTPS. Test injection happens via the
  private transport hooks only (_http_json / _http_bytes), never as "fake API".
- Secrets: refresh token is returned through the state store (oauth_store.py) as
  a 0600 env file and mirrored into the encrypted secrets object by the caller.
  Nothing secret (tokens, codes, client secret) is ever printed, logged, or
  embedded in exceptions.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.appdata"

# ── states (persisted non-secret status) ────────────────────────────────────
NOT_CONNECTED = "NOT_CONNECTED"
CONNECTING = "CONNECTING"
CONNECTED = "CONNECTED"
ERROR = "ERROR"
REAUTH_REQUIRED = "REAUTH_REQUIRED"
DISCONNECTED = "DISCONNECTED"

_KEY_PREFIX = "h:"
_KEY_SEP = "^"  # '/' inside keys → '^' on Drive (Drive disallows '/' in names reliably)


class GDriveError(RuntimeError):
    """Sanitized failure — never contains tokens/codes/secrets."""

    def __init__(self, op: str, detail: str):
        super().__init__(f"gdrive {op}: {detail[:200]}")
        self.op = op


class AuthRevoked(GDriveError):
    def __init__(self):
        super().__init__("refresh", "invalid_grant")


def encode_key(key: str) -> str:
    return _KEY_PREFIX + key.replace("/", _KEY_SEP)


def decode_key(name: str) -> Optional[str]:
    if not name.startswith(_KEY_PREFIX):
        return None
    return name[len(_KEY_PREFIX):].replace(_KEY_SEP, "/")


# ── OAuth (authorization URL building + one-time code exchange) ─────────────


def build_pkce() -> Dict[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return {"verifier": verifier, "challenge": challenge,
            "state": base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")}


def authorization_url(*, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # guarantees a refresh token on first grant
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{AUTH_URL}?{q}"


# HTTP hooks (real by default; injectable ONLY for tests) ──────────────────────
HttpJson = Callable[[str, Optional[str], Optional[bytes], Optional[Dict[str, str]], float, str], "tuple[int, Dict[str, Any]]"]
HttpBytes = Callable[[str, Optional[str], Optional[bytes], Optional[Dict[str, str]], float, str], "tuple[int, bytes]"]


def _real_http_json(url, auth, body, headers, timeout, method="GET"):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = auth
    req = urllib.request.Request(url, data=body, headers=h,
                                 method=method if body is None else ("POST" if method == "GET" else method))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"error": raw[:200]}
    except urllib.error.URLError as e:
        raise GDriveError("http", f"network: {type(e.reason).__name__}") from e
    except Exception as e:
        raise GDriveError("http", type(e).__name__) from e


def _real_http_bytes(url, auth, body, headers, timeout, method="GET"):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = auth
    req = urllib.request.Request(url, data=body, headers=h,
                                 method=method if body is None else ("POST" if method == "GET" else method))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400]
    except urllib.error.URLError as e:
        raise GDriveError("http", f"network: {type(e.reason).__name__}") from e


class GoogleDriveProvider:
    """Persistence backend exposing the same object ops hermes_persist expects
    (put_object/get_object/head_object/delete_object/list_objects).

    Credentials come from a CredentialStore (see oauth_store.py); refresh tokens
    are minted automatically when the access token expires.
    """

    def __init__(self, *, client_id: str, refresh_token: str, client_secret: str = "",
                 http_json: HttpJson = None, http_bytes: HttpBytes = None):
        if not client_id:
            raise GDriveError("init", "client_id is required")
        # refresh_token may be "" at init when the instance is built only to
        # perform the initial code exchange (exchange_code() then fills it).
        self._cid = client_id
        self._secret = client_secret
        self._rt = refresh_token
        self._json = http_json or _real_http_json
        self._bytes = http_bytes or _real_http_bytes
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    # ── auth ───────────────────────────────────────────────────────────────
    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return f"Bearer {self._token}"
        if not self._rt:
            raise GDriveError("refresh", "no refresh token — connect first")
        form: Dict[str, str] = {
            "grant_type": "refresh_token",
            "client_id": self._cid,
            "refresh_token": self._rt,
        }
        if self._secret:
            form["client_secret"] = self._secret
        status, data = self._json(
            TOKEN_URL, None, urllib.parse.urlencode(form).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"}, 30.0, "POST")
        if status != 200:
            if data.get("error") == "invalid_grant":
                raise AuthRevoked()
            raise GDriveError("refresh", f"HTTP {status} ({data.get('error', '?')})")
        tok = data.get("access_token")
        if not tok:
            raise GDriveError("refresh", "no access_token in response")
        self._token = tok
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        return f"Bearer {tok}"

    def exchange_code(self, *, code: str, redirect_uri: str, verifier: str) -> str:
        """Authorization code → refresh token (called ONCE at connect)."""
        form: Dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._cid,
            "code_verifier": verifier,
        }
        if self._secret:
            form["client_secret"] = self._secret
        status, data = self._json(
            TOKEN_URL, None, urllib.parse.urlencode(form).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"}, 30.0, "POST")
        if status != 200 or "refresh_token" not in data:
            if data.get("error") == "invalid_grant":
                raise AuthRevoked()
            raise GDriveError("exchange", f"HTTP {status} ({data.get('error', '?')})")
        self._rt = data["refresh_token"]
        return self._rt

    def revoke(self) -> None:
        try:
            self._json(REVOKE_URL, None,
                       urllib.parse.urlencode({"token": self._rt}).encode(),
                       {"Content-Type": "application/x-www-form-urlencoded"}, 15.0, "POST")
        finally:
            self._token = None
            self._token_exp = 0.0

    # ── health / identity ──────────────────────────────────────────────────
    def health_check(self) -> Dict[str, Any]:
        status, data = self._json(
            f"{DRIVE_API}/about?fields=user,storageQuota", self._auth(), None, None, 30.0)
        if status != 200:
            raise GDriveError("about", f"HTTP {status}")
        user = data.get("user") or {}
        quota = data.get("storageQuota") or {}
        return {"ok": True,
                "account": user.get("emailAddress") or user.get("displayName") or "unknown",
                "usage_bytes": quota.get("usage"), "limit_bytes": quota.get("limit")}

    # ── file mapping ───────────────────────────────────────────────────────
    def _find(self, name: str) -> Optional[Dict[str, str]]:
        query = "name = '" + name.replace("'", "\\'") + "' and trashed = false"
        params = urllib.parse.urlencode(
            {"spaces": "appDataFolder", "q": query,
             "fields": "files(id,name,modifiedTime,size)"})
        status, data = self._json(f"{DRIVE_API}/files?{params}",
                                  self._auth(), None, None, 30.0, "GET")
        if status != 200:
            raise GDriveError("find", f"HTTP {status}")
        for f in data.get("files", []):
            if f.get("name") == name:
                return f
        return None

    def list_objects(self, prefix: str = "", limit: int = 1000) -> List[str]:
        out: List[str] = []
        page = None
        while len(out) < limit:
            params = {"spaces": "appDataFolder",
                      "fields": "files(name),nextPageToken",
                      "pageSize": "500"}
            if page:
                params["pageToken"] = page
            status, data = self._json(f"{DRIVE_API}/files?{urllib.parse.urlencode(params)}",
                                      self._auth(), None, None, 30.0, "GET")
            if status != 200:
                raise GDriveError("list", f"HTTP {status}")
            for f in data.get("files", []):
                key = decode_key(f.get("name") or "")
                if key is not None and key.startswith(prefix):
                    out.append(key)
            page = data.get("nextPageToken")
            if not page:
                break
        return sorted(out)[:limit]

    # ── object ops (mirrors the R2Client surface used by sync.py) ──────────
    def put_object(self, key: str, data: bytes, *, if_none_match_star: bool = False) -> None:
        name = encode_key(key)
        existing = self._find(name)
        if if_none_match_star and existing is not None:
            from .r2 import PreconditionFailed
            raise PreconditionFailed(key)
        boundary = "----hermesgd" + os.urandom(8).hex()
        meta = {"name": name, "mimeType": "application/octet-stream"}
        if existing is None:
            meta["parents"] = ["appDataFolder"]
            url = f"{DRIVE_UPLOAD}/files?uploadType=multipart"
            method_url = url
        else:
            method_url = f"{DRIVE_UPLOAD}/files/{existing['id']}?uploadType=multipart&supportsAllDrives=false"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps(meta) + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/related; boundary={boundary}"}
        auth = self._auth()
        method = "POST" if existing is None else "PATCH"
        status, _resp = self._bytes(method_url, auth, body, headers, 60.0, method)
        if status >= 300:
            raise GDriveError("upload", f"HTTP {status}")

    def get_object(self, key: str) -> Optional[bytes]:
        f = self._find(encode_key(key))
        if f is None:
            return None
        status, body = self._bytes(f"{DRIVE_API}/files/{f['id']}?alt=media",
                                   self._auth(), None, None, 60.0, "GET")
        if status == 404:
            return None
        if status != 200:
            raise GDriveError("download", f"HTTP {status}")
        return body

    def head_object(self, key: str) -> Optional[Dict[str, str]]:
        return self._find(encode_key(key))

    def delete_object(self, key: str) -> None:
        f = self._find(encode_key(key))
        if f is None:
            return
        status, _ = self._bytes(f"{DRIVE_API}/files/{f['id']}", self._auth(), None, None, 30.0, "DELETE")
        if status not in (200, 204, 404):
            raise GDriveError("delete", f"HTTP {status}")
