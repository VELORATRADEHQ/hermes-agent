"""Task-run runtime (gateway/task_runtime.py) — deterministic MockTransport coverage.

Contract under test (Browser Use-style task/run providers):
  POST {base}/runs  (auth per entry: X-Browser-Use-API-Key, NEVER Authorization: Bearer)
  GET  {base}/runs/{run_id}  (bounded polling)
Success requires the task/run contract — a run id at creation and an explicit terminal status —
never bare HTTP 200. Failures are first-class explicit statuses; the key is never sent anywhere
but the declared auth header and never appears in results.
"""
import asyncio
import json

import httpx
import pytest

from gateway import task_runtime as tr


def bu_entry(**extra):
    e = {
        "base_url": "https://api.browser-use.com/api/v4",
        "auth": {"type": "api_key_header", "header": "X-Browser-Use-API-Key"},
        "api_key": "sk-live-bu",
        "runtime": {"type": "task_run"},
        "provider_capabilities": ["browser_tasks", "async_runs", "status_polling"],
        "task_run": {"poll": {"interval_s": 0.01, "timeout_s": 5.0, "max_polls": 50}},
    }
    e.update(extra)
    return e


class _Recorder:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def transport(self):
        rec = self

        def handler(request):
            rec.requests.append(request)
            status, body = rec.script.pop(0) if rec.script else (200, {})
            if isinstance(body, (dict, list)):
                return httpx.Response(status, json=body, headers={"content-type": "application/json"})
            return httpx.Response(status, content=body, headers={"content-type": "application/json"})

        return httpx.MockTransport(handler)


@pytest.mark.asyncio
class TestCapabilityGating:
    async def test_chat_provider_rejected_as_not_capable(self):
        r = await tr.run_task({"base_url": "https://x/v1", "api_key": "k"}, "do thing")
        assert r["status"] == tr.NOT_CAPABLE

    async def test_task_run_runtime_entry_capable(self):
        assert tr.is_task_capable(bu_entry())
        assert tr.is_task_capable(bu_entry(runtime={}))
        assert not tr.is_task_capable({"base_url": "https://x", "api_key": "k"})
        assert not tr.is_task_capable(bu_entry(runtime={"type": "openai_chat"}))

    async def test_empty_task_rejected(self):
        r = await tr.run_task(bu_entry(), "   ")
        assert r["status"] == tr.VALIDATION

    async def test_no_key_fail_closed_no_request(self):
        e = bu_entry(api_key="", key_env="")
        rec = _Recorder([(200, {})])
        r = await tr.run_task(e, "task", transport=rec.transport())
        assert r["status"] == tr.NO_KEY
        assert rec.requests == []  # no request was attempted without a credential

    async def test_url_contract_rejection(self):
        e = bu_entry(base_url="http://api.browser-use.com/api/v4")  # https-only
        r = await tr.run_task(e, "task")
        assert r["status"] == tr.REJECTED and "base_url_rejected" in r["detail"]


@pytest.mark.asyncio
class TestAuthContract:
    async def test_x_browser_use_key_header_and_never_bearer(self):
        rec = _Recorder([(200, {"id": "run-9"}), (200, {"status": "completed", "output": "ok"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.SUCCESS
        first = rec.requests[0]
        assert first.headers.get("X-Browser-Use-API-Key") == "sk-live-bu"
        assert "Authorization" not in first.headers    # the original blind spot stays fixed
        # credential appears ONLY as the declared auth header, payload carries no key
        body = first.content.decode()
        assert "sk-live-bu" not in body
        assert json.loads(body) == {"task": "do"}

    async def test_create_posts_to_base_runs(self):
        rec = _Recorder([(200, {"id": "run-1"}), (200, {"status": "completed"})])
        await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert rec.requests[0].method == "POST"
        assert str(rec.requests[0].url) == "https://api.browser-use.com/api/v4/runs"
        assert rec.requests[1].method == "GET"
        assert str(rec.requests[1].url) == "https://api.browser-use.com/api/v4/runs/run-1"


@pytest.mark.asyncio
class TestRunLifecycle:
    async def test_success_after_polling(self):
        rec = _Recorder([
            (200, {"id": "run-42"}),
            (200, {"status": "running"}),
            (200, {"status": "running"}),
            (200, {"status": "completed", "output": "final answer"}),
        ])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.SUCCESS and r["ok"]
        assert r["run_id"] == "run-42" and r["polls"] == 3
        assert r["output"] == "final answer"

    async def test_run_id_nested_shape(self):
        rec = _Recorder([(200, {"data": {"run_id": "run-x"}}), (200, {"status": "succeeded", "output": "done"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.SUCCESS and r["run_id"] == "run-x"

    async def test_provider_side_failure(self):
        rec = _Recorder([(200, {"id": "run-5"}), (200, {"status": "failed"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.FAILED and not r["ok"] and r["run_id"] == "run-5"

    async def test_inline_completion_at_creation(self):
        rec = _Recorder([(200, {"id": "run-1", "status": "completed", "output": "inline"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.SUCCESS and r["polls"] == 0 and r["output"] == "inline"

    async def test_max_polls_bounded(self):
        rec = _Recorder([(200, {"id": "run-1"})] + [(200, {"status": "running"})] * 50)
        e = bu_entry(task_run={"poll": {"interval_s": 0.01, "timeout_s": 600, "max_polls": 3}})
        r = await tr.run_task(e, "do", transport=rec.transport())
        assert r["status"] == tr.TIMEOUT and r["polls"] <= 3
        assert len(rec.requests) <= 4  # create + at most 3 polls — never infinite

    async def test_timeout_budget_bounded(self):
        rec = _Recorder([(200, {"id": "run-1"})] + [(200, {"status": "running"})] * 200)
        e = bu_entry(task_run={"poll": {"interval_s": 0.05, "timeout_s": 0.2, "max_polls": 500}})
        t0 = asyncio.get_event_loop().time()
        r = await tr.run_task(e, "do", transport=rec.transport())
        el = asyncio.get_event_loop().time() - t0
        assert r["status"] == tr.TIMEOUT and el < 2.5  # budget honored

    async def test_caller_cancellation(self):
        rec = _Recorder([(200, {"id": "run-1"})] + [(200, {"status": "running"})] * 50)
        cancel = asyncio.Event()
        e = bu_entry(task_run={"poll": {"interval_s": 0.05, "timeout_s": 600, "max_polls": 200}})

        async def trip():
            await asyncio.sleep(0.06)
            cancel.set()

        task = asyncio.create_task(tr.run_task(e, "do", transport=rec.transport(), cancel_event=cancel))
        tripper = asyncio.create_task(trip())
        r = await task
        await tripper
        assert r["status"] == tr.CANCELLED

    async def test_provider_side_cancelled(self):
        rec = _Recorder([(200, {"id": "run-1"}), (200, {"status": "cancelled"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.CANCELLED


@pytest.mark.asyncio
class TestFailureClassification:
    @pytest.mark.parametrize("code,expected", [
        (401, "auth"), (403, "auth"), (402, "quota"), (429, "quota"),
        (400, "validation"), (422, "validation"), (500, "provider_error"),
    ])
    async def test_create_http_errors(self, code, expected):
        rec = _Recorder([(code, {"detail": "x"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == expected and r["http_status"] == code

    async def test_poll_http_error(self):
        rec = _Recorder([(200, {"id": "run-1"}), (401, {"detail": "x"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == "auth" and r["polls"] == 1

    async def test_malformed_creation_no_run_id(self):
        rec = _Recorder([(200, {"nope": True})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.MALFORMED  # bare 200 with unusable payload is NOT success

    async def test_malformed_creation_non_json(self):
        rec = _Recorder([(200, b"<html>ok</html>")])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.MALFORMED

    async def test_malformed_poll_payload(self):
        rec = _Recorder([(200, {"id": "run-1"}), (200, b"not-json")])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.MALFORMED

    async def test_network_error(self):
        def boom(request):
            raise httpx.ConnectError("down")
        r = await tr.run_task(bu_entry(), "do", transport=httpx.MockTransport(boom))
        assert r["status"] == tr.NETWORK

    async def test_result_never_contains_secret(self):
        rec = _Recorder([(200, {"id": "run-1", "note": "error traces may leak inputs: sk-live-bu? no"}),
                         (200, {"status": "completed", "output": "hello"})])
        r = await tr.run_task(bu_entry(), "do", transport=rec.transport())
        assert r["status"] == tr.SUCCESS
        blob = json.dumps(r)
        assert "sk-live-bu" not in blob  # key never echoes inside results (output/detail fields are provider data only)


class TestNormalization:
    def test_task_runtime_config_bounds(self):
        cfg = tr.task_runtime_config(bu_entry())
        assert cfg["runs_path"] == "/runs" and 0.05 <= cfg["interval_s"] <= 60
        assert cfg["timeout_s"] and 1 <= cfg["max_polls"] <= 2000
        cfg2 = tr.task_runtime_config(bu_entry(task_run={"poll": {"interval_s": 999, "timeout_s": 5, "max_polls": -3},
                                                           "runs_path": "runs"}))
        assert cfg2["interval_s"] <= 60 and cfg2["max_polls"] >= 1
        assert cfg2["runs_path"] == "runs"

    def test_block_flags(self):
        assert tr._classify_http_error(429) == tr.QUOTA
        assert tr._classify_http_error(404) == tr.PROVIDER_ERROR
        assert tr._classify_http_error(418) == tr.MALFORMED
