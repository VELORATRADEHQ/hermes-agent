"""Telegram polling health — evidence-based, NO 'ping == healthy' shortcuts.

Classifies the runtime/adapter state into the failure buckets the ops playbook uses:
  A process stopped · B gateway down · C adapter stopped polling (loop alive, no polling)
  D stale heartbeat · E telegram API/network/relay broken · F bad token/config
  G machine idle/suspended (no gateway process AND everything else missing)
  H supervisor/service restart loop

Evidence sources (all read-only except ONE authenticated getMe probe):
  - gateway pidfile / process scan     (A/B/G)
  - state/gateway.heartbeat freshness  (D; off-loop witness: process alive vs loop frozen)
  - gateway log markers: last "Connected to Telegram (polling mode)" vs later connect/
    retry errors                                    (C/E; "connected once" is not proof)
  - live ``getMe`` round-trip via the configured base_url (relay) — proves network+relay+token
    (F: 401/404 → bad token; timeout/000 → E: network)
Secrets: the token is READ but never printed, logged, or returned — only booleans/ages.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

HEARTBEAT_FRESH_S = 120

_CONNECT_MARK = re.compile(r"Connected to Telegram \(polling mode\)")
_ERR_MARK = re.compile(r"Connect attempt|retrying in|ERROR.*[Tt]elegram|,polling|429|Unauthorized")


def _last_marks(log_path: Path, max_lines: int = 400) -> Dict[str, Optional[float]]:
    """Epoch of last connect marker and last telegram-ish error line (mtime-based fallback)."""
    out = {"last_connect": None, "last_error": None}
    if not log_path.is_file():
        return out
    try:
        lines = log_path.read_text(errors="replace").splitlines()[-max_lines:]
    except Exception:
        return out
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
    for ln in lines:
        m = ts_re.match(ln)
        epoch = None
        if m:
            try:
                epoch = time.mktime(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                pass
        if _CONNECT_MARK.search(ln):
            out["last_connect"] = epoch or out["last_connect"]
        elif _ERR_MARK.search(ln):
            out["last_error"] = epoch or out["last_error"]
    return out


def _read_token(home: Path) -> Optional[str]:
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return os.environ["TELEGRAM_BOT_TOKEN"]
    for fname in ("creds.env", ".env", "_runtime_env.sh"):
        p = Path(home) / fname
        if not p.is_file():
            continue
        try:
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return v or None
        except Exception:
            continue
    return None


def _read_base_url(home: Path) -> Optional[str]:
    cfg = Path(home) / "config.yaml"
    if not cfg.is_file():
        return None
    try:
        m = re.search(r"base_url:\s*['\"]?(\S+?)['\"]?\s*$", cfg.read_text(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


_GATEWAY_CMD_RE = re.compile(rb"hermes.*gateway.*run|gateway.*--accept-hooks.*run", re.S)


def _scan_proc_for_gateway() -> Optional[int]:
    """Best-effort Linux /proc scan for a running `hermes gateway ... run` process.
    Returns None on any failure; never raises."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    me = os.getpid()
    matches = []
    for ent in proc.iterdir():
        if not ent.name.isdigit():
            continue
        pid = int(ent.name)
        if pid == me:
            continue
        try:
            cmd = (ent / "cmdline").read_bytes()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            continue
        if b"hermes" in cmd and b"gateway" in cmd and b"run" in cmd and _GATEWAY_CMD_RE.search(cmd):
            matches.append((b"\x00-c\x00" in cmd or b"-c\x00" in cmd[:4096] and cmd.split(b"\x00")[0].endswith((b"sh", b"bash", b"dash")), pid, cmd))
    if not matches:
        return None
    # prefer an exec-clean gateway process over any shell wrapper (whose cmdline
    # embeds the same words inside a -c script argument)
    matches.sort(key=lambda m: 1 if m[0] else 0)
    return matches[0][1]


def _gateway_pid(home: Path) -> tuple:
    """Returns (pid_or_None, source: pidfile|proc-scan|none)."""
    for cand in (Path(home) / "state" / "gw.pid", Path(home) / "gateway.pid"):
        if cand.is_file():
            try:
                txt = cand.read_text().strip()
                nums = re.findall(r"\d+", txt)
                if nums:
                    return int(nums[-1]), "pidfile"
            except Exception:
                continue
    pid = _scan_proc_for_gateway()
    if pid is not None:
        return pid, "proc-scan"
    return None, "none"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OverflowError):
        return False
    except OSError:
        return False


def _probe_getme(base_url: str, token: str, timeout: float = 8.0) -> Dict[str, Any]:
    """One authenticated probe. Returns ok/code/ms only — never echoes the token/URL w/ token."""
    import urllib.error
    import urllib.request
    url = f"{base_url.rstrip('/')}/getMe"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ms = int((time.monotonic() - start) * 1000)
            body = resp.read(4096)
            return {"probed": True, "code": resp.status, "ms": ms,
                    "ok": b'"ok":true' in body.replace(b" ", b"")}
    except urllib.error.HTTPError as exc:
        return {"probed": True, "code": exc.code, "ms": int((time.monotonic() - start) * 1000), "ok": False}
    except Exception:
        return {"probed": True, "code": 0, "ms": int((time.monotonic() - start) * 1000), "ok": False}


def check(home: Path, *, probe: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
    """Full health evaluation. Returns classification + evidence (secret-free)."""
    home = Path(home)
    now = time.time() if now is None else now
    r: Dict[str, Any] = {"ok": False, "classification": "UNKNOWN", "evidence": {}}
    ev = r["evidence"]

    # A/B: process
    pid, pid_src = _gateway_pid(home)
    alive = _pid_alive(pid) if pid else False
    ev["gateway_pid_alive"] = alive
    ev["pid_source"] = pid_src
    if not alive:
        hb = home / "state" / "gateway.heartbeat"
        log = home / "state" / "hermes-gateway.log"
        if not hb.exists() and not log.exists():
            r["classification"] = "G"   # nothing ever ran here → machine idle/fresh/suspended
        else:
            r["classification"] = "A"   # ran before, process gone
        return r

    # D: heartbeat freshness
    hb = home / "state" / "gateway.heartbeat"
    if hb.exists():
        age = now - hb.stat().st_mtime
        ev["heartbeat_age_s"] = int(age)
        if age > HEARTBEAT_FRESH_S:
            r["classification"] = "D"
            return r
    else:
        ev["heartbeat_age_s"] = None

    # C: connected-marker vs later error stream
    log = home / "state" / "hermes-gateway.log"
    marks = _last_marks(log)
    ev["polling_connected_seen"] = marks["last_connect"] is not None
    ev["error_after_connect"] = (
        marks["last_error"] is not None and marks["last_connect"] is not None
        and marks["last_error"] > marks["last_connect"])
    if marks["last_connect"] is None:
        ev["polling_connected_seen"] = False
    if ev["error_after_connect"]:
        # errors continue after last successful connect → E or C (probe decides)
        pass

    # E/F: authenticated probe via relay (if configured + token present)
    base_url = _read_base_url(home)
    token = _read_token(home)
    ev["base_url_configured"] = base_url is not None
    ev["token_present"] = token is not None
    if probe and base_url and token:
        pr = _probe_getme(base_url, token)
        ev["probe"] = pr
        if pr["code"] in (401, 403, 404):
            r["classification"] = "F"   # bad token/credentials path
            return r
        if not pr["ok"]:
            r["classification"] = "E"   # network/relay broken
            return r
    elif not token:
        r["classification"] = "F"
        return r

    if ev["error_after_connect"]:
        r["classification"] = "C"
        return r
    if not ev["polling_connected_seen"]:
        r["classification"] = "C"
        return r

    r["ok"] = True
    r["classification"] = "OK"
    return r
