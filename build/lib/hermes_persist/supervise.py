"""Non-systemd supervisor: run a gateway child, restart on exit with capped backoff,
and gate on REAL Telegram polling health (never "process exists").

Restart policy (StartLimit emulation, no infinite loop):
  restarts allowed per sliding window  → restart_limit (default 5)
  window size                          → window_seconds (default 600)
  backoff                              → base * 2^attempt, capped at max_backoff
Health gate (when health_fn provided):
  every check_interval_s, evaluate health_fn() → report dict with "ok".
  unhealthy_strikes consecutive failures → gracefully terminate child (SIGTERM,
  grace_seconds, then SIGKILL) and count it as a restart.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

HEALTH_OK = 0


@dataclass
class Policy:
    restart_limit: int = 5
    window_seconds: float = 600.0
    base_backoff_s: float = 2.0
    max_backoff_s: float = 60.0
    grace_seconds: float = 20.0
    check_interval_s: float = 30.0
    unhealthy_strikes: int = 3


@dataclass
class RunResult:
    restarts: int = 0
    health_restarts: int = 0
    exits: List[int] = field(default_factory=list)
    halted_reason: str = ""


def _terminate(proc: subprocess.Popen, grace: float) -> None:
    with _suppress():
        proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with _suppress():
            proc.kill()
        with _suppress():
            proc.wait(timeout=5)


class _suppress:
    def __enter__(self):  # pragma: no cover - trivial
        return self

    def __exit__(self, *exc):
        return True


def supervise(cmd: List[str], *, env: Optional[dict] = None,
              health_fn: Optional[Callable[[], dict]] = None,
              policy: Optional[Policy] = None,
              now: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep,
              log: Callable[[str], None] = print,
              stop_after_restarts: Optional[int] = None) -> RunResult:
    """Blocking supervisor loop (returns when child stops being restarted).

    stop_after_restarts: TEST/OBSERVABILITY hook — halt the supervisor after this many
    restarts (production: None → policy window cap governs).
    """
    pol = policy or Policy()
    res = RunResult()
    attempts_window: List[float] = []
    attempt = 0

    while True:
        start = now()
        log(f"supervisor: start: {' '.join(cmd[:2])}…")
        proc = subprocess.Popen(cmd, env=env)
        strikes = 0
        killed_for_health = False
        last_check = start

        while True:
            rc = proc.poll()
            if rc is not None:
                res.exits.append(rc)
                break
            if health_fn is not None and now() - last_check >= pol.check_interval_s:
                last_check = now()
                try:
                    ok = bool((health_fn() or {}).get("ok"))
                except Exception:
                    ok = False
                strikes = 0 if ok else strikes + 1
                if strikes >= pol.unhealthy_strikes:
                    killed_for_health = True
                    log("supervisor: telegram health FAILED x%d — restarting child" % strikes)
                    _terminate(proc, pol.grace_seconds)
                    res.health_restarts += 1
                    break
            sleep(0.25)

        uptime = now() - start
        attempt = 0 if (uptime > pol.window_seconds) else attempt + 1
        # StartLimit: only count restarts inside the window
        attempts_window = [t for t in attempts_window if now() - t <= pol.window_seconds]
        attempts_window.append(now())
        res.restarts += 1

        if stop_after_restarts is not None and res.restarts >= stop_after_restarts:
            res.halted_reason = f"stopped after {res.restarts} restarts (hook)"
            break
        if len(attempts_window) > pol.restart_limit:
            res.halted_reason = (f"halted: {len(attempts_window)} restarts within "
                                 f"{pol.window_seconds:.0f}s window (cap {pol.restart_limit})")
            log("supervisor: " + res.halted_reason)
            break
        delay = min(pol.max_backoff_s, pol.base_backoff_s * (2 ** (attempt - 1))) if attempt else pol.base_backoff_s
        why = "health" if killed_for_health else f"exit {res.exits[-1] if res.exits else '?'}"
        log(f"supervisor: child down ({why}, uptime {uptime:.1f}s) → restart #{res.restarts} in {delay:.1f}s")
        sleep(delay)
    return res
