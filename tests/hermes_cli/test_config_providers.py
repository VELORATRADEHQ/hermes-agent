"""Tests for the task-7 unified provider declarations in hermes_cli/config_providers.py.

Contract under test:
  * Legacy ``providers:`` entries (no new keys) normalize EXACTLY as before — no new keys
    appear, no behavior changes, no migration is needed.
  * New optional blocks (``auth:``/``probe:``/``discovery:``/``runtime:``/
    ``provider_capabilities:``) normalize into a strict, immutable shape.
  * Malformed blocks fail closed: the block is dropped, the provider itself survives,
    and legacy behavior stays in effect.
"""

from hermes_cli.config_providers import (
    INFERENCE_CAPABILITIES,
    LEGACY_CAPABILITY_DEFAULTS,
    _norm_capabilities,
    _normalize_custom_provider_entry,
    derive_effective_capabilities,
)


def _legacy_entry(**extra):
    base = {
        "name": "legacybox",
        "base_url": "https://legacy.example.com/v1",
        "api_key": "sk-test-123",
        "api_mode": "chat_completions",
        "model": "m1",
    }
    base.update(extra)
    return base


def test_legacy_entry_untouched():
    out = _normalize_custom_provider_entry(_legacy_entry())
    assert out is not None
    assert out["name"] == "legacybox"
    assert out["base_url"] == "https://legacy.example.com/v1"
    assert out["api_key"] == "sk-test-123"
    assert out["api_mode"] == "chat_completions"
    assert out["model"] == "m1"
    # No task-7 keys are invented for a legacy entry.
    for k in ("auth", "probe", "discovery", "runtime", "provider_capabilities"):
        assert k not in out


def test_legacy_capabilities_dict_passthrough_unchanged():
    out = _normalize_custom_provider_entry(_legacy_entry(capabilities={"vision": True, "stream": False}))
    assert out["capabilities"] == {"vision": True, "stream": False}


def test_auth_bearer_default_and_explicit():
    bearer = _normalize_custom_provider_entry(_legacy_entry(auth={"type": "bearer"}))
    assert bearer["auth"] == {"type": "bearer"}
    none_auth = _normalize_custom_provider_entry(_legacy_entry(auth={"type": "none"}))
    assert none_auth["auth"] == {"type": "none"}


def test_auth_api_key_header_with_custom_header():
    out = _normalize_custom_provider_entry(
        _legacy_entry(auth={"type": "api_key_header", "header": "X-Browser-Use-API-Key"})
    )
    assert out["auth"]["type"] == "api_key_header"
    assert out["auth"]["header"] == "X-Browser-Use-API-Key"


def test_auth_invalid_type_drops_block_fail_closed():
    out = _normalize_custom_provider_entry(_legacy_entry(auth={"type": "magic"}))
    assert "auth" not in out
    non_dict = _normalize_custom_provider_entry(_legacy_entry(auth="bearer"))
    assert "auth" not in non_dict


def test_probe_block_normalization():
    out = _normalize_custom_provider_entry(
        _legacy_entry(probe={
            "method": "get", "path": "/api/v3/billing/account",
            "headers": "X-Browser-Use-API-Key: ${KEY}", "body": {"ping": True},
            "expected_statuses": [200, "oops", -1, 401],
        })
    )
    probe = out["probe"]
    assert probe["method"] == "GET"
    assert probe["path"] == "/api/v3/billing/account"
    assert isinstance(probe["headers"], str) and "X-Browser-Use-API-Key" in probe["headers"]
    assert probe["body"] == '{"ping": true}'
    assert probe["expected_statuses"] == [200, 401]


def test_probe_non_relative_path_dropped():
    out = _normalize_custom_provider_entry(_legacy_entry(probe={"path": "https://evil.example.com/x"}))
    assert "path" not in (out.get("probe") or {})
    out2 = _normalize_custom_provider_entry(_legacy_entry(probe={"method": "POST", "path": "/ok"}))
    assert out2["probe"]["method"] == "POST" and out2["probe"]["path"] == "/ok"


def test_discovery_none_and_custom_https_only():
    none = _normalize_custom_provider_entry(_legacy_entry(discovery={"type": "none"}))
    assert none["discovery"] == {"type": "none"}
    custom = _normalize_custom_provider_entry(
        _legacy_entry(discovery={"type": "custom", "models_url": "https://x.example.com/catalog"})
    )
    assert custom["discovery"]["models_url"] == "https://x.example.com/catalog"
    http = _normalize_custom_provider_entry(
        _legacy_entry(discovery={"type": "custom", "models_url": "http://insecure.example.com/m"})
    )
    assert "models_url" not in http["discovery"]


def test_runtime_protocols():
    ok = _normalize_custom_provider_entry(_legacy_entry(runtime={"protocol": "task_run"}))
    assert ok["runtime"] == {"protocol": "task_run"}
    bad = _normalize_custom_provider_entry(_legacy_entry(runtime={"protocol": "telekinesis"}))
    assert "runtime" not in bad


def test_provider_capabilities_normalization():
    assert _norm_capabilities(None) == []
    assert _norm_capabilities("chat") == []          # only containers accepted
    caps = _norm_capabilities([" chat ", "CHAT", "browser_tasks", "INVALID-CAP", "has space", 42])
    assert caps == ["chat", "browser_tasks"]
    out = _normalize_custom_provider_entry(_legacy_entry(provider_capabilities=["browser_tasks", "async_runs"]))
    assert out["provider_capabilities"] == ["browser_tasks", "async_runs"]


def test_effective_capabilities_explicit_wins_else_legacy_default():
    legacy = _normalize_custom_provider_entry(_legacy_entry())
    eff = derive_effective_capabilities(legacy)
    assert eff == LEGACY_CAPABILITY_DEFAULTS
    assert eff & INFERENCE_CAPABILITIES  # legacy entries are always picker-visible

    task_only = _normalize_custom_provider_entry(
        _legacy_entry(provider_capabilities=["browser_tasks", "async_runs", "status_polling"])
    )
    eff2 = derive_effective_capabilities(task_only)
    assert eff2 == frozenset({"browser_tasks", "async_runs", "status_polling"})
    assert eff2.isdisjoint(INFERENCE_CAPABILITIES)


def test_inference_capability_sets_nonempty():
    assert INFERENCE_CAPABILITIES >= {"chat", "completion"}
    assert LEGACY_CAPABILITY_DEFAULTS >= {"chat"}
    assert not LEGACY_CAPABILITY_DEFAULTS.isdisjoint(INFERENCE_CAPABILITIES)
