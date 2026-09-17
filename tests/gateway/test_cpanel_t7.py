"""Task 7 cpanel coverage: probe-gated registration + capability-aware provider UI.

All probe traffic is fully mocked via ``cp._probe_entry`` — NO real network, NO real
provider calls. Security behavior asserted: nothing persists on failed validation
(no config key, no env write, no secret), old config survives failed edits.
"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import gateway.cpanel as cp
from tests.gateway.test_cpanel_t5 import (LoAdapter, TQuery, _cb, _c, tmsg, cfg, run, _wizard_start, env)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """Same harness baseline as tests/gateway/test_cpanel_t5.py (autouse fixtures do NOT
    cross module boundaries — the callbacks below are allowlisted admin interactions)."""
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "admin")
    monkeypatch.setattr(cp, "_is_admin", lambda a, u: True)

    class _FakeProf:
        def __init__(self, name): self.name = name
        base_url = "https://api.openai.com/v1"
        env_vars = ("OPENAI_API_KEY",)
        models_url = ""
        auth_type = "api_key"
        supports_health_check = False
        supports_model_listing = True
        def __hash__(self): return hash(self.name)
    monkeypatch.setattr(cp, "_profiles", lambda: {"openai": _FakeProf("openai")})


@pytest.fixture(autouse=True)
def _clean_filter_state():
    """_PROV_LIST_FILTER is keyed by adapter id(); CPython id reuse must not leak filter state between tests."""
    cp._PROV_LIST_FILTER.clear()
    yield
    cp._PROV_LIST_FILTER.clear()


@pytest.fixture()
def real_telegram():
    """tests/gateway/conftest.py globally MagicMocks telegram; cpanel builds real keyboards
    for label assertions, so swap the real package in temporarily."""
    import importlib, sys
    keys = [m for m in sys.modules if m == "telegram" or m.startswith("telegram.")]
    saved = {m: sys.modules.pop(m) for m in keys}
    real = importlib.import_module("telegram")
    yield real
    for m in [m for m in sys.modules if m == "telegram" or m.startswith("telegram.")]:
        sys.modules.pop(m)
    sys.modules.update(saved)


def _loc(key: str, default: str) -> str:
    """Expected localized text in the CURRENT display language (fa on this deployment):
    computed through the same _T the app renders with — tests stay language-independent."""
    return cp._T(key, default)


PROBE_OK = {"ok": True, "verdict": "SUCCESS", "category": "success", "http_status": 200,
            "capabilities": ["chat", "completion"], "task_only": False,
            "model_ids": ["m1"], "notes": []}
PROBE_AUTH = {"ok": False, "verdict": "AUTH", "category": "auth", "http_status": 401,
              "capabilities": ["chat", "completion"], "task_only": False, "model_ids": [], "notes": ["auth:401"]}

# ── H: registration transactionality ─────────────────────────────────────────

def test_save_blocked_when_probe_fails(env, monkeypatch):
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: ([], "no_key"))
    monkeypatch.setattr(cp, "_probe_entry", AsyncMock(return_value=dict(PROBE_AUTH)))
    monkeypatch.setattr(cp, "_profiles", lambda: {})
    a = _wizard_start(env, monkeypatch)
    for t in ("blockme", "Block Me", "-", "https://api.block.example/v1"):
        run(_c(a, tmsg(t)))
    run(_cb(a, "hctl:cwx:auth:none"))
    run(_cb(a, "hctl:cwx:save"))
    c = cfg(env)
    # transactional: no providers entry, no key, no meta — pending kept for retry
    assert "blockme" not in c.get("providers", {})
    assert "blockme" not in (env / ".env").read_text()
    assert cp._pending_get("1")["step"] == "confirm"


def test_save_succeeds_with_header_auth_block_persisted(env, monkeypatch):
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: (["m1"], "success"))
    monkeypatch.setattr(cp, "_probe_entry", AsyncMock(return_value=dict(PROBE_OK)))
    monkeypatch.setattr(cp, "_profiles", lambda: {})
    a = _wizard_start(env, monkeypatch)
    for t in ("hdr", "Hdr Provider", "-", "https://api.hdr.example/v1"):
        run(_c(a, tmsg(t)))
    run(_cb(a, "hctl:cwx:auth:apihdr"))
    assert cp._pending_get("1")["step"] == "authhdr"
    run(_c(a, tmsg("X-Browser-Use-API-Key")))
    assert cp._pending_get("1")["step"] == "key"
    run(_c(a, tmsg("sk-hdr-999")))
    run(_cb(a, "hctl:cwx:save"))
    e = cfg(env)["providers"]["hdr"]
    assert e["auth"] == {"type": "api_key_header", "header": "X-Browser-Use-API-Key"}
    assert e["key_env"].startswith("CUSTOM_") and e["key_env"].endswith("API_KEY")
    envv = (env / ".env").read_text()
    assert "sk-hdr-999" in envv and "sk-hdr-999" not in yaml.safe_dump(cfg(env))
    assert oct(os.stat(env / ".env").st_mode & 0o777) == "0o600"


def test_base_edit_failed_probe_keeps_old_url(env, monkeypatch):
    monkeypatch.setattr(cp, "_probe_entry", AsyncMock(return_value=dict(PROBE_AUTH)))
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://old.example/v1")
    run(_cb(a, "hctl:cwx:eb:acme"))
    run(_c(a, tmsg("https://new.example/v2")))
    assert cfg(env)["providers"]["acme"]["api"] == "https://old.example/v1"
    assert a.sent and (_loc("cpanel.cwx.editblocked", "kept unchanged")[:12].strip() in (a.sent[-1]["text"] or ""))


def test_key_rotation_failed_probe_keeps_old_secret(env, monkeypatch):
    monkeypatch.setattr(cp, "_probe_entry", AsyncMock(return_value=dict(PROBE_AUTH)))
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-old")
    run(_cb(a, "hctl:cwx:ek:acme"))
    run(_c(a, tmsg("sk-new-999")))
    body = (env / ".env").read_text()
    assert "sk-old" in body and "sk-new-999" not in body
    # and absolutely no "NOT stored" breadcrumb leaks secrets into replies
    assert "sk-new-999" not in (a.sent[-1]["text"] or "")


def test_key_rotation_ok_probe_persists_0600(env, monkeypatch):
    monkeypatch.setattr(cp, "_probe_entry", AsyncMock(return_value=dict(PROBE_OK)))
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-old")
    run(_cb(a, "hctl:cwx:ek:acme"))
    run(_c(a, tmsg("sk-new-999")))
    assert "CUSTOM_ACME_API_KEY=sk-new-999" in (env / ".env").read_text()
    assert oct(os.stat(env / ".env").st_mode & 0o777) == "0o600"


# ── K: capability-aware UI + gates ───────────────────────────────────────────

def _seed_removed(env, monkeypatch, overrides=None):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://api.acme.example/v1")
    for k, v in (overrides or {}).items():
        a._save_gateway_config_key(f"providers.acme.{k}", v)
    return a


def test_task_only_detail_hides_set_default_and_models(env, monkeypatch, real_telegram):
    a = _seed_removed(env, monkeypatch, {
        "provider_capabilities": ["browser_tasks", "async_runs"],
        "discovery": {"type": "none"}, "runtime": {"protocol": "task_run"}})
    text, kb = cp.screen_custom_detail(a, "acme")
    codes = [str(b.callback_data or "") for row in kb.inline_keyboard for b in row]
    assert not any(c.startswith("hctl:cust:setdef:") for c in codes)
    assert not any(c.startswith("hctl:cust:models:") for c in codes)
    assert any(c.startswith("hctl:cust:go:") for c in codes)  # Test stays for task providers
    assert _loc("cpanel.cust.taskbadge", "task-only") in text
    assert _loc("cpanel.cust.discnone", "Model discovery: Not applicable") in text
    assert "browser_tasks" in text


def test_legacy_detail_keeps_setdefault_and_models(env, monkeypatch, real_telegram):
    a = _seed_removed(env, monkeypatch, {})
    text, kb = cp.screen_custom_detail(a, "acme")
    codes = [str(b.callback_data or "") for row in kb.inline_keyboard for b in row]
    assert any(c.startswith("hctl:cust:setdef:") for c in codes)
    assert any(c.startswith("hctl:cust:models:") for c in codes)
    assert _loc("cpanel.cust.capslegacy", "(legacy default)") in text


def test_setdef_directly_rejected_for_task_only(env, monkeypatch):
    a = _seed_removed(env, monkeypatch, {
        "provider_capabilities": ["browser_tasks"],
        "default_model": "does-not-matter"})
    q = run(_cb(a, "hctl:cust:setdef:acme"))
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert "🚫" in body and _loc("cpanel.cust.nosetdef", "task-only")[:8] in body
    assert cfg(env).get("model", {}).get("provider") != "custom:acme"


def test_models_direct_shows_not_applicable(env, monkeypatch):
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: (_ for _ in ()).throw(AssertionError("no discover")))
    a = _seed_removed(env, monkeypatch, {
        "provider_capabilities": ["browser_tasks"], "discovery": {"type": "none"}})
    q = run(_cb(a, "hctl:cust:models:acme"))
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert _loc("cpanel.cust.discnone", "Model discovery: Not applicable") in body


def test_test_button_uses_generic_probe_and_shows_verdict(env, monkeypatch):
    monkeypatch.setattr(cp, "_probe_entry", AsyncMock(return_value=dict(PROBE_AUTH)))
    a = _seed_removed(env, monkeypatch, {})
    q = run(_cb(a, "hctl:cust:go:acme"))
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert "HTTP 401" in body and _loc("cpanel.test.auth", "Reason: authentication failed — check the stored key.") in body


def test_compact_list_tags_task_only_and_filters(env, monkeypatch, real_telegram):
    a = LoAdapter()
    a._save_gateway_config_key("providers.tasker.api", "https://api.tasker.example/")
    a._save_gateway_config_key("providers.tasker.provider_capabilities", ["browser_tasks", "async_runs"])
    a._save_gateway_config_key("providers.chatty.api", "https://api.chatty.example/v1")
    a._save_gateway_config_key("providers.chatty.key_env", "CUSTOM_CHATTY_API_KEY")
    cp._env_write("CUSTOM_CHATTY_API_KEY", "k")
    text, kb = cp.screen_providers(a)
    assert "tasker" in text and _loc("cpanel.cust.tasktag", "· task-only") in text
    # filter chips present
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(str(c).startswith("hctl:cust:listf:") for c in codes)
    # task filter: hides chatty, keeps tasker
    q = run(_cb(a, "hctl:cust:listf:task"))
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert "tasker" in body and "chatty" not in body
    # ready filter: tasker has no key -> out
    q2 = run(_cb(a, "hctl:cust:listf:ready"))
    body2 = (q2.edited[-1][0] or "") if q2.edited else ""
    assert "tasker" not in body2 and "chatty" in body2
    # reset for other tests
    run(_cb(a, "hctl:cust:listf:all"))
