"""Snapshot manifest: build, serialize, validate, verify hashes.

A manifest is the contract for a snapshot: schema_version, hermes version,
commit, environment id, UTC timestamp, and per-object key/category/size/sha256.
Never contains secret values — only curated PERSISTENT objects are listed
(catalog.classify == PERSISTENT), and even their *names* are already known-safe
(config.yaml, state.db, sessions/sessions.json, …).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import MANIFEST_SCHEMA_VERSION
from .catalog import iter_persistent

REQUIRED_TOP = ("schema_version", "hermes_version", "commit", "env_id", "created_at", "objects")
REQUIRED_OBJ = ("key", "category", "size", "sha256")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_id(ts: Optional[str] = None) -> str:
    """Immutable snapshot key fragment (UTC, lexicographically sortable)."""
    return (ts or now_utc()).replace("-", "").replace(":", "")


def build_manifest(home: Path, *, hermes_version: str, commit: str, env_id: str,
                   created_at: Optional[str] = None) -> Dict[str, Any]:
    home = Path(home)
    objects: List[Dict[str, Any]] = []
    for rel, path in iter_persistent(home):
        objects.append({
            "key": f"state/{rel}",
            "category": rel.split("/")[0] if "/" in rel else rel,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "hermes_version": hermes_version,
        "commit": commit,
        "env_id": env_id,
        "created_at": created_at or now_utc(),
        "objects": objects,
    }


def validate_manifest(m: Any) -> List[str]:
    """Structural validation (no jsonschema dependency). Returns error strings."""
    errs: List[str] = []
    if not isinstance(m, dict):
        return ["manifest is not an object"]
    for k in REQUIRED_TOP:
        if k not in m:
            errs.append(f"missing top-level key: {k}")
    if m.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errs.append(f"unsupported schema_version: {m.get('schema_version')!r}")
    objs = m.get("objects")
    if not isinstance(objs, list):
        errs.append("objects is not a list")
        return errs
    for i, o in enumerate(objs):
        if not isinstance(o, dict):
            errs.append(f"objects[{i}] not an object")
            continue
        for k in REQUIRED_OBJ:
            if k not in o:
                errs.append(f"objects[{i}] missing {k}")
        sha = o.get("sha256")
        if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
            errs.append(f"objects[{i}] bad sha256")
        size = o.get("size")
        if not (isinstance(size, int) and size >= 0):
            errs.append(f"objects[{i}] bad size")
        key = o.get("key", "")
        if not (isinstance(key, str) and key and ".." not in key and not key.startswith("/")):
            errs.append(f"objects[{i}] bad key")
    return errs


def verify_bytes(objects: Dict[str, bytes], manifest: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Verify fetched object bytes against manifest hashes/sizes. objects: key->bytes."""
    problems: List[str] = []
    for o in manifest.get("objects", []):
        key, want_sha, want_size = o["key"], o["sha256"], o["size"]
        data = objects.get(key)
        if data is None:
            problems.append(f"MISSING {key}")
            continue
        if len(data) != want_size:
            problems.append(f"SIZE-MISMATCH {key}: {len(data)} != {want_size}")
            continue
        if sha256_bytes(data) != want_sha:
            problems.append(f"HASH-MISMATCH {key}")
    return (not problems, problems)


def verify_home(home: Path, manifest: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Verify restored files on disk against the manifest."""
    objects: Dict[str, bytes] = {}
    home = Path(home)
    for o in manifest.get("objects", []):
        rel = o["key"][len("state/"):] if o["key"].startswith("state/") else o["key"]
        p = home / rel
        if p.is_file():
            objects[o["key"]] = p.read_bytes()
    return verify_bytes(objects, manifest)


def dumps(m: Dict[str, Any]) -> str:
    return json.dumps(m, indent=2, sort_keys=True) + "\n"


def loads(text: str) -> Dict[str, Any]:
    return json.loads(text)
