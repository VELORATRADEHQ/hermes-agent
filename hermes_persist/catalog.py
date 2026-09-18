"""Curated state catalog: which files under HERMES_HOME are synchronized.

Classification: PERSISTENT / SECRET / CACHE / LOG / EPHEMERAL.

Only PERSISTENT entries are backed up. Everything else is explicitly excluded
so a careless `sync ~/.hermes` can never leak secrets or bloat snapshots with
caches. Rules are data (globs), deny wins over include.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
from typing import Iterator, Tuple

# ── PERSISTENT: durable state of record (user/identity/conversations/config) ──
INCLUDE_PERSISTENT = (
    "config.yaml",                       # global model/agent/gateway defaults + platform extra (authz lists)
    "state.db",                          # sessions, messages, gateway_routing (model pins), delivery, FTS
    "sessions/sessions.json",            # session index (NOT request dumps — see deny)
    "pairing/**",                        # pairing store (new layout)
    "platforms/pairing/**",              # pairing store (legacy layout; get_hermes_dir-aware)
    "memories/**",                       # long-term memory files
    "supermemory.json",                  # supermemory plugin config/state (no secrets inside)
    "SOUL.md",                           # operator-defined persona/custom instructions
    "channel_directory.json",            # channel registry metadata
    "kanban.db",                         # kanban tasks (user data)
    "cron/jobs.json",                    # cron job definitions (never outputs/heartbeats/locks)
    "state/cpanel-ui.json",              # control panel UI persistence
    "gateway_state.json",                # small gateway registry (non-secret fields only upstream)
)

# ── SECRET: never backup, never in manifests-as-content (names may be reported) ──
SECRET = (
    ".env", ".env.*", "creds.env", "_runtime_env.sh", "dn_keep.env",
    "auth.json", "**/*token*", "**/*secret*", "**/*.key", "**/*.pem",
)

# ── CACHE: re-downloadable/regenerable ──
CACHE = (
    "models_dev_cache*", "ollama_cloud_models_cache.json", "provider_models_cache.json",
    ".skills_prompt_snapshot.json", ".update_check",
    "cache/**", "audio_cache/**", "image_cache/**", "tts_cache/**",
)

# ── LOG: diagnostics/privacy — never leave the runtime ──
LOG = (
    "logs/**", "state/hermes-gateway.log", "gateway-starts.log", "ka.log", "ka_last",
    "sessions/request_dump_*.json",       # raw wire dumps: privacy + disposable
    "logs", "*.log",
)

# ── EPHEMERAL: runtime artifacts / locks / vendored code ──
EPHEMERAL = (
    "*.lock", "**/*.lock", "*.pid", "*.sock", "state/gateway.heartbeat", "state/gateway.loop-tick.*",
    "state/gw.pid", "cron/.jobs.lock", "cron/.tick.lock", "cron/ticker_*", "cron/output/**",
    "runtime/**", "sandboxes/**", "pending_messages/**", "hooks/**",
    "skills/**", "bin/**", "backups/**", ".install_id.lock",
    "state_db_migrations/**",
)

_DENY = SECRET + CACHE + LOG + EPHEMERAL


def _match_any(rel: str, patterns) -> bool:
    r = rel.replace(os.sep, "/")
    for pat in patterns:
        if fnmatch.fnmatch(r, pat) or fnmatch.fnmatch(PurePosixPath(r).name, pat):
            return True
    return False


def classify(rel: str) -> str:
    """Classify a home-relative path. Deny (secret/cache/log/ephemeral) wins."""
    rel = rel.replace(os.sep, "/")
    while rel.startswith("./"):  # strip "./" prefixes only — never a dotfile's dot
        rel = rel[2:]
    if _match_any(rel, _DENY):
        if _match_any(rel, SECRET):
            return "SECRET"
        if _match_any(rel, CACHE):
            return "CACHE"
        if _match_any(rel, LOG):
            return "LOG"
        return "EPHEMERAL"
    if _match_any(rel, INCLUDE_PERSISTENT):
        return "PERSISTENT"
    return "EPHEMERAL"  # unknown files are never silently synchronized


def iter_persistent(home: Path) -> Iterator[Tuple[str, Path]]:
    """Yield (relative_posix_path, absolute_path) for every curated PERSISTENT file."""
    home = Path(home)
    for base, dirs, files in os.walk(home):
        rel_base = os.path.relpath(base, home)
        parts = [] if rel_base == "." else rel_base.split(os.sep)
        # prune whole trees that can only contain excluded content
        head = parts[0] if parts else ""
        if head in {"logs", "cache", "audio_cache", "image_cache", "runtime", "sandboxes",
                    "pending_messages", "hooks", "skills", "bin", "backups"}:
            dirs[:] = []
            continue
        if head == "cron":
            dirs[:] = [d for d in dirs if d != "output"]
        for f in sorted(files):
            rel = "/".join(parts + [f]) if parts else f
            if classify(rel) == "PERSISTENT":
                yield rel, Path(base) / f
