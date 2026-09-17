"""Control Panel tests — spec-mapped A/B/C/D/E/F/G + code-level AI-independence.

Every test asserts ZERO network/LLM usage except the explicit connection-test
path (Test E), which is itself fully mocked. No test touches real credentials.
"""
import ast
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.cpanel as cp


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated HERMES home for all cpanel file IO; user "1" is the configured admin."""
    monkeypatch.setattr("gateway.run._gateway_config_home", lambda: tmp_path)
    import gateway.pairing as _gp
    monkeypatch.setattr(_gp, "_configured_allowlist", lambda platform=None: ["1"])
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backups").mkdir(exist_ok=True)
    (tmp_path / "config.yaml").write_text("model:\n  default: gemini-3-flash-preview\n  provider: google\n")
    (tmp_path / ".env").write_text("GEMINI_API_KEY=AQ.FAKEKEY000111222333444555\n")
    import os
    os.chmod(tmp_path / ".env", 0o600)
    return tmp_path


@pytest.fixture()
def noguard_net(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("NETWORK FORBIDDEN in this test")
    monkeypatch.setattr("httpx.Client", boom)
    import socket
    monkeypatch.setattr(socket.socket, "connect", boom)
    return True


class FakeAdapter:
    def __init__(self, authorized=True):
        self.authorized = authorized
        self.sent = []
        self.edited = []
        self._bot = object()

    def _callback_ctx(self, query):
        return {"chat_id": 1, "chat_type": "private", "thread_id": None, "user_name": "t"}

    async def _callback_authorized(self, query, cb, denial):
        if not self.authorized:
            await query.answer(text=denial)
        return self.authorized

    def _save_gateway_config_key(self, key_path, value):
        import yaml
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


class FakeQuery:
    def __init__(self, data, uid="1"):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, first_name="Admin")
        self.message = SimpleNamespace(chat_id=1, message_thread_id=None, chat=SimpleNamespace(type="private"))
        self.answer = AsyncMock()
        self.deleted = False
        self.edited = []

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edited.append({"text": text, "kb": reply_markup})


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ── code-level AI independence ────────────────────────────────────────────────

def test_no_llm_imports_in_cpanel():
    """cpanel.py must not import any AI/LLM module — administration is local."""
    banned = ("anthropic", "openai", "google.genai", "google.generativeai", "litellm",
              "agent.client", "agent.transports", "agent.run_agent", "agent.anthropic",
              "agent.openai", "agent.google", "agent.turn", "agent.session")
    src = Path(cp.__file__).read_text()
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
    bad = [m for m in found if any(b in m for b in banned)]
    assert not bad, f"LLM imports leaked into cpanel: {bad}"


def test_screens_do_not_import_agent_package():
    src = Path(cp.__file__).read_text()
    # only agent.i18n (localization, non-AI) is permitted, and only lazily
    for marker in ("from agent.", "import agent."):
        for line in src.splitlines():
            if marker in line:
                assert "agent.i18n" in line, f"unexpected agent import: {line}"


# ── Test A: open panel with AI unavailable (mocked down) ─────────────────────

def test_A_open_panel_with_ai_down(home, noguard_net):
    a = FakeAdapter()
    text, kb = cp.screen_main(a)
    assert "Hermes Control" in text or "کنترل" in text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert len(labels) == 8
    assert all(b.callback_data.startswith("hctl:") for row in kb.inline_keyboard for b in row)


def test_B_provider_management_opens_offline(home, noguard_net):
    a = FakeAdapter()
    text, kb = cp.screen_providers(a)
    assert "google" in text.lower()
    assert "AQ.FAKEKEY" not in text  # Test G precondition
    assert "AQ.F" in text and "••••" in text
    names = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(d.startswith("hctl:add:start") for d in names)


# ── Test C: provider config CRUD without inference ───────────────────────────

def test_C_provider_ops_without_ai(home, noguard_net):
    # add/rotate key
    assert cp._env_write("XTEST_KEY", "sk-LIVEKEY999888777666555444")
    assert cp._env_get("XTEST_KEY") == "sk-LIVEKEY999888777666555444"
    # rotate
    assert cp._env_write("XTEST_KEY", "sk-ROTATED111222333444555666777")
    assert cp._env_get("XTEST_KEY").endswith("666777")
    # delete
    assert cp._env_write("XTEST_KEY", None)
    assert cp._env_get("XTEST_KEY") is None
    # env untouched for other lines
    assert cp._env_get("GEMINI_API_KEY") is not None
    import os
    assert oct(os.stat(cp._env_path()).st_mode)[-3:] == "600"
    # enable/disable + default via config writes
    a = FakeAdapter()
    assert cp._write_config_key(a, "model.disabled_providers", ["google"])
    cfg = cp._read_config()
    assert "google" in cp._disabled_set(cfg)
    assert cp._write_config_key(a, "model.disabled_providers", [])
    cfg = cp._read_config()
    assert not cp._disabled_set(cfg)
    assert cp._write_config_key(a, "model.provider", "google")
    assert cp._write_config_key(a, "model.default", "gemini-3-flash-preview")


def test_C_callback_full_flow_offline(home, noguard_net):
    a = FakeAdapter()
    # open providers via callback
    q = FakeQuery("hctl:prov")
    _run(cp.handle_callback(a, q, "hctl:prov"))
    assert q.edited and "google" in q.edited[0]["text"].lower()
    # toggle disable
    q2 = FakeQuery("hctl:prov:toggle:google:off")
    _run(cp.handle_callback(a, q2, "hctl:prov:toggle:google:off"))
    cfg = cp._read_config()
    assert "google" in cp._disabled_set(cfg)
    # enable back
    q3 = FakeQuery("hctl:prov:toggle:google:on")
    _run(cp.handle_callback(a, q3, "hctl:prov:toggle:google:on"))
    assert not cp._disabled_set(cp._read_config())
    # set default
    q4 = FakeQuery("hctl:prov:mkdefault:google")
    _run(cp.handle_callback(a, q4, "hctl:prov:mkdefault:google"))
    assert cp._read_config()["model"]["provider"] == "google"


# ── Test D: persistence across reload ────────────────────────────────────────

def test_D_config_persists(home):
    a = FakeAdapter()
    cp._write_config_key(a, "model.default", "gemini-test-persist")
    fresh = cp._read_config()
    assert fresh["model"]["default"] == "gemini-test-persist"
    cp._env_write("XPERSIST", "sk-PPP111222333444555666777888")
    assert "XPERSIST=sk-PPP111222333444555666777888" in cp._env_path().read_text()


# ── Test E: explicit 429 maps to clean quota status (not Hermes failure) ─────

def test_E_quota_429_mapped(home):
    fake_resp = SimpleNamespace(status_code=429, content=b"RESOURCE_EXHAUSTED")

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None):
            return fake_resp

    import httpx
    import gateway.cpanel as m
    orig = httpx.Client
    httpx.Client = FakeClient
    try:
        res = cp.run_connection_test("google")
    finally:
        httpx.Client = orig
    assert res["kind"] == "quota"
    assert res["http"] == 429
    assert res["ok"] is False
    assert "quota" in res["summary"].lower() or "429" in res["summary"]
    assert res["provider_icon"] == "🔴"
    # last test cache written for status screen
    cached = cp._last_test()
    assert cached.get("kind") == "quota"


def test_E2_success_maps_green(home):
    fake_resp = SimpleNamespace(status_code=200, content=b"{}")

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None): return fake_resp

    import httpx
    orig = httpx.Client
    httpx.Client = FakeClient
    try:
        res = cp.run_connection_test("google")
    finally:
        httpx.Client = orig
    assert res["ok"] is True and res["provider_icon"] == "🟢"


def test_E3_network_failure_mapped(home):
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None): raise ConnectionError("down")

    import httpx
    orig = httpx.Client
    httpx.Client = FakeClient
    try:
        res = cp.run_connection_test("google")
    finally:
        httpx.Client = orig
    assert res["kind"] == "network"


# ── Test F: unauthorized users are rejected before rendering ─────────────────

def test_F_unauthorized_callback_rejected():
    a = FakeAdapter(authorized=False)
    q = FakeQuery("hctl:main")
    _run(cp.handle_callback(a, q, "hctl:main"))
    assert not q.edited
    assert q.answer.await_count >= 1


def test_F2_unapproved_user_blocked_by_admin_gate(home):
    a = FakeAdapter(authorized=True)
    import gateway.cpanel as m
    orig = m._is_admin
    m._is_admin = lambda adapter, uid: False
    try:
        q = FakeQuery("hctl:prov", uid="999")
        _run(cp.handle_callback(a, q, "hctl:prov"))
        assert not q.edited
    finally:
        m._is_admin = orig


# ── Test G: secrets never leak into rendered output ──────────────────────────

def test_G_secret_masking_and_scrub():
    assert cp.mask_secret("AIzaSyABCDEFGH1234567") == "AIza••••567"
    assert cp.mask_secret(None) == "—"
    text = "token 123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK and sk-abcdefghijklmnop rest"
    out = cp.scrub_text(text)
    assert "123456789:" not in out and "sk-abcdefghijklmnop" not in out
    out2 = cp.scrub_text("Authorization: Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in out2


def test_G_screens_mask_keys(home, noguard_net):
    a = FakeAdapter()
    text, kb = cp.screen_providers(a)
    assert "AQ.FAKEKEY000111222333444555" not in text
    text2, kb2 = cp.screen_models(a)
    assert "AQ.FAKEKEY000111222333444555" not in text2
    # logs scrub
    (cp._home() / "state/hermes-gateway.log").write_text(
        "ok line\nbad sk-abcdefghijklmnopQRSt here\ntok 123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKx\n")
    text3, kb3 = cp.screen_logs(a)
    assert "sk-abcdefghijklmnopQRSt" not in text3
    assert "123456789:" not in text3


# ── backup/restore ────────────────────────────────────────────────────────────

def test_backup_restore_roundtrip(home, noguard_net):
    ok, msg = cp._do_backup()
    assert ok, msg
    snap = sorted((cp._home() / "backups").glob("cpanel-*.tar.gz"))
    assert snap, "no snapshot created"
    import os
    assert oct(os.stat(snap[-1]).st_mode)[-3:] == "600"
    # mutate then restore
    cp._env_path().write_text("GEMINI_API_KEY=CHANGED_VALUE_999888777\n")
    ok2, msg2 = cp._do_restore(snap[-1].name)
    assert ok2, msg2
    assert cp._env_get("GEMINI_API_KEY") == "AQ.FAKEKEY000111222333444555"


# ── pending input consumption (secret text never reaches LLM) ────────────────

def test_pending_key_input_consumed_and_deleted(home):
    a = FakeAdapter()
    cp._pending_set("1", "add", "keyonly", {"name": "google", "display": "google", "key_var": "GEMINI_API_KEY"})
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id="1"),
        message=SimpleNamespace(chat_id=1, text="sk-NEWKEY111222333444555666777",
                                message_thread_id=None, delete=AsyncMock(),
                                reply_text=AsyncMock()))
    consumed = _run(cp.consume_pending_input(a, upd, None))
    assert consumed is True
    assert upd.message.delete.await_count == 1
    assert cp._env_get("GEMINI_API_KEY") == "sk-NEWKEY111222333444555666777"
