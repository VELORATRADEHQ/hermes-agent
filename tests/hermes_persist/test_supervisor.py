"""Supervisor restart policy: restarts happen, backoff, capped (no infinite loop),
health-gate triggers restart without process death."""
import sys

from hermes_persist import supervise
from hermes_persist.supervise import Policy


def _exit_cmd(rc=0):
    return [sys.executable, "-c", f"raise SystemExit({rc})"]


def _sleep_cmd(sec=10):
    return [sys.executable, "-c", f"import time;time.sleep({sec})"]


class FakeClock:
    """Deterministic monotonic clock + instant sleep for fast tests."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        # advance logical time by only a sliver per call: real subprocess spawns
        # take wall-clock time, and the poll loop calls sleep() many times per
        # child, so a naive `+= s` fabricates hours of fake uptime.
        self.t += min(s, 0.001)


def _run(cmd, clock, **kw):
    logs = []
    kw.setdefault("now", clock.now)
    kw.setdefault("sleep", clock.sleep)
    kw.setdefault("log", logs.append)
    return supervise.supervise(cmd, **kw), logs


def test_restart_on_exit_then_cap(tmp_path):
    clock = FakeClock()
    pol = Policy(restart_limit=3, window_seconds=600, base_backoff_s=1, grace_seconds=1)
    # under the flat test clock all restarts land in the same 600s window, so the
    # StartLimit cap (3) binds before the 6-restart termination hook ever would
    res, logs = _run(_exit_cmd(1), clock, policy=pol, stop_after_restarts=6)
    assert res.restarts == 4  # 3 allowed + the attempt that discovered the cap
    assert res.exits == [1] * 4
    assert "cap 3" in res.halted_reason
    assert any("restart #3" in ln for ln in logs)


def test_backoff_increases_and_capped_at_max():
    clock = FakeClock()
    pol = Policy(restart_limit=50, window_seconds=600, base_backoff_s=2, max_backoff_s=5)
    delays = []

    def spy_sleep(s):
        if s > 0.4:
            delays.append(s)
        clock.sleep(s)

    res, _ = _run(_exit_cmd(2), clock, policy=pol, sleep=spy_sleep, stop_after_restarts=5)
    assert delays[0] >= 2 and any(d >= 5 for d in delays)
    assert max(delays) <= 5  # never exceeds cap


def test_no_more_than_limit_restarts_within_window_halts():
    clock = FakeClock()
    pol = Policy(restart_limit=2, window_seconds=1e9, base_backoff_s=0.1)
    res, logs = _run(_exit_cmd(1), clock, policy=pol, stop_after_restarts=10)
    assert "halted" in res.halted_reason.lower()
    assert res.restarts <= 3  # window cap (limit) + the attempt that discovered it
    assert res.restarts < 10  # hook would have allowed more — policy stopped first


def test_health_gate_restarts_healthy_loop_child():
    clock = FakeClock()
    pol = Policy(restart_limit=10, window_seconds=600, base_backoff_s=0.1,
                 check_interval_s=0.05, unhealthy_strikes=2, grace_seconds=0.5)
    calls = {"n": 0}

    def unhealthy():
        calls["n"] += 1
        return {"ok": False}

    # child sleeps (would live forever without the health gate); hook stops after 1 restart
    res, logs = _run(_sleep_cmd(30), clock, policy=pol, health_fn=unhealthy,
                     stop_after_restarts=1)
    assert res.restarts == 1
    assert res.health_restarts == 1
    assert calls["n"] >= 2


def test_healthy_fn_no_spare_restarts():
    clock = FakeClock()
    pol = Policy(check_interval_s=0.05, unhealthy_strikes=2, grace_seconds=0.5,
                 base_backoff_s=0.1)
    # child exits quickly; health always ok → no health-triggered restarts beyond exit restarts
    res, _ = _run(_exit_cmd(0), clock, policy=pol, health_fn=lambda: {"ok": True},
                  stop_after_restarts=2)
    assert res.health_restarts == 0
    assert res.restarts == 2
