"""CLI for ops tooling:  python3 -m hermes_state <backup|restore|verify|healthcheck>

Env (never printed):
  R2_ENDPOINT            e.g. <account_id>.r2.cloudflarestorage.com (https added if missing)
  R2_BUCKET              bucket name (single dedicated bucket)
  R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY   bucket-scoped S3 credential
  HERMES_HOME            state root (default: ~/.hermes)
  HERMES_ENV_ID          this runtime's identity for the writer lock (default: hostname)
  HERMES_VERSION / HERMES_COMMIT   recorded into the manifest
Exit codes: 0 ok; 2 operational failure; 3 verification/integrity failure.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from pathlib import Path

from . import StateSyncError, manifest as mf, sync
from .r2 import R2Client


def _client(args) -> R2Client:
    missing = [k for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
               if not os.environ.get(k)]
    if missing:
        raise StateSyncError(f"missing R2 env vars: {', '.join(missing)}")
    return R2Client(endpoint=os.environ["R2_ENDPOINT"], bucket=os.environ["R2_BUCKET"],
                    access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                    secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])


def _home(args) -> Path:
    return Path(args.home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _env_id(args) -> str:
    return args.env_id or os.environ.get("HERMES_ENV_ID") or socket.gethostname()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hermes_state")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("backup", "restore", "verify", "healthcheck"):
        s = sub.add_parser(name)
        s.add_argument("--home")
        s.add_argument("--env-id")
        if name in ("restore", "verify"):
            s.add_argument("--sid", default=None, help="snapshot id (default: current)")
        if name == "restore":
            s.add_argument("--force", action="store_true", help="skip writer lock / adopt")
        if name == "healthcheck":
            s.add_argument("--no-r2", action="store_true")
    args = p.parse_args(argv)
    home = _home(args)
    try:
        if args.cmd == "healthcheck" and args.no_r2:
            report = sync.healthcheck(home, None)
        else:
            client = _client(args)
            if args.cmd == "backup":
                sid = sync.backup(home, client, env_id=_env_id(args),
                                  hermes_version=os.environ.get("HERMES_VERSION", "unknown"),
                                  commit=os.environ.get("HERMES_COMMIT", "unknown"))
                print(f"backup OK snapshot={sid}")
                return 0
            if args.cmd == "verify":
                ok, problems, sid = sync.verify_snapshot(client, args.sid)
                print(f"verify {'OK' if ok else 'FAILED'} snapshot={sid}")
                for prob in problems:
                    print(f"  PROBLEM: {prob}")
                return 0 if ok else 3
            if args.cmd == "restore":
                sid, restored = sync.restore(home, client, env_id=_env_id(args),
                                             sid=args.sid, force=args.force)
                print(f"restore OK snapshot={sid} files={len(restored)}")
                for rel in restored:
                    print(f"  restored: {rel}")
                return 0
            if args.cmd == "healthcheck":
                report = sync.healthcheck(home, client)
        print(json.dumps(report, indent=2))
        return 0 if report.get("ok") else 2
    except StateSyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
