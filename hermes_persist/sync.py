"""Snapshot orchestration: backup / restore / verify / healthcheck.

Layout inside the bucket (single dedicated bucket, e.g. ``hermes-state``):

  snapshots/<sid>/state/<rel>            immutable curated PERSISTENT objects
  snapshots/<sid>/manifest.json          the snapshot's manifest (immutable)
  manifests/<sid>.json                   manifest mirror (immutable)
  manifests/current.json                 the single mutable "current" pointer (write-and-promote)
  locks/state-writer.lock                best-effort single-writer claim (env_id + ttl)

Secrets are never uploaded: the catalog's include-set contains no secret paths,
and backup() runs a final leak guard that refuses any path not classified PERSISTENT.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import manifest as mf
from .catalog import classify, iter_persistent
from .r2 import PreconditionFailed, R2Client, R2Error

LOCK_KEY = "locks/state-writer.lock"
LOCK_TTL_SECONDS = 900


class StateSyncError(RuntimeError):
    pass


class WriterLockActive(StateSyncError):
    def __init__(self, holder: str):
        super().__init__(f"another runtime holds the writer lock: {holder}")


def _sid() -> str:
    return mf.snapshot_id()


def acquire_writer_lock(client: R2Client, env_id: str, *, force: bool = False) -> None:
    """Best-effort single-writer claim via conditional PUT; immutable snapshots mean a
    second writer can never corrupt existing snapshots, only produce a new one."""
    body = json.dumps({
        "env_id": env_id,
        "acquired_at": mf.now_utc(),
        "ttl_seconds": LOCK_TTL_SECONDS,
    }).encode()
    try:
        client.put_object(LOCK_KEY, body, if_none_match_star=True)
        return
    except PreconditionFailed:
        pass
    except R2Error as exc:
        if exc.status in (400, 501):  # conditional writes unsupported → documented best-effort
            if client.head_object(LOCK_KEY) is None:
                client.put_object(LOCK_KEY, body)
                return
    existing = client.get_object(LOCK_KEY)
    if force or existing is None:
        client.put_object(LOCK_KEY, body)
        return
    holder = "unknown"
    try:
        holder = json.loads(existing).get("env_id", "unknown")
    except Exception:
        pass
    raise WriterLockActive(holder)


def release_writer_lock(client: R2Client, env_id: str) -> None:
    existing = client.get_object(LOCK_KEY)
    if existing:
        try:
            if json.loads(existing).get("env_id") != env_id:
                return  # never release someone else's lock
        except Exception:
            return
    client.delete_object(LOCK_KEY)


def leak_guard(home: Path) -> List[str]:
    """Files iterated as PERSISTENT that the classifier disagrees with → refuse."""
    return [rel for rel, _ in iter_persistent(home) if classify(rel) != "PERSISTENT"]


def backup(home: Path, client: R2Client, *, env_id: str, hermes_version: str, commit: str,
           lock: bool = True) -> str:
    home = Path(home)
    bad = leak_guard(home)
    if bad:
        raise StateSyncError(f"leak guard refused paths: {bad}")
    sid = _sid()
    if lock:
        acquire_writer_lock(client, env_id)
    try:
        m = mf.build_manifest(home, hermes_version=hermes_version, commit=commit, env_id=env_id)
        errs = mf.validate_manifest(m)
        if errs:
            raise StateSyncError(f"manifest invalid: {errs}")
        prefix = f"snapshots/{sid}/"
        for rel, path in iter_persistent(home):
            client.put_object(prefix + "state/" + rel, path.read_bytes(), if_none_match_star=True)
        client.put_object(prefix + "manifest.json", mf.dumps(m).encode(), if_none_match_star=True)
        client.put_object(f"manifests/{sid}.json", mf.dumps(m).encode(), if_none_match_star=True)
        client.put_object("manifests/current.json", mf.dumps(m).encode())  # promote last
        return sid
    finally:
        if lock:
            try:
                release_writer_lock(client, env_id)
            except Exception:
                pass


def current_manifest(client: R2Client) -> Optional[Dict[str, Any]]:
    data = client.get_object("manifests/current.json")
    if data is None:
        return None
    m = mf.loads(data.decode("utf-8"))
    errs = mf.validate_manifest(m)
    if errs:
        raise StateSyncError(f"current manifest invalid: {errs}")
    return m


def verify_snapshot(client: R2Client, sid: Optional[str] = None
                    ) -> Tuple[bool, List[str], Optional[str]]:
    """Verify a snapshot's objects against its manifest. Returns (ok, problems, sid)."""
    if sid is None or sid == "current":
        m = current_manifest(client)
        if m is None:
            return False, ["no current manifest"], None
        sid = mf.snapshot_id(m["created_at"])
    prefix = f"snapshots/{sid}/"
    raw = client.get_object(prefix + "manifest.json") or client.get_object(f"manifests/{sid}.json")
    if raw is None:
        return False, [f"manifest not found for snapshot {sid}"], sid
    m = mf.loads(raw.decode("utf-8"))
    errs = mf.validate_manifest(m)
    problems = [f"manifest: {e}" for e in errs]
    objects: Dict[str, bytes] = {}
    for o in m.get("objects", []):
        data = client.get_object(prefix + o["key"])
        if data is not None:
            objects[o["key"]] = data
    ok_bytes, probs = mf.verify_bytes(objects, m)
    problems.extend(probs)
    return (ok_bytes and not errs), problems, sid


def restore(home: Path, client: R2Client, *, env_id: str, sid: Optional[str] = None,
            force: bool = False) -> Tuple[str, List[str]]:
    """Restore a VERIFIED snapshot into ``home``. Never touches secrets: existing
    .env/auth.json/creds files in home are neither read nor modified. Local
    pre-restore safety copy → <home>/backups/pre-restore/<ts>/.
    Returns (sid, restored_relative_paths)."""
    home = Path(home)
    ok, problems, sid = verify_snapshot(client, sid)
    if not ok:
        raise StateSyncError(f"snapshot failed verification, restore aborted: {problems}")
    raw = client.get_object(f"snapshots/{sid}/manifest.json") or client.get_object(f"manifests/{sid}.json")
    m = mf.loads(raw.decode("utf-8"))  # type: ignore[union-attr]

    locked = False
    if not force:
        acquire_writer_lock(client, env_id)
        locked = True
    try:
        prefix = f"snapshots/{sid}/"
        fetched: Dict[str, bytes] = {}
        for o in m["objects"]:
            rel = o["key"][len("state/"):]
            data = client.get_object(prefix + o["key"])
            if data is None:
                raise StateSyncError(f"object vanished mid-restore: {o['key']}")
            fetched[rel] = data

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety = home / "backups" / "pre-restore" / ts
        for rel, _old in iter_persistent(home):
            src = home / rel
            if src.is_file():
                dst = safety / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        restored: List[str] = []
        for rel, data in fetched.items():
            if classify(rel) != "PERSISTENT":
                raise StateSyncError(f"refusing to restore non-persistent path: {rel}")
            dst = home / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".restore-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
            restored.append(rel)
        ok2, problems2 = mf.verify_home(home, m)
        if not ok2:
            raise StateSyncError(f"post-restore verification failed: {problems2}")
        return sid, restored
    finally:
        if locked:
            try:
                release_writer_lock(client, env_id)
            except Exception:
                pass


def healthcheck(home: Path, client: Optional[R2Client], *,
                required_env: Tuple[str, ...] = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY")) -> Dict[str, Any]:
    """Presence-only checks; never returns secret values."""
    report: Dict[str, Any] = {"home": str(home), "checks": {}}
    c = report["checks"]

    def _env_present(name: str) -> bool:
        if os.environ.get(name):
            return True
        envf = Path(home) / ".env"
        if envf.is_file():
            try:
                for line in envf.read_text(errors="replace").splitlines():
                    if line.replace("export ", "").startswith(name + "="):
                        return bool(line.split("=", 1)[1].strip().strip('"').strip("'"))
            except Exception:
                return False
        return False

    c["secrets_env"] = {n: ("PRESENT" if _env_present(n) else "MISSING") for n in required_env}
    c["config_yaml"] = "PRESENT" if (Path(home) / "config.yaml").is_file() else "MISSING"

    if client is not None:
        try:
            keys25 = client.list_objects(prefix="manifests/", limit=25)
            c["r2_connect"] = "OK"
            c["r2_has_snapshots"] = any(k.endswith(".json") and "current" not in k for k in keys25)
        except Exception as exc:
            c["r2_connect"] = f"FAIL {type(exc).__name__}"
            c["r2_has_snapshots"] = "UNKNOWN"
        try:
            c["r2_current_manifest"] = "OK" if current_manifest(client) else "MISSING"
        except Exception as exc:
            c["r2_current_manifest"] = f"FAIL {type(exc).__name__}"
    else:
        c["r2_connect"] = "SKIPPED (no client)"

    c["persistent_files"] = sorted(rel for rel, _ in iter_persistent(Path(home))) if Path(home).is_dir() else []
    ok = all(v == "PRESENT" for v in c["secrets_env"].values()) and c["config_yaml"] == "PRESENT"
    if client is not None:
        ok = ok and c["r2_connect"] == "OK" and c["r2_current_manifest"] == "OK"
    report["ok"] = ok
    return report


# ── encrypted secrets backup (see secrets.py for the two-layer bootstrap design) ──
SECRETS_OBJECT_KEY = "secrets/secrets.enc"

DEFAULT_SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY",
                        "B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY",
                        "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def read_secret_values(home: Path, names: Tuple[str, ...] = DEFAULT_SECRET_NAMES) -> Dict[str, str]:
    """Collect secret values from process env, then ~/.hermes/.env / creds.env (0600).
    Returns only PRESENT non-empty names. Never prints values."""
    out: Dict[str, str] = {}
    for name in names:
        v = os.environ.get(name)
        if v:
            out[name] = v
    for fname in (".env", "creds.env", "_runtime_env.sh"):
        p = Path(home) / fname
        if not p.is_file():
            continue
        try:
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in names and v and k not in out:
                    out[k] = v
        except Exception:
            continue
    return out


def upload_secrets(client, home: Path, master_key: bytes,
                   names: Tuple[str, ...] = DEFAULT_SECRET_NAMES) -> Tuple[int, List[str]]:
    """Encrypt the PRESENT secrets and store them as ONE object ``secrets/secrets.enc``.
    Returns (payload_bytes, names_included) — names only, never values."""
    from . import secrets as sec
    values = read_secret_values(home, names)
    if not values:
        raise StateSyncError("no secret values found in env or ~/.hermes/.env/creds.env")
    blob = sec.encrypt_secrets(values, master_key)
    client.put_object(SECRETS_OBJECT_KEY, blob)
    return len(blob), sorted(values.keys())


def fetch_secrets(bootstrap_client, master_key: bytes) -> Dict[str, str]:
    """Download + decrypt secrets.enc IN MEMORY (bootstrap read-only client).
    Returns the name→value dict; caller decides where they may be written (0600)."""
    from . import secrets as sec
    blob = bootstrap_client.get_object(SECRETS_OBJECT_KEY)
    if blob is None:
        raise StateSyncError(f"encrypted secrets object not found: {SECRETS_OBJECT_KEY}")
    return sec.decrypt_secrets(blob, master_key)


def write_creds_env(home: Path, values: Dict[str, str], *,
                    filename: str = "creds.env") -> Path:
    """Atomically write KEY=VALUE lines with 0600. Values are never echoed by this function."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    target = home / filename
    lines = "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
    tmp = target.with_name(target.name + ".tmp")
    old_umask = os.umask(0o177)
    try:
        tmp.write_text(lines)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        os.umask(old_umask)
    return target
