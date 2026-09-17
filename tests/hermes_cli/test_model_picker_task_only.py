"""Tests: task-only custom providers never appear in the /model picker.

A custom provider whose declared ``provider_capabilities:`` contains NO inference
capability (chat/completion/embeddings) runs tasks, not text completions — it must not
show up as an ordinary model choice. Legacy entries (no declaration) keep old behavior
and always remain visible. Built-ins are untouched because they never carry the new key.
"""

import pytest

import hermes_cli.model_switch as model_switch
from hermes_cli.model_switch_providers import list_picker_providers


BROWSER_USE_ENTRY = {
    "name": "browseruse",
    "base_url": "https://api.browser-use.com/api/v4",
    "api_key": "sk-hidden",
    "provider_key": "browseruse",
    "provider_capabilities": ["browser_tasks", "async_runs", "status_polling"],
    "auth": {"type": "api_key_header", "header": "X-Browser-Use-API-Key"},
}

LEGACY_ENTRY = {
    "name": "legacybox",
    "base_url": "https://legacy.example.com/v1",
    "api_key": "sk-test",
    "provider_key": "legacybox",
}

ROWS = [
    {"slug": "openai", "name": "openai", "models": ["gpt-4o"], "is_current": False,
     "is_user_defined": False, "api_url": ""},
    {"slug": "custom:browseruse", "name": "browseruse", "models": ["m1"], "is_current": False,
     "is_user_defined": True, "api_url": "https://api.browser-use.com/api/v4"},
    {"slug": "custom:legacybox", "name": "legacybox", "models": ["m1"], "is_current": False,
     "is_user_defined": True, "api_url": "https://legacy.example.com/v1"},
]


@pytest.fixture(autouse=True)
def _stub_listing(monkeypatch):
    monkeypatch.setattr(model_switch, "list_authenticated_providers",
                        lambda **kwargs: [dict(r) for r in ROWS])


def _slugs(custom_providers):
    return [p["slug"] for p in list_picker_providers(
        custom_providers=custom_providers, include_moa=False, max_models=None)]


def test_task_only_provider_hidden_legacy_kept():
    slugs = _slugs([BROWSER_USE_ENTRY, LEGACY_ENTRY])
    assert "custom:browseruse" not in slugs
    assert "custom:legacybox" in slugs
    assert "openai" in slugs


def test_inference_capable_declaration_kept():
    chat_plus_tasks = dict(BROWSER_USE_ENTRY, provider_capabilities=["chat", "browser_tasks"])
    slugs = _slugs([chat_plus_tasks])
    assert "custom:browseruse" in slugs


def test_no_custom_providers_argument_is_safe():
    # No entries passed -> lookup empty -> everything survives (defensive: must not crash).
    slugs = _slugs(None)
    assert set(slugs) == {"openai", "custom:browseruse", "custom:legacybox"}


def test_row_carried_capabilities_also_filter(monkeypatch):
    # A row that itself carries declared capabilities (future-proof path).
    rows = [dict(r) for r in ROWS]
    rows[1]["provider_capabilities"] = ["browser_tasks"]
    monkeypatch.setattr(model_switch, "list_authenticated_providers", lambda **kwargs: rows)
    slugs = [p["slug"] for p in list_picker_providers(custom_providers=[])]
    assert "custom:browseruse" not in slugs
    assert "openai" in slugs


def test_capability_lookup_alias_shapes():
    from hermes_cli.model_switch_providers import _custom_capability_lookup
    lookup = _custom_capability_lookup([BROWSER_USE_ENTRY])
    frozen = frozenset({"browser_tasks", "async_runs", "status_polling"})
    assert lookup["custom:browseruse"] == frozen
    assert lookup["browseruse"] == frozen
