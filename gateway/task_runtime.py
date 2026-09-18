"""Task-run execution for task-capable custom providers (task-8).

Some providers describe work, not chat: Browser Use-style task/run APIs
(``POST {base}/runs`` → run id → ``GET {base}/runs/{run_id}`` polled until a terminal status).
This module is that runtime: capability-gated (never an LLM, never a /model choice), bounded
polling, transport-injectable for offline tests, and it never logs or echoes the credential.

Design invariants:
  * The provider entry must come out of ``hermes_cli.config_providers`` normalization
    (``runtime: task_run`` or capabilities declaring ``browser_tasks``/``async_runs``).
  * Auth comes from the entry's auth block — Browser Use = ``api_key_header`` with
    ``X-Browser-Use-API-Key``. Bearer is NOT assumed (that was the original blind spot).
  * HTTP 200 alone is never success: creation must return a run id; status polls must return
    a status field; terminal-success requires an explicit success status string.
  * Everything is fail-explicit: auth/quota/validation/provider_error/timeout/network/
    cancelled/malformed are first-class results, never silent fallbacks.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import httpx

from gateway.provider_probe import validate_probe_url

# Result statuses (string-stable; UI maps these to localized copy)
SUCCESS = "success"
FAILED = "failed"          # terminal provider-side run failure
CANCELLED = "cancelled"    # caller cancellation honored
TIMEOUT = "timeout"        # polling budget exhausted
AUTH = "auth"
QUOTA = "quota"
VALIDATION = "validation"  # 400/422 rejected the task contract
PROVIDER_ERROR = "provider_error"  # 5xx / terminal failure state without detail
NETWORK = "network"
MALFORMED = "malformed"    # response didn't match the task/run contract
NO_KEY = "no_key"
NOT_CAPABLE = "not_capable"
REJECTED = "rejected"      # SSRF/URL contract refusal

_SUCCESS_STATES = {"completed", "finished", "succeeded", "success", "done"}
_FAILURE_STATES = {"failed", "error", "errored", "failure"}
_CANCELLED_STATES = {"cancelled", "canceled", "stopped", "aborted"}
_ALL_TERMINAL = _SUCCESS_STATES | _FAILURE_STATES | _CANCELLED_STATES

_MAX_BODY = 128 * 1024
_MAX_OUTPUT_ECHO = 3500


def task_runtime_config(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalized task-run config from a config_providers entry.

    Contract (declared structure wins; legacy task-less entries are NOT capable):
      base_url/api, auth (as in provider_probe), runs_path (default "/runs"),
      poll: {interval_s, timeout_s, max_polls}.
    """
    block = entry.get("task_run") if isinstance(entry.get("task_run"), dict) else {}
    poll = block.get("poll") if isinstance(block.get("poll"), dict) else {}
    return {
        "base_url": str(entry.get("base_url") or entry.get("api") or "").strip().rstrip("/"),
        "runs_path": str(block.get("runs_path") or "/runs").strip() or "/runs",
        "interval_s": _bounded_float(poll.get("interval_s"), 5.0, 0.05, 60.0),
        "timeout_s": _bounded_float(poll.get("timeout_s"), 600.0, 0.2, 3600.0),
        "max_polls": _bounded_int(poll.get("max_polls"), 120, 1, 2000),
    }


def _bounded_float(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def _bounded_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, i))


def is_task_capable(entry: Dict[str, Any]) -> bool:
    """Task-run capability: explicit runtime task_run or task capabilities declared."""
    runtime = entry.get("runtime")
    if isinstance(runtime, dict):
        rtype = str(runtime.get("type") or "").strip().lower()
        if rtype == "task_run":
            return True
        if rtype:
            return False
    caps = entry.get("provider_capabilities")
    capset = set()
    if isinstance(caps, (list, tuple)):
        capset = {str(c).strip().lower() for c in caps}
    return bool(capset & {"browser_tasks", "async_runs"})


def _resolve_secret(entry: Dict[str, Any]) -> str:
    """Credential for the request. NEVER logged; call sites must scrub errors containing it."""
    secret = str(entry.get("api_key") or "").strip() or str(entry.get("_task_secret") or "").strip()
    if not secret:
        env = str(entry.get("key_env") or "").strip()
        if env:
            secret = str(os.environ.get(env) or "").strip()
    return secret


def _auth_headers(entry: Dict[str, Any], secret: str) -> Dict[str, str]:
    """Auth headers per the entry contract: Browser Use → X-Browser-Use-API-Key (no Bearer)."""
    auth = entry.get("auth") if isinstance(entry.get("auth"), dict) else {}
    atype = str(auth.get("type") or "").strip().lower() or ("bearer" if secret else "none")
    if atype in ("none", "native"):
        return {}
    if atype == "api_key_header":
        header = str(auth.get("header") or "X-API-Key").strip()
        if header.lower() in {"host", "content-length", "connection"} or header.lower().startswith("proxy-"):
            header = "X-API-Key"
        return {header: secret} if secret else {}
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def _result(status: str, *, run_id: Optional[str] = None, output: Optional[str] = None,
            http_status: Optional[int] = None, detail: str = "", polls: int = 0,
            elapsed_s: float = 0.0) -> Dict[str, Any]:
    out = {"status": status, "ok": status == SUCCESS, "run_id": run_id,
           "output": (output or "")[:_MAX_OUTPUT_ECHO] if output else "",
           "http_status": http_status, "detail": detail[:400], "polls": polls,
           "elapsed_s": round(elapsed_s, 2)}
    return out


def _classify_http_error(status: int) -> str:
    if status in (401, 403):
        return AUTH
    if status in (402, 429):
        return QUOTA
    if status in (400, 422):
        return VALIDATION
    if status == 404:
        return PROVIDER_ERROR
    return PROVIDER_ERROR if status >= 500 else MALFORMED


def _extract_json(response: httpx.Response) -> Tuple[Optional[Any], bool]:
    ctype = (response.headers.get("content-type") or "").lower()
    try:
        body = response.content[:_MAX_BODY]
        data = json.loads(body) if body else None
        return data, True
    except Exception:
        return None, False


def _run_id_from(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for k in ("id", "run_id", "runId", "task_id"):
            v = payload.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v)
        inner = payload.get("run") if isinstance(payload.get("run"), dict) else payload.get("data")
        if isinstance(inner, dict):
            return _run_id_from(inner)
    return None


def _status_from(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """(terminal-or-normalized status, output text). Status strings are provider-supplied."""
    if not isinstance(payload, dict):
        return None, None
    raw = payload.get("status") or payload.get("state")
    if not isinstance(raw, str):
        inner = payload.get("run") if isinstance(payload.get("run"), dict) else payload.get("data")
        return _status_from(inner) if isinstance(inner, dict) else (None, None)
    s = raw.strip().lower()
    output = None
    for k in ("output", "result", "answer", "final_response"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            output = v
            break
    return s, output


async def run_task(entry: Dict[str, Any], task_text: str, *,
                   transport: Optional[httpx.AsyncBaseTransport] = None,
                   cancel_event: Optional[asyncio.Event] = None) -> Dict[str, Any]:
    """Execute one task-run against a task-capable custom provider entry.

    Returns a normalized result dict (never raises for provider/transport faults; auth and
    contract problems are explicit statuses). ``transport`` injects a mock for offline tests.
    ``cancel_event`` lets the caller request cooperative cancellation (bounded regardless).
    """
    started = asyncio.get_event_loop().time()
    task_text = (task_text or "").strip()[:4000]
    if not task_text:
        return _result(VALIDATION, detail="empty task text")
    if not is_task_capable(entry):
        return _result(NOT_CAPABLE, detail="provider is not task-capable (runtime/capabilities)")

    cfg = task_runtime_config(entry)
    base_url = cfg["base_url"]
    bad = validate_probe_url(base_url)
    if bad is not None:
        return _result(REJECTED, detail=f"base_url_rejected:{bad}")

    secret = _resolve_secret(entry)
    auth_headers = _auth_headers(entry, secret)
    requires_auth = not (isinstance(entry.get("auth"), dict) and str(entry["auth"].get("type") or "").lower()
                         in ("none", "native"))
    if requires_auth and not secret:
        return _result(NO_KEY, detail="no credential for task provider")

    runs_path = cfg["runs_path"]
    if not runs_path.startswith("/"):
        runs_path = "/" + runs_path
    create_url = urljoin(base_url + "/", runs_path.lstrip("/"))
    headers = {"Content-Type": "application/json", "Accept": "application/json", **auth_headers}
    client_kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(30.0, connect=10.0)}
    if transport is not None:
        client_kwargs["transport"] = transport

    polls = 0

    def _cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # 1. create the run
            resp = await client.post(create_url, json={"task": task_text}, headers=headers)
            node, json_ok = _extract_json(resp)
            if resp.status_code >= 400:
                return _result(_classify_http_error(resp.status_code), http_status=resp.status_code,
                               detail="run creation rejected")
            if not json_ok or not isinstance(node, dict):
                return _result(MALFORMED, http_status=resp.status_code,
                               detail="run creation returned non-JSON/invalid payload")
            run_id = _run_id_from(node)
            if not run_id:
                return _result(MALFORMED, http_status=resp.status_code,
                               detail="run creation returned no run id")
            status, output = _status_from(node)
            if status in _SUCCESS_STATES:
                return _result(SUCCESS, run_id=run_id, output=output or "",
                               http_status=resp.status_code, detail="completed inline")
            if status in _FAILURE_STATES:
                return _result(FAILED, run_id=run_id, output=output or "",
                               http_status=resp.status_code, detail="provider reported failure inline")

            poll_url = urljoin(base_url + "/", f"{runs_path.lstrip('/')}/{run_id}")
            # 2. bounded polling until terminal status / timeout / cancellation / max polls
            while polls < cfg["max_polls"]:
                if _cancelled():
                    return _result(CANCELLED, run_id=run_id, polls=polls,
                                   elapsed_s=asyncio.get_event_loop().time() - started,
                                   detail="cancelled by caller")
                elapsed = asyncio.get_event_loop().time() - started
                if elapsed >= cfg["timeout_s"]:
                    return _result(TIMEOUT, run_id=run_id, polls=polls, elapsed_s=elapsed,
                                   detail=f"polling exceeded {cfg['timeout_s']:.0f}s")
                try:
                    await asyncio.wait_for(
                        asyncio.shield(_wait_or_cancel(cancel_event, cfg["interval_s"])),
                        timeout=cfg["interval_s"] + 5.0,
                    )
                except asyncio.TimeoutError:
                    pass
                if _cancelled():
                    return _result(CANCELLED, run_id=run_id, polls=polls,
                                   elapsed_s=asyncio.get_event_loop().time() - started,
                                   detail="cancelled by caller")
                polls += 1
                resp = await client.get(poll_url, headers=headers)
                node, json_ok = _extract_json(resp)
                if resp.status_code >= 400:
                    return _result(_classify_http_error(resp.status_code), run_id=run_id,
                                   http_status=resp.status_code, polls=polls,
                                   detail="status poll rejected")
                if not json_ok or not isinstance(node, dict):
                    return _result(MALFORMED, run_id=run_id, http_status=resp.status_code,
                                   polls=polls, detail="status poll returned invalid payload")
                status, output = _status_from(node)
                elapsed = asyncio.get_event_loop().time() - started
                if status in _SUCCESS_STATES:
                    return _result(SUCCESS, run_id=run_id, output=output or "",
                                   http_status=resp.status_code, polls=polls, elapsed_s=elapsed)
                if status in _FAILURE_STATES:
                    return _result(FAILED, run_id=run_id, output=output or "",
                                   http_status=resp.status_code, polls=polls, elapsed_s=elapsed,
                                   detail="provider reported failure")
                if status in _CANCELLED_STATES:
                    return _result(CANCELLED, run_id=run_id, polls=polls, elapsed_s=elapsed,
                                   detail="provider-side cancellation")
                # running/pending/unknown → keep polling until budget exhausted (the wall-clock
                # budget also applies mid-loop: slow responses must never extend it silently)
                if asyncio.get_event_loop().time() - started >= cfg["timeout_s"]:
                    return _result(TIMEOUT, run_id=run_id, polls=polls, elapsed_s=elapsed,
                                   detail=f"polling exceeded {cfg['timeout_s']:.0f}s")
            return _result(TIMEOUT, run_id=run_id, polls=polls,
                           elapsed_s=asyncio.get_event_loop().time() - started,
                           detail=f"no terminal status after {polls} polls")
    except httpx.TimeoutException:
        return _result(NETWORK, detail="connection/read timeout", elapsed_s=asyncio.get_event_loop().time() - started)
    except httpx.HTTPError as exc:
        return _result(NETWORK, detail=f"network error: {type(exc).__name__}",
                       elapsed_s=asyncio.get_event_loop().time() - started)
    except asyncio.CancelledError:
        return _result(CANCELLED, detail="task cancelled",
                       elapsed_s=asyncio.get_event_loop().time() - started)


async def _wait_or_cancel(cancel_event: Optional[asyncio.Event], interval_s: float) -> None:
    """Sleep for the poll interval, waking early on caller cancellation."""
    if cancel_event is None:
        await asyncio.sleep(interval_s)
        return
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=interval_s)
    except asyncio.TimeoutError:
        pass
