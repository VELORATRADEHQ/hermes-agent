"""Tests for the task-7 generic provider probe engine (gateway/provider_probe.py).

The whole surface is exercised with an in-process transport — NO real provider is
ever contacted. Coverage: all verdict classifications, the not-bare-200 success
contract, the SSRF guards, the Browser-Use-style header+caps flow, and the legacy
defaults that keep old configs behaving exactly as before.
"""

import json
from typing import Any

import httpx
import pytest

from gateway import provider_probe as pp


def _entry(**extra) -> dict:
    base = {"name": "probebox", "base_url": "https://probe.example.com/v1", "api_key": "sk-live"}
    base.update(extra)
    return base


def _transport(status: int, payload: Any = b'{"data": [{"id": "m1"}]}',
               content_type: str = "application/json") -> httpx.MockTransport:
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload).encode()
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload,
                              headers={"content-type": content_type})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
class TestProbes:
    async def test_success_openai_style_models(self):
        out = await pp.probe_provider_entry(_entry(), transport=_transport(200))
        assert out["verdict"] == pp.SUCCESS
        assert out["ok"] is True and out["category"] == "success"
        assert out["http_status"] == 200
        assert out["model_ids"] == ["m1"]
        assert out["auth_used"] is True

    async def test_auth_401_and_403(self):
        for status in (401, 403):
            out = await pp.probe_provider_entry(_entry(), transport=_transport(status, b'{"detail": "nope"}'))
            assert out["verdict"] == pp.AUTH == out["verdict"]
            assert out["category"] == "auth" and out["ok"] is False and out["http_status"] == status

    async def test_quota_429(self):
        out = await pp.probe_provider_entry(_entry(), transport=_transport(429, b'{"error":"slow down"}'))
        assert out["verdict"] == pp.QUOTA and out["category"] == "quota"
        out = await pp.probe_provider_entry(
            _entry(), transport=_transport(200, b'{"non_field_errors": ["quota exceeded"]}'))
        assert out["verdict"] == pp.QUOTA

    async def test_not_found_and_provider_error(self):
        out = await pp.probe_provider_entry(_entry(), transport=_transport(404, b'{"detail":"not_found"}'))
        assert out["verdict"] == pp.NOT_FOUND
        out = await pp.probe_provider_entry(_entry(), transport=_transport(503, b'{}'))
        assert out["verdict"] == pp.PROVIDER_ERROR
        assert out["category"] == "provider_error"

    async def test_timeout_and_network(self):
        def timeout_handler(request):
            raise httpx.ReadTimeout("t/o")
        out = await pp.probe_provider_entry(_entry(), transport=httpx.MockTransport(timeout_handler))
        assert out["verdict"] == pp.TIMEOUT and out["category"] == "timeout"

        def net_handler(request):
            raise httpx.ConnectError("dns")
        out = await pp.probe_provider_entry(_entry(), transport=httpx.MockTransport(net_handler))
        assert out["verdict"] == pp.NETWORK

    async def test_malformed_garbage_even_on_200(self):
        for junk in (b"<html><body>PROXY OK</body></html>", b"", b"plain text", b'{"ok": true}'):
            out = await pp.probe_provider_entry(_entry(), transport=_transport(200, junk))
            assert out["verdict"] == pp.MALFORMED, junk[:20]

    async def test_no_key_refuses_probe(self):
        out = await pp.probe_provider_entry(_entry(api_key="", key_env=""), transport=_transport(200))
        assert out["verdict"] == pp.NO_KEY
        assert out["auth_used"] is False

    async def test_success_contract_no_bare_200(self):
        # 200 with JSON-but-not-models/analytic payload => MALFORMED (contract has no truth).
        out = await pp.probe_provider_entry(_entry(), transport=_transport(200, b'{"status": "pong"}'))
        assert out["verdict"] == pp.MALFORMED


class TestRequestBuilding:
    def test_browser_use_style_entry(self):
        method, url, headers, body, rejection = pp.build_provider_probe_request(_entry(
            base_url="https://api.browser-use.com/api/v4",
            auth={"type": "api_key_header", "header": "X-Browser-Use-API-Key"},
            probe={"method": "GET", "path": "/api/v3/billing/account"},
            provider_capabilities=["browser_tasks", "async_runs", "status_polling"],
        ))
        assert rejection is None
        assert (method, url) == ("GET", "https://api.browser-use.com/api/v3/billing/account")
        assert headers.get("X-Browser-Use-API-Key") == "sk-live"
        assert "Authorization" not in headers
        assert body is None

    @pytest.mark.asyncio
    async def test_browser_use_style_success_and_never_bare_200(self):
        bu = _entry(
            base_url="https://api.browser-use.com/api/v4",
            auth={"type": "api_key_header", "header": "X-Browser-Use-API-Key"},
            probe={"method": "GET", "path": "/api/v3/billing/account"},
            provider_capabilities=["browser_tasks", "async_runs", "status_polling"])
        account = {"projectId": "p1", "credits": 14.91, "rateLimit": 10, "isFreeTier": True,
                   "concurrentSessionLimit": 1}
        out = await pp.probe_provider_entry(bu, transport=_transport(200, account))
        assert out["verdict"] == pp.SUCCESS
        assert out["task_only"] is True
        assert out["capabilities"] == ["async_runs", "browser_tasks", "status_polling"]
        out = await pp.probe_provider_entry(bu, transport=_transport(401, b'{"detail":"Invalid API key"}'))
        assert out["verdict"] == pp.AUTH

    def test_same_origin_different_api_path_allowed(self):
        # Different api-version paths on the same ORIGIN are legitimate (v3 billing + v4 base).
        method, url, headers, body, rejection = pp.build_provider_probe_request(_entry(
            base_url="https://api.browser-use.com/api/v4",
            probe={"path": "/api/v3/billing/account"}))
        assert rejection is None and url.endswith("/api/v3/billing/account")

    @pytest.mark.parametrize("bad_base", [
        "http://plain.example.com/x",           # https only
        "file:///etc/passwd",
        "gopher://old.example.com/",
        "https://user:pw@host.example.com/v1",  # userinfo
        "https://api.${BASE}/v1",               # substitution
        "not-a-url",
    ])
    def test_ssrf_base_rejected(self, bad_base):
        method, url, headers, body, rejection = pp.build_provider_probe_request(_entry(base_url=bad_base))
        assert rejection is not None and rejection.startswith("base_url_rejected")

    def test_absolute_probe_path_rejected(self):
        method, url, headers, body, rejection = pp.build_provider_probe_request(
            _entry(probe={"path": "https://evil.example.com/x"}))
        assert rejection == "path_rejected:not_relative"

    @pytest.mark.asyncio
    async def test_cross_origin_redirect_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://evil.example.com/models"})
        out = await pp.probe_provider_entry(_entry(), transport=httpx.MockTransport(handler))
        assert out["verdict"] == pp.MALFORMED

    @pytest.mark.asyncio
    async def test_http_scheme_redirect_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://probe.example.com/v1/models"})
        out = await pp.probe_provider_entry(_entry(), transport=httpx.MockTransport(handler))
        assert out["verdict"] == pp.MALFORMED

    @pytest.mark.asyncio
    async def test_same_origin_redirect_followed(self):
        calls = {"n": 0}
        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(302, headers={"location": "https://probe.example.com/v1/models2"})
            return httpx.Response(200, content=b'{"data": [{"id": "m9"}]}')
        out = await pp.probe_provider_entry(_entry(), transport=httpx.MockTransport(handler))
        assert out["verdict"] == pp.SUCCESS
        assert out["model_ids"] == ["m9"] and out["url"].endswith("/models")

    def test_no_secret_means_no_key_even_with_auth_none(self):
        entry = _entry(api_key="")
        method, url, headers, body, rejection = pp.build_provider_probe_request(entry)
        assert rejection == "no_key"

    def test_auth_none_never_needs_key(self):
        entry = _entry(api_key="", auth={"type": "none"})
        method, url, headers, body, rejection = pp.build_provider_probe_request(entry)
        assert rejection is None
        assert headers == {}

    def test_legacy_defaults_untouched(self):
        # No auth/probe blocks: Bearer + {base}/models (the old behavior, verbatim).
        entry = {"name": "old", "base_url": "https://old.example.com/v1", "api_key": "k"}
        method, url, headers, body, rejection = pp.build_provider_probe_request(entry)
        assert rejection is None
        assert url == "https://old.example.com/v1/models"
        assert headers == {"Authorization": "Bearer k"}

    def test_probe_headers_and_body_passed(self):
        method, url, headers, body, rejection = pp.build_provider_probe_request(_entry(
            probe={"method": "POST", "path": "/ping", "headers": {"X-Probe": "1"},
                   "body": {"q": 1}, "expected_statuses": [200]}))
        assert method == "POST" and body == '{"q": 1}'
        assert headers.get("X-Probe") == "1"


class TestCategories:
    @pytest.mark.parametrize("verdict,category", [
        (pp.SUCCESS, "success"), (pp.AUTH, "auth"), (pp.QUOTA, "quota"),
        (pp.NOT_FOUND, "not_found"), (pp.PROVIDER_ERROR, "provider_error"),
        (pp.TIMEOUT, "timeout"), (pp.NETWORK, "network"),
        (pp.MALFORMED, "malformed"), (pp.NO_KEY, "no_key"),
    ])
    def test_category_mapping(self, verdict, category):
        assert pp.category_of(verdict) == category
