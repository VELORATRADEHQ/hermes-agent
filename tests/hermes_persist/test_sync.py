"""Backup/restore/verify orchestration with an in-memory fake R2 client.

Covers: snapshot creation + manifest, hash verification, current-pointer promotion,
second-writer lockout, restore refusing to touch secrets, corrupted snapshot
rejection, missing-object detection, local pre-restore safety copy.
"""
from pathlib import Path

import pytest

from hermes_persist import manifest as mf, sync
from hermes_persist.r2 import PreconditionFailed


class FakeR2:
    def __init__(self):
        self.store = {}
        self.cond_fail_supported = True

    def put_object(self, key, data, *, if_none_match_star=False):
        if if_none_match_star and self.cond_fail_supported and key in self.store:
            raise PreconditionFailed(key)
        self.store[key] = bytes(data)

    def get_object(self, key):
        return self.store.get(key)

    def head_object(self, key):
        return {"etag": "x"} if key in self.store else None

    def delete_object(self, key):
        self.store.pop(key, None)

    def list_objects(self, prefix="", limit=1000):
        return sorted(k for k in self.store if k.startswith(prefix))[:limit]


def _home(tmp: Path, seeded=True) -> Path:
    home = tmp / "home"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "pairing").mkdir(exist_ok=True)
    if seeded:
        (home / "config.yaml").write_text("model:\n  default: m\n")
        (home / "state.db").write_bytes(b"db-v1")
        (home / "sessions" / "sessions.json").write_text('{"agent:main:telegram:dm:123": {}}')
        (home / "pairing" / "approved.json").write_text('{"approved": ["123"]}')
    # secrets that must never be uploaded or touched
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=111:FAKEFAKEFAKEFAKE\nGEMINI_API_KEY=AQ.FAKE\n")
    (home / "auth.json").write_text('{"stored": "creds"}')
    return home


def test_backup_creates_immutable_snapshot_and_current(tmp_path):
    home = _home(tmp_path)
    r2 = FakeR2()
    sid = sync.backup(home, r2, env_id="envA", hermes_version="0.21.3", commit="d213a2a")
    assert f"snapshots/{sid}/manifest.json" in r2.store
    assert f"manifests/{sid}.json" in r2.store
    assert "manifests/current.json" in r2.store
    cur = mf.loads(r2.store["manifests/current.json"].decode())
    assert mf.snapshot_id(cur["created_at"]) == sid
    keys = {o["key"] for o in cur["objects"]}
    assert "state/state.db" in keys
    # no secret path was ever written into the bucket
    joined = "\n".join(r2.store.keys())
    for forbidden in (".env", "auth.json", "creds.env", "_runtime_env.sh"):
        assert forbidden not in joined
    # no secret bytes anywhere in stored payloads
    blob = b"".join(r2.store.values())
    assert b"FAKEFAKEFAKEFAKE" not in blob and b"AQ.FAKE" not in blob
    # lock released after backup
    assert sync.LOCK_KEY not in r2.store


def test_second_writer_lockout(tmp_path):
    r2 = FakeR2()
    sync.acquire_writer_lock(r2, "envA")
    with pytest.raises(sync.WriterLockActive):
        sync.acquire_writer_lock(r2, "envB")
    sync.release_writer_lock(r2, "envA")
    sync.acquire_writer_lock(r2, "envB")  # now fine
    # envA cannot release envB's lock
    sync.release_writer_lock(r2, "envA")
    assert sync.LOCK_KEY in r2.store


def test_verify_and_restore_roundtrip_new_server(tmp_path):
    home = _home(tmp_path)
    r2 = FakeR2()
    sid = sync.backup(home, r2, env_id="envA", hermes_version="0.21.3", commit="d213a2a")

    # A brand-new server: empty home with only locally-provided secrets
    new_home = tmp_path / "fresh"
    new_home.mkdir()
    (new_home / ".env").write_text("TELEGRAM_BOT_TOKEN=222:NEWNEW\n")

    ok, probs, got_sid = sync.verify_snapshot(r2, sid)
    assert ok and probs == [] and got_sid == sid
    sid_out, restored = sync.restore(new_home, r2, env_id="envB", sid=sid)
    assert sid_out == sid
    assert "state.db" in restored and "config.yaml" in restored
    assert (new_home / "state.db").read_bytes() == b"db-v1"
    assert (new_home / "pairing" / "approved.json").read_text() == '{"approved": ["123"]}'
    # secrets on the new server were never read or modified by restore
    assert (new_home / ".env").read_text() == "TELEGRAM_BOT_TOKEN=222:NEWNEW\n"
    assert not (new_home / "auth.json").exists()
    ok2, probs2 = mf.verify_home(new_home, mf.loads(r2.store[f"snapshots/{sid}/manifest.json"].decode()))
    assert ok2 and probs2 == []


def test_restore_rejects_corrupted_snapshot(tmp_path):
    home = _home(tmp_path)
    r2 = FakeR2()
    sid = sync.backup(home, r2, env_id="envA", hermes_version="0.21.3", commit="d213a2a")
    r2.store[f"snapshots/{sid}/state/state.db"] = b"db-v9"  # same size as "db-v1", different content
    ok, probs, _ = sync.verify_snapshot(r2, sid)
    assert not ok and any("HASH-MISMATCH" in p for p in probs)
    new_home = tmp_path / "fresh"; new_home.mkdir()
    with pytest.raises(sync.StateSyncError):
        sync.restore(new_home, r2, env_id="envB", sid=sid)
    assert not (new_home / "state.db").exists()


def test_verify_detects_missing_object(tmp_path):
    home = _home(tmp_path)
    r2 = FakeR2()
    sid = sync.backup(home, r2, env_id="envA", hermes_version="0.21.3", commit="d213a2a")
    del r2.store[f"snapshots/{sid}/state/state.db"]
    ok, probs, _ = sync.verify_snapshot(r2, sid)
    assert not ok and any("MISSING" in p for p in probs)


def test_restore_makes_local_safety_copy_and_no_snapshot_leak(tmp_path):
    home = _home(tmp_path)
    r2 = FakeR2()
    sid1 = sync.backup(home, r2, env_id="envA", hermes_version="0.21.3", commit="c1")
    (home / "state.db").write_bytes(b"db-v2")
    _sid, restored = sync.restore(home, r2, env_id="envA", sid=sid1)
    safety = list((home / "backups" / "pre-restore").glob("*/state.db"))
    assert safety and any(p.read_bytes() == b"db-v2" for p in safety)
    # the local backups/ dir itself is EPHEMERAL and is never synced to R2
    assert not any("backups/pre-restore" in k for k in r2.store)


def test_healthcheck_presence_only(tmp_path, monkeypatch):
    home = _home(tmp_path)
    r2 = FakeR2()
    sync.backup(home, r2, env_id="envA", hermes_version="0.21.3", commit="c")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rep = sync.healthcheck(home, r2)
    assert rep["ok"] is True                      # secrets found in .env (present, not printed)
    assert rep["checks"]["secrets_env"]["TELEGRAM_BOT_TOKEN"] == "PRESENT"
    assert "FAKEFAKE" not in str(rep)
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    rep2 = sync.healthcheck(empty_home, r2)
    assert rep2["ok"] is False
    assert rep2["checks"]["secrets_env"]["TELEGRAM_BOT_TOKEN"] == "MISSING"
