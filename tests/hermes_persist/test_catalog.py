"""Curated catalog: what gets synchronized and what never does."""
from pathlib import Path

from hermes_persist.catalog import classify, iter_persistent


def _home(tmp_path: Path) -> Path:
    home = tmp_path
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(exist_ok=True)
    (home / "pairing").mkdir(exist_ok=True)
    (home / "memories").mkdir(exist_ok=True)
    (home / "logs").mkdir(exist_ok=True)
    (home / "cron" / "output").mkdir(parents=True)
    (home / "runtime" / "x").mkdir(parents=True)
    (home / "cache" / "deep").mkdir(parents=True)
    files = {
        # curated persistent
        "config.yaml": b"model: {}\n",
        "state.db": b"sqlite-bytes",
        "sessions/sessions.json": b"{}",
        "pairing/approved.json": b"{}",
        "memories/notes.md": b"# memory",
        "SOUL.md": b"persona",
        "channel_directory.json": b"{}",
        "cron/jobs.json": b"[]",
        "state/cpanel-ui.json": b"{}",
        # secrets (must never sync)
        ".env": b"TELEGRAM_BOT_TOKEN=111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n",
        "auth.json": b'{"keys": {}}',
        "creds.env": b"GEMINI_API_KEY=AQ.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n",
        "_runtime_env.sh": b"export GEMINI_API_KEY=AQ.x\n",
        "dn_keep.env": b"DEEPNOTE_API_TOKEN=zzzzz\n",
        # cache
        "models_dev_cache.json": b"{}",
        "provider_models_cache.json": b"{}",
        ".skills_prompt_snapshot.json": b"{}",
        ".update_check": b"{}",
        "cache/deep/blob": b"x",
        # logs/dumps
        "logs/agent.log": b"log",
        "state/hermes-gateway.log": b"log",
        "sessions/request_dump_20260918_1.json": b"{}",
        # ephemeral
        "gateway.pid": b"1",
        "state/gateway.heartbeat": b"{}",
        "state/gw.pid": b"1",
        "cron/output/run.log": b"x",
        "cron/.jobs.lock": b"",
        "runtime/x/state.json": b"{}",
    }
    for rel, data in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return home


def test_only_persistent_set_synced(tmp_path):
    home = _home(tmp_path)
    synced = {rel for rel, _ in iter_persistent(home)}
    assert {"config.yaml", "state.db", "sessions/sessions.json", "pairing/approved.json",
            "memories/notes.md", "SOUL.md", "channel_directory.json",
            "cron/jobs.json", "state/cpanel-ui.json"} <= synced
    secrets = {".env", "auth.json", "creds.env", "_runtime_env.sh", "dn_keep.env"}
    cache_logs = {"models_dev_cache.json", ".skills_prompt_snapshot.json", "logs/agent.log",
                  "state/hermes-gateway.log", "sessions/request_dump_20260918_1.json"}
    ephemeral = {"gateway.pid", "state/gateway.heartbeat", "state/gw.pid",
                 "cron/output/run.log", "cron/.jobs.lock", "runtime/x/state.json"}
    assert not (synced & secrets), synced & secrets
    assert not (synced & cache_logs), synced & cache_logs
    assert not (synced & ephemeral), synced & ephemeral


def test_classify_categories():
    assert classify(".env") == "SECRET"
    assert classify("auth.json") == "SECRET"
    assert classify("creds.env") == "SECRET"
    assert classify("models_dev_cache.json") == "CACHE"
    assert classify("logs/gateway.log") == "LOG"
    assert classify("gateway.lock") == "EPHEMERAL"
    assert classify("config.yaml") == "PERSISTENT"
    assert classify("state.db") == "PERSISTENT"
    assert classify("totally_unknown.bin") == "EPHEMERAL"  # never silently synced
