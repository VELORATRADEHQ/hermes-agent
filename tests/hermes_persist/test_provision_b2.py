"""B2 Native API provisioning — hermetic (mock transport), and no-secret-leak discipline."""
import json
import os

import pytest

from hermes_persist import provision_b2 as pb

FAKE_MASTER_ID = "00MASTERIDFAKE" * 2
FAKE_MASTER_KEY = "K" * 25 + "MASTERFAKE"

AUTH_RESP = {
    "accountId": "acc-fake",
    "authorizationToken": "auth-token-fake",
    "apiInfo": {"storageApi": {
        "apiUrl": "https://api004.backblazeb2.com",
        "s3ApiUrl": "https://s3.us-west-004.backblazeb2.com",
    }},
}


class MockTransport:
    """Records request payloads; returns scripted responses. Never stores keys."""

    def __init__(self, post_responses=None):
        self.calls = []
        self.post_responses = post_responses or {}

    def get(self, url, basic_b64, timeout):
        self.calls.append(("GET", url, basic_b64))
        assert url == pb.AUTH_URL
        import base64
        assert base64.b64decode(basic_b64).decode() == f"{FAKE_MASTER_ID}:{FAKE_MASTER_KEY}"
        return 200, dict(AUTH_RESP)

    def post(self, url, headers, body, timeout):
        verb = url.rsplit("/", 1)[-1]
        payload = json.loads(body.decode())
        self.calls.append((verb, payload))
        for matcher, resp in self.post_responses.items():
            if matcher in verb:
                return 200, resp
        raise AssertionError(f"unexpected verb {verb}")


def test_authorize_derives_region_and_s3_endpoint():
    mt = MockTransport()
    auth = pb.authorize(FAKE_MASTER_ID, FAKE_MASTER_KEY, http_get=mt.get)
    assert auth["apiUrl"] == "https://api004.backblazeb2.com"
    assert auth["accountId"] == "acc-fake"
    assert auth["region"] == "us-west-004"
    assert pb.derive_endpoint(auth["region"]) == "s3.us-west-004.backblazeb2.com"


def test_authorize_fail_closed_on_bad_status():
    mt = MockTransport()
    mt.get = lambda u, b, t: (401, {"message": "bad_auth"})
    with pytest.raises(pb.ProvisionError):
        pb.authorize(FAKE_MASTER_ID, FAKE_MASTER_KEY, http_get=mt.get)


def _full_responses():
    return {
        "b2_list_buckets": {"buckets": []},
        "b2_create_bucket": {"bucketId": "bkt-fake", "bucketName": "hermes-state",
                             "bucketType": "allPrivate"},
        "b2_create_key": {"applicationKeyId": "kid-fake",
                          "applicationKey": "k-fake-app-key"},
    }


def test_provision_creates_private_bucket_and_scoped_keys(tmp_path, monkeypatch):
    monkeypatch.setenv(pb.MASTER_KEY_ID_ENV, FAKE_MASTER_ID)
    monkeypatch.setenv(pb.MASTER_KEY_ENV, FAKE_MASTER_KEY)
    mt = MockTransport(_full_responses())
    out = tmp_path / "prov"
    meta = pb.provision(bucket="hermes-state", out_dir=str(out),
                        http_get=mt.get, http_post=mt.post)

    verbs = [c[0] for c in mt.calls if c[0] != "GET"]
    assert verbs[0] == "b2_list_buckets"
    assert "b2_create_bucket" in verbs
    # bucket must be private
    bucket_body = next(c[1] for c in mt.calls if c[0] == "b2_create_bucket")
    assert bucket_body["bucketType"] == "allPrivate"
    assert bucket_body["bucketName"] == "hermes-state"
    # two keys, correctly scoped
    key_bodies = [c[1] for c in mt.calls if c[0] == "b2_create_key"]
    assert len(key_bodies) == 2
    runtime = next(k for k in key_bodies if k["keyName"] == "hermes-runtime")
    bootstrap = next(k for k in key_bodies if k["keyName"] == "hermes-secret-reader")
    assert runtime["bucketId"] == "bkt-fake"
    assert runtime["capabilities"] == ["readFiles", "writeFiles", "deleteFiles"]
    assert "namePrefix" not in runtime
    assert bootstrap["capabilities"] == ["readFiles"]
    assert bootstrap["namePrefix"] == "secrets/"
    assert bootstrap["bucketId"] == "bkt-fake"
    # metadata never leaks key material
    s = json.dumps(meta)
    assert "k-fake-app-key" not in s and "kid-fake" not in s
    assert meta["region"] == "us-west-004"


def test_provision_reuses_existing_bucket(tmp_path, monkeypatch):
    monkeypatch.setenv(pb.MASTER_KEY_ID_ENV, FAKE_MASTER_ID)
    monkeypatch.setenv(pb.MASTER_KEY_ENV, FAKE_MASTER_KEY)
    resp = _full_responses()
    resp["b2_list_buckets"] = {"buckets": [{"bucketId": "bkt-old",
                                            "bucketName": "hermes-state"}]}
    mt = MockTransport(resp)
    meta = pb.provision(bucket="hermes-state", out_dir=str(tmp_path / "p"),
                        http_get=mt.get, http_post=mt.post)
    assert meta["bucket_created"] is False
    assert not any(c[0] == "b2_create_bucket" for c in mt.calls)


def test_env_files_are_0600_and_master_key_independent(tmp_path, monkeypatch):
    monkeypatch.setenv(pb.MASTER_KEY_ID_ENV, FAKE_MASTER_ID)
    monkeypatch.setenv(pb.MASTER_KEY_ENV, FAKE_MASTER_KEY)
    mt = MockTransport(_full_responses())
    out = tmp_path / "p2"
    meta = pb.provision(bucket="hermes-state", out_dir=str(out),
                        http_get=mt.get, http_post=mt.post)
    for rel in ("b2-runtime.env", "b2-bootstrap.env", "master-key.env"):
        p = out / rel
        assert p.exists()
        assert (os.stat(p).st_mode & 0o777) == 0o600
    mk = (out / "master-key.env").read_text()
    assert mk.startswith("HERMES_MASTER_KEY=")
    key_hex = mk.splitlines()[0].split("=", 1)[1]
    assert len(key_hex) == 64
    # master key must NOT equal or contain any B2 material
    assert "k-fake-app-key" not in mk
    # runtime env has endpoint + bucket + both creds; bootstrap env has prefix-scope creds
    rt = (out / "b2-runtime.env").read_text()
    bb = (out / "b2-bootstrap.env").read_text()
    for field in ("B2_ENDPOINT=s3.us-west-004.backblazeb2.com", "B2_BUCKET=hermes-state",
                  "B2_APPLICATION_KEY_ID=", "B2_APPLICATION_KEY="):
        assert field in rt
    assert "B2_BOOTSTRAP_APPLICATION_KEY" in bb
    # master key must not be anywhere in the B2 env files
    assert key_hex not in rt and key_hex not in bb


def test_provision_requires_master_env(tmp_path, monkeypatch):
    monkeypatch.delenv(pb.MASTER_KEY_ID_ENV, raising=False)
    monkeypatch.delenv(pb.MASTER_KEY_ENV, raising=False)
    with pytest.raises(pb.ProvisionError):
        pb.provision(bucket="hermes-state", out_dir=str(tmp_path / "x"))


def test_logs_and_errors_never_contain_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(pb.MASTER_KEY_ID_ENV, FAKE_MASTER_ID)
    monkeypatch.setenv(pb.MASTER_KEY_ENV, FAKE_MASTER_KEY)
    mt = MockTransport(_full_responses())
    logs = []
    pb.provision(bucket="hermes-state", out_dir=str(tmp_path / "z"),
                 http_get=mt.get, http_post=mt.post, log=logs.append)
    blob = "\n".join(logs) + capsys.readouterr().out
    assert FAKE_MASTER_KEY not in blob and FAKE_MASTER_ID not in blob


def test_s3_probe_full_cycle():
    # unit-level: probe sequence enforced through a fake R2Client
    store = {}

    class FakeClient:
        def __init__(self, **kw):
            self.kw = kw

        def put_object(self, k, data, **kw): store[k] = data
        def get_object(self, k): return store.get(k)
        def head_object(self, k): return {}
        def delete_object(self, k): store.pop(k, None)
        def list_objects(self, prefix=""):
            return [k for k in store if k.startswith(prefix)]

    import hermes_persist.provision_b2 as m
    import hermes_persist.r2 as r2mod
    orig = r2mod.R2Client
    r2mod.R2Client = FakeClient
    try:
        res = m.s3_probe(endpoint="s3.us-west-004.backblazeb2.com", bucket="hermes-state",
                         key_id="kid", key="k")
        assert res == {"put": True, "get": True, "list": True, "delete": True}
        assert store == {}
    finally:
        r2mod.R2Client = orig
