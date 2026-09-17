"""Task 5: persistent panel control + custom OpenAI-compatible provider (offline)."""
import asyncio, os, types, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.cpanel as cp
import yaml


# ── shared fakes ─────────────────────────────────────────────────────────────
class LoAdapter:
    def __init__(self, authorized=True):
        self.authorized = authorized
        self.sent = []
        self.edited = []
        self._bot = object()
        self._handle_command = AsyncMock()

    def _callback_ctx(self, query):
        return {"chat_id": 1, "chat_type": "private", "thread_id": None, "user_name": "t"}

    async def _callback_authorized(self, query, cb, denial):
        if not self.authorized:
            await query.answer(text=denial)
        return self.authorized

    def _save_gateway_config_key(self, key_path, value):
        cfg_file = Path(cp._home() / "config.yaml")
        data = yaml.safe_load(cfg_file.read_text()) or {}
        node = data
        *parents, leaf = key_path.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
        cfg_file.write_text(yaml.safe_dump(data))
        return True

    async def _send_control_message(self, chat_id, text, *, parse_mode, thread_id, metadata, reply_markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "kb": reply_markup})
        return SimpleNamespace(message_id=1)


class TQuery:
    def __init__(self, data, uid="1"):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, first_name="Admin")
        self.message = SimpleNamespace(chat_id=1, message_thread_id=None, chat=SimpleNamespace(type="private"))
        self.answer = AsyncMock()
        self.edited = []

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edited.append((text, reply_markup))


async def _cb(adapter, data, uid="1"):
    q = TQuery(data, uid)
    await cp.handle_callback(adapter, q, data)
    return q


def tu(text=None, query=None, uid="1"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, language_code="fa"),
        callback_query=query,
        message=None,
    )


def tmsg(text, uid="1"):
    m = SimpleNamespace(text=text, chat_id=1, message_thread_id=None,
                        delete=AsyncMock(), reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid, language_code="fa"),
                           callback_query=None, message=m)


def _c(a, upd, ctx=None):
    return cp.consume_pending_input(a, upd, ctx)


def tmsg_ctx(text, uid="1"):
    return tmsg(text, uid)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({}))
    (home / ".env").write_text("")
    monkeypatch.setattr(cp, "_home", lambda: home)
    monkeypatch.setattr(cp, "_read_config", lambda: (yaml.safe_load((home / "config.yaml").read_text()) or {}))
    yield home


def cfg(home):
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


# ══ Part 1 — persistent control ═════════════════════════════════════════════
def test_p1_admin_start_attaches_persistent_keyboard(env, monkeypatch):
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "admin")
    sent_kb = object()
    monkeypatch.setattr(cp, "_persist_keyboard", lambda a: sent_kb)
    a = LoAdapter()
    run(cp.handle_start_command(a, tmsg("/start"), None))
    assert a.sent and a.sent[0]["kb"] is sent_kb
    assert cp._persist_ts("1") > 0

def test_p1_authorized_user_start_has_no_admin_keyboard(env, monkeypatch):
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "user")
    a = LoAdapter()
    run(cp.handle_start_command(a, tmsg("/start"), None))
    assert a.sent and a.sent[0]["kb"] is None

def test_p1_unknown_start_defers_to_native(env, monkeypatch):
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "unknown")
    a = LoAdapter()
    run(cp.handle_start_command(a, tmsg("/start"), None))
    assert a._handle_command.await_count == 1 and not a.sent

def test_p1_tap_opens_panel_and_rechecks_auth(env, monkeypatch):
    seen = {}
    def kind(a, u):
        seen["called"] = True
        return "admin"
    monkeypatch.setattr(cp, "_start_user_kind", kind)
    monkeypatch.setattr(cp, "_persist_keyboard", lambda a: None)
    a = LoAdapter()
    consumed = run(_c(a, tmsg("🎛 پنل مدیریت")))
    assert consumed is True and seen.get("called")
    assert a.sent
    texts = [m["text"] or "" for m in a.sent]
    assert any(("Hermes Control" in t) or ("هرمس" in t) for t in texts), f"tap must open the existing panel: {texts!r}"
    assert a.sent[-1]["kb"] is not None  # existing inline panel keyboard (env-independent)

def test_p1_tap_other_locale_label_still_recognized(env, monkeypatch):
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "admin")
    monkeypatch.setattr(cp, "_persist_keyboard", lambda a: None)
    a = LoAdapter()
    assert run(_c(a, tmsg("🎛 Control Panel"))) is True

def test_p1_tap_by_non_admin_falls_through(env, monkeypatch):
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "user")
    a = LoAdapter()
    consumed = run(_c(a, tmsg("🎛 پنل مدیریت")))
    assert consumed is False and not a.sent and not a.edited

def test_p1_revoked_admin_keyboard_removed_once(env, monkeypatch):
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "user")
    rem = object()
    monkeypatch.setattr(cp, "_persist_remove", lambda a: rem)
    cp._persist_mark("1")
    a = LoAdapter()
    run(_c(a, tmsg("hello")))
    assert any(m["kb"] is rem for m in a.sent)
    a.sent.clear()
    run(_c(a, tmsg("again")))
    assert not a.sent  # marker cleared → detach exactly once

def test_p1_first_tap_reattaches_when_marker_missing(env, monkeypatch):
    monkeypatch.setattr(cp, "_start_user_kind", lambda a, u: "admin")
    sentinel = object()
    monkeypatch.setattr(cp, "_persist_keyboard", lambda a: sentinel)
    a = LoAdapter()
    run(_c(a, tmsg("🎛 پنل مدیریت")))
    assert any(m["kb"] is sentinel for m in a.sent) and cp._persist_ts("1") > 0


# ══ Part 2 — custom provider ════════════════════════════════════════════════
def _wizard_start(env, monkeypatch):
    a = LoAdapter()
    q = TQuery("hctl:cust:new")
    run(_cb(a, q.data))
    return a


def test_p2_wizard_full_bearer_flow(env, monkeypatch):
    monkeypatch.setattr(cp, "_profiles", lambda: {"openai": SimpleNamespace()})
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: (["m1", "m2"], "success"))
    a = _wizard_start(env, monkeypatch)
    assert cp._pending_get("1")["step"] == "id"
    run(_c(a, tmsg("acme")))
    run(_c(a, tmsg("Acme LLM")))
    run(_c(a, tmsg("-")))
    run(_c(a, tmsg("https://api.acme.example/v1")))
    q = TQuery("hctl:cwx:auth:bearer"); run(_cb(a, q.data))
    assert cp._pending_get("1")["step"] == "key"
    run(_c(a, tmsg("sk-secret-123456")))
    q2 = TQuery("hctl:cwx:model:m1"); run(_cb(a, q2.data))
    assert cp._pending_get("1")["step"] == "confirm"
    q3 = TQuery("hctl:cwx:save"); run(_cb(a, q3.data))
    c = cfg(env)
    e = c["providers"]["acme"]
    assert e["api"] == "https://api.acme.example/v1"
    assert e["name"] == "Acme LLM"
    assert e["default_model"] == "m1"
    assert e["models_discovered"] is True
    assert "api_key" not in (c["providers"]["acme"])
    envv = (env / ".env").read_text()
    assert "CUSTOM_ACME_API_KEY=sk-secret-123456" in envv
    assert oct(os.stat(env / ".env").st_mode & 0o777) == "0o600"
    assert "sk-secret-123456" not in (env / "config.yaml").read_text()

def test_p2_key_message_deleted_after_capture(env, monkeypatch):
    monkeypatch.setattr(cp, "_profiles", lambda: {"openai": SimpleNamespace()})
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: (["m1"], "success"))
    a = _wizard_start(env, monkeypatch)
    for t in ("acme", "Acme", "-", "https://api.acme.example/v1"):
        run(_c(a, tmsg(t)))
    run(_cb(a, "hctl:cwx:auth:bearer"))
    m = tmsg("sk-abc-123456789")
    run(_c(a, m))
    assert m.message.delete.await_count == 1

def test_p2_id_validation(env, monkeypatch):
    monkeypatch.setattr(cp, "_profiles", lambda: {"openai": SimpleNamespace()})
    a = _wizard_start(env, monkeypatch)
    run(_c(a, tmsg("ACME!")))
    assert cp._pending_get("1")["step"] == "id"
    run(_c(a, tmsg("openai")))
    assert cp._pending_get("1")["step"] == "id"
    run(_c(a, tmsg("acme")))
    assert cp._pending_get("1")["step"] == "display"

def test_p2_duplicate_id_rejected(env, monkeypatch):
    monkeypatch.setattr(cp, "_profiles", lambda: {"openai": SimpleNamespace()})
    LoAdapter()._save_gateway_config_key.__self__  # noop
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x")
    a2 = _wizard_start(env, monkeypatch)
    run(_c(a2, tmsg("acme")))
    assert cp._pending_get("1")["step"] == "id"

def test_p2_base_url_validation(env, monkeypatch):
    monkeypatch.setattr(cp, "_profiles", lambda: {})
    a = _wizard_start(env, monkeypatch)
    for t in ("acme", "Acme", "-"):
        run(_c(a, tmsg(t)))
    run(_c(a, tmsg("ht tp://bad url")))
    assert cp._pending_get("1")["step"] == "base"
    run(_c(a, tmsg("http://ext.example/v1")))   # http non-local refused
    assert cp._pending_get("1")["step"] == "base"
    run(_c(a, tmsg("http://localhost:11434/v1")))
    assert cp._pending_get("1")["step"] == "auth"

def test_p2_noauth_flow_saves_without_key(env, monkeypatch):
    monkeypatch.setattr(cp, "_profiles", lambda: {})
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: (["llama3"], "success"))
    a = _wizard_start(env, monkeypatch)
    for t in ("lmstudio", "LM Studio", "-", "http://localhost:1234/v1"):
        run(_c(a, tmsg(t)))
    run(_cb(a, "hctl:cwx:auth:none"))
    run(_cb(a, "hctl:cwx:model:llama3"))
    run(_cb(a, "hctl:cwx:save"))
    e = cfg(env)["providers"]["lmstudio"]
    assert e["api"] == "http://localhost:1234/v1" and "key_env" not in e

def test_p2_discovery_failure_still_saves(env, monkeypatch):
    monkeypatch.setattr(cp, "_profiles", lambda: {})
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: (None, "malformed"))
    a = _wizard_start(env, monkeypatch)
    for t in ("weird", "Weird", "-", "https://w.example/v1"):
        run(_c(a, tmsg(t)))
    run(_cb(a, "hctl:cwx:auth:none"))
    assert cp._pending_get("1")["step"] == "confirm"
    run(_cb(a, "hctl:cwx:save"))
    assert "weird" in cfg(env)["providers"]

def test_p2_set_default_route(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://api.acme.example/v1")
    a._save_gateway_config_key("providers.acme.default_model", "m1")
    run(_cb(a, "hctl:cust:setdef:acme"))
    assert cfg(env)["model"]["provider"] == "custom:acme"
    assert cfg(env)["model"]["default"] == "m1"

def test_p2_set_default_requires_model(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://api.acme.example/v1")
    q = run(_cb(a, "hctl:cust:setdef:acme"))
    assert "Pick a default model" in q.edited[-1][0] or "مدل" in q.edited[-1][0]
    assert ("model" not in cfg(env))

def test_p2_toggle_enable_disable(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    run(_cb(a, "hctl:cust:toggle:acme"))
    assert cfg(env)["providers"]["acme"]["enabled"] is False
    run(_cb(a, "hctl:cust:toggle:acme"))
    assert cfg(env)["providers"]["acme"]["enabled"] is True

def test_p2_delete_removes_config_and_key(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    a._save_gateway_config_key("model.provider", "custom:acme")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-x")
    run(_cb(a, "hctl:cust:delgo:acme"))
    c = cfg(env)
    assert "acme" not in c.get("providers", {})
    assert "CUSTOM_ACME_API_KEY" not in (env / ".env").read_text()

def test_p2_edit_base_url(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://old.example/v1")
    run(_cb(a, "hctl:cwx:eb:acme"))
    assert cp._pending_get("1")["step"] == "basedit"
    run(_c(a, tmsg("https://new.example/v2")))
    assert cfg(env)["providers"]["acme"]["api"] == "https://new.example/v2"

def test_p2_rotate_key_rewrites_env_var(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-old")
    run(_cb(a, "hctl:cwx:ek:acme"))
    run(_c(a, tmsg("sk-new-999")))
    body = (env / ".env").read_text()
    assert "sk-new-999" in body and "sk-old" not in body

def test_p2_providers_screen_shows_custom(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://api.acme.example/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-dontprint")
    text, kb = cp.screen_providers(a)
    assert "🧩 acme" in text and "https://api.acme.example" in text
    assert "sk-dontprint" not in text

def test_p2_detail_screen_masks_key(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-neverprint")
    text, kb = cp.screen_custom_detail(a, "acme")
    assert "sk-neverprint" not in text
    assert "…" in text or "sk" in text  # masked form present

def test_p2_setmodel_writes_default_model(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    run(_cb(a, "hctl:cust:setmodel:acme:gpt-mini"))
    assert cfg(env)["providers"]["acme"]["default_model"] == "gpt-mini"

def test_p2_models_screen_from_discovery(env, monkeypatch):
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: (["m1", "m2"], "success"))
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    q = run(_cb(a, "hctl:cust:models:acme"))
    assert q.edited and "m1" in (q.edited[-1][0] or ""), f"models list must render in editable text: {q.edited!r}"

def test_p2_website_is_metadata_only_no_scrape(env, monkeypatch):
    calls = []
    monkeypatch.setattr(cp, "discover_models", lambda b, m, k: calls.append((b, m, k)) or (None, "malformed"))
    monkeypatch.setattr(cp, "_profiles", lambda: {})
    a = _wizard_start(env, monkeypatch)
    for t in ("metaonly", "Meta Only", "https://acme.ai", "https://api.acme.example/v1"):
        run(_c(a, tmsg(t)))
    run(_cb(a, "hctl:cwx:auth:none"))
    assert calls and calls[0][0] == "https://api.acme.example/v1"
    assert all("acme.ai" not in (c[0] or "") for c in calls)
    run(_cb(a, "hctl:cwx:save"))
    meta = cfg(env).get("cpanel_providers_meta", {})
    assert meta.get("metaonly", {}).get("website") == "https://acme.ai"


class _Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload if payload is not None else {"data": [{"id": "m"}]}
    def json(self):
        return self._payload


@pytest.mark.parametrize("code,kind", [(200, "success"), (401, "auth"), (403, "auth"),
                                       (404, "not_found"), (429, "quota"), (500, "provider_error")])
def test_p2_test_connection_kinds(env, monkeypatch, code, kind):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-k")
    import httpx
    class _Cli:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None): return _Resp(code)
    monkeypatch.setattr(httpx, "Client", _Cli)
    res = cp.run_connection_test("custom:acme")
    assert res["kind"] == kind
    if kind == "success":
        assert res["count"] == 1

def test_p2_test_connection_no_key(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    res = cp.run_connection_test("custom:acme")
    assert res["kind"] == "no_key"

def test_p2_test_connection_malformed_json(env, monkeypatch):
    a = LoAdapter()
    a._save_gateway_config_key("providers.acme.api", "https://x.example/v1")
    cp._env_write("CUSTOM_ACME_API_KEY", "sk-k")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    import httpx
    class _Cli:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None): return _Resp(200, {"unexpected": True})
    monkeypatch.setattr(httpx, "Client", _Cli)
    ids, kind = cp.discover_models("https://x.example/v1", "", "sk-k")
    assert ids is None and kind == "malformed"

def test_p2_discover_models_parses_openai_shape(env, monkeypatch):
    import httpx
    class _Cli:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            assert url == "https://x.example/v1/models"
            return _Resp(200, {"data": [{"id": "b"}, {"id": "a"}]})
    monkeypatch.setattr(httpx, "Client", _Cli)
    ids, kind = cp.discover_models("https://x.example/v1", "", None)
    assert kind == "success" and ids == ["a", "b"]


# ══ Regression guards ═══════════════════════════════════════════════════════
def test_rg_native_providers_still_listed(env, monkeypatch):
    a = LoAdapter()
    text, kb = cp.screen_providers(a)
    assert "openai" in text  # stubbed native registry entry must appear
