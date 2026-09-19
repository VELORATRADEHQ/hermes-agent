"""Telegram health classifier: failure buckets A..G (probe mocked — no network)."""
import os
import time
from pathlib import Path

import pytest

from hermes_persist import telegram_health as th


def _home(tmp: Path, *, pid=True, hb_age=10, log=""):
    home = tmp / "home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        'gateway:\n  platforms:\n    telegram:\n      extra:\n        base_url: "https://relay.example/bot"\n')
    if pid is not None:
        target = os.getpid() if pid is True else int(pid)
        (home / "state" / "gw.pid").write_text(f"GATEWAY_PID={target}\n")
    hb = home / "state" / "gateway.heartbeat"
    if hb_age is not None:
        hb.write_text("{}")
        past = time.time() - hb_age
        import os as _os
        _os.utime(hb, (past, past))
    if log:
        (home / "state" / "hermes-gateway.log").write_text(log)
    return home


GOOD_LOG = (
    "2026-09-18 19:00:00,000 WARNING x: [Telegram] Connected to Telegram (polling mode)\n"
    "2026-09-18 19:05:00,000 INFO x: tick\n")
BAD_LOG_ERR_AFTER = (
    "2026-09-18 19:00:00,000 WARNING x: [Telegram] Connected to Telegram (polling mode)\n"
    "2026-09-18 19:58:00,000 WARNING x: [Telegram] Connect attempt 2/8 failed — retrying in 15s\n")


def test_g_no_artifacts(tmp_path, monkeypatch):
    home = tmp_path / "nohome"
    home.mkdir()
    monkeypatch.setattr(th, "_scan_proc_for_gateway", lambda: None)  # G == nothing findable, hermetically
    r = th.check(home, probe=False)
    assert r["classification"] == "G" and not r["ok"]


def test_a_process_gone(tmp_path):
    home = _home(tmp_path, pid=2**22 - 3)  # implausible pid, but log/hb exist
    _ = home  # artifacts exist → A not G
    (home / "state" / "hermes-gateway.log").write_text(GOOD_LOG)
    r = th.check(home, probe=False)
    assert r["classification"] == "A" and not r["ok"]
    assert r["evidence"]["gateway_pid_alive"] is False


def test_d_stale_heartbeat(tmp_path):
    home = _home(tmp_path, hb_age=9999, log=GOOD_LOG)
    r = th.check(home, probe=False)
    assert r["classification"] == "D" and not th.check(home, probe=False)["ok"]


def test_c_connected_once_but_errors_after(tmp_path):
    home = _home(tmp_path, hb_age=5, log=BAD_LOG_ERR_AFTER)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(th, "_read_token", lambda home: "x:FAKETOKEN")
    monkey.setattr(th, "_probe_getme", lambda b, t, timeout=8.0: {"probed": True, "code": 200, "ms": 5, "ok": True})
    r = th.check(home)
    assert r["classification"] == "C"
    assert r["evidence"]["error_after_connect"] is True
    monkey.undo()


def test_c_never_connected(tmp_path):
    home = _home(tmp_path, hb_age=5, log="2026-09-18 19:00:00 INFO booting\n")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(th, "_read_token", lambda home: "x:FAKETOKEN")
    monkey.setattr(th, "_probe_getme", lambda b, t, timeout=8.0: {"probed": True, "code": 200, "ms": 5, "ok": True})
    r = th.check(home)
    assert r["classification"] == "C"
    monkey.undo()


def test_e_network_broken(tmp_path):
    home = _home(tmp_path, hb_age=5, log=GOOD_LOG)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(th, "_read_token", lambda home: "x:FAKETOKEN")
    monkey.setattr(th, "_probe_getme", lambda b, t, timeout=8.0: {"probed": True, "code": 0, "ms": 8000, "ok": False})
    r = th.check(home)
    assert r["classification"] == "E"
    monkey.undo()


def test_f_bad_token(tmp_path):
    home = _home(tmp_path, hb_age=5, log=GOOD_LOG)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(th, "_read_token", lambda home: "x:FAKETOKEN")
    monkey.setattr(th, "_probe_getme", lambda b, t, timeout=8.0: {"probed": True, "code": 401, "ms": 5, "ok": False})
    r = th.check(home)
    assert r["classification"] == "F"
    monkey.undo()


def test_ok_path(tmp_path):
    home = _home(tmp_path, hb_age=5, log=GOOD_LOG)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(th, "_read_token", lambda home: "x:FAKETOKEN")
    monkey.setattr(th, "_probe_getme", lambda b, t, timeout=8.0: {"probed": True, "code": 200, "ms": 5, "ok": True})
    r = th.check(home)
    assert r["ok"] is True and r["classification"] == "OK"
    assert "FAKETOKEN" not in str(r)  # no secret in output
    monkey.undo()


def test_output_never_contains_secret(tmp_path):
    home = _home(tmp_path, hb_age=5, log=GOOD_LOG)
    secret = "999:TOPSECRETVALUEFAKE"
    (home / "creds.env").write_text(f"TELEGRAM_BOT_TOKEN={secret}\n")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(th, "_probe_getme", lambda b, t, timeout=8.0: {"probed": True, "code": 401, "ms": 3, "ok": False})
    r = th.check(home)
    assert secret not in str(r)
    monkey.undo()


def test_scan_matches_synthetic_gateway_cmdline():
    assert th._GATEWAY_CMD_RE.search(b"/root/harea/venv/bin/python3.13\x00/root/harea/venv/bin/hermes\x00gateway\x00--accept-hooks\x00run")
    assert not th._GATEWAY_CMD_RE.search(b"python\x00manage.py\x00runserver")
    assert not th._GATEWAY_CMD_RE.search(b"bash\x00some\x00hermes\x00unrelated")


def test_proc_scan_finds_live_gateway_and_prefers_exec_clean_entry():
    import subprocess as sp
    fake = sp.Popen(["bash", "-c", "exec -a 'hermes gateway --accept-hooks run' sleep 3"])
    try:
        for _ in range(20):  # tiny settle loop
            pid = th._scan_proc_for_gateway()
            if pid == fake.pid:
                break
        assert pid == fake.pid
        assert th._pid_alive(pid)
    finally:
        fake.terminate(); fake.wait()


def test_tunnel_classifies_without_pidfile_via_proc_scan(tmp_path, monkeypatch):
    home = _home(tmp_path, pid=None, hb_age=5, log=GOOD_LOG)
    import subprocess as sp
    fake = sp.Popen(["bash", "-c", "exec -a 'hermes gateway --accept-hooks run' sleep 3"])
    try:
        deadline = __import__("time").time() + 5
        while th._scan_proc_for_gateway() != fake.pid and __import__("time").time() < deadline:
            __import__("time").sleep(0.05)
        monkeypatch.setattr(th, "_read_token", lambda home: "x:FAKETOKEN")
        monkeypatch.setattr(th, "_probe_getme",
                            lambda b, t, timeout=8.0: {"probed": True, "code": 200, "ms": 5, "ok": True})
        r = th.check(home)
        assert r["evidence"]["pid_source"] == "proc-scan"
        assert r["evidence"]["gateway_pid_alive"] is True
        assert r["classification"] == "OK"
    finally:
        fake.terminate(); fake.wait()
