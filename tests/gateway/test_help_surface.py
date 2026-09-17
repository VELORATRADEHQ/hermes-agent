"""Task 7: clean Telegram surface — menu visibility filter + localized /help.

I (help localization, both languages, no admin leak) + I-bis (public menu filter).
No network, no telegram dependency — pure text/handler logic with fakes.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.cpanel_help import build_help_text, handle_help_command


# ── public menu filter (plugins/platforms/telegram/adapter.py) ────────────────

def _menu_filter():
    # The adapter module needs telegram packages that live in the deployed gw venv; in the
    # stripped harness they are absent -> skip (the codespace suite runs it for real).
    mod = pytest.importorskip("plugins.platforms.telegram.adapter",
                              reason="adapter deps only exist in the gateway venv")
    return mod.TelegramAdapter._filter_public_menu


def test_public_menu_keeps_exactly_help_and_model():
    menu = [("help", "h"), ("model", "m"), ("panel", "p"), ("status", "s"),
            ("skills", "k"), ("memory", "x"), ("agent", "a"), ("gateway", "g")]
    visible, hidden = _menu_filter()(menu, 0)
    assert [n for n, _ in visible] == ["help", "model"]
    assert hidden == len(menu) - 2


def test_public_menu_fails_closed_if_required_missing():
    with pytest.raises(RuntimeError):
        _menu_filter()([("model", "m")], 0)


def test_public_menu_name_matching_case_insensitive():
    visible, hidden = _menu_filter()([("Help", "h"), ("MODEL", "m"), ("Agents", "a")], 0)
    assert [n for n, _ in visible] == ["Help", "MODEL"]
    assert hidden == 1


# ── help page content ─────────────────────────────────────────────────────────

def test_help_normal_user_no_admin_leak():
    text = build_help_text(is_admin=False)
    assert "/help" in text and "/model" in text
    assert "/panel" not in text
    # absolutely none of these internal commands may appear for a normal user
    for internal in ("/status", "/agent", "/gateway", "/memory", "/skills", "/approve",
                     "/settings", "/load", "/users"):
        assert internal not in text


def test_help_admin_sees_admin_section_only_panel():
    text = build_help_text(is_admin=True)
    assert "/panel" in text
    assert "/help" in text and "/model" in text
    for internal in ("/status", "/agent", "/gateway", "/memory", "/skills"):
        assert internal not in text


def test_help_uses_i18n_when_available(monkeypatch):
    """Localized output flows through agent.i18n (fa/en both covered by the same path)."""
    import sys
    fake = SimpleNamespace(t=lambda key, default: f"FA[{key}]")
    monkeypatch.setitem(sys.modules, "agent.i18n", fake)
    text = build_help_text(is_admin=False)
    assert "FA[help.title]" in text and "FA[help.intro]" in text
    assert "FA[help.section.admin]" not in text  # admin keys must not even be REQUESTED
    text_admin = build_help_text(is_admin=True)
    assert "FA[help.section.admin]" in text_admin


def test_help_i18n_failure_falls_back_gracefully(monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "agent.i18n", raising=False)
    real = sys.modules.get("agent.i18n")
    sys.modules["agent.i18n"] = None  # import raises
    try:
        text = build_help_text(is_admin=True)
        assert "/help" in text and "/model" in text and "/panel" in text
    finally:
        if real is not None:
            sys.modules["agent.i18n"] = real
        else:
            sys.modules.pop("agent.i18n", None)


def test_help_handler_sends_via_control_channel(monkeypatch):
    sent = []

    class A:
        async def _send_control_message(self, chat_id, text, *, parse_mode, thread_id, metadata, reply_markup=None):
            sent.append((str(chat_id), text))
            return SimpleNamespace(message_id=1)

    upd = SimpleNamespace(
        message=SimpleNamespace(chat_id=5, message_thread_id=None, reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id="1"),
    )
    import gateway.cpanel as cp_mod
    monkeypatch.setattr(cp_mod, "_is_admin", lambda adapter, uid: False)
    asyncio.run(handle_help_command(A(), upd, None))
    assert sent and sent[0][0] == "5"
    body = sent[0][1]
    assert "/help" in body and "/model" in body and "/panel" not in body
