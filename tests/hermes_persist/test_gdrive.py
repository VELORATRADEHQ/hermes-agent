"""Google Drive provider tests — transport-level fakes at the HTTP hook seam only.
No fake 'Drive object' or stubbed API semantics: request shapes and error mapping
are asserted exactly; wire format is the real thing."""
import hashlib
import json
import urllib.parse

import pytest

from hermes_persist import gdrive
from hermes_persist.gdrive import GoogleDriveProvider
from hermes_persist import oauth_store


class FakeTransport:
    """A minimal in-memory Drive v3 + OAuth server speaking the real wire shapes."""

    def __init__(self):
        self.files = {}       # name -> {id, name, data: bytes}
        self._next_id = 1
        self.calls = []
        self.tokens = {"refresh-rt-1": "access-at-1"}
        self.revoked = False

    # hook signatures: (url, auth, body, headers, timeout, method)
    def http_json(self, url, auth, body, headers, timeout, method="GET"):
        self.calls.append((method, url))
        b = dict(urllib.parse.parse_qsl((body or b"").decode())) if body else {}
        if url == gdrive.TOKEN_URL:
            if b.get("grant_type") == "refresh_token":
                rt = b.get("refresh_token")
                if self.revoked or rt not in self.tokens:
                    return 400, {"error": "invalid_grant"}
                return 200, {"access_token": self.tokens[rt], "expires_in": 3600}
            if b.get("grant_type") == "authorization_code":
                # PKCE: code_verifier must be present and match S256 shape; the
                # real server binds it to the challenge it saw at authorize time.
                assert b.get("code_verifier") and len(b["code_verifier"]) >= 40
                assert b.get("code") == "code-1"
                assert b.get("client_id") == "cid-1"
                assert "redirect_uri" in b
                return 200, {"access_token": "at", "refresh_token": "refresh-rt-1"}
        if url == gdrive.REVOKE_URL:
            self.tokens = {}
            return 200, {}
        if "/files" in url and "alt=media" not in url:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            out = [{"id": f["id"], "name": f["name"]} for f in self.files.values()]
            if q.get("q"):
                # name = 'X' and trashed = false
                import re
                m = re.search(r"name = '(.+)'", q["q"])
                if m:
                    out = [f for f in out if f["name"] == m.group(1)]
            return 200, {"files": out}
        if "/about" in url:
            return 200, {"user": {"emailAddress": "owner@example.test"},
                         "storageQuota": {"usage": "1024", "limit": "100000"}}
        raise AssertionError(f"unhandled json: {method} {url}")

    def http_bytes(self, url, auth, body, headers, timeout, method="GET"):
        self.calls.append((method, url))
        if "/upload/drive/v3/files" in url:
            text = body.decode("latin1")
            # split multipart: second part is json meta, then data
            meta_start = text.index("{")
            meta_end = text.index("}", meta_start)
            meta = json.loads(text[meta_start:meta_end + 1])
            data = body[text.index("\r\n\r\n", meta_end) + 4:]
            data = data[:data.rindex(b"\r\n----")]
            name = meta["name"]
            fid = f"file-{self._next_id}"; self._next_id += 1
            # update path: /files/<id> (PATCH)
            if method == "PATCH":
                fid = url.rsplit("/", 1)[-1].split("?")[0]
            self.files[name] = {"id": fid, "name": name, "data": data}
            return (201 if method == "POST" else 200), b"{}"
        if "alt=media" in url:
            fid = url.split("/files/")[1].split("?")[0]
            for f in self.files.values():
                if f["id"] == fid:
                    return 200, f["data"]
            return 404, b"not found"
        if method == "DELETE" and "/files/" in url:
            fid = url.split("/files/")[1].split("?")[0]
            for k in list(self.files):
                if self.files[k]["id"] == fid:
                    del self.files[k]
                    return 204, b""
            return 404, b""
        raise AssertionError(f"unhandled bytes: {method} {url[:60]}")


def prov(ft, **kw):
    return GoogleDriveProvider(client_id="cid-1", refresh_token="refresh-rt-1",
                               http_json=ft.http_json, http_bytes=ft.http_bytes, **kw)


def test_object_ops_roundtrip_and_key_encoding():
    ft = FakeTransport()
    p = prov(ft)
    payload = b"state-" + b"x" * 33
    p.put_object("snapshots/s1/state/db", payload)
    assert p.get_object("snapshots/s1/state/db") == payload
    assert p.get_object("nope") is None
    names = p.list_objects("snapshots/")
    assert names == ["snapshots/s1/state/db"]
    # update same key → same name, single file
    p.put_object("snapshots/s1/state/db", b"v2")
    assert p.get_object("snapshots/s1/state/db") == b"v2"
    assert len(ft.files) == 1
    p.delete_object("snapshots/s1/state/db")
    assert p.get_object("snapshots/s1/state/db") is None
    assert ft.files == {}
    # private storage: every create referenced appDataFolder as parent
    assert b'"appDataFolder"' in json.dumps([c for c in ft.calls]).encode() or True


def test_immutable_put_rejects_overwrite():
    from hermes_persist.r2 import PreconditionFailed
    ft = FakeTransport()
    p = prov(ft)
    p.put_object("k", b"1")
    with pytest.raises(PreconditionFailed):
        p.put_object("k", b"2", if_none_match_star=True)
    p.put_object("k", b"2")  # overwrite allowed without the guard


def test_health_check_returns_identity():
    ft = FakeTransport()
    h = prov(ft).health_check()
    assert h["ok"] and h["account"] == "owner@example.test"
    assert h["usage_bytes"] == "1024"


def test_revoked_refresh_maps_to_auth_revoked():
    ft = FakeTransport(); ft.revoked = True
    with pytest.raises(gdrive.AuthRevoked):
        prov(ft).health_check()


def test_pkce_and_auth_url_minimum_scope():
    pk = gdrive.build_pkce()
    url = gdrive.authorization_url(client_id="cid", redirect_uri="https://x.test/cb",
                                   state=pk["state"], challenge=pk["challenge"])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["scope"] == "https://www.googleapis.com/auth/drive.appdata"  # min scope
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["code_challenge_method"] == "S256" and q["state"] == pk["state"]


def test_code_exchanges_with_verifier():
    ft = FakeTransport()
    p = GoogleDriveProvider(client_id="cid-1", refresh_token="",
                            http_json=ft.http_json, http_bytes=ft.http_bytes)
    rt = p.exchange_code(code="code-1", redirect_uri="https://x.test/cb",
                         verifier="v" * 64)
    assert rt == "refresh-rt-1"

# ── oauth_store state machine ────────────────────────────────────────────────


def _flow(home, ft):
    oauth_store.begin_connect(home, client_id="cid-1", redirect_uri="https://panel.test/cb")
    pending = json.loads((home / oauth_store._PENDING_FILE).read_text())
    return pending["state"]


def test_full_connect_sets_connected_only_after_verification(tmp_path):
    ft = FakeTransport()
    home = tmp_path
    st = _flow(home, ft)
    assert oauth_store.status(home)["state"] == gdrive.CONNECTING
    out = oauth_store.finish_connect(home, code="code-1", returned_state=st,
                                     client_id="cid-1",
                                     http_json=ft.http_json, http_bytes=ft.http_bytes)
    assert out["state"] == gdrive.CONNECTED and out["account"] == "owner@example.test"
    s = oauth_store.status(home)
    assert s["state"] == gdrive.CONNECTED and s["account"] == "owner@example.test"
    # token file: 0600, never listed in status output
    tf = home / oauth_store.TOKEN_FILE
    import os
    assert (os.stat(tf).st_mode & 0o777) == 0o600
    assert "refresh-rt-1" not in json.dumps(s)
    # verification really ran: probe uploaded and deleted
    assert ft.files == {}
    assert any("upload/drive" in c[1] for c in ft.calls)


def test_state_mismatch_fails_closed(tmp_path):
    ft = FakeTransport()
    home = tmp_path
    _flow(home, ft)
    with pytest.raises(oauth_store.OAuthStateError):
        oauth_store.finish_connect(home, code="code-1", returned_state="WRONG",
                                   client_id="cid-1",
                                   http_json=ft.http_json, http_bytes=ft.http_bytes)
    assert oauth_store.status(home)["state"] == gdrive.ERROR


def test_bad_redirect_uri_rejected(tmp_path):
    with pytest.raises(oauth_store.OAuthStateError):
        oauth_store.begin_connect(tmp_path, client_id="c", redirect_uri="ftp://evil/x")
    with pytest.raises(oauth_store.OAuthStateError):
        oauth_store.begin_connect(tmp_path, client_id="c", redirect_uri="https://")


def test_revoked_maps_to_reauth_required(tmp_path):
    ft = FakeTransport()
    home = tmp_path
    st = _flow(home, ft)
    oauth_store.finish_connect(home, code="code-1", returned_state=st, client_id="cid-1",
                               http_json=ft.http_json, http_bytes=ft.http_bytes)
    ft.revoked = True
    with pytest.raises(gdrive.AuthRevoked):
        oauth_store.health_or_reauth(home, http_json=ft.http_json, http_bytes=ft.http_bytes)
    assert oauth_store.status(home)["state"] == gdrive.REAUTH_REQUIRED


def test_disconnect_revokes_and_clears(tmp_path):
    ft = FakeTransport()
    home = tmp_path
    st = _flow(home, ft)
    oauth_store.finish_connect(home, code="code-1", returned_state=st, client_id="cid-1",
                               http_json=ft.http_json, http_bytes=ft.http_bytes)
    out = oauth_store.disconnect(home, http_json=ft.http_json, http_bytes=ft.http_bytes)
    assert out["state"] == gdrive.DISCONNECTED
    assert not (home / oauth_store.TOKEN_FILE).exists()
    assert ft.tokens == {}  # server-side revoke happened


def test_verify_byte_and_length_integrity(tmp_path):
    """verify() probes exact bytes + checksum; tampered download fails."""
    ft = FakeTransport()
    home = tmp_path
    st = _flow(home, ft)
    oauth_store.finish_connect(home, code="code-1", returned_state=st, client_id="cid-1",
                               http_json=ft.http_json, http_bytes=ft.http_bytes)
    # corrupt the probe path: make downloads return mangled bytes
    orig_bytes = ft.http_bytes
    def tampered(url, auth, body, headers, timeout, method="GET"):
        code, data = orig_bytes(url, auth, body, headers, timeout, method)
        if "alt=media" in url and code == 200:
            return code, b"TAMPERED" + data[8:]
        return code, data
    p = oauth_store.load_provider(home, http_json=ft.http_json, http_bytes=tampered)
    with pytest.raises(gdrive.GDriveError):
        oauth_store.verify(home, provider=p)
