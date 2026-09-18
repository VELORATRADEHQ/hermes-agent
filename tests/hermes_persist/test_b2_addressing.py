"""Backblaze B2 endpoint addressing on the pure-stdlib R2Client:
virtual-host style for backblazeb2.com, path-style elsewhere,
SigV4 region auto-derived from endpoint, explicit overrides honored."""
import pytest

from hermes_persist.r2 import R2Client

KW = dict(access_key_id="K" * 20, secret_access_key="S" * 40)


def test_b2_virtual_hosted_and_region():
    c = R2Client(endpoint="https://s3.us-west-004.backblazeb2.com", bucket="hermes-state", **KW)
    assert c._base == "https://hermes-state.s3.us-west-004.backblazeb2.com"
    assert c._path_prefix == ""
    assert c._addressing == "virtual"
    assert c._region == "us-west-004"  # derived from endpoint host


def test_b2_scheme_added_when_missing():
    c = R2Client(endpoint="s3.eu-central-003.backblazeb2.com", bucket="bkt", **KW)
    assert c._base == "https://bkt.s3.eu-central-003.backblazeb2.com"
    assert c._region == "eu-central-003"


def test_r2_style_path_by_default():
    c = R2Client(endpoint="https://abc123.r2.cloudflarestorage.com", bucket="bkt", **KW)
    assert c._base == "https://abc123.r2.cloudflarestorage.com"
    assert c._path_prefix == "/bkt"
    assert c._addressing == "path"
    assert c._region == "auto"


def test_force_path_on_b2_and_force_virtual_elsewhere():
    c = R2Client(endpoint="https://s3.us-west-004.backblazeb2.com", bucket="bkt",
                 addressing="path", **KW)
    assert c._path_prefix == "/bkt"
    c2 = R2Client(endpoint="https://minio.lan", bucket="bkt", addressing="virtual",
                  region="us-east-1", **KW)
    assert c2._base == "https://bkt.minio.lan"
    assert c2._region == "us-east-1"


def test_region_override_wins_over_derivation():
    c = R2Client(endpoint="https://s3.us-west-004.backblazeb2.com", bucket="bkt",
                 region="us-east-1", **KW)
    assert c._region == "us-east-1"


def test_signed_url_shapes(monkeypatch):
    # capture the signed request without doing real IO
    seen = {}
    import urllib.request

    class R:
        status = 200
        headers = {}

        def read(self):
            return b"b"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeResp:
        def __init__(self):
            self.status = 200
            self.headers = R.headers

        def read(selfs):
            return b"b"

        def __enter__(selfs):
            return selfs

        def __exit__(selfs, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization", "")
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = R2Client(endpoint="https://s3.us-west-004.backblazeb2.com", bucket="hermes-state", **KW)
    c.get_object("secrets/secrets.enc")
    assert seen["url"] == "https://hermes-state.s3.us-west-004.backblazeb2.com/secrets/secrets.enc"
    assert "us-west-004/s3/aws4_request" in seen["auth"]
