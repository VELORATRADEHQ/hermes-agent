"""hermes_state — curated persistent-state snapshots for Hermes Agent.

Design (see README-SECTION in hermes-ops/README.md):
- GitHub holds code only. R2 holds the durable state of record via immutable
  timestamped snapshots + a sha256 manifest + a single ``current`` pointer.
- Secrets (.env, auth.json, creds, tokens) are NEVER part of state objects.
- Only the curated PERSISTENT set (see ``catalog.py``) is synchronized; caches,
  logs, locks, pids, dumps, venvs and bundled skills are explicitly excluded.
- Single active writer enforced via a best-effort lock object ``locks/…``;
  snapshots are immutable so a second writer can never corrupt an existing one.

Stdlib-only by design: ops scripts must run on a bare fresh runtime
(Deepnote/VPS) with the system python, before the Hermes venv exists.
"""

__version__ = "0.1.0"
MANIFEST_SCHEMA_VERSION = 1


def __getattr__(name):  # lazy re-export without import cycles
    if name in ("StateSyncError", "WriterLockActive"):
        from . import sync
        return getattr(sync, name)
    raise AttributeError(name)
