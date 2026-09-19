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


def _env_first(*names: str) -> str:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return ""


def _client(args) -> R2Client:
    """Backend is configurable: Backblaze B2 (B2_*), generic S3 (S3_*) or Cloudflare R2 (R2_*).
    Names only — values are never printed or logged."""
    endpoint = _env_first("B2_ENDPOINT", "S3_ENDPOINT", "R2_ENDPOINT")
    bucket = _env_first("B2_BUCKET", "S3_BUCKET", "R2_BUCKET")
    ak = _env_first("B2_APPLICATION_KEY_ID", "S3_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID")
    sk = _env_first("B2_APPLICATION_KEY", "S3_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY")
    missing = [n for n, v in (("B2_ENDPOINT|S3_ENDPOINT|R2_ENDPOINT", endpoint),
                              ("B2_BUCKET|S3_BUCKET|R2_BUCKET", bucket),
                              ("B2_APPLICATION_KEY_ID|…|R2_ACCESS_KEY_ID", ak),
                              ("B2_APPLICATION_KEY|…|R2_SECRET_ACCESS_KEY", sk)) if not v]
    if missing:
        raise StateSyncError(f"missing storage env vars (any alias works): {', '.join(missing)}")
    return R2Client(endpoint=endpoint, bucket=bucket, access_key_id=ak, secret_access_key=sk)


def _home(args) -> Path:
    return Path(args.home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _env_id(args) -> str:
    return args.env_id or os.environ.get("HERMES_ENV_ID") or socket.gethostname()


def _master_key() -> bytes:
    from . import secrets as sec
    raw = os.environ.get("SECRETS_MASTER_KEY", "")
    if not raw:
        raise StateSyncError("SECRETS_MASTER_KEY missing (64 hex chars, injected via runtime env)")
    return sec.normalize_master_key(raw)


def _bootstrap_client() -> R2Client:
    """Read-only bootstrap credential: B2_BOOTSTRAP_* (prefix-scoped to secrets/).
    Falls back to the main storage credential when the deployment has no split yet
    (documented in hermes-ops/README.md — split is the target posture)."""
    endpoint = _env_first("B2_BOOTSTRAP_ENDPOINT", "B2_ENDPOINT", "S3_ENDPOINT", "R2_ENDPOINT")
    bucket = _env_first("B2_BOOTSTRAP_BUCKET", "B2_BUCKET", "S3_BUCKET", "R2_BUCKET")
    ak = _env_first("B2_BOOTSTRAP_APPLICATION_KEY_ID",
                    "B2_APPLICATION_KEY_ID", "S3_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID")
    sk = _env_first("B2_BOOTSTRAP_APPLICATION_KEY",
                    "B2_APPLICATION_KEY", "S3_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY")
    if not all((endpoint, bucket, ak, sk)):
        raise StateSyncError("missing bootstrap storage env (B2_BOOTSTRAP_* or B2_*)")
    return R2Client(endpoint=endpoint, bucket=bucket, access_key_id=ak, secret_access_key=sk)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hermes_persist")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("backup", "restore", "verify", "healthcheck",
                 "secrets-put", "secrets-fetch", "secrets-check",
                 "telegram-health", "supervisor"):
        s = sub.add_parser(name)
        s.add_argument("--home")
        s.add_argument("--env-id")
        if name in ("restore", "verify"):
            s.add_argument("--sid", default=None, help="snapshot id (default: current)")
        if name == "restore":
            s.add_argument("--force", action="store_true", help="skip writer lock / adopt")
        if name == "healthcheck":
            s.add_argument("--no-r2", action="store_true")
        if name == "secrets-put":
            s.add_argument("--names", default=None,
                           help="comma list of secret names to include (default: standard set)")
        if name == "secrets-fetch":
            s.add_argument("--write", action="store_true",
                           help="write ~/.hermes/creds.env 0600 (default: verify only, no disk)")
        if name == "telegram-health":
            s.add_argument("--no-probe", action="store_true")
        if name == "supervisor":
            s.add_argument("child", nargs=argparse.REMAINDER, help="-- <gateway command>")
    args = p.parse_args(argv)
    home = _home(args)
    try:
        if args.cmd == "telegram-health":
            from . import telegram_health
            rep = telegram_health.check(home, probe=not args.no_probe)
            print(json.dumps(rep, indent=2))
            return 0 if rep.get("ok") else 3
        if args.cmd == "supervisor":
            from . import supervise, telegram_health
            cmd = list(getattr(args, "child", []) or [])
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            if not cmd:
                print("supervisor: missing child command after --", file=sys.stderr)
                return 2
            def _health():
                return telegram_health.check(home)
            pidfile = Path(home) / "state" / "gw.pid"
            res = supervise.supervise(cmd, health_fn=_health, pidfile=pidfile)
            print(f"supervisor HALT: {res.halted_reason} restarts={res.restarts} health_restarts={res.health_restarts}")
            return 1 if res.restarts else 0
        if args.cmd.startswith("secrets-"):
            return _secrets_cmd(args, home)
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


def _secrets_cmd(args, home) -> int:
    from . import secrets as sec, sync
    key = _master_key()
    if args.cmd == "secrets-put":
        client = _client(args)  # RW credential
        names = tuple(n.strip() for n in args.names.split(",")) if args.names else None
        size, included = sync.upload_secrets(client, home, key, names or sync.DEFAULT_SECRET_NAMES)
        print(f"secrets-put OK object={sync.SECRETS_OBJECT_KEY} bytes={size} names={','.join(included)}")
        return 0
    if args.cmd == "secrets-fetch":
        values = sync.fetch_secrets(_bootstrap_client(), key)
        print(f"secrets-fetch OK names={','.join(sorted(values))} (values not printed)")
        if args.write:
            path = sync.write_creds_env(home, values)
            print(f"creds written: {path} (0600)")
        return 0
    if args.cmd == "secrets-check":
        blob = _bootstrap_client().get_object(sync.SECRETS_OBJECT_KEY)
        if blob is None:
            print("secrets-check FAILED: object missing", file=sys.stderr)
            return 3
        meta = sec.secrets_names_in_envelope(blob)
        values = sync.fetch_secrets(_bootstrap_client(), key)  # full auth check
        print(f"secrets-check OK v={meta['v']} alg={meta['alg']} kdf={meta['kdf']} "
              f"names={','.join(sorted(values))}")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
