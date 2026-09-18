"""Manifest build/validate/verify incl. corruption & missing-object detection."""
from pathlib import Path

from hermes_persist import manifest as mf


def _home(tmp_path: Path) -> Path:
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n")
    (tmp_path / "state.db").write_bytes(b"db-bytes-v1")
    (tmp_path / "sessions" / "sessions.json").write_text("{}")
    return tmp_path


def test_build_and_validate(tmp_path):
    m = mf.build_manifest(_home(tmp_path), hermes_version="0.21.3", commit="abc123",
                          env_id="test-env", created_at="2026-09-18T00:00:00Z")
    assert m["schema_version"] == 1
    assert m["hermes_version"] == "0.21.3"
    assert mf.validate_manifest(m) == []
    keys = {o["key"] for o in m["objects"]}
    assert "state/config.yaml" in keys and "state/state.db" in keys
    for o in m["objects"]:
        assert len(o["sha256"]) == 64 and o["size"] >= 0


def test_verify_bytes_detects_everything(tmp_path):
    m = mf.build_manifest(_home(tmp_path), hermes_version="x", commit="c", env_id="e")
    good = {o["key"]: (tmp_path / o["key"][len("state/"):]).read_bytes() for o in m["objects"]}
    ok, probs = mf.verify_bytes(good, m)
    assert ok and probs == []
    # missing object
    bad_missing = dict(good); bad_missing.pop("state/state.db")
    ok, probs = mf.verify_bytes(bad_missing, m)
    assert not ok and any(p.startswith("MISSING") for p in probs)
    # corrupted object
    bad_bytes = dict(good); bad_bytes["state/state.db"] = b"tampered!!!"
    ok, probs = mf.verify_bytes(bad_bytes, m)
    assert not ok and any("MISMATCH" in p for p in probs)


def test_validate_rejects_garbage():
    assert mf.validate_manifest(None) == ["manifest is not an object"]
    assert mf.validate_manifest({})  # missing keys
    bad = {"schema_version": 99, "hermes_version": "x", "commit": "c", "env_id": "e",
           "created_at": "t", "objects": [{"key": "../escape", "category": "c",
                                           "size": -1, "sha256": "zz"}]}
    errs = mf.validate_manifest(bad)
    assert any("schema_version" in e for e in errs)
    assert any("bad key" in e for e in errs)
    assert any("bad sha256" in e for e in errs)
    assert any("bad size" in e for e in errs)


def test_verify_home_roundtrip(tmp_path):
    home = _home(tmp_path)
    m = mf.build_manifest(home, hermes_version="x", commit="c", env_id="e")
    ok, probs = mf.verify_home(home, m)
    assert ok and probs == []
    (home / "state.db").write_bytes(b"changed")
    ok, probs = mf.verify_home(home, m)
    assert not ok and any("MISMATCH" in p for p in probs)
