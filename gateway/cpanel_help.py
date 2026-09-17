"""Task 7: localized, professional /help page for the Telegram surface.

Design contract:
  * The page lists ONLY the commands the calling user can actually USE. For normal
    users that is exactly the public menu surface (/help, /model). Admins additionally
    see the admin entry (/panel) in a clearly separated section — no other internal
    command names ever appear.
  * Everything renders through agent.i18n's ``t()`` with safe defaults; no AI call,
    no network, no internal command leak regardless of configured language.
  * Presentation-only: this page never overrides authorization (pairing gates still
    decide who may run what).
"""


def _t(key: str, default: str) -> str:
    try:
        from agent.i18n import t  # localization only — no LLM
        out = t(key, default)
        return out or default
    except Exception:
        return default


def build_help_text(*, is_admin: bool) -> str:
    """Structured help page. ``is_admin`` only adds the admin-only section."""
    lines = [
        _t("help.title", "📖 Hermes — Help & Commands").strip(),
        "─" * 26,
        "",
        _t("help.intro", "Private chat with your Hermes AI gateway. Ask anything in plain text.").strip(),
        "",
        _t("help.section.commands", "Available commands:").strip(),
        f"  • /help — {_t('help.cmd.help', 'show this page')}",
        f"  • /model — {_t('help.cmd.model', 'browse providers and switch the active AI model')}",
    ]
    if is_admin:
        lines += [
            "",
            _t("help.section.admin", "Admin tools (visible to you because you are an admin):").strip(),
            f"  • /panel — {_t('help.cmd.panel', 'open the Control Panel (providers, users, settings)')}",
        ]
    lines += [
        "",
        _t("help.footer", "Type your question directly — no command needed for normal chat.").strip(),
    ]
    return "\n".join(lines)


async def handle_help_command(adapter, update, context) -> None:
    """`/help` — localized structured page, admin-aware. Requires no extra auth beyond the
    pairing gate that already covered the message (any approved user may see this page)."""
    msg = getattr(update, "message", None)
    if not msg:
        return
    uid = str(getattr(getattr(update, "effective_user", None), "id", ""))
    try:
        from gateway.cpanel import _is_admin
        is_admin = _is_admin(adapter, uid)
    except Exception:
        is_admin = False
    text = build_help_text(is_admin=is_admin)
    try:
        sender = getattr(adapter, "_send_control_message", None)
        thread_id = getattr(msg, "message_thread_id", None)
        if callable(sender):
            await sender(str(getattr(msg, "chat_id", "")), text, parse_mode=None,
                         thread_id=thread_id, metadata=None, reply_markup=None)
        else:
            await msg.reply_text(text)
    except Exception:
        try:
            await msg.reply_text(text)
        except Exception:
            pass
