"""Google Drive connection manager + refresh-token store.

State machine: NOT_CONNECTED → CONNECTING → CONNECTED | ERROR | REAUTH_REQUIRED
                                          ↘ DISCONNECTED (explicit user action)

CONNECTING→CONNECTED transition is GATED on the full real verification sequence:
  refresh-mint OK → about/health OK → test upload → test download → byte equality
  → SHA-256 equality → cleanup delete succeeded.
The state file NEVER becomes CONNECTED any other way.

Refresh-token storage (never printed/logged/committed):
  <home>/state/gdrive_token.env   (0600, contains GDRIVE_REFRESH_TOKEN + client id)
  …and the caller may additionally mirror it into the encrypted secrets object
  (sync.upload_secrets) so migration to a new runtime avoids re-OAuth
  (Phase 6 requirement). The secrets object is AES-256-GCM and never contains
  the master key or the bootstrap credential.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import gdrive

STATE_FILE = "state/gdrive_status.json"
TOKEN_FILE = "state/gdrive_token.env"
_PENDING_FILE = "state/gdrive_oauth_pending.json"


class OAuthStateError(RuntimeError):
    pass


def _write_0600(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def status(home: Path) -> Dict[str, Any]:
    base = Path(home) / STATE_FILE
    if base.is_file():
        try:
            s = json.loads(base.read_text())
            if s.get("state") in (gdrive.NOT_CONNECTED, gdrive.CONNECTING, gdrive.CONNECTED,
                                  gdrive.ERROR, gdrive.REAUTH_REQUIRED, gdrive.DISCONNECTED):
                return s
        except Exception:
            pass
    return {"state": gdrive.NOT_CONNECTED, "account": None,
            "last_success_sync": None, "last_backup": None}


def _save_status(home: Path, s: Dict[str, Any]) -> None:
    p = Path(home) / STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_0600(p, json.dumps(s, indent=1))


def set_state(home: Path, state: str, **fields) -> None:
    s = status(home)
    s["state"] = state
    s.update(fields)
    _save_status(home, s)


# ── OAuth connect flow ───────────────────────────────────────────────────────


def begin_connect(home: Path, *, client_id: str, redirect_uri: str) -> Dict[str, str]:
    """Create PKCE + state, persist the pending flow (0600), return the auth URL.
    The pending file holds the verifier (secret-ish short-lived) and state."""
    import urllib.parse as _up
    _parsed = _up.urlsplit(redirect_uri)
    _ok = (_parsed.scheme == "https" and _parsed.netloc) or           (_parsed.scheme == "http" and _parsed.netloc.split(":")[0] in ("127.0.0.1", "localhost"))
    if not _ok or len(redirect_uri) < 10:
        raise OAuthStateError("redirect_uri must be a valid https:// URI or loopback — refusing")
    pk = gdrive.build_pkce()
    pending = {"verifier": pk["verifier"], "state": pk["state"],
               "client_id": client_id, "redirect_uri": redirect_uri,
               "created": time.time()}
    p = Path(home) / _PENDING_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_0600(p, json.dumps(pending))
    set_state(home, gdrive.CONNECTING)
    return {
        "auth_url": gdrive.authorization_url(client_id=client_id,
                                             redirect_uri=redirect_uri,
                                             state=pk["state"], challenge=pk["challenge"]),
        "state": pk["state"],
    }


def finish_connect(home: Path, *, code: str, returned_state: str, client_id: str,
                   client_secret: str = "", http_json=None, http_bytes=None) -> Dict[str, Any]:
    """Exchange the one-time code, verify, store refresh credential, verify Drive.

    OAuth-state validation: `returned_state` must equal the stored pending state —
    otherwise fail closed (possible CSRF/mix-up).
    """
    p = Path(home) / _PENDING_FILE
    if not p.is_file():
        set_state(home, gdrive.ERROR, error="no pending oauth flow")
        raise OAuthStateError("no pending connect flow found")
    pending = json.loads(p.read_text())
    if time.time() - float(pending.get("created", 0)) > 1800:
        set_state(home, gdrive.ERROR, error="stale oauth flow")
        raise OAuthStateError("stale connect flow (>30 min) — restart Connect")
    if returned_state != pending.get("state"):
        set_state(home, gdrive.ERROR, error="oauth state mismatch")
        raise OAuthStateError("state mismatch — refusing (possible CSRF)")

    prov = gdrive.GoogleDriveProvider(client_id=client_id, refresh_token="",
                                      client_secret=client_secret,
                                      http_json=http_json, http_bytes=http_bytes)
    try:
        refresh = prov.exchange_code(code=code, redirect_uri=pending["redirect_uri"],
                                     verifier=pending["verifier"])
    except gdrive.AuthRevoked as exc:
        set_state(home, gdrive.REAUTH_REQUIRED)
        raise OAuthStateError("authorization declined/revoked at exchange") from exc
    # store credential (0600) BEFORE verification; verification must still pass
    tok = Path(home) / TOKEN_FILE
    _write_0600(tok, "\n".join([f"GDRIVE_CLIENT_ID={client_id}",
                                f"GDRIVE_REFRESH_TOKEN={refresh}",
                                ""]) )
    connected = gdrive.GoogleDriveProvider(client_id=client_id, refresh_token=refresh,
                                           client_secret=client_secret,
                                           http_json=http_json, http_bytes=http_bytes)
    try:
        verify(home, provider=connected)  # raises on any failure
    except Exception:
        set_state(home, gdrive.ERROR, error="verification failed after connection")
        raise
    try:
        os.unlink(p)
    except OSError:
        pass
    h = connected.health_check()
    set_state(home, gdrive.CONNECTED, account=h.get("account"), error=None)
    return {"state": gdrive.CONNECTED, "account": h.get("account")}


def verify(home: Path, *, provider: Optional[gdrive.GoogleDriveProvider] = None,
           http_json=None, http_bytes=None) -> Dict[str, Any]:
    """Full real verification: health → upload → download → bytes → SHA-256 → delete.
    CONNECTED is set only inside finish_connect after this function returns without
    raising; this function never sets CONNECTED itself."""
    prov = provider or load_provider(home, http_json=http_json, http_bytes=http_bytes)
    hc = prov.health_check()
    probe_key = "ops/gdrive-verify.tmp"
    payload = b"hermes-gdrive-verify:" + os.urandom(16).hex().encode()
    prov.put_object(probe_key, payload)
    back = prov.get_object(probe_key)
    if back != payload:
        raise gdrive.GDriveError("verify", "downloaded probe bytes differ")
    if hashlib.sha256(back).hexdigest() != hashlib.sha256(payload).hexdigest():
        raise gdrive.GDriveError("verify", "sha256 mismatch")
    prov.delete_object(probe_key)
    if prov.get_object(probe_key) is not None:
        raise gdrive.GDriveError("verify", "probe cleanup failed")
    s = status(home)
    if s.get("state") == gdrive.CONNECTED:
        s["last_success_sync"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_status(home, s)
    return hc


def load_provider(home: Path, *, client_secret: str = "", http_json=None, http_bytes=None) -> gdrive.GoogleDriveProvider:  # hooks: tests only

    tok = Path(home) / TOKEN_FILE
    if not tok.is_file():
        raise OAuthStateError("no stored Google Drive credential (NOT_CONNECTED)")
    vals: Dict[str, str] = {}
    for ln in tok.read_text().splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            vals[k.strip()] = v.strip()
    cid = vals.get("GDRIVE_CLIENT_ID")
    rt = vals.get("GDRIVE_REFRESH_TOKEN")
    if not cid or not rt:
        raise OAuthStateError("stored credential incomplete — reconnect required")
    return gdrive.GoogleDriveProvider(client_id=cid, refresh_token=rt,
                                      client_secret=client_secret,
                                      http_json=http_json, http_bytes=http_bytes)


def disconnect(home: Path, *, revoke: bool = True, http_json=None, http_bytes=None) -> Dict[str, Any]:
    prov = None
    try:
        prov = load_provider(home, http_json=http_json, http_bytes=http_bytes)
    except OAuthStateError:
        pass
    if prov is not None and revoke:
        try:
            prov.revoke()
        except Exception:
            pass  # revocation failure must not block local disconnect
    for f in (Path(home) / TOKEN_FILE,):
        try:
            os.unlink(f)
        except OSError:
            pass
    set_state(home, gdrive.DISCONNECTED, account=None, error=None,
              last_success_sync=status(home).get("last_success_sync"))
    return {"state": gdrive.DISCONNECTED}


def health_or_reauth(home: Path, *, http_json=None, http_bytes=None) -> Dict[str, Any]:
    """Runtime health probe: invalid_grant ⇒ REAUTH_REQUIRED (spec: revoked
    credentials must surface REAUTH_REQUIRED, never silently re-accounted)."""
    prov = load_provider(home, http_json=http_json, http_bytes=http_bytes)
    try:
        return verify(home, provider=prov)
    except gdrive.AuthRevoked:
        set_state(home, gdrive.REAUTH_REQUIRED)
        raise
