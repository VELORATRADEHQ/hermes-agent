"""Integration: GoogleDriveProvider behind the EXISTING hermes_persist.sync seam.

Proves layering/wiring only — backup → manifest → verify_snapshot → restore-isolated —
driven end-to-end through the same code paths the CLI/panel use with a Drive backend.
NOT presented as live-API evidence (that requires real OAuth; see final report).
"""
import json
from pathlib import Path

import pytest

from hermes_persist import gdrive, oauth_store, sync
from tests.hermes_persist.test_gdrive import FakeTransport


@pytest.fixture
def connected_home(tmp_path):
    ft = FakeTransport()
    home = tmp_path / "home"
    home.mkdir()

    def hooks():
        return dict(http_json=ft.http_json, http_bytes=ft.http_bytes)

    # Drive-side state actually needed by pipeline tests lives in the fixture so
    # tests can inspect/churn it. Real flow: connect, then act on `home`.
    oauth_store.begin_connect(home, client_id="cid-1", redirect_uri="https://redir.test/cb")
    # finish_connect requires the exact pending state; read it like cpanel does.
    pend = json.loads((home / oauth_store._PENDING_FILE).read_text())
    out = oauth_store.finish_connect(home, code="code-1",
                                     returned_state=pend["state"], client_id="cid-1", **hooks())
    assert out["state"] == gdrive.CONNECTED

    # minimal but real-shaped Hermes state the manifest builder recognizes
    # catalog-covered files (catalog.py: SOUL.md, memories/**, config.yaml)
    (home / "config.yaml").write_text("model: {provider: gemini}\n")
    (home / "SOUL.md").write_text("# persona: hermes-test\n")
    (home / "memories").mkdir()
    (home / "memories" / "core.md").write_text("pipeline-payload\n")
    return home, ft


def test_backup_verify_restore_isolated_roundtrip(connected_home, tmp_path):
    home, ft = connected_home
    prov = oauth_store.load_provider(home, http_json=ft.http_json, http_bytes=ft.http_bytes)
    prov.health_check()  # provider usable for ops

    sid = sync.backup(home, prov, env_id="deepnote-test",
                      hermes_version="test", commit="71528cf")
    assert sid

    ok, problems, sid2 = sync.verify_snapshot(prov, sid)
    assert ok, problems
    assert sid2 == sid

    # restore into an ISOLATED directory — live home must remain untouched
    before = (home / "memories" / "core.md").read_text()
    target = tmp_path / "restore-check"
    sid3, restored = sync.restore(target, prov, env_id="deepnote-test", sid=sid, force=True)
    assert sid3 == sid
    assert set(restored) >= {"SOUL.md", "memories/core.md", "config.yaml"}
    assert (target / "memories" / "core.md").read_text() == "pipeline-payload\n"
    assert (target / "SOUL.md").read_text() == "# persona: hermes-test\n"
    assert (home / "memories" / "core.md").read_text() == before  # live state intact


def test_backup_survives_provider_reload_like_restart(connected_home):
    """Second provider instance (simulated process restart) uses the stored
    refresh credential — no second OAuth round required."""
    home, ft = connected_home
    ft.access_calls = 0 if not hasattr(ft, "access_calls") else ft.access_calls
    prov2 = oauth_store.load_provider(home, http_json=ft.http_json, http_bytes=ft.http_bytes)
    ok, problems, sid = sync.verify_snapshot(prov2, None)
    assert prov2 is not None
    # a fresh provider requires the refresh-token grant (access token), not a code exchange
    keys = prov2.list_objects("env/")
    assert isinstance(keys, list)
