"""Task-8: Admin Control Panel model switching must reach the REAL runtime state.

Regression context: the panel wrote ``model.default`` (global) but a persisted per-session
/model pin (``gateway_routing.model_override``) has TOP precedence per-turn, so the admin who
pressed "set active model" kept running their old pinned model forever — the panel looked
decorative. Also the panel's ``model.disabled_providers`` was panel-internal only; the native
runtime gate is ``providers.<name>.enabled: false``.

These tests assert:
  1. Panel set-model persists the global default (config file truth).
  2. A stale /model pin on the invoking chat is CLEARED + the cached agent evicted (next turn
     re-resolves from the global default).
  3. Pins already matching are untouched; other sessions are never touched.
  4. Provider toggle writes the NATIVE gate (providers.<name>.enabled), and Set-Default
     auto-enables the chosen provider.
  5. Failures in session state never pretend success; the config write still stands
     (fail-visible, not UI-mute).
"""
from types import SimpleNamespace

import gateway.cpanel as cp
from tests.gateway.test_cpanel_t5 import (LoAdapter, TQuery, _cb, _c, tmsg, tmsg_ctx, cfg, run, env)
from tests.gateway.test_cpanel_t7 import real_telegram  # markup assertions need real PTB objects

import pytest


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """Same harness baseline as tests/gateway/test_cpanel_t5.py."""
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


class _FakeStore:
    def __init__(self, overrides=None):
        self.overrides = dict(overrides or {})
        self.writes = []

    def get_model_override(self, key):
        ov = self.overrides.get(key)
        return dict(ov) if ov else None

    def set_model_override(self, key, value):
        self.writes.append((key, value))
        if value is None:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = dict(value)


class _FailStore(_FakeStore):
    def set_model_override(self, key, value):
        raise RuntimeError("db locked")


class _FakeRunner:
    def __init__(self, store):
        self.session_store = store
        self.evicted = []

    def _normalize_source_for_session_key(self, src):
        return src

    def _session_key_for_source(self, src):
        return f"sess:telegram:{src.chat_id}"

    def _evict_cached_agent(self, session_key):
        self.evicted.append(session_key)


def _attach(a, store=None):
    """Wire a fake gateway runner (with session store) to the Lo adapter."""
    store = store if store is not None else _FakeStore()
    runner = _FakeRunner(store)
    a.gateway_runner = runner
    return runner


def _loc(key, default):
    return cp._T(key, default)


# ── helpers under test ────────────────────────────────────────────────────────

def test_current_chat_session_resolution(env):
    # Without a runner attached: graceful degradation (plain config writes still work).
    a = LoAdapter()
    a.gateway_runner = None
    key, store, runner = cp._current_chat_session(a, TQuery("hctl:models"), chat_id="1")
    assert key is None and store is None and runner is None
    # With a runner: derive the invoking chat's key through the runner's own key function.
    a2 = LoAdapter()
    st = _FakeStore()
    _attach(a2, st)
    key2, store2, runner2 = cp._current_chat_session(a2, TQuery("hctl:models"), chat_id="449106486")
    assert key2 == "sess:telegram:449106486" and store2 is st and runner2 is not None


def test_session_pin_read_and_clear(env):
    store = _FakeStore({"sess:telegram:1": {"model": "mA", "provider": "openrouter"}})
    assert cp._session_model_pin(store, "sess:telegram:1")["model"] == "mA"
    assert cp._session_model_pin(store, "sess:telegram:2") is None
    runner = _FakeRunner(store)
    assert cp._clear_session_pin_and_evict(store, runner, "sess:telegram:1") is True
    assert store.overrides == {}
    assert runner.evicted == ["sess:telegram:1"]
    # store failure: pin NOT cleared, no pretend-evict success claim
    fstore = _FailStore({"sess:telegram:1": {"model": "mA"}})
    assert cp._clear_session_pin_and_evict(fstore, runner, "sess:telegram:1") is False
    assert "sess:telegram:1" in fstore.overrides


# ── screen truthfulness ───────────────────────────────────────────────────────

def test_models_screen_shows_pin_state(env, real_telegram):
    a = LoAdapter()
    text, kb = cp.screen_models(a, pin={"model": "gemini-3.5-flash", "provider": "gemini"})
    assert _loc("cpanel.models.pin", "This chat is pinned via /model to: ")[:10] in text
    assert "gemini-3.5-flash" in text
    codes = [str(b.callback_data or "") for row in kb.inline_keyboard for b in row]
    assert "hctl:models:clearpin" in codes
    text2, kb2 = cp.screen_models(a, pin=None)
    assert _loc("cpanel.models.nopin", "Effective next message in this chat: global default.")[:10] in text2
    codes2 = [str(b.callback_data or "") for row in kb2.inline_keyboard for b in row]
    assert "hctl:models:clearpin" not in codes2


# ── the core regression: panel set-model applies to the invoking chat ─────────

def test_models_set_persists_global(env):
    a = LoAdapter()
    q = run(_cb(a, "hctl:models:set:mB"))
    assert cfg(env)["model"]["default"] == "mB"


def test_models_set_clears_stale_pin_and_evicts(env):
    a = LoAdapter()
    store = _FakeStore({"sess:telegram:1": {"model": "gemini-3.5-flash", "provider": "gemini"}})
    runner = _attach(a, store)
    q = run(_cb(a, "hctl:models:set:mB"))
    assert cfg(env)["model"]["default"] == "mB"              # global persisted
    assert ("sess:telegram:1", None) in store.writes         # pin cleared
    assert store.overrides == {}                             # actually gone
    assert "sess:telegram:1" in runner.evicted               # cached agent rebuilt next turn
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert _loc("cpanel.models.pinapplied", "Applied to this chat too")[:12] in body


def test_models_set_keeps_matching_pin_untouched(env):
    a = LoAdapter()
    store = _FakeStore({"sess:telegram:1": {"model": "mB", "provider": "openrouter"}})
    runner = _attach(a, store)
    run(_cb(a, "hctl:models:set:mB"))
    assert store.writes == []                                # no clear call
    assert runner.evicted == []


def test_models_set_other_sessions_untouched(env):
    a = LoAdapter()
    store = _FakeStore({
        "sess:telegram:1": {"model": "old"},                 # invoking chat — cleared
        "sess:telegram:999": {"model": "other-user"},        # somebody else's pin stays
    })
    _attach(a, store)
    run(_cb(a, "hctl:models:set:mB"))
    assert store.overrides.get("sess:telegram:999") == {"model": "other-user"}


def test_models_set_store_failure_still_persists_and_warns(env):
    a = LoAdapter()
    _attach(a, _FailStore({"sess:telegram:1": {"model": "old"}}))
    q = run(_cb(a, "hctl:models:set:mB"))
    assert cfg(env)["model"]["default"] == "mB"              # write stands
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert "⚠️" in body and _loc("cpanel.models.pinblocked", "could not be cleared")[:8] in body


def test_models_clearpin_button(env):
    a = LoAdapter()
    store = _FakeStore({"sess:telegram:1": {"model": "old", "provider": "x"}})
    runner = _attach(a, store)
    q = run(_cb(a, "hctl:models:clearpin"))
    assert store.overrides == {}
    assert "sess:telegram:1" in runner.evicted
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert _loc("cpanel.models.pincleared", "Pin cleared")[:8] in body


def test_models_clearpin_without_pin(env):
    a = LoAdapter()
    store = _FakeStore()
    runner = _attach(a, store)
    run(_cb(a, "hctl:models:clearpin"))
    assert store.writes == [] and runner.evicted == []


# ── native disabled-provider gate bridge ──────────────────────────────────────

def test_disabled_set_reads_native_gate(env):
    a = LoAdapter()
    a._save_gateway_config_key("providers.brokenprov.enabled", False)     # native gate
    a._save_gateway_config_key("model.disabled_providers", ["legacyone"])  # legacy panel list
    dis = cp._disabled_set(cfg(env))
    assert "brokenprov" in dis and "legacyone" in dis


def test_provider_toggle_writes_native_gate(env):
    a = LoAdapter()
    a._save_gateway_config_key("model.disabled_providers", ["openai"])
    run(_cb(a, "hctl:prov:toggle:openAI:off"))
    c = cfg(env)
    assert c["providers"]["openAI"]["enabled"] is False      # native gate — runtime honored
    assert "openai" not in (c["model"].get("disabled_providers") or [])   # legacy migrated out
    run(_cb(a, "hctl:prov:toggle:openAI:on"))
    assert cfg(env)["providers"]["openAI"]["enabled"] is True


def test_toggle_active_provider_warns(env):
    a = LoAdapter()
    a._save_gateway_config_key("model.provider", "openAI")
    q = run(_cb(a, "hctl:prov:toggle:openAI:off"))
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert _loc("cpanel.prov.offactive", "You disabled the ACTIVE default provider")[:10] in body


def test_mkdefault_auto_enables_provider(env):
    a = LoAdapter()
    a._save_gateway_config_key("providers.openAI.enabled", False)
    a._save_gateway_config_key("model.disabled_providers", ["openai"])
    run(_cb(a, "hctl:prov:mkdefault:openAI"))
    c = cfg(env)
    assert c["model"]["provider"] == "openAI"
    assert "openai" not in (c["model"].get("disabled_providers") or [])


# ── production-path proof: the per-turn resolver picks the panel's model ─────

@pytest.fixture
def gateway_store(tmp_path, monkeypatch):
    """Real SessionStore over a tmp sessions dir (SQLite disabled), like the live deployment's
    gateway_routing rows that carry persisted /model pins."""
    import hermes_state
    monkeypatch.setattr(hermes_state, "SessionDB", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _live_runner(store):
    """Real GatewayRunner (bare-constructed like the existing override suites) around the store."""
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.session_store = store
    runner.config = GatewayConfig()
    return runner


def test_panel_set_model_reaches_real_per_turn_resolver(env, gateway_store):
    """The decisive regression: BEFORE, a persisted /model pin wins over the config default on
    every turn (the panel looked decorative). AFTER a panel `models:set`, the pin is cleared
    through the REAL session store and the production per-turn resolver
    (``_resolve_session_agent_runtime`` — the same method run_turn calls before constructing
    the AIAgent whose ``agent.model`` becomes the request ``model`` param) returns the panel's
    model on the very next resolution."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    store = gateway_store
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="449106486",
                        chat_type="dm", user_id="449106486")
    entry = store.get_or_create_session(src)
    session_key = entry.session_key
    pin = {"model": "gemini-3.5-flash", "provider": "gemini",
           "base_url": "https://generativelanguage.googleapis.com/v1beta"}
    store.set_model_override(session_key, pin)

    runner = _live_runner(store)

    # Credentials layer mocked to a valid openrouter runtime so the resolver's provider-kwargs
    # path is deterministic; the model itself comes from the config/pin logic under test.
    from unittest.mock import patch as _patch
    runtime_kwargs = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                      "api_key": "sk-test", "requested_provider": "openrouter", "api_mode": "chat_completions",
                      "credential_pool": None, "request_overrides": {}, "capabilities": {}}

    # BEFORE: production resolver returns the PINNED model — config default is shadowed
    # (this is the exact divergence the user observed).
    cfgA = {"model": {"default": "model-A-config", "provider": "openrouter"}}
    with _patch("gateway.run._resolve_runtime_agent_kwargs", return_value=dict(runtime_kwargs)), \
         _patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", return_value=dict(runtime_kwargs)):
        model_before, _ = runner._resolve_session_agent_runtime(session_key=session_key, user_config=cfgA)
    assert model_before == "gemini-3.5-flash"

    # Panel apply: handler with the LIVE runner attached to the adapter, callback issued FROM
    # the chat that owns the pin (adapter ctx points at the live admin DM, as the real
    # Telegram adapter's _callback_ctx would derive it from the query message).
    a = LoAdapter()
    a.gateway_runner = runner
    a._callback_ctx = lambda _q: {"chat_id": "449106486", "chat_type": "private",
                                  "thread_id": None, "user_name": "t"}
    q = TQuery("hctl:models:set:model-B-panel")
    q.message.chat_id = "449106486"
    run(cp.handle_callback(a, q, "hctl:models:set:model-B-panel"))
    assert cfg(env)["model"]["default"] == "model-B-panel"

    # AFTER: same resolver, fresh config — the panel's model wins on the very next turn.
    cfgB = {"model": {"default": "model-B-panel", "provider": "openrouter"}}
    with _patch("gateway.run._resolve_runtime_agent_kwargs", return_value=dict(runtime_kwargs)), \
         _patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", return_value=dict(runtime_kwargs)):
        model_after, _ = runner._resolve_session_agent_runtime(session_key=session_key, user_config=cfgB)
    assert model_after == "model-B-panel"

    # Persistence stays honest: the pin row is actually gone from the store.
    assert store.get_model_override(session_key) is None
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert _loc("cpanel.models.pinapplied", "Applied to this chat too")[:12] in body


def test_panel_switch_back_and_restart_persistence(env, gateway_store):
    """B→A switching reaches the resolver, and a simulated gateway restart (fresh store handle
    + fresh bare runner over the same sessions dir) keeps resolving the panel's global default —
    the pin does not resurrect."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from unittest.mock import patch as _patch

    store = gateway_store
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="449106486", chat_type="dm", user_id="449106486")
    session_key = store.get_or_create_session(src).session_key
    store.set_model_override(session_key, {"model": "pinned-old", "provider": "gemini"})
    runner = _live_runner(store)
    runtime_kwargs = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                      "api_key": "sk-test", "requested_provider": "openrouter", "api_mode": "chat_completions",
                      "credential_pool": None, "request_overrides": {}, "capabilities": {}}

    a = LoAdapter()
    a.gateway_runner = runner

    def _set(m_id):
        a._callback_ctx = lambda _q: {"chat_id": "449106486", "chat_type": "private",
                                      "thread_id": None, "user_name": "t"}
        q = TQuery(f"hctl:models:set:{m_id}")
        q.message.chat_id = "449106486"
        run(cp.handle_callback(a, q, f"hctl:models:set:{m_id}"))

    def _resolve(m_cfg):
        with _patch("gateway.run._resolve_runtime_agent_kwargs", return_value=dict(runtime_kwargs)), \
             _patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", return_value=dict(runtime_kwargs)):
            return runner._resolve_session_agent_runtime(
                session_key=session_key,
                user_config={"model": {"default": m_cfg, "provider": "openrouter"}})[0]

    _set("model-B")
    assert _resolve("model-B") == "model-B"
    _set("model-A")
    assert _resolve("model-A") == "model-A"

    # Simulated restart: new store handle over the same dir + fresh runner (empty memory).
    import gateway.session as gs
    store2 = gs.SessionStore(sessions_dir=store.sessions_dir, config=store.config)
    runner2 = _live_runner(store2)
    a2 = LoAdapter()
    a2.gateway_runner = runner2
    assert store2.get_model_override(session_key) is None      # nothing to rehydrate
    with _patch("gateway.run._resolve_runtime_agent_kwargs", return_value=dict(runtime_kwargs)), \
         _patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", return_value=dict(runtime_kwargs)):
        model = runner2._resolve_session_agent_runtime(
            session_key=session_key, user_config={"model": {"default": "model-A", "provider": "openrouter"}})[0]
    assert model == "model-A"


# ── task providers: Run-task route in the panel (capability-gated; not an LLM) ─

def _seed_task_provider(a, pid="buser", cred=True):
    a._save_gateway_config_key(f"providers.{pid}.api", "https://api.browser-use.com/api/v4")
    a._save_gateway_config_key(f"providers.{pid}.runtime.type", "task_run")
    a._save_gateway_config_key(f"providers.{pid}.provider_capabilities", ["browser_tasks", "async_runs"])
    if cred:
        a._save_gateway_config_key(f"providers.{pid}.key_env", f"CUSTOM_{pid.upper()}_API_KEY")
        cp._env_write(f"CUSTOM_{pid.upper()}_API_KEY", "k")
    return pid


def test_run_task_button_only_for_task_only(env, real_telegram):
    a = LoAdapter()
    pid = _seed_task_provider(a)
    text, kb = cp.screen_custom_detail(a, pid)
    codes = [str(b.callback_data or "") for row in kb.inline_keyboard for b in row]
    assert any(c.startswith(f"hctl:cust:runtask:{pid}") for c in codes)
    # chat providers never get the button
    a._save_gateway_config_key("providers.acme.api", "https://x/v1")
    a._save_gateway_config_key("providers.acme.key_env", "CUSTOM_ACME_API_KEY")
    cp._env_write("CUSTOM_ACME_API_KEY", "k")
    text2, kb2 = cp.screen_custom_detail(a, "acme")
    codes2 = [str(b.callback_data or "") for row in kb2.inline_keyboard for b in row]
    assert not any(c.startswith("hctl:cust:runtask:") for c in codes2)


def test_runtask_callback_guards(env):
    a = LoAdapter()
    # chat provider → not capable, no pending
    a._save_gateway_config_key("providers.acme.api", "https://x/v1")
    q = run(_cb(a, "hctl:cust:runtask:acme"))
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert _loc("cpanel.task.notcapable", "This provider is not task-capable.")[:10] in body
    assert cp._pending_get("1") is None
    # task-only without credential → nokey, no pending
    _seed_task_provider(a, pid="nocred", cred=False)
    q2 = run(_cb(a, "hctl:cust:runtask:nocred"))
    body2 = (q2.edited[-1][0] or "") if q2.edited else ""
    assert _loc("cpanel.task.nokey", "No credential configured")[:10] in body2
    assert cp._pending_get("1") is None


def test_runtask_flow_executes_runtime_and_renders_status(env, monkeypatch):
    a = LoAdapter()
    pid = _seed_task_provider(a)
    seen = {}

    async def fake_run(entry, instruction, **kw):
        seen["instruction"] = instruction
        seen["entry_base"] = entry.get("base_url") or entry.get("api")
        return {"status": "success", "ok": True, "run_id": "run-77", "output": "done: title read",
                "http_status": 200, "detail": "", "polls": 2, "elapsed_s": 0.3}

    monkeypatch.setattr(cp, "_run_task_for_entry", fake_run)
    q = run(_cb(a, f"hctl:cust:runtask:{pid}"))
    body = (q.edited[-1][0] or "") if q.edited else ""
    assert _loc("cpanel.task.ask", "Send the task instruction")[:10] in body
    p = cp._pending_get("1")
    assert p and p.get("flow") == "task" and p.get("data", {}).get("pid") == pid
    # admin sends the instruction as a plain message → executes once, renders truthfully
    handled = run(_c(a, tmsg_ctx("open example.com and read the title")))
    assert handled is True
    assert a.sent and "success" in (a.sent[-1]["text"] or "")
    assert "run-77" in (a.sent[-1]["text"] or "")
    assert "done: title read" in (a.sent[-1]["text"] or "")
    assert seen["instruction"] == "open example.com and read the title"
    assert "browser-use.com" in seen["entry_base"]


def test_runtask_flow_failure_is_explicit(env, monkeypatch):
    a = LoAdapter()
    pid = _seed_task_provider(a)

    async def fake_fail(entry, instruction, **kw):
        return {"status": "auth", "ok": False, "run_id": None, "output": "", "http_status": 401,
                "detail": "invalid key", "polls": 0, "elapsed_s": 0.1}

    monkeypatch.setattr(cp, "_run_task_for_entry", fake_fail)
    run(_cb(a, f"hctl:cust:runtask:{pid}"))
    run(_c(a, tmsg_ctx("run something")))
    assert a.sent and "auth" in (a.sent[-1]["text"] or "") and "401" in (a.sent[-1]["text"] or "")
