"""Hermes Control Panel — AI-independent admin surface for Telegram.

Architecture: pure configuration/state/transport logic. This module must NEVER
import agent/LLM/inference modules (enforced by tests/gateway/test_cpanel.py).
Every screen is a pure function of local state; network calls happen ONLY in the
explicit, user-tapped connection test (bounded, single attempt, no retries).

Callback grammar: ``hctl:<screen>[:<op>[:<arg>]]`` — collision-free with all
existing adapter prefixes (mp/cp/ea/sc/cl/gt/mg/mm/mc/mb/mx/update_prompt).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PREFIX = "hctl:"
_MAX_TEXT = 3800  # keep screens comfortably under Telegram's 4096 limit
_TEST_TIMEOUT_S = 15.0
_STATE_FILE = "cpanel-last-test.json"

# ── i18n (lazy, zero hard dependency; never an AI path) ────────────────────────

def _T(key: str, default: str) -> str:
    try:
        from agent.i18n import t  # localization only — no LLM
        v = t(key)
        return v if isinstance(v, str) and v and v != key else default
    except Exception:
        return default

# ── pending text-input state (in-memory, mirrors _approval_state pattern) ─────

_PENDING: Dict[str, Dict[str, Any]] = {}          # chat_id -> flow state
_RECENT: Dict[str, float] = {}                    # anti double-tap (chat+data)->ts

def _pending_set(chat_id: str, flow: str, step: str, data: Dict[str, Any]) -> None:
    _PENDING[str(chat_id)] = {"flow": flow, "step": step, "data": data, "ts": time.time()}

def _pending_pop(chat_id: str) -> Optional[Dict[str, Any]]:
    return _PENDING.pop(str(chat_id), None)

def _pending_get(chat_id: str) -> Optional[Dict[str, Any]]:
    p = _PENDING.get(str(chat_id))
    if p and time.time() - p.get("ts", 0) > 900:
        _PENDING.pop(str(chat_id), None)
        return None
    return p

# ── home/config helpers ───────────────────────────────────────────────────────

def _home() -> Path:
    from gateway.run import _gateway_config_home  # native home resolution
    return _gateway_config_home()

def _env_path() -> Path:
    return _home() / ".env"

def _read_config() -> Dict[str, Any]:
    from gateway.run import _gateway_config_home
    from hermes_cli.config import read_user_config_raw
    return read_user_config_raw(_gateway_config_home() / "config.yaml")

def _write_config_key(adapter, key_path: str, value: Any) -> bool:
    save = getattr(adapter, "_save_gateway_config_key", None)
    if callable(save):
        return bool(save(key_path, value))
    try:
        from gateway.run import _gateway_config_home
        from gateway.slash_commands import _nested_dict
        from hermes_cli.config import read_user_config_raw, atomic_config_write
        path = _gateway_config_home() / "config.yaml"
        cfg = read_user_config_raw(path)
        *parents, leaf = key_path.split(".")
        _nested_dict(cfg, *parents)[leaf] = value
        atomic_config_write(path, cfg)
        return True
    except Exception as exc:  # pragma: no cover
        logger.error("cpanel config write failed %s: %s", key_path, exc)
        return False

def _read_env_pairs() -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    try:
        for raw in _env_path().read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            pairs.append((k.strip(), v.strip().strip('"').strip("'")))
    except Exception:
        pass
    return pairs

def _env_get(name: str) -> Optional[str]:
    for k, v in _read_env_pairs():
        if k == name:
            return v
    return None

def _custom_providers() -> Dict[str, Dict[str, Any]]:
    """config.yaml ``providers:`` map — the NATIVE custom-provider shape (v12).

    Never invents a parallel abstraction: these entries feed
    hermes_cli.config_providers.get_compatible_custom_providers() at runtime."""
    cfg = _read_config()
    raw = cfg.get("providers")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, entry in raw.items():
        if isinstance(entry, dict):
            e = dict(entry)
            e["_key"] = str(key)
            e["enabled"] = entry.get("enabled", True) is not False
            out[str(key)] = e
    return out


def _custom_id_ok(pid: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", pid or ""))


def _custom_url_ok(url: str) -> Tuple[bool, str]:
    u = (url or "").strip()
    if not u or any(ch.isspace() for ch in u):
        return False, "empty/whitespace"
    m = re.match(r"^(https?)://([^@\s/]+(?:/[^\s]*)?)$", u)
    if not m or "@" in u:
        return False, "must be https://host[/path] without userinfo"
    scheme, rest = m.group(1), m.group(2)
    host = rest.split("/")[0].split(":")[0].lower()
    if scheme == "http" and host not in ("localhost", "127.0.0.1", "::1") and not host.endswith(".local"):
        return False, "http only allowed for localhost"
    return True, host


def _custom_env_var(pid: str) -> str:
    return "CUSTOM_" + pid.upper().replace("-", "_")[:16] + "_API_KEY"


def _custom_write(adapter, pid: str, fields: Dict[str, Any]) -> None:
    """Write the native providers.<pid> mapping (dotted leaves; id slug forbids dots)."""
    for leaf, value in fields.items():
        _write_config_key(adapter, f"providers.{pid}.{leaf}", value)


def _custom_delete(adapter, pid: str) -> None:
    cfg = _read_config()
    prov = dict(cfg.get("providers") or {})
    prov.pop(pid, None)
    leaf_ok = _write_config_key(adapter, "providers", prov)


def _custom_active_route() -> str:
    cfg = _read_config()
    return str(((cfg.get("model") or {}).get("provider")) or "")


def _first_set_env(env_vars: Tuple[str, ...]) -> Tuple[str, Optional[str]]:
    """Return (var_name, value) of the first credential var that is actually set.

    Falls back to (env_vars[0], None) when none is set, so callers still know
    the canonical write target."""
    vars_ = tuple(env_vars or ())
    for v in vars_:
        val = _env_get(v)
        if val:
            return v, val
    return (vars_[0] if vars_ else "", None)


def _env_write(name: str, value: Optional[str]) -> bool:
    """Set or remove (value=None) a VAR= line in ~/.hermes/.env, atomic, 600."""
    path = _env_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines: List[str] = []
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.error("cpanel env read failed: %s", exc)
        return False
    out: List[str] = []
    done = False
    for raw in lines:
        if raw.strip().startswith(f"{name}=") or raw.strip() == name:
            if value is not None:
                out.append(f"{name}={value}")
            done = True
            continue
        out.append(raw)
    if value is not None and not done:
        out.append(f"{name}={value}")
    try:
        fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.chmod(path, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        return True
    except Exception as exc:
        logger.error("cpanel env write failed: %s", exc)
        return False

# ── persistent control state (per-chat attach markers; never stores secrets) ──

def _ui_state_path() -> Path:
    return _home() / "state" / "cpanel-ui.json"


def _ui_state_read() -> Dict[str, Any]:
    try:
        return json.loads(_ui_state_path().read_text() or "{}")
    except Exception:
        return {}


def _ui_state_write(data: Dict[str, Any]) -> None:
    p = _ui_state_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="cpanel-ui.", dir=str(p.parent))
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    with suppress_exc():
        os.chmod(p, 0o600)


def suppress_exc():
    import contextlib
    return contextlib.suppress(Exception)


def _persist_mark(chat_id: str) -> None:
    st = _ui_state_read()
    st.setdefault("persist", {})[str(chat_id)] = time.time()
    _ui_state_write(st)


def _persist_unmark(chat_id: str) -> bool:
    st = _ui_state_read()
    if str(chat_id) in st.get("persist", {}):
        st["persist"].pop(str(chat_id), None)
        _ui_state_write(st)
        return True
    return False


def _persist_ts(chat_id: str) -> float:
    try:
        return float(_ui_state_read().get("persist", {}).get(str(chat_id), 0) or 0)
    except Exception:
        return 0.0


def _persist_label() -> str:
    return _T("cpanel.persist.button", "🎛 Control Panel")


def _persist_labels_all() -> List[str]:
    """Every localized form of the persistent button label — tapping must work after a
    language switch too. Hard-configured locales only (en + fa ship with the panel)."""
    out = {_T("cpanel.persist.button", "🎛 Control Panel"), "🎛 Control Panel", "🎛 پنل مدیریت"}
    return [x for x in out if x.strip()]


def _persist_keyboard(adapter):
    """Persistent ReplyKeyboard with one localized button. UI sugar ONLY — never auth."""
    try:
        from telegram import ReplyKeyboardMarkup, KeyboardButton
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton(_persist_label())]], resize_keyboard=True, is_persistent=True)


def _persist_remove(adapter):
    try:
        from telegram import ReplyKeyboardRemove
    except Exception:
        return None
    return ReplyKeyboardRemove()


async def _persist_attach(adapter, chat_id: str, thread_id=None) -> None:
    """(Re)attach the persistent button with a minimal note; idempotent via marker TTL."""
    note = _T("cpanel.persist.ready", "🎛 Quick access pinned below — tap it anytime.")
    try:
        sender = getattr(adapter, "_send_control_message", None)
        if callable(sender):
            await sender(str(chat_id), note, parse_mode=None, thread_id=thread_id,
                         metadata=None, reply_markup=_persist_keyboard(adapter))
            _persist_mark(str(chat_id))
    except Exception as exc:
        logger.warning("cpanel persist attach failed: %s", scrub_text(str(exc)))


async def _persist_detach(adapter, chat_id: str, thread_id=None) -> None:
    note = _T("cpanel.persist.removed", "Quick access removed.")
    try:
        sender = getattr(adapter, "_send_control_message", None)
        if callable(sender):
            await sender(str(chat_id), note, parse_mode=None, thread_id=thread_id,
                         metadata=None, reply_markup=_persist_remove(adapter))
    except Exception as exc:
        logger.warning("cpanel persist detach failed: %s", scrub_text(str(exc)))


# ── credential masking (never render full secrets) ────────────────────────────

_SCRUB_RES = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{12,}"),
    re.compile(r"\bAQ\.[A-Za-z0-9_-]{6,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)=)[^\s,;]{4,}"),
]

def mask_secret(value: Optional[str]) -> str:
    if not value:
        return "—"
    v = str(value)
    if len(v) <= 7:
        return "•" * len(v)
    return f"{v[:4]}••••{v[-3:]}"

def scrub_text(text: str) -> str:
    out = str(text)
    for rx in _SCRUB_RES:
        if rx.pattern.startswith("(?i)(bearer"):
            out = rx.sub(lambda m: m.group(1) + "[redacted]", out)
        elif rx.pattern.startswith("(?i)((?:api"):
            out = rx.sub(lambda m: m.group(1) + "[redacted]", out)
        else:
            out = rx.sub("[redacted]", out)
    return out

# ── provider reality (native registry, never invented) ────────────────────────

def _profiles() -> Dict[str, Any]:
    from providers import list_providers
    out: Dict[str, Any] = {}
    for p in list_providers():
        name = getattr(p, "name", "") or ""
        if name:
            out[name] = p
    return out

# ── runtime coupling: session model pins (/model) vs global default (panel) ─────────────
#
# Source of truth map (see README "Operator surface"):
#   global default  = config.yaml model.default + model.provider  (Admin Control Panel)
#   per-session pin = gateway session store ``model_override``    (Telegram /model)
# Precedence per turn (gateway/run_turn.py): session pin > channel override > global default.
# The panel must (a) persist the global default, and (b) apply it to the admin's CURRENT chat
# by clearing that chat's pin when it would shadow the new default — otherwise the panel look
# "UI-only" for exactly the admin using it (observed regression; gateway_routing model_override).

def _gateway_runner(adapter):
    return getattr(adapter, "gateway_runner", None)


def _current_chat_session(adapter, query=None, chat_id: Optional[str] = None,
                          chat_type: Optional[str] = None, thread_id: Optional[Any] = None,
                          user_id: Optional[str] = None) -> Tuple[Optional[str], Any, Any]:
    """(session_key, session_store, runner) for the chat the callback happened in.

    Best-effort: returns (None, None, None) when the gateway runner is not attached (unit tests,
    CLI surfaces) — callers must degrade to plain config writes in that case.
    """
    runner = _gateway_runner(adapter)
    if runner is None:
        return None, None, None
    try:
        from gateway.session import SessionSource
        from gateway.config import Platform
        msg = getattr(query, "message", None) if query is not None else None
        cid = chat_id or str(getattr(msg, "chat_id", "") or "")
        if not cid:
            return None, None, None
        src_chat_type = chat_type or str(getattr(getattr(msg, "chat", None), "type", "") or "dm")
        src = SessionSource(
            platform=Platform.TELEGRAM, chat_id=cid,
            chat_type=("dm" if src_chat_type in ("private", "dm") else "group"),
            user_id=user_id or str(getattr(getattr(query, "from_user", None), "id", "") or "") or None,
            thread_id=str(thread_id if thread_id is not None else getattr(msg, "message_thread_id", "") or "") or None,
        )
        normalize = getattr(runner, "_normalize_source_for_session_key", None)
        if callable(normalize):
            src = normalize(src)
        key_fn = getattr(runner, "_session_key_for_source", None)
        session_key = key_fn(src) if callable(key_fn) else None
        store = getattr(runner, "session_store", None)
        return session_key, store, runner
    except Exception:
        logger.debug("cpanel: could not resolve current chat session", exc_info=True)
        return None, None, None


def _session_model_pin(session_store, session_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """Persisted /model override for *session_key* (non-secret fields only) or None."""
    if not session_store or not session_key:
        return None
    try:
        ov = session_store.get_model_override(session_key)
        if isinstance(ov, dict) and ov.get("model"):
            return ov
    except Exception:
        logger.debug("cpanel: model pin read failed", exc_info=True)
    return None


def _clear_session_pin_and_evict(session_store, runner, session_key: Optional[str]) -> bool:
    """Clear the /model pin for one session at ALL layers and evict the cached AIAgent so the
    next turn re-resolves from the global default:

      1. the persisted store row (``gateway_routing.model_override``; survives restarts),
      2. the running process's hydrated in-memory pin (``SessionState.conversation.model_override``
         — once hydrated, the per-turn resolver prefers memory and would otherwise shadow the new
         default for the rest of the process lifetime),
      3. the legacy runner override dict (historical compatibility), then
      4. the cached AIAgent (its config signature includes the model; a stale instance would
         keep serving the old model).

    Returns True only when the persisted pin was cleared (store is authoritative for restart
    persistence). In-memory and eviction failures are logged but never mask the truth.
    """
    if not session_store or not session_key:
        return False
    try:
        session_store.set_model_override(session_key, None)
    except Exception:
        logger.error("cpanel: failed to clear /model pin for session=%s", session_key, exc_info=True)
        return False
    if runner is not None:
        try:
            state = runner._peek_session_state(session_key)
            conv = getattr(state, "conversation", None) if state is not None else None
            if conv is not None and getattr(conv, "model_override", None) is not None:
                conv.model_override = None
        except Exception:
            logger.debug("cpanel: in-memory pin clear failed (persisted pin already gone)", exc_info=True)
        try:
            legacy = getattr(runner, "_session_model_overrides", None)
            if isinstance(legacy, dict):
                legacy.pop(session_key, None)
        except Exception:
            logger.debug("cpanel: legacy override map cleanup failed", exc_info=True)
        evict = getattr(runner, "_evict_cached_agent", None)
        if callable(evict):
            try:
                evict(session_key)
            except Exception:
                logger.debug("cpanel: agent eviction failed (pin cleared anyway)", exc_info=True)
    return True

def _disabled_set(cfg: Dict[str, Any]) -> set:
    """Providers disabled from the panel. UNION of the native runtime gate
    (``providers.<name>.enabled: false`` — honored by hermes_cli.runtime_provider) and the
    legacy panel-only list (``model.disabled_providers``) for backward compatibility. The panel
    writes the native form; the legacy key is migrated out on any write."""
    out = {str(x).lower() for x in ((cfg.get("model") or {}).get("disabled_providers") or []) if x}
    provs = cfg.get("providers")
    if isinstance(provs, dict):
        for name, block in provs.items():
            if isinstance(block, dict) and block.get("enabled") is False:
                out.add(str(name).lower())
    return out

def _provider_rows() -> List[Dict[str, Any]]:
    cfg = _read_config()
    model = cfg.get("model") or {}
    active_provider = str(model.get("provider") or "")
    active_model = str(model.get("default") or "")
    disabled = _disabled_set(cfg)
    rows = []
    for name, prof in sorted(_profiles().items()):
        env_vars = tuple(getattr(prof, "env_vars", ()) or ())
        key_var, key_val = _first_set_env(env_vars)
        rows.append({
            "name": name,
            "display": getattr(prof, "display_name", "") or name,
            "key_var": key_var,
            "key_masked": mask_secret(key_val),
            "has_key": bool(key_val),
            "enabled": name.lower() not in disabled,
            "is_default": name == active_provider,
            "auth_type": getattr(prof, "auth_type", "api_key"),
            "supports_health": bool(getattr(prof, "supports_health_check", True)),
        })
    return rows, active_provider, active_model

# ── keyboard builders (plain text screens; parse_mode=None everywhere) ────────

def _kb(adapter, rows: List[List[Tuple[str, str]]]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])

def _back_cancel(adapter, back: str = "hctl:main"):
    return _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), back)], [("✗ " + _T("cpanel.close", "Close"), "hctl:noop-close")]])

def screen_main(adapter):
    cfg = _read_config()
    model = cfg.get("model") or {}
    last = _last_test()
    prov_line = f"{model.get('provider') or '—'} / {model.get('default') or '—'}"
    ai_line = last.get("summary") or _T("cpanel.ai.unknown", "not tested yet (explicit tests only)")
    text = "\n".join([
        _T("cpanel.main.title", "🛡️ Hermes Control"),
        "─" * 26,
        f"{_T('cpanel.main.provider', 'Provider')}: {prov_line}",
        f"{_T('cpanel.main.ai', 'AI availability')}: {ai_line}",
        "",
        _T("cpanel.main.hint", "Choose a section. All operations are local — no AI call."),
    ])
    L1 = _T("cpanel.m.providers", "🔑 Providers"); L2 = _T("cpanel.m.models", "🤖 Models")
    L3 = _T("cpanel.m.test", "🧪 Test Connection"); L4 = _T("cpanel.m.status", "📊 System Status")
    L5 = _T("cpanel.m.users", "👤 Users"); L6 = _T("cpanel.m.settings", "⚙️ Settings")
    L7 = _T("cpanel.m.logs", "📋 Logs"); L8 = _T("cpanel.m.backup", "💾 Backup / Restore")
    rows = [[(L1, "hctl:prov")], [(L2, "hctl:models")], [(L3, "hctl:test")], [(L4, "hctl:status")],
            [(L5, "hctl:users")], [(L6, "hctl:settings")], [(L7, "hctl:logs")], [(L8, "hctl:backup")]]
    return text, _kb(adapter, rows)

# ── task-7 unified-provider helpers (presentation only; no runtime change) ───

_TASK_CAPS = frozenset({"browser_tasks", "async_runs", "status_polling"})

def _entry_caps(entry: Dict[str, Any]) -> List[str]:
    """Declared `provider_capabilities:` of a saved provider entry (may be [])."""
    try:
        from hermes_cli.config_providers import _norm_capabilities
        return _norm_capabilities((entry or {}).get("provider_capabilities"))
    except Exception:
        raw = (entry or {}).get("provider_capabilities")
        return [str(x).strip().lower() for x in raw] if isinstance(raw, list) else []


def _entry_effective_caps(entry: Dict[str, Any]) -> List[str]:
    """Explicit declaration wins; legacy entries report legacy inference defaults."""
    try:
        from hermes_cli.config_providers import derive_effective_capabilities
        return sorted(derive_effective_capabilities(entry or {}))
    except Exception:
        caps = _entry_caps(entry)
        return sorted(caps if caps else ["chat", "completion"])


def _entry_task_only(entry: Dict[str, Any]) -> bool:
    try:
        from hermes_cli.config_providers import INFERENCE_CAPABILITIES
        caps = _entry_caps(entry)
        return bool(caps) and frozenset(caps).isdisjoint(INFERENCE_CAPABILITIES)
    except Exception:
        caps = _entry_caps(entry)
        return bool(caps) and not (set(caps) & {"chat", "completion", "embeddings"})


def _entry_protocol_line(entry: Dict[str, Any]) -> str:
    runtime = (entry or {}).get("runtime")
    proto = str(runtime.get("protocol")) if isinstance(runtime, dict) and runtime.get("protocol") else ""
    disc = (entry or {}).get("discovery")
    dtype = str(disc.get("type")) if isinstance(disc, dict) and disc.get("type") else "models"
    if not proto:
        return "openai-compatible (chat/completions + models)"
    disc_line = {"models": "models", "none": "no discovery", "custom": "custom discovery"}.get(dtype, "models")
    return f"{proto} runtime · {disc_line}"


def _entry_auth_line(entry: Dict[str, Any]) -> str:
    auth = (entry or {}).get("auth")
    if isinstance(auth, dict) and auth.get("type"):
        atype = str(auth.get("type"))
        if atype == "api_key_header" and auth.get("header"):
            return f"{auth['header']} header"
        return atype
    return "bearer"


def _entry_discovery_supported(entry: Dict[str, Any]) -> bool:
    disc = (entry or {}).get("discovery")
    return not (isinstance(disc, dict) and str(disc.get("type")) == "none")


def _entry_has_cred(entry: Dict[str, Any]) -> bool:
    entry = entry or {}
    key_var = str(entry.get("key_env") or "")
    auth = entry.get("auth")
    if isinstance(auth, dict) and str(auth.get("type")) == "none":
        return True
    return bool((_env_get(key_var) if key_var else "") or entry.get("api_key"))


def _probe_note(kind: str, ok: bool) -> str:
    """Localized human explanation for a probe verdict (seconds lines of the 🧪 test)."""
    mapping = {
        "quota": _T("cpanel.test.quota", "Reason: provider quota/rate limit (e.g. Google RESOURCE_EXHAUSTED). This is NOT a Hermes failure."),
        "auth": _T("cpanel.test.auth", "Reason: authentication failed — check the stored key."),
        "not_found": _T("cpanel.test.404", "Reason: endpoint not found — check base URL / models path."),
        "timeout": _T("cpanel.test.timeout", "Reason: connection timeout."),
        "network": _T("cpanel.test.net", "Reason: network failure."),
        "malformed": _T("cpanel.test.malformed", "Reason: response did not satisfy the expected probe contract."),
        "no_key": _T("cpanel.test.nokey", "Reason: credential not configured."),
        "provider_error": _T("cpanel.test.perr", "Reason: provider-side error (5xx). Known transient capacity states are shown as-is."),
    }
    if ok:
        return _T("cpanel.test.oknote", "Provider API reachable. (Connectivity only — not an inference check.)")
    return mapping.get(kind, _T("cpanel.test.other", "See status code above."))


async def _probe_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Run the generic task-7 probe against a config entry (never raises)."""
    try:
        from gateway import provider_probe
        return await provider_probe.probe_provider_entry(dict(entry or {}))
    except Exception as exc:
        from gateway.provider_probe import NETWORK
        return {"verdict": NETWORK, "category": "network", "ok": False,
                "http_status": 0, "notes": [f"engine:{type(exc).__name__}"],
                "capabilities": _entry_effective_caps(entry), "task_only": _entry_task_only(entry),
                "model_ids": []}


def _wizard_probe_entry(d: Dict[str, Any]) -> Dict[str, Any]:
    """Translate wizard session state into a probe-shaped config entry."""
    auth_type = str(d.get("auth") or "bearer")
    auth: Dict[str, Any] = {"type": auth_type}
    if auth_type == "api_key_header" and d.get("auth_header"):
        auth["header"] = str(d["auth_header"])
    entry = {
        "name": str(d.get("id") or "wizard"),
        "base_url": str(d.get("base_url") or "").rstrip("/"),
        "api_key": str(d.get("key") or ""),
        "auth": auth,
    }
    for block in ("probe", "discovery", "runtime", "provider_capabilities"):
        if d.get(block):
            entry[block] = d[block]
    return entry


def _pending_nearby_declared_nodiscovery(d: Dict[str, Any]) -> bool:
    dblock = d.get("discovery")
    return isinstance(dblock, dict) and str(dblock.get("type")) == "none"


# filter state for the compact providers list (presentation only)
_PROV_LIST_FILTER: Dict[str, str] = {}


def _prov_filter(adapter) -> str:
    aid = str(id(adapter))
    return _PROV_LIST_FILTER.get(aid, "all")


def _prov_filter_set(adapter, mode: str) -> None:
    _PROV_LIST_FILTER[str(id(adapter))] = mode


def _custom_row_visible(entry: Dict[str, Any], mode: str) -> bool:
    if mode == "task":
        return _entry_task_only(entry)
    if mode == "ready":
        return bool(entry.get("enabled", True)) and _entry_has_cred(entry)
    return True


def screen_providers(adapter):
    rows, active_p, active_m = _provider_rows()
    lines = [_T("cpanel.prov.title", "🔑 Provider Management"), "─" * 26]
    kb_rows = []
    for r in rows:
        state = "🟢" if r["enabled"] else "⚪"
        star = "⭐" if r["is_default"] else ""
        key = "🔑" if r["has_key"] else "—"
        lines.append(f"{state}{star} {r['name']} · key:{key} {r['key_masked']}")
        kb_rows.append([(r["name"], f"hctl:prov:sel:{r['name']}")])
    customs = _custom_providers()
    lst_filter = _prov_filter(adapter)
    if customs:
        lines.append("")
        lines.append(_T("cpanel.cust.section", "🧩 Custom providers:"))
    active_route = _custom_active_route()
    for pid, e in sorted(customs.items()):
        if not _custom_row_visible(e, lst_filter):
            continue
        state = "🟢" if e.get("enabled") else "⚪"
        star = "⭐" if active_route == f"custom:{pid}" else ""
        proto_btn = _T("cpanel.cust.tasktag", "· task-only") if _entry_task_only(e) else ""
        has_key = _entry_has_cred(e)
        lines.append(f"{state}{star} 🧩 {pid}{proto_btn} · key:{'🔑' if has_key else '—'}")
        kb_rows.append([(f"🧩 {pid}{proto_btn.replace('·', '•')}", f"hctl:cust:sel:{pid}")])
    lines += ["", _T("cpanel.prov.legend", "🟢 enabled · ⚪ disabled · ⭐ default · tap a provider for actions")]
    # presentation-level filter chips (no behavior change — only rows shown)
    def _chip(mode: str, label: str) -> Tuple[str, str]:
        return (("✓ " if lst_filter == mode else "") + label, f"hctl:cust:listf:{mode}")
    kb_rows.append([
        _chip("all", _T("cpanel.cust.filt.all", "All")),
        _chip("ready", _T("cpanel.cust.filt.ready", "Ready")),
        _chip("task", _T("cpanel.cust.filt.task", "Task-only")),
    ])
    kb_rows.append([("➕ " + _T("cpanel.prov.add", "Add Provider"), "hctl:add:start")])
    kb = _kb(adapter, kb_rows + [[("◀️ " + _T("cpanel.back", "Back"), "hctl:main")]])
    return "\n".join(lines)[:_MAX_TEXT], kb

def screen_provider_detail(adapter, name: str):
    rows, _, _ = _provider_rows()
    r = next((x for x in rows if x["name"] == name), None)
    if not r:
        return _T("cpanel.prov.gone", "Provider not found."), _back_cancel(adapter, "hctl:prov")
    lines = [
        f"🔑 {r['display']} ({name})", "─" * 26,
        f"{_T('cpanel.prov.status', 'Status')}: {'🟢 enabled' if r['enabled'] else '⚪ disabled'}",
        f"{_T('cpanel.prov.default', 'Default')}: {'⭐ yes' if r['is_default'] else 'no'}",
        f"{_T('cpanel.prov.auth', 'Auth')}: {r['auth_type']}",
        f"{_T('cpanel.prov.key', 'API key')}: {r['key_masked']}  ({r['key_var'] or '—'})",
    ]
    tog = "⚪ Disable" if r["enabled"] else "🟢 Enable"
    tog_op = "off" if r["enabled"] else "on"
    kb = _kb(adapter, [
        [(f"✏️ {_T('cpanel.prov.editkey','Rotate key')}", f"hctl:add:keyonly:{name}")],
        [(tog, f"hctl:prov:toggle:{name}:{tog_op}")],
        [(f"⭐ {_T('cpanel.prov.setdefault','Set default')}", f"hctl:prov:mkdefault:{name}")],
        [(f"🧪 {_T('cpanel.prov.test','Test')}", f"hctl:test:go:{name}")],
        [(f"🗑 {_T('cpanel.prov.delkey','Delete key')}", f"hctl:prov:delask:{name}")],
        [("◀️ " + _T("cpanel.back", "Back"), "hctl:prov")],
    ])
    return "\n".join(lines), kb

def screen_models(adapter, pin: Optional[Dict[str, Any]] = None):
    cfg = _read_config()
    model = cfg.get("model") or {}
    active_m = str(model.get("default") or "")
    pname = str(model.get("provider") or "")
    candidates = _model_candidates(pname)
    lines = [ _T("cpanel.models.title", "🤖 Model Management"), "─" * 26,
              f"{_T('cpanel.models.provider', 'Provider')}: {pname or '—'}",
              f"{_T('cpanel.models.global', 'Global default')}: {active_m or '—'}", "" ]
    pin_model = str((pin or {}).get("model") or "")
    if pin_model:
        lines.append(_T("cpanel.models.pin", "📌 This chat is pinned via /model to: ") + pin_model)
        lines.append(_T("cpanel.models.pineff", "   → overrides the global default in THIS chat only."))
    else:
        lines.append(_T("cpanel.models.nopin", "Effective next message in this chat: global default."))
    kb_rows = []
    if pin_model:
        kb_rows.append([("🧹 " + _T("cpanel.models.cleapin", "Clear this chat's /model pin"), "hctl:models:clearpin")])
    if candidates:
        lines.append(_T("cpanel.models.pick", "Tap to set as global default (persisted; applied to this chat; no AI call):"))
        for m in candidates[:12]:
            mark = "✓ " if m == active_m else ""
            kb_rows.append([(f"{mark}{m}"[:60], f"hctl:models:set:{m[:40]}")])
    else:
        lines.append(_T("cpanel.models.none", "No model catalog cached for this provider. Run 🧪 Test (lists models on success) or type a model id below."))
    kb_rows.append([(f"✏️ {_T('cpanel.models.type','Type model id')}", "hctl:models:type")])
    kb_rows.append([(f"🧪 {_T('cpanel.models.testsel','Test current selection')}", "hctl:test")])
    kb_rows.append([("◀️ " + _T("cpanel.back", "Back"), "hctl:main")])
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, kb_rows)

def _model_candidates(provider_name: str) -> List[str]:
    try:
        cache = json.loads((_home() / "provider_models_cache.json").read_text())
        items = cache.get(provider_name) or cache.get(provider_name.lower()) or []
        if isinstance(items, dict):
            items = list(items.keys())
        out = [str(x) for x in items if isinstance(x, (str,))]
        if out:
            return out[:40]
    except Exception:
        pass
    try:
        prof = _profiles().get(provider_name)
        fb = list(getattr(prof, "fallback_models", ()) or ())
        return [str(x) for x in fb][:40]
    except Exception:
        return []

def screen_test(adapter, name: Optional[str] = None):
    rows, active_p, active_m = _provider_rows()
    sel = name or active_p
    lines = [_T("cpanel.test.title", "🧪 Connection Test"), "─" * 26,
             _T("cpanel.test.desc", "Explicit provider/API connectivity check. Single attempt, no auto-retry. Never faked."), ""]
    kb_rows = []
    for r in rows:
        if r["name"] == sel:
            kb_rows.append([(f"▶ {r['name']}", "hctl:noop")])
        else:
            kb_rows.append([(r["name"], f"hctl:test:sel:{r['name']}")])
    kb_rows.append([(f"🧪 {_T('cpanel.test.run','Run test')}", f"hctl:test:go:{sel}" if sel else "hctl:noop")])
    kb_rows.append([("◀️ " + _T("cpanel.back", "Back"), "hctl:main")])
    if sel:
        lines.append(f"{_T('cpanel.test.selected','Selected')}: {sel}")
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, kb_rows)

def screen_status(adapter):
    home = _home()
    cfg = _read_config()
    model = cfg.get("model") or {}
    pid = None
    try:
        pid = int((home / "gateway.pid").read_text().strip().split()[0])
    except Exception:
        pass
    alive = False
    if pid:
        try:
            os.kill(pid, 0); alive = True
        except Exception:
            alive = False
    life = {}
    try:
        life = json.loads((home / "state/gateway.lifecycle.json").read_text())
    except Exception:
        pass
    hb_age = None
    try:
        hb_age = int(time.time() - os.path.getmtime(home / "state/gateway.heartbeat"))
    except Exception:
        pass
    db = home / "state.db"
    db_ok = db.exists() and db.stat().st_size > 0
    cfg_ok = bool(cfg)
    tg = "🟢" if getattr(adapter, "_bot", None) is not None else "🔴"
    last = _last_test()
    ver = "?"
    try:
        import importlib.metadata as _md
        ver = _md.version("hermes-agent")
    except Exception:
        pass
    lines = [
        _T("cpanel.status.title", "📊 System Status"), "─" * 26,
        f"Hermes Gateway: {'🟢 running pid ' + str(pid) if alive else '🔴 stopped'}",
        f"Lifecycle: {life.get('phase', '—')}" + (f" (heartbeat {hb_age}s ago)" if hb_age is not None else ""),
        f"Telegram: {tg}",
        f"Configuration: {'🟢' if cfg_ok else '🔴'}",
        f"State DB: {'🟢' if db_ok else '🔴'}",
        f"Active Provider/Model: {model.get('provider') or '—'} / {model.get('default') or '—'}",
        f"Provider Availability: {last.get('provider_icon', '—')} (last explicit test)",
        f"AI: {last.get('ai_icon', '—')} — {last.get('summary', 'not tested')}",
        f"Version: {ver}",
        "",
        _T("cpanel.status.note", "Infrastructure status is independent of AI provider health."),
    ]
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, [[("🔄 " + _T("cpanel.refresh", "Refresh"), "hctl:status")], [("◀️ " + _T("cpanel.back", "Back"), "hctl:main")]])

def screen_users(adapter):
    from gateway.pairing import PairingStore
    store = PairingStore()
    pend = store.list_pending("telegram")
    appr = store.list_approved("telegram")
    lines = [_T("cpanel.users.title", "👤 User Management"), "─" * 26,
             _T("cpanel.users.pending", "Pending requests") + f": {len(pend)}"]
    kb_rows = []
    for p in pend[:6]:
        label = f"{p.get('user_name') or p.get('user_id')} ({p.get('age_minutes', 0)}m)"
        rid = p.get("request_id") or ""
        lines.append(f"· {label}")
        if rid:
            kb_rows.append([(f"✅ {_T('cpanel.users.approve','Approve')} {label[:24]}", f"hctl:users:ok:{rid}")])
    lines.append("")
    lines.append(_T("cpanel.users.approved", "Approved users") + f": {len(appr)}")
    for a in appr[:6]:
        label = f"{a.get('user_name') or ''} [{a.get('user_id')}]"
        lines.append(f"· {label}")
        kb_rows.append([(f"🚫 {_T('cpanel.users.revoke','Revoke')} {label[:24]}", f"hctl:users:rv:{a.get('user_id')}")])
    lines.append("")
    lines.append(_T("cpanel.users.note", "Codes are never displayed. Rejected/ignored requests expire automatically."))
    kb_rows.append([("◀️ " + _T("cpanel.back", "Back"), "hctl:main")])
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, kb_rows)

def screen_settings(adapter):
    cfg = _read_config()
    disp = cfg.get("display") or {}
    comp = cfg.get("compression") or {}
    lang = str(disp.get("language") or "en")
    ce = comp.get("enabled", True)
    lines = [_T("cpanel.settings.title", "⚙️ Hermes Settings"), "─" * 26,
             f"{_T('cpanel.settings.lang', 'Language (display.language)')}: {lang}",
             f"{_T('cpanel.settings.comp', 'Context compression (compression.enabled)')}: {'🟢 on' if ce else '⚪ off'}",
             "",
             _T("cpanel.settings.note", "Only settings Hermes already supports are editable here.")]
    kb = _kb(adapter, [
        [("☁️ " + _T("cpanel.m.pstorage", "Persistent Storage"), "hctl:pstorage")],
        [("🇬🇧 English", "hctl:settings:lang:en"), ("🇮🇷 فارسی", "hctl:settings:lang:fa")],
        [("🟢 Compression on" if not ce else "⚪ Compression off", f"hctl:settings:comp:{'off' if ce else 'on'}")],
        [("◀️ " + _T("cpanel.back", "Back"), "hctl:main")],
    ])
    return "\n".join(lines)[:_MAX_TEXT], kb

def screen_logs(adapter):
    logf = _home() / "state/hermes-gateway.log"
    lines = [_T("cpanel.logs.title", "📋 Logs (sanitized)"), "─" * 26]
    try:
        size = logf.stat().st_size
        data = logf.read_bytes()[-6000:].decode("utf-8", "replace")
        tail = [l for l in data.splitlines() if l.strip()][-18:]
        lines.append(f"(last {len(tail)} of {size} bytes)")
        lines += [scrub_text(l)[:180] for l in tail]
    except Exception as exc:
        lines.append(_T("cpanel.logs.unavail", "log unavailable: ") + str(exc))
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, [[("🔄 " + _T("cpanel.refresh", "Refresh"), "hctl:logs")], [("◀️ " + _T("cpanel.back", "Back"), "hctl:main")]])

def screen_backup(adapter):
    bdir = _home() / "backups"
    snaps = []
    try:
        snaps = sorted((p for p in bdir.glob("cpanel-*.tar.gz")), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        pass
    lines = [_T("cpanel.backup.title", "💾 Backup / Restore"), "─" * 26,
             _T("cpanel.backup.covers", "Covers: config.yaml, auth.json (600), .env (600) — stored locally, never uploaded."), ""]
    kb_rows = [[("💾 " + _T("cpanel.backup.create", "Create backup now"), "hctl:backup:go")]]
    if snaps:
        lines.append(_T("cpanel.backup.recent", "Recent snapshots:"))
        for s in snaps[:5]:
            lines.append(f"· {s.name} ({s.stat().st_size} bytes)")
            kb_rows.append([(f"♻️ {_T('cpanel.backup.restore','Restore')} {s.name[:28]}", f"hctl:backup:ask:{s.name}")])
    kb_rows.append([("◀️ " + _T("cpanel.back", "Back"), "hctl:main")])
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, kb_rows)

def screen_confirm(adapter, what: str, yes_data: str, back: str):
    lines = [ _T("cpanel.confirm.title", "⚠️ Confirm"), "─" * 26, what,
              "", _T("cpanel.confirm.note", "This action is explicit and deliberate.") ]
    kb = _kb(adapter, [[("✅ " + _T("cpanel.confirm.yes", "Confirm"), yes_data)],
                       [("❌ " + _T("cpanel.confirm.no", "Cancel"), back)]])
    return "\n".join(lines), kb

# ── connection test (explicit tap only; single attempt; honest mapping) ───────

def _last_test() -> Dict[str, Any]:
    try:
        return json.loads((_home() / "state" / _STATE_FILE).read_text())
    except Exception:
        return {}

def _save_test(res: Dict[str, Any]) -> None:
    try:
        st_dir = _home() / "state"; st_dir.mkdir(parents=True, exist_ok=True)
        (st_dir / _STATE_FILE).write_text(json.dumps(res))
    except Exception:
        pass

def run_connection_test(provider_name: str) -> Dict[str, Any]:
    """Explicit connectivity probe for ONE provider. No inference. No retries."""
    ts = time.time()
    if provider_name.startswith("custom:"):
        pid = provider_name.split(":", 1)[1]
        entry = _custom_providers().get(pid)
        if not entry:
            return {"ok": False, "kind": "unknown_provider", "provider": provider_name, "ts": ts}
        key_var_used = "key"
        key = _env_get((entry.get("key_env") or "")) if entry.get("key_env") else (entry.get("api_key") or None)
        base_url = str(entry.get("api") or entry.get("base_url") or "").rstrip("/")
        models_url = str(entry.get("models_url") or "") or (base_url + "/models" if base_url else "")
        pname = "custom:" + pid
        auth_type = "api_key" if entry.get("key_env") or entry.get("api_key") else "none"
        ename = pid
    else:
        prof = _profiles().get(provider_name)
        if not prof:
            return {"ok": False, "kind": "unknown_provider", "provider": provider_name, "ts": ts}
        env_vars = tuple(getattr(prof, "env_vars", ()) or ())
        key_var_used, key = _first_set_env(env_vars)
        base_url = (getattr(prof, "base_url", "") or "").rstrip("/")
        models_url = getattr(prof, "models_url", "") or (base_url + "/models" if base_url else "")
        pname = provider_name.lower()
        auth_type = str(getattr(prof, "auth_type", "api_key"))
        ename = provider_name
    if not base_url and not models_url:
        res = {"ok": None, "kind": "unsupported", "reason": "no HTTP models endpoint for this provider", "provider": ename, "ts": ts}
        _save_test(_decorate(res)); return res
    host = re.sub(r"^(https?://[^/]+).*$", r"\1", models_url or base_url)
    if auth_type == "api_key" and not key:
        res = {"ok": False, "kind": "no_key", "reason": "credential not configured", "provider": ename, "ts": ts}
        _save_test(_decorate(res)); return res
    headers: Dict[str, str] = {}
    url = models_url
    if key and ("google" in pname or "gemini" in pname):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={key}"
    elif key and auth_type == "api_key":
        headers["Authorization"] = f"Bearer {key}"
    headers.setdefault("User-Agent", "hermes-cpanel/1.0")
    try:
        import httpx
        with httpx.Client(timeout=_TEST_TIMEOUT_S, follow_redirects=False) as cli:
            resp = cli.get(url, headers=headers)
        code = resp.status_code
        if 200 <= code < 300:
            res = {"ok": True, "kind": "success", "http": code, "provider": provider_name, "host": host, "ts": ts}
            try:
                payload = resp.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, list):
                    res["count"] = len(data)
            except Exception:
                pass
        elif code in (401, 403):
            res = {"ok": False, "kind": "auth", "http": code, "provider": provider_name, "host": host, "ts": ts}
        elif code == 404:
            res = {"ok": False, "kind": "not_found", "http": code, "provider": provider_name, "host": host, "ts": ts, "detail": "endpoint mismatch (check models URL / base path)"}
        elif code == 429:
            res = {"ok": False, "kind": "quota", "http": code, "provider": provider_name, "host": host, "ts": ts, "detail": "RESOURCE_EXHAUSTED / rate limit"}
        elif 500 <= code < 600:
            res = {"ok": False, "kind": "provider_error", "http": code, "provider": provider_name, "host": host, "ts": ts}
        else:
            res = {"ok": False, "kind": "other", "http": code, "provider": provider_name, "host": host, "ts": ts}
    except Exception as exc:
        kind_exc = "timeout" if "Timeout" in type(exc).__name__ or "timeout" in type(exc).__name__.lower() else "network"
        res = {"ok": False, "kind": kind_exc, "provider": provider_name, "host": host, "ts": ts,
               "detail": scrub_text(type(exc).__name__)}
    decorated = _decorate(res)
    _save_test(decorated)
    return decorated

def discover_models(base_url: str, models_url: str, key: Optional[str], timeout_s: float = _TEST_TIMEOUT_S) -> Tuple[Optional[List[str]], str]:
    """GET the models endpoint, parse the OpenAI-style catalog. Returns (ids|None, kind).

    kinds: success | auth | quota | not_found | timeout | network | malformed | no_key
    Never logs the key; only the endpoint URL is touched — no website scraping, ever."""
    base_url = (base_url or "").strip().rstrip("/")
    url = (models_url or "").strip() or (base_url + "/models" if base_url else "")
    if not url:
        return None, "unsupported"
    headers = {"User-Agent": "hermes-cpanel/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        import httpx
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as cli:
            resp = cli.get(url, headers=headers)
        code = resp.status_code
        if code in (401, 403):
            return None, "auth"
        if code == 404:
            return None, "not_found"
        if code == 429:
            return None, "quota"
        if code >= 500:
            return None, "provider_error"
        if not (200 <= code < 300):
            return None, "other"
        try:
            data = resp.json()
        except Exception:
            return None, "malformed"
        ids: List[str] = []
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            for item in data["data"]:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
                elif isinstance(item, str):
                    ids.append(item)
        elif isinstance(data, list):
            ids = [str(x.get("id")) for x in data if isinstance(x, dict) and x.get("id")] or [str(x) for x in data if isinstance(x, str)]
        if not ids:
            return None, "malformed"
        return sorted(set(ids))[:100], "success"
    except Exception as exc:
        kind_exc = "timeout" if "timeout" in type(exc).__name__.lower() else "network"
        logger.info("cpanel discover failed: %s", type(exc).__name__)
        return None, kind_exc


def _decorate(res: Dict[str, Any]) -> Dict[str, Any]:
    kind = res.get("kind")
    prov = res.get("provider", "?")
    if res.get("ok") is True:
        icon, summary = "🟢", f"{prov}: reachable (HTTP {res.get('http')})"
    elif kind == "auth":
        icon, summary = "🔴", f"{prov}: authentication failed (HTTP {res.get('http')})"
    elif kind == "quota":
        icon, summary = "🔴", f"{prov}: quota/rate limit (HTTP 429)"
    elif kind == "timeout":
        icon, summary = "🔴", f"{prov}: connection timeout"
    elif kind == "network":
        icon, summary = "🔴", f"{prov}: network failure ({res.get('detail', '')})".strip()
    elif kind == "no_key":
        icon, summary = "🔴", f"{prov}: no credential configured"
    elif kind == "unsupported":
        icon, summary = "—", f"{prov}: connectivity test unsupported for this provider type"
    elif kind == "not_found":
        icon, summary = "🔴", f"{prov}: endpoint not found (HTTP 404)"
    elif kind == "malformed":
        icon, summary = "🔴", f"{prov}: malformed response (not an OpenAI-style JSON catalog)"
    elif kind == "provider_error":
        icon, summary = "🔴", f"{prov}: provider error (HTTP {res.get('http')})"
    else:
        icon, summary = "🔴", f"{prov}: unspecified failure"
    out = dict(res)
    out["provider_icon"] = icon
    out["ai_icon"] = icon
    out["summary"] = summary
    return out

# ── flows (add provider wizard) ───────────────────────────────────────────────

async def _run_task_for_entry(entry: Dict[str, Any], instruction: str) -> Dict[str, Any]:
    """One task-run via the task-capable runtime (gateway/task_runtime.py). The secret is resolved
    from the environment/confit at call time inside the runtime and is NEVER in the return value."""
    from gateway import task_runtime
    return await task_runtime.run_task(dict(entry), instruction)


def _format_task_result(pid: str, result: Dict[str, Any]) -> str:
    """Truth-first task result rendering: explicit status, evidence, never secrets."""
    status = str(result.get("status") or "?")
    icon = {"success": "✅", "failed": "❌", "cancelled": "⏹", "timeout": "⏱",
            "auth": "🔑", "quota": "💳", "validation": "⚠️", "provider_error": "🧯",
            "network": "📡", "malformed": "🧩", "no_key": "🚫", "not_capable": "🚫",
            "rejected": "🛡"}.get(status, "❓")
    lines = [f"{icon} {pid}: {_T('cpanel.task.status', 'status')}: {status}", "─" * 26]
    if result.get("http_status"):
        lines.append(f"HTTP {result['http_status']}")
    if result.get("run_id"):
        lines.append(f"run id: {result['run_id'][:60]}")
    if result.get("polls") is not None and status not in ("no_key", "not_capable", "rejected", "validation"):
        lines.append(f"polls: {result.get('polls', 0)} · {result.get('elapsed_s', 0)}s")
    if result.get("detail"):
        lines.append(f"detail: {result['detail']}")
    if result.get("output"):
        lines.append("")
        lines.append(str(result["output"]))
    return "\n".join(lines)[:_MAX_TEXT]


def screen_custom_detail(adapter, pid: str):
    e = _custom_providers().get(pid)
    if not e:
        return _T("cpanel.cust.gone", "Provider not found (maybe deleted)."), _back_cancel(adapter, "hctl:prov")
    url = str(e.get("api") or e.get("base_url") or "—")
    website = str(e.get("website") or "—")
    key_var = str(e.get("key_env") or "")
    key_val = _env_get(key_var) if key_var else (e.get("api_key") if e.get("api_key") else None)
    state = "🟢" if e.get("enabled") else "⚪"
    star = "⭐" if _custom_active_route() == f"custom:{pid}" else ""
    dmodel = str(e.get("default_model") or "—")
    task_only = _entry_task_only(e)
    discovery_supported = _entry_discovery_supported(e)
    caps_label = ", ".join(_entry_effective_caps(e)) or "—"
    if not discovery_supported:
        discovery_line = _T("cpanel.cust.discnone", "Model discovery: Not applicable (provider does not expose a model catalog)")
    elif _entry_caps(e):
        discovery_line = _T("cpanel.cust.discdecl", "Model discovery: configured via declaration")
    else:
        discovery_line = _T("cpanel.cust.disclegacy", "Model discovery: supported (OpenAI-style GET {base}/models)")
    lines = [f"🧩 {pid} {state}{star}" + (" · " + _T("cpanel.cust.taskbadge", "task-only") if task_only else ""), "─" * 26,
             f"{_T('cpanel.cust.baseurl', 'API base URL')}: {url}",
             f"{_T('cpanel.cust.website', 'Website (metadata only — never scraped)')}: {website}",
             f"{_T('cpanel.cust.key', 'API key')}: {mask_secret(key_val)}",
             f"{_T('cpanel.cust.dmodel', 'Default model')}: {dmodel}",
             "",
             f"{_T('cpanel.cust.protocol', 'Protocol')}: {_entry_protocol_line(e)}",
             f"{_T('cpanel.cust.authlbl', 'Auth')}: {_entry_auth_line(e)}",
             f"{_T('cpanel.cust.caps', 'Capabilities')}: {caps_label} "
             + (_T("cpanel.cust.capsexplicit", "(declared)") if _entry_caps(e)
                else _T("cpanel.cust.capslegacy", "(legacy default)")),
             discovery_line]
    if task_only:
        lines.append(_T("cpanel.cust.tasknote", "This provider runs tasks, not chat completions: it is not offered as a /model default."))
    cb = "hctl:cust:"
    first_row = [("🧪 " + _T("cpanel.test.go", "Test"), f"{cb}go:{pid}")]
    if discovery_supported and not task_only:
        first_row.append(("📋 " + _T("cpanel.cust.models", "Models"), f"{cb}models:{pid}"))
    rows = [first_row]
    if task_only:
        rows.insert(1, [("▶ " + _T("cpanel.cust.runtask", "Run task"), f"{cb}runtask:{pid}")])
    if not task_only:
        rows.append([("⭐ " + _T("cpanel.prov.setdefault", "Set Default"), f"{cb}setdef:{pid}")])
    rows.append([("🔄 " + _T("cpanel.cust.toggle", "Enable/Disable"), f"{cb}toggle:{pid}")])
    rows.append([("✏️ " + _T("cpanel.cust.edit", "Edit"), f"{cb}editask:{pid}"),
                 ("🗑 " + _T("cpanel.cust.delete", "Delete"), f"{cb}delask:{pid}")])
    rows.append([("◀️ " + _T("cpanel.back", "Back"), "hctl:prov")])
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, rows)


def _cwx_steps_text(step: str, d: Dict[str, Any]) -> str:
    prompts = {
        "id": _T("cpanel.cwx.id", "Send a short provider id (a-z, 0-9, dash). e.g. acme-llm"),
        "display": _T("cpanel.cwx.display", "Send a display name (shown in the panel)."),
        "website": _T("cpanel.cwx.website", "Send the provider WEBSITE url (metadata only; '-' to skip). API calls use the base URL, never the website."),
        "base": _T("cpanel.cwx.base", "Send the API BASE URL (e.g. https://api.provider.example/v1)."),
        "auth": _T("cpanel.cwx.auth", "Choose auth type:"),
        "key": _T("cpanel.cwx.key", "Send the API key now (stored locally 600, shown masked, your message is deleted). '-' for no key."),
        "basedit": _T("cpanel.cwx.base", "Send the new API BASE URL."),
        "webedit": _T("cpanel.cwx.website", "Send the new website url ('-' to clear)."),
        "keyedit": _T("cpanel.cwx.keyedit", "Send the new API key (rotated; old value is replaced)."),
    }
    return prompts.get(step, "")


def _cwx_summary(d: Dict[str, Any], extra: str = "") -> str:
    rows = [_T("cpanel.cwx.review", "Review — nothing sent to AI:"), "─" * 26,
            f"id: {d.get('id')}", f"name: {d.get('display')}",
            f"website: {d.get('website') or '—'}",
            f"base_url: {d.get('base_url')}",
            f"auth: {d.get('auth')}",
            f"key: {d.get('key_masked', '—')}",
            f"models: {', '.join((d.get('models') or [])[:4]) or '—'}",
            f"default_model: {d.get('default_model') or '—'}"]
    if extra:
        rows.insert(0, extra)
    return "\n".join(rows)


def _add_start(adapter):
    profs = list(_profiles().keys())
    rows = []
    labels = {"gemini": "Google Gemini", "google": "Google Gemini", "openai": "OpenAI", "anthropic": "Anthropic"}
    rows.append([("🧩 " + _T("cpanel.cust.addbtn", "Custom (OpenAI-compatible)"), "hctl:cust:new")])
    for name in profs:
        disp = labels.get(name, name)
        rows.append([(disp, f"hctl:add:pick:{name}")])
    kb_rows = rows + [[("◀️ " + _T("cpanel.back", "Back"), "hctl:prov")]]
    text = "\n".join([ _T("cpanel.add.title", "➕ Add Provider"), "─" * 26,
                       _T("cpanel.add.pick", "Choose provider type (only providers Hermes actually supports are listed):")])
    return text, _kb(adapter, kb_rows)

def _add_confirm_text(data: Dict[str, Any]) -> str:
    return "\n".join([
        _T("cpanel.add.confirm", "Provider configuration"), "─" * 26,
        f"Provider: {data.get('display') or data.get('name')}",
        f"Model: {data.get('model') or '—'}",
        f"API key: {data.get('key_masked', '—')}",
        (f"Base URL: {data.get('base_url')}" if data.get("base_url") else ""),
    ]).strip()

# ── patch-facing API ──────────────────────────────────────────────────────────

_UNAUTHORIZED = "⛔ You are not authorized to use this panel."

def _admin_ids_from_config() -> Optional[frozenset]:
    """``gateway.platforms.telegram.extra.allow_admin_from`` (DM admin list) from config.yaml.

    Same canonical key ``gateway.slash_access`` uses for slash-command admin gating, so the
    Control Panel and slash commands recognize exactly the same admins. Returns ``None`` when
    the key is absent (legacy fallback below applies); an *empty* configured list is a real
    configuration → fail-closed (nobody is admin via this source)."""
    try:
        cfg = _read_config()
        plat = (((cfg.get("gateway") or {}).get("platforms") or {}).get("telegram") or {})
        if not isinstance(plat, dict):
            return None
        extra = plat.get("extra") if isinstance(plat.get("extra"), dict) else {}
        raw = extra.get("allow_admin_from", plat.get("allow_admin_from"))  # bridged root key too
        if raw is None:
            return None
        from gateway.slash_access import _coerce_id_list  # native normalizer
        return _coerce_id_list(raw)
    except Exception:
        return None


def _is_admin(adapter, user_id: str) -> bool:
    """Admin resolution (Telegram DM), fail-closed.

    Order: (1) ``allow_admin_from`` when configured — the canonical admin list shared with
    ``gateway.slash_access``; (2) legacy fallback — the DM talk-allowlist env ids (NOTE:
    ``_configured_allowlist`` returns ``(env_var, ids)``; the previous code compared user ids
    against ``str()`` of the *tuple elements*, so with an env allowlist configured NOBODY was
    ever admin — the /panel + settings + model screens all answered ⛔); (3) pairing-approved
    users. Never weakens the pairing gate: unauthorized users are rejected earlier by the
    callback auth check."""
    try:
        admin_ids = _admin_ids_from_config()
    except Exception:
        admin_ids = None
    if admin_ids is not None:
        return bool(admin_ids) and str(user_id) in admin_ids
    try:
        from gateway.pairing import _configured_allowlist, PairingStore  # native
        configured = _configured_allowlist("telegram")
        if configured:
            _env_var, ids = configured
            return str(user_id) in {str(x) for x in ids}
        return PairingStore().is_approved("telegram", user_id)
    except Exception:
        return False

async def handle_command(adapter, update, context) -> None:
    """`/panel` entry — buttons from here on."""
    msg = getattr(update, "message", None)
    if not msg:
        return
    uid = str(getattr(getattr(update, "effective_user", None), "id", ""))
    if not _is_admin(adapter, uid):
        try:
            await msg.reply_text(_UNAUTHORIZED)
        except Exception:
            pass
        return
    chat_id = str(getattr(msg, "chat_id", ""))
    thread_id = getattr(msg, "message_thread_id", None)
    text, kb = screen_main(adapter)
    try:
        pmode = getattr(adapter, "ParseMode", None)  # adapter has it imported
    except Exception:
        pmode = None
    try:
        md = None
        sender = getattr(adapter, "_send_control_message", None)
        if callable(sender):
            await sender(chat_id, text, parse_mode=None, thread_id=thread_id, metadata=None, reply_markup=kb)
        else:
            await msg.reply_text(text=text, reply_markup=kb)
    except Exception as exc:
        logger.warning("cpanel panel send failed: %s", scrub_text(str(exc)))

# ── /start menu (authorized users only; unknowns are deferred to the native path) ──

def _start_user_kind(adapter, user_id: str) -> str:
    """admin | user | unknown — resolved ONLY via native pairing/allowlist helpers.

    admin  = configured allowlist member, or (no allowlist) pairing-approved
    user   = authorized by the native union (allowlist member or pairing-approved) but not admin
    unknown = anything else (incl. helper failures) → caller defers to native /start handling
    """
    uid = str(user_id or "").strip()
    if not uid:
        return "unknown"
    try:
        from gateway.pairing import _configured_allowlist, PairingStore  # native
        al = _configured_allowlist("telegram")
        ids: Any = []
        if al:
            ids = al[1] if isinstance(al, tuple) and len(al) == 2 else al
            if isinstance(ids, str):
                ids = [x.strip() for x in ids.split(",") if x.strip()]
        in_allow = bool(ids) and uid in {str(x) for x in ids}
        try:
            approved = bool(PairingStore().is_approved("telegram", uid))
        except Exception:
            approved = False
    except Exception:
        return "unknown"  # fail-closed: never guess
    if in_allow or (not al and approved):
        return "admin"
    if in_allow or approved:
        return "user"
    return "unknown"


async def handle_start_command(adapter, update, context) -> None:
    """`/start` — welcome + inline menu. Admin sees the Control Panel button (hctl:main);

    mere authorized users get the menu without admin controls; unknown users are handed
    back to the native command pipeline unchanged (silent ack / pairing)."""
    msg = getattr(update, "message", None)
    if not msg or not getattr(msg, "text", None):
        return
    uid = str(getattr(getattr(update, "effective_user", None), "id", ""))
    kind = _start_user_kind(adapter, uid)
    if kind == "unknown":
        native = getattr(adapter, "_handle_command", None)
        if callable(native):
            try:
                await native(update, context)  # native /start: ack ignore + pairing flow
            except Exception as exc:
                logger.warning("cpanel start: native handoff failed: %s", scrub_text(str(exc)))
        return
    name = (getattr(getattr(update, "effective_user", None), "first_name", "") or "").strip()
    welcome = _T("cpanel.start.welcome", "🛡️ Hermes is running. Choose an option:")
    if name:
        welcome = f"{name} — {welcome}"
    chat_id = str(getattr(msg, "chat_id", "") or uid)
    thread_id = getattr(msg, "message_thread_id", None)
    if kind == "admin":
        # Persistent ReplyKeyboard: the visible, always-available panel control.
        # (No inline twin: the tap below routes into the same panel; see consume_pending_input.)
        kb = _persist_keyboard(adapter)
        try:
            sender = getattr(adapter, "_send_control_message", None)
            if callable(sender):
                await sender(chat_id, welcome, parse_mode=None, thread_id=thread_id, metadata=None, reply_markup=kb)
                _persist_mark(chat_id)
            else:
                await msg.reply_text(welcome, reply_markup=kb)
                _persist_mark(chat_id)
        except Exception as exc:
            logger.warning("cpanel start menu send failed: %s", scrub_text(str(exc)))
        return
    # authorized non-admin: plain welcome, NO admin button (UI sugar only; routes re-check)
    try:
        sender = getattr(adapter, "_send_control_message", None)
        if callable(sender):
            await sender(chat_id, welcome, parse_mode=None, thread_id=thread_id, metadata=None, reply_markup=None)
        else:
            await msg.reply_text(welcome)
    except Exception as exc:
        logger.warning("cpanel start menu send failed: %s", scrub_text(str(exc)))



# ── persistent storage: Google Drive (real OAuth; verification-gated) ─────────

def _gd_cfg():
    """Env-only inputs; secret material never rendered. Redirect defaults to the
    loopback paste flow (user copies the FULL redirect URL back into the chat)."""
    import os as _os
    cid = (_os.environ.get("GOOGLE_DRIVE_CLIENT_ID") or "").strip()
    red = (_os.environ.get("GOOGLE_DRIVE_REDIRECT_URI") or "http://127.0.0.1").strip()
    sec = _os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")  # optional (PKCE works without)
    return cid, red, sec


def _gd_state_line(state: str) -> str:
    mapping = {
        "NOT_CONNECTED": "⚪ NOT_CONNECTED", "CONNECTING": "🟡 CONNECTING",
        "CONNECTED": "🟢 CONNECTED", "ERROR": "🔴 ERROR",
        "REAUTH_REQUIRED": "🟠 REAUTH_REQUIRED", "DISCONNECTED": "⚫ DISCONNECTED",
    }
    return mapping.get(state, state)


def screen_pstorage(adapter):
    from hermes_persist import oauth_store, gdrive
    home = _home()
    st = oauth_store.status(home)
    state = str(st.get("state") or gdrive.NOT_CONNECTED)
    cid, _, _ = _gd_cfg()
    last_sync = st.get("last_success_sync") or _T("cpanel.pst.never", "never")
    last_err = st.get("error") or "—"
    lines = [
        _T("cpanel.pst.title", "☁️ Persistent Storage — Google Drive"), "─" * 26,
        f"{_T('cpanel.pst.status', 'Status')}: {_gd_state_line(state)}",
        f"{_T('cpanel.pst.lastsync', 'Last sync')}: {last_sync}",
        f"{_T('cpanel.pst.lastbackup', 'Last backup')}: {st.get('last_backup') or _T('cpanel.pst.never', 'never')}",
        f"{_T('cpanel.pst.account', 'Account')}: {st.get('account') or '—'}",
        f"{_T('cpanel.pst.lastverif', 'Last verified sync')}: {st.get('last_success_sync') or _T('cpanel.pst.unchecked', 'unchecked')}",
        "",
        _T("cpanel.pst.note",
           "Data lives in the hidden appDataFolder of YOUR Drive (never public, minimum scope drive.appdata). "
           "Secrets stay AES-256-GCM encrypted there — never plaintext creds."),
    ]
    if state == gdrive.ERROR:
        lines.append(f"{_T('cpanel.pst.err', 'Error')}: {scrub_text(str(last_err))[:300]}")
    if state == gdrive.REAUTH_REQUIRED:
        lines.append(_T("cpanel.pst.reauth", "Credential was revoked — reconnect is required (Hermes never creates another account silently)."))
    rows = []
    if state in (gdrive.NOT_CONNECTED, gdrive.DISCONNECTED, gdrive.REAUTH_REQUIRED, gdrive.ERROR):
        label = ("🔒 " + _T("cpanel.pst.reconnect", "Reconnect Google Drive")) if state == gdrive.REAUTH_REQUIRED             else ("🔗 " + _T("cpanel.pst.connect", "Connect Google Drive"))
        rows = [[(label, "hctl:pstorage:connect")]] if cid else []
        if not cid:
            lines.append("")
            lines.append(_T("cpanel.pst.blocked", "BLOCKED — GOOGLE OAUTH CONFIGURATION REQUIRED (set GOOGLE_DRIVE_CLIENT_ID; optional GOOGLE_DRIVE_CLIENT_SECRET)."))
    if state == gdrive.CONNECTED:
        rows = [
            [("🔁 " + _T("cpanel.pst.syncnow", "Sync Now (health+verify)"), "hctl:pstorage:syncnow")],
            [("💾 " + _T("cpanel.pst.backupnow", "Backup Now"), "hctl:pstorage:backupnow")],
            [("♻️ " + _T("cpanel.pst.restore", "Restore Latest (isolated dir, safe)"), "hctl:pstorage:restore")],
            [("⛔ " + _T("cpanel.pst.disconnect", "Disconnect"), "hctl:pstorage:disconnect")],
        ]
    rows.append([("◀️ " + _T("cpanel.back", "Back"), "hctl:settings")])
    return "\n".join(lines)[:_MAX_TEXT], _kb(adapter, rows)


def _gd_finish_code(adapter, chat_id: str, raw: str) -> str:
    """Consume the pasted redirect-URL / code → real exchange → verify → CONNECTED only on success."""
    import urllib.parse as _up
    from hermes_persist import oauth_store
    home = _home()
    cid, red, sec = _gd_cfg()
    text_in = (raw or "").strip()
    code, returned_state = text_in, None
    if "://" in text_in or "code=" in text_in:
        try:
            qs = _up.parse_qs(_up.urlsplit(text_in if "://" in text_in else "?" + text_in).query)
            if qs.get("code"):
                code = qs["code"][0]
                returned_state = (qs.get("state") or [None])[0]
        except Exception:
            pass
    if not returned_state:
        try:  # bare-code paste flow: bind to the state of the stored pending flow
            import json as _json
            _pending = _json.loads((home / oauth_store._PENDING_FILE).read_text())
            returned_state = _pending.get("state")
        except Exception:
            returned_state = None
    try:
        out = oauth_store.finish_connect(home, code=code, returned_state=returned_state,
                                         client_id=cid, client_secret=sec)
    except Exception as exc:
        from hermes_persist import gdrive as _gd
        st = oauth_store.status(home)
        return (_T("cpanel.pst.connectfail", "❌ Connect failed — status: ") + _gd_state_line(str(st.get("state") or _gd.ERROR)) +
                "\n" + _T("cpanel.pst.reason", "Reason") + ": " + scrub_text(str(exc))[:300])
    acct = out.get("account") or "—"
    return _T("cpanel.pst.connected", "✅ Google Drive CONNECTED — verified (health→upload→download→checksum→cleanup). Account: ") + acct


async def handle_callback(adapter, query, data: str) -> None:
    cb = adapter._callback_ctx(query)
    if not await adapter._callback_authorized(query, cb, _UNAUTHORIZED):
        return
    uid = str(getattr(query.from_user, "id", ""))
    if not _is_admin(adapter, uid):
        await query.answer(text=_UNAUTHORIZED)
        return
    parts = data.split(":")
    screen = parts[1] if len(parts) > 1 else "main"
    op = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""
    arg2 = parts[4] if len(parts) > 4 else ""
    chat_id = str(cb.get("chat_id") or "")

    if screen == "noop-close":
        try:
            await query.answer()
            await query.message.delete()
        except Exception:
            pass
        return
    if screen in ("noop",):
        await query.answer()
        return

    text: str = ""
    kb = None
    try:
        if screen == "main":
            text, kb = screen_main(adapter)
        elif screen == "prov":
            if op == "sel" and arg:
                text, kb = screen_provider_detail(adapter, arg)
            elif op == "toggle" and arg:
                nm = arg.lower()
                # Native runtime gate (hermes_cli.runtime_provider._raise_if_provider_disabled);
                # legacy panel list is migrated out. NOTE: disabling the ACTIVE provider makes
                # turns fail visibly until a new default is chosen — warn, never silently route.
                cfg = _read_config()
                legacy = [str(x) for x in ((cfg.get("model") or {}).get("disabled_providers") or []) if x]
                if nm in legacy:
                    legacy.remove(nm)
                    _write_config_key(adapter, "model.disabled_providers", legacy)
                if arg2 == "off":
                    _write_config_key(adapter, f"providers.{arg}.enabled", False)
                else:
                    _write_config_key(adapter, f"providers.{arg}.enabled", True)
                text, kb = screen_provider_detail(adapter, arg)
                active_p = str((_read_config().get("model") or {}).get("provider") or "")
                if arg2 == "off" and active_p == arg:
                    text = "⚠️ " + _T("cpanel.prov.offactive",
                                      "You disabled the ACTIVE default provider — chat turns will fail until you choose another default.") + "\n\n" + text
            elif op == "mkdefault" and arg:
                # A default provider must not be disabled: enable natively + clear legacy pin.
                cfg = _read_config()
                legacy = [str(x) for x in ((cfg.get("model") or {}).get("disabled_providers") or []) if x]
                if arg.lower() in legacy:
                    legacy.remove(arg.lower())
                    _write_config_key(adapter, "model.disabled_providers", legacy)
                provs = cfg.get("providers")
                if isinstance(provs, dict) and isinstance(provs.get(arg), dict) and provs[arg].get("enabled") is False:
                    _write_config_key(adapter, f"providers.{arg}.enabled", True)
                ok = _write_config_key(adapter, "model.provider", arg)
                rows, _, am = _provider_rows()
                text = ("⭐ " + (arg if ok else "")) + "\n" + _T("cpanel.prov.mkdefault.done", "Default provider saved. Model stays: ") + am
                kb = _back_cancel(adapter, f"hctl:prov:sel:{arg}")
            elif op == "delask" and arg:
                text, kb = screen_confirm(adapter, f"🗑 Delete the stored API key for {arg}? (config untouched)", f"hctl:prov:del:{arg}", f"hctl:prov:sel:{arg}")
            elif op == "del" and arg:
                prof = _profiles().get(arg)
                var = (tuple(getattr(prof, "env_vars", ()) or ()) or ("",))[0]
                ok = _env_write(var, None) if var else False
                text, kb = screen_provider_detail(adapter, arg)
                text = ("✅ key removed" if ok else "⚠️ nothing removed / unsupported") + "\n\n" + text
            else:
                text, kb = screen_providers(adapter)
        elif screen == "add":
            if op == "start":
                text, kb = _add_start(adapter)
            elif op == "pick" and arg:
                prof = _profiles().get(arg)
                labels = {"gemini": "Google Gemini", "google": "Google Gemini", "openai": "OpenAI", "anthropic": "Anthropic"}
                disp = labels.get(arg, getattr(prof, "display_name", "") or arg)
                env_vars = tuple(getattr(prof, "env_vars", ()) or ())
                if env_vars:
                    _pending_set(chat_id, "add", "key", {"name": arg, "display": disp, "key_var": env_vars[0]})
                    text = "\n".join([f"➕ {disp}", "─" * 26,
                                      _T("cpanel.add.keyask", "Send the API key as a normal message now."), "",
                                      _T("cpanel.add.keynote", "It will be stored locally (600), shown masked, and your message will be deleted.")])
                    kb = _back_cancel(adapter, "hctl:add:start")
                else:
                    text = _T("cpanel.add.nokey", "This provider type does not take an API key here.")
                    kb = _back_cancel(adapter, "hctl:add:start")
            elif op == "keyonly" and arg:
                prof = _profiles().get(arg)
                env_vars = tuple(getattr(prof, "env_vars", ()) or ())
                if env_vars:
                    _pending_set(chat_id, "add", "keyonly", {"name": arg, "display": arg, "key_var": env_vars[0]})
                    text = _T("cpanel.add.keyask", "Send the new API key as a normal message now.")
                    kb = _back_cancel(adapter, f"hctl:prov:sel:{arg}")
                else:
                    text, kb = screen_provider_detail(adapter, arg)
            elif op == "model" and arg:
                p = _pending_get(chat_id) or {"data": {}}
                d = p.get("data", {})
                d["model"] = arg if arg != "-" else ""
                _pending_set(chat_id, "add", "confirm", d)
                text = _add_confirm_text(d)
                kb = _kb(adapter, [[("💾 " + _T("cpanel.save", "Save"), "hctl:add:save")],
                                   [("✏️ " + _T("cpanel.edit", "Edit (restart wizard)"), "hctl:add:start"),
                                    ("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:add:cancel")]])
            elif op == "save":
                d = (_pending_pop(chat_id) or {}).get("data", {})
                name = d.get("name", "")
                if name and d.get("key"):
                    _env_write(d["key_var"], d.pop("key"))
                if d.get("model"):
                    _write_config_key(adapter, "model.default", d["model"])
                    _write_config_key(adapter, "model.provider", name)
                text, kb = screen_providers(adapter)
                text = "✅ " + _T("cpanel.add.saved", "Saved (no AI call made).") + "\n\n" + text
            elif op == "cancel":
                _pending_pop(chat_id)
                text, kb = screen_providers(adapter)
            else:
                text, kb = _add_start(adapter)
        elif screen == "models":
            session_key, session_store, runner = _current_chat_session(adapter, query, chat_id=chat_id)
            pin = _session_model_pin(session_store, session_key)
            if op == "set" and arg:
                saved = _write_config_key(adapter, "model.default", arg)
                applied = ""
                if not saved:
                    text = "⚠️ " + _T("cpanel.savefail", "Save failed — configuration was NOT changed.") + "\n\n" + screen_models(adapter, pin=pin)[0]
                else:
                    # Apply to THIS chat: a stale /model pin would keep shadowing the new global
                    # default and the panel would look decorative (the reported regression).
                    if pin and str(pin.get("model")) != arg:
                        if _clear_session_pin_and_evict(session_store, runner, session_key):
                            applied = "\n" + _T("cpanel.models.pinapplied",
                                                "Applied to this chat too (old /model pin cleared). Other chats keep their own /model pin.")
                        else:
                            applied = "\n⚠️ " + _T("cpanel.models.pinblocked",
                                                   "This chat has a /model pin that could not be cleared. Use the 🧹 button and re-test.")
                    elif pin:
                        applied = "\n" + _T("cpanel.models.pinalready", "This chat's /model pin already matches.")
                    pin_after = _session_model_pin(session_store, session_key)
                    text, kb = screen_models(adapter, pin=pin_after)
                    text = f"✅ global default = {arg}" + applied + "\n\n" + text
            elif op == "clearpin":
                if pin:
                    if _clear_session_pin_and_evict(session_store, runner, session_key):
                        text = "🧹 " + _T("cpanel.models.pincleared", "Pin cleared — this chat now follows the global default.") + "\n\n"
                        pin = None
                    else:
                        text = "⚠️ " + _T("cpanel.models.pinclearfail", "Could not clear the pin (session store unavailable).") + "\n\n"
                else:
                    text = _T("cpanel.models.nopin2", "This chat has no /model pin.") + "\n\n"
                text2, kb = screen_models(adapter, pin=pin)
                text = text + text2
            elif op == "type":
                _pending_set(chat_id, "models", "type", {})
                text = _T("cpanel.models.typeask", "Send the model id as a normal message now.")
                kb = _back_cancel(adapter, "hctl:models")
            else:
                text, kb = screen_models(adapter, pin=pin)
        elif screen == "test":
            if op == "sel" and arg:
                text, kb = screen_test(adapter, arg)
            elif op == "go" and arg:
                await query.answer(text=_T("cpanel.test.running", "Testing…"))
                res = run_connection_test(arg)
                dur = time.time() - res.get("ts", time.time())
                head = f"{res.get('provider_icon','—')} {res.get('summary','')}"
                body = screen_status_test_note = ""
                if res.get("ok") is True:
                    body = _T("cpanel.test.oknote", "Provider API reachable. (Connectivity only — not an inference check.)")
                elif res.get("kind") == "quota":
                    body = _T("cpanel.test.quota", "Reason: provider quota/rate limit (e.g. Google RESOURCE_EXHAUSTED). This is NOT a Hermes failure.")
                elif res.get("kind") == "auth":
                    body = _T("cpanel.test.auth", "Reason: authentication failed — check the stored key.")
                elif res.get("kind") == "network":
                    body = _T("cpanel.test.net", "Reason: network failure.")
                elif res.get("kind") == "provider_error":
                    body = _T("cpanel.test.perr", "Reason: provider-side error (5xx). Known transient capacity states are shown as-is.")
                elif res.get("kind") == "no_key":
                    body = _T("cpanel.test.nokey", "Reason: credential not configured.")
                else:
                    body = _T("cpanel.test.other", "See status code above.")
                text = "\n".join(["🧪 " + _T("cpanel.test.result", "Test result"), "─" * 26, head, "", body])
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), "hctl:test:sel:" + arg)]])
            else:
                text, kb = screen_test(adapter)
        elif screen == "cust":
            if op == "new":
                _pending_set(chat_id, "cwx", "id", {"id": "", "display": "", "website": "", "base_url": "",
                                                    "auth": "bearer", "key": "", "key_masked": "—",
                                                    "models": [], "default_model": ""})
                text = "🧩 " + _T("cpanel.cust.wiz", "New custom provider (OpenAI-compatible). Cancel anytime with the button below.") + "\n\n" + _cwx_steps_text("id", {})
                kb = _kb(adapter, [[("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]])
            elif op == "cancel":
                _pending_pop(chat_id)
                text, kb = screen_providers(adapter)
                text = "❌ " + _T("cpanel.cancel", "Cancel") + "\n\n" + text
            elif op == "sel" and arg:
                text, kb = screen_custom_detail(adapter, arg)
            elif op == "go" and arg:
                await query.answer(text=_T("cpanel.test.running", "Testing…"))
                e = _custom_providers().get(arg) or {}
                key_var = str(e.get("key_env") or "")
                if key_var:
                    e = dict(e)
                    e["api_key"] = _env_get(key_var) or ""
                res = await _probe_entry(e)
                ok = bool(res.get("ok"))
                icon = "🟢" if ok else "🔴"
                head = f"{icon} custom:{arg}: HTTP {res.get('http_status') or '—'} — {res.get('category', '?')}"
                caps_line = ""
                if res.get("capabilities"):
                    caps_line = _T("cpanel.test.caps", "Capabilities: ") + ", ".join(res["capabilities"])
                note = _probe_note(str(res.get("category")), ok)
                text = "\n".join(x for x in ["🧪 " + _T("cpanel.test.result", "Test result"), "─" * 26,
                                             head, caps_line, "", note] if x)
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            elif op == "listf" and arg in ("all", "ready", "task"):
                _prov_filter_set(adapter, arg)
                text, kb = screen_providers(adapter)
            elif op == "models" and arg and (_entry_task_only(_custom_providers().get(arg) or {}) or not _entry_discovery_supported(_custom_providers().get(arg) or {})):
                text = "ℹ️ " + _T("cpanel.cust.discnone", "Model discovery: Not applicable (provider does not expose a model catalog)")
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            elif op == "models" and arg:
                e = _custom_providers().get(arg) or {}
                base = str(e.get("api") or e.get("base_url") or "")
                key = _env_get(str(e.get("key_env") or "")) if e.get("key_env") else e.get("api_key")
                await query.answer(text=_T("cpanel.cust.discovering", "Discovering models…"))
                ids, kindd = discover_models(base, str(e.get("models_url") or ""), key)
                if ids:
                    rows = [[(m[:50], f"hctl:cust:setmodel:{arg}:{m[:30]}")] for m in ids[:8]]
                    rows.append([("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")])
                    text = "\n".join([f"📋 {arg} — {_T('cpanel.cust.found', 'models found')}: {len(ids)}", "─" * 26] + ids[:12])
                    kb = _kb(adapter, rows)
                else:
                    text = "\n".join([f"📋 {arg}", "─" * 26, _T("cpanel.cust.nodisc", "Discovery failed: ") + kindd])
                    kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            elif op == "setmodel" and arg and arg2:
                _write_config_key(adapter, f"providers.{arg}.default_model", arg2)
                e = _custom_providers().get(arg) or {}
                meta = (_read_config().get("cpanel_providers_meta") or {})
                text, kb = screen_custom_detail(adapter, arg)
                text = f"✅ default_model = {arg2}\n\n" + text
            elif op == "setdef" and arg and _entry_task_only(_custom_providers().get(arg) or {}):
                # Set-Default is gated on chat capability: task-only providers are never a default route.
                text = "🚫 " + _T("cpanel.cust.nosetdef", "This provider is task-only (no chat capability) and cannot be the default model route.")
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            elif op == "setdef" and arg:
                e = _custom_providers().get(arg) or {}
                dm = str(e.get("default_model") or "")
                if not dm:
                    text = _T("cpanel.cust.needmodel", "Pick a default model first (Models → choose).")
                    kb = _kb(adapter, [[("📋 Models", f"hctl:cust:models:{arg}")],
                                       [("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
                else:
                    _write_config_key(adapter, "model.provider", f"custom:{arg}")
                    _write_config_key(adapter, "model.default", dm)
                    text, kb = screen_custom_detail(adapter, arg)
                    text = f"⭐ default route = custom:{arg} / {dm}\n\n" + text
            elif op == "toggle" and arg:
                e = _custom_providers().get(arg) or {}
                _write_config_key(adapter, f"providers.{arg}.enabled", not e.get("enabled", True))
                text, kb = screen_custom_detail(adapter, arg)
                text = ("✅ enabled" if not e.get("enabled", True) else "⚪ disabled") + "\n\n" + text
            elif op == "runtask" and arg:
                e = _custom_providers().get(arg) or {}
                if not _entry_task_only(e):
                    text = "🚫 " + _T("cpanel.task.notcapable", "This provider is not task-capable.")
                    kb = _back_cancel(adapter, f"hctl:cust:sel:{arg}")
                elif not _entry_has_cred(e):
                    text = "🚫 " + _T("cpanel.task.nokey", "No credential configured for this task provider.")
                    kb = _back_cancel(adapter, f"hctl:cust:sel:{arg}")
                else:
                    _pending_set(chat_id, "task", "instruction", {"pid": arg})
                    text = "▶ " + _T("cpanel.task.ask",
                                      "Send the task instruction as a normal message now. It will run against this provider with bounded polling; no credentials are shown.")
                    kb = _back_cancel(adapter, f"hctl:cust:sel:{arg}")
            elif op == "editask" and arg:
                rows = [
                    [(_T("cpanel.cust.ebase", "✏️ Change base URL"), f"hctl:cwx:eb:{arg}")],
                    [(_T("cpanel.cust.eweb", "✏️ Change website (metadata)"), f"hctl:cwx:ew:{arg}")],
                    [(_T("cpanel.cust.ekey", "🔑 Rotate API key"), f"hctl:cwx:ek:{arg}")],
                    [(_T("cpanel.cust.ename", "✏️ Change display name"), f"hctl:cwx:en:{arg}")],
                    [("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")],
                ]
                text, kb = f"✏️ {arg}", _kb(adapter, rows)
            elif op == "delask" and arg:
                text, kb = screen_confirm(adapter, f"🗑 Delete custom provider {arg}? (config + stored key removed)", f"hctl:cust:delgo:{arg}", f"hctl:cust:sel:{arg}")
            elif op == "delgo" and arg:
                e = _custom_providers().pop(arg, None) if False else (_custom_providers().get(arg) or {})
                if e.get("key_env"):
                    _env_write(str(e["key_env"]), None)
                meta = dict(_read_config().get("cpanel_providers_meta") or {})
                meta.pop(arg, None)
                _write_config_key(adapter, "cpanel_providers_meta", meta)
                _custom_delete(adapter, arg)
                warn = ""
                if _custom_active_route() == f"custom:{arg}":
                    warn = "\n⚠️ " + _T("cpanel.cust.activerm", "This provider was the default route — pick a new default!")
                text, kb = screen_providers(adapter)
                text = ("🗑 deleted " + arg + warn) + "\n\n" + text
            else:
                text, kb = screen_providers(adapter)
        elif screen == "cwx":
            pend = _pending_get(chat_id) or {"step": "", "data": {}}
            d = dict(pend.get("data") or {})
            pid_new = str(d.get("id") or "")
            if op == "auth" and arg == "apihdr":
                _pending_set(chat_id, "cwx", "authhdr", d)
                text = _T("cpanel.cwx.authhdr", "Send the auth HEADER NAME (e.g. X-API-Key, X-Browser-Use-API-Key).")
                kb = _kb(adapter, [[("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]])
            elif op == "auth" and arg in ("bearer", "none"):
                d["auth"] = arg
                if arg == "bearer":
                    _pending_set(chat_id, "cwx", "key", d)
                    text = _cwx_steps_text("key", d)
                    kb = _kb(adapter, [[("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]])
                else:
                    d["key"] = ""; d["key_masked"] = "—"
                    base = str(d.get("base_url") or "")
                    ids, kindd = discover_models(base, "", None)
                    d["models"] = ids or []
                    if ids:
                        _pending_set(chat_id, "cwx", "modelpick", d)
                        rows = [[(m[:50], f"hctl:cwx:model:{m[:30]}")] for m in ids[:8]]
                        rows.append([("➖ " + _T("cpanel.add.skipmodel", "Skip / set later"), "hctl:cwx:model:-")])
                        rows.append([("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")])
                        text = _T("cpanel.cust.pickmodel", "Discovery OK — pick a default model:") + "\n" + _cwx_summary(d)
                        kb = _kb(adapter, rows)
                    else:
                        _pending_set(chat_id, "cwx", "confirm", d)
                        rows = [[("💾 " + _T("cpanel.save", "Save"), "hctl:cwx:save")],
                                [("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]]
                        text = "⚠️ " + _T("cpanel.cust.nodiscshort", "discovery failed: ") + kindd + "\n\n" + _cwx_summary(d)
                        kb = _kb(adapter, rows)
            elif op == "model":
                if arg and arg != "-":
                    d["default_model"] = arg
                _pending_set(chat_id, "cwx", "confirm", d)
                rows = [[("💾 " + _T("cpanel.save", "Save"), "hctl:cwx:save")],
                        [("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]]
                text = _cwx_summary(d)
                kb = _kb(adapter, rows)
            elif op == "save":
                pid = str(d.get("id") or "")
                if not _custom_id_ok(pid):
                    _pending_pop(chat_id)
                    text, kb = screen_providers(adapter)
                    text = "⚠️ " + _T("cpanel.cust.badid", "invalid id — nothing saved") + "\n\n" + text
                else:
                    # TRANSACTIONAL SAVE: generic probe must succeed BEFORE anything is persisted.
                    await query.answer(text=_T("cpanel.test.running", "Testing…"))
                    probe_res = await _probe_entry(_wizard_probe_entry(d))
                    if not probe_res.get("ok"):
                        # NOTHING was saved — keep the pending state so the user can retry.
                        _pending_set(chat_id, "cwx", "confirm", d)
                        head = f"🔴 {probe_res.get('category', '?')} (HTTP {probe_res.get('http_status') or '—'})"
                        rows = [[("🔁 " + _T("cpanel.cwx.testagain", "Re-test & save"), "hctl:cwx:save")],
                                [("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:new")],
                                [("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]]
                        text = "⚠️ " + _T("cpanel.cwx.saveblocked", "Probe failed — provider was NOT saved:") \
                            + f"\n{head}\n" + _probe_note(str(probe_res.get("category")), False) \
                            + "\n\n" + _cwx_summary(d)
                        kb = _kb(adapter, rows)
                    else:
                        _pending_pop(chat_id)
                        fields = {"api": d["base_url"], "name": str(d.get("display") or pid)}
                        if str(d.get("auth") or "bearer") != "bearer":
                            fields["auth"] = {"type": d["auth"]}
                            if d["auth"] == "api_key_header" and d.get("auth_header"):
                                fields["auth"]["header"] = str(d["auth_header"])
                        for block in ("probe", "discovery", "runtime", "provider_capabilities"):
                            if d.get(block):
                                fields[block] = d[block]
                        if d.get("key"):
                            var = _custom_env_var(pid)
                            _env_write(var, d["key"])
                            fields["key_env"] = var
                        if d.get("default_model"):
                            fields["default_model"] = d["default_model"]
                        if d.get("models"):
                            fields["models"] = {m: {} for m in d["models"][:20]}
                            fields["models_discovered"] = True
                        _custom_write(adapter, pid, fields)
                        if d.get("website"):
                            meta = dict(_read_config().get("cpanel_providers_meta") or {})
                            meta[pid] = {"website": d["website"], "display": d.get("display") or pid}
                            _write_config_key(adapter, "cpanel_providers_meta", meta)
                        text, kb = screen_custom_detail(adapter, pid)
                        text = "✅ " + _T("cpanel.cwx.testsaved", "Probe OK — saved.") + "\n\n" + text
            elif op == "eb" and arg:
                _pending_set(chat_id, "cwx", "basedit", {"id": arg})
                text = _cwx_steps_text("base", {})
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            elif op == "ew" and arg:
                _pending_set(chat_id, "cwx", "webedit", {"id": arg})
                text = _cwx_steps_text("website", {})
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            elif op == "ek" and arg:
                _pending_set(chat_id, "cwx", "keyedit", {"id": arg})
                text = _cwx_steps_text("keyedit", {})
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            elif op == "en" and arg:
                _pending_set(chat_id, "cwx", "nameedit", {"id": arg})
                text = _T("cpanel.cwx.display", "Send a display name (shown in the panel).")
                kb = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), f"hctl:cust:sel:{arg}")]])
            else:
                text, kb = screen_providers(adapter)
        elif screen == "status":
            text, kb = screen_status(adapter)
        elif screen == "users":
            if op == "ok" and arg:
                from gateway.pairing import PairingStore
                done = PairingStore().approve_request("telegram", arg)
                text, kb = screen_users(adapter)
                text = ("✅ approved " + str((done or {}).get("user_name") or (done or {}).get("user_id")) if done else "⌛ request expired/invalid") + "\n\n" + text
            elif op == "rv" and arg:
                text, kb = screen_confirm(adapter, f"🚫 Revoke access for {arg}?", f"hctl:users:rvgo:{arg}", "hctl:users")
            elif op == "rvgo" and arg:
                from gateway.pairing import PairingStore
                done = PairingStore().revoke("telegram", arg)
                text, kb = screen_users(adapter)
                text = ("✅ revoked" if done else "⚠️ not found") + "\n\n" + text
            else:
                text, kb = screen_users(adapter)
        elif screen == "settings":
            if op == "lang" and arg in ("en", "fa"):
                ok = _write_config_key(adapter, "display.language", arg)
                try:
                    from agent.i18n import reset_language_cache
                    reset_language_cache()
                except Exception:
                    pass
                text, kb = screen_settings(adapter)
                text = ("✅ language = " + arg + "\n\n" if ok else "⚠️ save failed\n\n") + text
            elif op == "comp" and arg in ("on", "off"):
                ok = _write_config_key(adapter, "compression.enabled", arg == "on")
                text, kb = screen_settings(adapter)
                text = ("✅ saved\n\n" if ok else "⚠️ save failed\n\n") + text
            else:
                text, kb = screen_settings(adapter)
        elif screen == "pstorage":
            from hermes_persist import oauth_store, gdrive
            home = _home()
            cid, red, sec = _gd_cfg()
            if op == "connect":
                if not cid:
                    text = _T("cpanel.pst.blocked", "BLOCKED — GOOGLE OAUTH CONFIGURATION REQUIRED (set GOOGLE_DRIVE_CLIENT_ID).")
                    text2, kb = screen_pstorage(adapter)
                    text, kb = text + "\n\n" + text2, kb
                else:
                    try:
                        out = oauth_store.begin_connect(home, client_id=cid, redirect_uri=red)
                        auth_url = out["auth_url"]
                        _pending_set(str(cb.get("chat_id") or ""), "gdrive", "await_code", {})
                        text2, kb = screen_pstorage(adapter)
                        kb2 = _kb(adapter, [[("◀️ " + _T("cpanel.back", "Back"), "hctl:pstorage:cancelflow")]])
                        try:
                            sender = getattr(adapter, "_send_control_message", None)
                            if callable(sender):
                                await sender(str(cb.get("chat_id") or ""),
                                             "🔗 " + _T("cpanel.pst.openurl", "Open this link, authorize Hermes (Drive hidden app-data folder only),\nthen paste the FULL redirect URL (or bare code) here:") + "\n\n" + auth_url,
                                             parse_mode=None, thread_id=None, metadata=None, reply_markup=kb2)
                        except Exception:
                            pass
                        text = _T("cpanel.pst.authurlsent", "🔗 Authorization link sent — paste the redirected URL here once Google shows it.") + "\n\n" + text2
                    except Exception as exc:
                        text2, kb = screen_pstorage(adapter)
                        text = ("⚠️ " + scrub_text(str(exc))[:300] + "\n\n") + text2
            elif op == "cancelflow":
                try:
                    _pending_pop(str(cb.get("chat_id") or ""))
                    (home / oauth_store._PENDING_FILE).unlink(missing_ok=True)
                    oauth_store.set_state(home, gdrive.NOT_CONNECTED)
                except Exception:
                    pass
                text, kb = screen_pstorage(adapter)
            elif op == "syncnow":
                try:
                    out = oauth_store.health_or_reauth(home)
                    text2, kb = screen_pstorage(adapter)
                    ok = out.get("state") == gdrive.CONNECTED
                    text = ("✅ " if ok else "⚠️ ") + _T("cpanel.pst.syncout", "Sync check: ") + str(out.get("state")) + "\n\n" + text2
                except Exception as exc:
                    text2, kb = screen_pstorage(adapter)
                    text = ("🔴 " + _T("cpanel.pst.syncfail", "Sync check failed: ") + scrub_text(str(exc))[:200] + "\n\n") + text2
            elif op == "backupnow":
                try:
                    p_st = oauth_store.status(home).get("state")
                    if p_st != gdrive.CONNECTED:
                        raise RuntimeError("not connected")
                    from hermes_persist import sync as _sync
                    import socket as _sock, os as _os
                    prov = oauth_store.load_provider(home, client_secret=sec)
                    sid = _sync.backup(home, prov, env_id=_os.environ.get("HERMES_ENV_ID") or _sock.gethostname(),
                                       hermes_version=_os.environ.get("HERMES_VERSION", "unknown"),
                                       commit=_os.environ.get("HERMES_COMMIT", "unknown"))
                    oauth_store.set_state(home, gdrive.CONNECTED, last_backup=sid)
                    text2, kb = screen_pstorage(adapter)
                    text = ("✅ " + _T("cpanel.pst.backupdone", "Backup complete, snapshot: ") + sid + "\n\n") + text2
                except Exception as exc:
                    text2, kb = screen_pstorage(adapter)
                    text = ("🔴 " + _T("cpanel.pst.backupfail", "Backup failed: ") + scrub_text(str(exc))[:250] + "\n\n") + text2
            elif op == "restore":
                try:
                    p_st = oauth_store.status(home).get("state")
                    if p_st != gdrive.CONNECTED:
                        raise RuntimeError("not connected")
                    from hermes_persist import sync as _sync
                    import socket as _sock, os as _os, time as _tm
                    prov = oauth_store.load_provider(home, client_secret=sec)
                    ok, problems, sid = _sync.verify_snapshot(prov, None)
                    if not ok:
                        raise RuntimeError("snapshot verify failed: " + "; ".join(problems)[:200])
                    target = home / "state" / ("restore-check-" + str(int(_tm.time())))  # isolated: never touches live state
                    sid2, restored = _sync.restore(target, prov, env_id=_os.environ.get("HERMES_ENV_ID") or _sock.gethostname(),
                                                   sid=sid, force=True)
                    names = (", ".join(restored[:6]) + ("…" if len(restored) > 6 else ""))
                    text2, kb = screen_pstorage(adapter)
                    text = (f"✅ " + _T("cpanel.pst.restoredone", "Verified restore of latest snapshot into isolated dir:") +
                            f"\n{target}  ({len(restored)} files: {names})\n\n") + text2
                except Exception as exc:
                    text2, kb = screen_pstorage(adapter)
                    text = ("🔴 " + _T("cpanel.pst.restorefail", "Restore failed: ") + scrub_text(str(exc))[:250] + "\n\n") + text2
            elif op == "disconnect":
                out = oauth_store.disconnect(home)
                text2, kb = screen_pstorage(adapter)
                text = ("✅ " + _T("cpanel.pst.disconnected", "Disconnected — credential revoked server-side and local token removed.") + "\n\n") + text2
            else:
                text, kb = screen_pstorage(adapter)
        elif screen == "logs":
            text, kb = screen_logs(adapter)
        elif screen == "backup":
            if op == "go":
                ok, msg = _do_backup()
                text, kb = screen_backup(adapter)
                text = ("✅ " + msg if ok else "⚠️ " + msg) + "\n\n" + text
            elif op == "ask" and arg:
                text, kb = screen_confirm(adapter, f"♻️ Restore from {arg}? Current config.yaml/auth.json/.env will be replaced (a fresh backup is taken first). Restart may be required.", f"hctl:backup:do:{arg}", "hctl:backup")
            elif op == "do" and arg:
                ok, msg = _do_restore(arg)
                text, kb = screen_backup(adapter)
                text = ("✅ " + msg if ok else "⚠️ " + msg) + "\n\n" + text
            else:
                text, kb = screen_backup(adapter)
        else:
            text, kb = screen_main(adapter)
    except Exception as exc:
        logger.error("cpanel callback failed %s: %s", screen, scrub_text(str(exc)))
        text = _T("cpanel.error", "⚠️ Operation failed — details were logged (sanitized).")
        kb = _back_cancel(adapter)
    try:
        await query.answer()
    except Exception:
        pass
    try:
        if kb is not None:
            await query.edit_message_text(text=text[:3900], reply_markup=kb)
        else:
            await query.edit_message_text(text=text[:3900])
    except Exception as exc:
        logger.debug("cpanel edit failed (transient): %s", type(exc).__name__)

async def consume_pending_input(adapter, update, context) -> bool:
    """Intercept plain-text input while a cpanel flow is pending. Returns True if consumed.

    Consumed messages NEVER reach the LLM/agent. API-key messages are deleted."""
    msg = getattr(update, "message", None)
    if not msg or not getattr(msg, "text", None):
        return False
    chat_id = str(getattr(msg, "chat_id", ""))
    uid0 = str(getattr(getattr(update, "effective_user", None), "id", ""))
    kind0 = _start_user_kind(adapter, uid0)
    text0 = msg.text.strip()

    # Revocation hygiene: had a persistent button but is no longer admin → remove once.
    if kind0 != "admin" and _persist_unmark(chat_id):
        await _persist_detach(adapter, chat_id, getattr(msg, "message_thread_id", None))

    # Persistent button tap (any localized label): re-check auth AT EXECUTION TIME.
    if text0 in _persist_labels_all():
        if kind0 == "admin":
            if _persist_ts(chat_id) <= 0:
                await _persist_attach(adapter, chat_id, getattr(msg, "message_thread_id", None))
            text, kb = screen_main(adapter)
            try:
                sender = getattr(adapter, "_send_control_message", None)
                if callable(sender):
                    await sender(chat_id, text, parse_mode=None,
                                 thread_id=getattr(msg, "message_thread_id", None),
                                 metadata=None, reply_markup=kb)
                else:
                    await msg.reply_text(text, reply_markup=kb)
            except Exception as exc:
                logger.warning("cpanel persist open failed: %s", scrub_text(str(exc)))
            return True
        # not admin: NOT consumed — the text falls through to the normal pipeline
        return False

    p = _pending_get(chat_id)
    if not p:
        return False
    uid = str(getattr(getattr(update, "effective_user", None), "id", ""))
    if not _is_admin(adapter, uid):
        return False
    text_in = msg.text.strip()
    flow, step, data = p["flow"], p["step"], p["data"]

    if flow == "gdrive" and step == "await_code":
        try:
            await msg.delete()  # may carry an OAuth code — do not leave it in chat history
        except Exception:
            pass
        _pending_pop(chat_id)
        out = _gd_finish_code(adapter, chat_id, text_in)
        try:
            await adapter._send_control_message(chat_id, out, parse_mode=None,
                                                thread_id=getattr(msg, "message_thread_id", None), metadata=None)
        except Exception:
            try:
                await msg.reply_text(out)
            except Exception:
                pass
        return True

    if flow == "add" and step in ("key", "keyonly"):
        try:
            await msg.delete()
        except Exception:
            pass
        data["key"] = text_in
        data["key_masked"] = mask_secret(text_in)
        if step == "keyonly":
            _env_write(data["key_var"], text_in)
            _pending_pop(chat_id)
            out = "✅ " + _T("cpanel.add.keyrot", "Key rotated (stored locally, masked): ") + data["key_masked"]
        else:
            cands = _model_candidates(data.get("name", ""))[:8]
            _pending_set(chat_id, "add", "model", data)
            rows = [[(m[:58], f"hctl:add:model:{m[:40]}")] for m in cands]
            rows.append([( "➖ " + _T("cpanel.add.skipmodel", "Skip / set later"), "hctl:add:model:-")])
            rows.append([("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:add:cancel")])
            try:
                await adapter._send_control_message(chat_id, _T("cpanel.add.modelask", "API key captured (masked): ") + data["key_masked"] + "\n" + _T("cpanel.add.modelpick", "Pick a default model:"),
                                                    parse_mode=None, thread_id=getattr(msg, "message_thread_id", None), metadata=None,
                                                    reply_markup=_kb(adapter, rows))
            except Exception:
                await msg.reply_text("key captured (masked). Now pick a model via /panel.")
            return True
        try:
            await msg.reply_text(out)
        except Exception:
            pass
        return True

    if flow == "task" and step == "instruction":
        d = dict((p or {}).get("data") or {})
        _pending_pop(chat_id)
        pid = str(d.get("pid") or "")
        e = _custom_providers().get(pid)
        instruction = (text_in or "").strip()
        if not e or not _entry_task_only(e):
            try:
                await adapter._send_control_message(chat_id, "🚫 " + _T("cpanel.task.notcapable", "This provider is not task-capable."),
                                                    parse_mode=None, thread_id=getattr(msg, "message_thread_id", None), metadata=None,
                                                    reply_markup=_back_cancel(adapter, "hctl:prov"))
            except Exception:
                pass
            return True
        if not instruction:
            _pending_set(chat_id, "task", "instruction", d)
            try:
                await adapter._send_control_message(chat_id, _T("cpanel.task.ask", "Send the task instruction as a normal message now."),
                                                    parse_mode=None, thread_id=getattr(msg, "message_thread_id", None), metadata=None,
                                                    reply_markup=_back_cancel(adapter, f"hctl:cust:sel:{pid}"))
            except Exception:
                pass
            return True
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await adapter._send_control_message(chat_id, "… " + _T("cpanel.task.running", "Task is running (bounded polling)…"),
                                                parse_mode=None, thread_id=getattr(msg, "message_thread_id", None), metadata=None)
        except Exception:
            pass
        result = await _run_task_for_entry(e, instruction)
        body = _format_task_result(pid, result)
        try:
            await adapter._send_control_message(chat_id, body, parse_mode=None,
                                                thread_id=getattr(msg, "message_thread_id", None), metadata=None,
                                                reply_markup=_back_cancel(adapter, f"hctl:cust:sel:{pid}"))
        except Exception:
            pass
        return True

    if flow == "models" and step == "type":
        _pending_pop(chat_id)
        model_id = re.sub(r"\s+", "", text_in)[:80]
        try:
            await msg.delete()
        except Exception:
            pass
        ok = _write_config_key(adapter, "model.default", model_id)
        applied = ""
        verified_note = ""
        if ok:
            cfg = _read_config()
            pname = str((cfg.get("model") or {}).get("provider") or "")
            if model_id not in _model_candidates(pname):
                verified_note = "\n⚠️ " + _T("cpanel.models.unverified",
                                            "Model id not in the provider's known catalog — saved, but unverified. Run 🧪 Test.")
            session_key, session_store, runner = _current_chat_session(
                adapter, None, chat_id=chat_id, chat_type=str(getattr(getattr(msg, "chat", None), "type", "") or "dm"),
                thread_id=getattr(msg, "message_thread_id", None),
                user_id=str(getattr(getattr(msg, "from_user", None), "id", "") or ""))
            pin = _session_model_pin(session_store, session_key)
            if pin and str(pin.get("model")) != model_id:
                if _clear_session_pin_and_evict(session_store, runner, session_key):
                    applied = "\n" + _T("cpanel.models.pinapplied",
                                        "Applied to this chat too (old /model pin cleared). Other chats keep their own /model pin.")
                else:
                    applied = "\n⚠️ " + _T("cpanel.models.pinblocked",
                                           "This chat has a /model pin that could not be cleared.")
        kb_text, kb = screen_models(adapter, pin=None)
        try:
            await adapter._send_control_message(chat_id, ("✅ global default = " + model_id + applied + verified_note + "\n\n" if ok else "⚠️ save failed\n\n") + kb_text,
                                                parse_mode=None, thread_id=getattr(msg, "message_thread_id", None), metadata=None, reply_markup=kb)
        except Exception:
            pass
        return True
    if flow == "cwx":
        d = dict(p.get("data") or {})

        async def _say_text(text: str, kb=None) -> None:
            sender = getattr(adapter, "_send_control_message", None)
            if callable(sender):
                await sender(chat_id, text, parse_mode=None,
                             thread_id=getattr(msg, "message_thread_id", None),
                             metadata=None, reply_markup=kb)
            else:
                with suppress_exc():
                    await msg.reply_text(text, reply_markup=kb)

        cancel_kb = _kb(adapter, [[("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]])
        raw = (text_in or "").strip()
        if step == "id":
            pid = raw.lower()
            if not _custom_id_ok(pid):
                await _say_text("⚠️ " + _T("cpanel.cust.badid", "invalid id (a-z, 0-9, dash, <=20 chars) — try again"), cancel_kb)
                return True
            if pid in _profiles():
                await _say_text("⚠️ " + _T("cpanel.cust.reserved", "that id is reserved by a native provider — choose another"), cancel_kb)
                return True
            if pid in _custom_providers():
                await _say_text("⚠️ " + _T("cpanel.cust.dup", "id already exists — choose another"), cancel_kb)
                return True
            d["id"] = pid
            _pending_set(chat_id, "cwx", "display", d)
            await _say_text(_cwx_steps_text("display", d), cancel_kb)
            return True
        if step == "display":
            d["display"] = raw[:40] or d.get("id", "custom")
            _pending_set(chat_id, "cwx", "website", d)
            await _say_text(_cwx_steps_text("website", d), cancel_kb)
            return True
        if step == "authhdr":
            hdr = re.sub(r"[^A-Za-z0-9-]", "", raw)[:64]
            if not hdr or not hdr[0].isalpha():
                await _say_text("⚠️ " + _T("cpanel.cust.badheader", "invalid header name (letters/digits/dash, starting with a letter)"), cancel_kb)
                return True
            d["auth"] = "api_key_header"
            d["auth_header"] = hdr
            _pending_set(chat_id, "cwx", "key", d)
            await _say_text(_cwx_steps_text("key", d), cancel_kb)
            return True
        if step in ("website", "webedit"):
            website = "" if raw in ("-", "—") else raw
            ok = (not website) or bool(re.match(r"^https?://[^@/\s]+", website))
            if not ok:
                await _say_text("⚠️ " + _T("cpanel.cust.badurl", "invalid URL — try again or '-' to skip"), cancel_kb)
                return True
            if step == "website":
                d["website"] = website
                _pending_set(chat_id, "cwx", "base", d)
                await _say_text(_cwx_steps_text("base", d), cancel_kb)
            else:
                pid = str(d.get("id") or "")
                meta = dict(_read_config().get("cpanel_providers_meta") or {})
                cur = dict(meta.get(pid) or {})
                cur["website"] = website
                if not cur["website"]:
                    cur.pop("website", None)
                if cur:
                    meta[pid] = cur
                else:
                    meta.pop(pid, None)
                _write_config_key(adapter, "cpanel_providers_meta", meta)
                _pending_pop(chat_id)
                txt, kb2 = screen_custom_detail(adapter, pid)
                await _say_text("✅\n\n" + txt, kb2)
            return True
        if step in ("base", "basedit"):
            ok, why = _custom_url_ok(raw)
            if not ok:
                await _say_text("⚠️ URL: " + why, cancel_kb)
                return True
            url = raw.rstrip("/")
            if step == "base":
                d["base_url"] = url
                _pending_set(chat_id, "cwx", "auth", d)
                rows = [[("🔑 " + _T("cpanel.cust.bearer", "API key (Bearer)"), "hctl:cwx:auth:bearer")],
                        [("🏷 " + _T("cpanel.cust.authhdr", "API key in a custom header (e.g. X-API-Key)"), "hctl:cwx:auth:apihdr")],
                        [("🆓 " + _T("cpanel.cust.noauth", "No auth (open/local endpoint)"), "hctl:cwx:auth:none")],
                        [("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]]
                await _say_text(_cwx_steps_text("auth", d), _kb(adapter, rows))
            else:
                # base URL edit is transactional: probe the NEW endpoint with the stored
                # credential BEFORE touching config; on failure the old config stands.
                pid = str(d.get("id") or "")
                e = dict(_custom_providers().get(pid) or {})
                key_var0 = str(e.get("key_env") or "")
                e["api_key"] = (_env_get(key_var0) if key_var0 else e.get("api_key")) or ""
                e["base_url"] = url
                e.pop("api", None)
                trial = await _probe_entry(e)
                if not trial.get("ok"):
                    head = f"🔴 {trial.get('category', '?')} (HTTP {trial.get('http_status') or '—'})"
                    await _say_text("⚠️ " + _T("cpanel.cwx.editblocked", "Probe failed — existing config kept unchanged:") + f"\n{head}\n" + _probe_note(str(trial.get("category")), False), cancel_kb)
                    return True
                _pending_pop(chat_id)
                _write_config_key(adapter, f"providers.{pid}.api", url)
                txt, kb2 = screen_custom_detail(adapter, pid)
                await _say_text("✅ " + _T("cpanel.cwx.testsaved", "Probe OK — saved.") + "\n\n" + txt, kb2)
            return True
        if step in ("key", "keyedit"):
            key = "" if raw in ("-", "—") else raw
            with suppress_exc():
                if key and hasattr(msg, "delete"):
                    await msg.delete()
            d["key"] = key
            d["key_masked"] = mask_secret(key) if key else "—"
            if step == "keyedit":
                # Key rotation is transactional too: validate the NEW secret with a real
                # probe FIRST — a never-validated secret is never persisted.
                pid = str(d.get("id") or "")
                e = dict(_custom_providers().get(pid) or {})
                e["api_key"] = key
                key_var0 = str(e.get("key_env") or "")
                if not key_var0:
                    e.pop("key_env", None)
                trial = await _probe_entry(e)
                if not trial.get("ok"):
                    head = f"🔴 {trial.get('category', '?')} (HTTP {trial.get('http_status') or '—'})"
                    await _say_text("⚠️ " + _T("cpanel.cwx.keyblocked", "Probe failed — the new key was NOT stored:") + f"\n{head}\n" + _probe_note(str(trial.get("category")), False), cancel_kb)
                    return True
                var = key_var0 or _custom_env_var(pid)
                _env_write(var, key if key else None)
                if key:
                    _write_config_key(adapter, f"providers.{pid}.key_env", var)
                _pending_pop(chat_id)
                txt, kb2 = screen_custom_detail(adapter, pid)
                await _say_text("✅ " + _T("cpanel.cwx.keysaved", "Probe OK — key rotated (stored locally, masked).") + "\n\n" + txt, kb2)
                return True
            base = str(d.get("base_url") or "")
            if _pending_nearby_declared_nodiscovery(d):
                d["models"] = []
                _pending_set(chat_id, "cwx", "confirm", d)
                rows = [[("💾 " + _T("cpanel.save", "Save"), "hctl:cwx:save")],
                        [("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]]
                await _say_text("ℹ️ " + _T("cpanel.cust.discnone", "Model discovery: Not applicable (provider does not expose a model catalog)") + "\n\n" + _cwx_summary(d), _kb(adapter, rows))
                return True
            ids, kindd = discover_models(base, "", key or None)
            d["models"] = ids or []
            if ids:
                _pending_set(chat_id, "cwx", "modelpick", d)
                rows = [[(m[:50], f"hctl:cwx:model:{m[:30]}")] for m in ids[:8]]
                rows.append([("➖ " + _T("cpanel.add.skipmodel", "Skip / set later"), "hctl:cwx:model:-")])
                rows.append([("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")])
                await _say_text(_T("cpanel.cust.pickmodel", "Discovery OK — pick a default model:") + "\n" + _cwx_summary(d), _kb(adapter, rows))
            else:
                _pending_set(chat_id, "cwx", "confirm", d)
                rows = [[("💾 " + _T("cpanel.save", "Save"), "hctl:cwx:save")],
                        [("❌ " + _T("cpanel.cancel", "Cancel"), "hctl:cust:cancel")]]
                await _say_text("⚠️ " + _T("cpanel.cust.nodiscshort", "discovery failed: ") + kindd + "\n\n" + _cwx_summary(d), _kb(adapter, rows))
            return True
        if step == "nameedit":
            pid = str(d.get("id") or "")
            _write_config_key(adapter, f"providers.{pid}.name", raw[:40])
            _pending_pop(chat_id)
            txt, kb2 = screen_custom_detail(adapter, pid)
            await _say_text("✅\n\n" + txt, kb2)
            return True
        return True
    return True

# ── backup/restore ────────────────────────────────────────────────────────────

def _do_backup(tag: str = "") -> Tuple[bool, str]:
    home = _home()
    bdir = home / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    safe_tag = re.sub(r"[^a-z0-9-]", "", (tag or "").lower())
    name = f"cpanel-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}{safe_tag}.tar.gz"
    if safe_tag == "" and (bdir / name).exists():
        name = f"cpanel-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{int(time.time()*1000)%100000:05d}.tar.gz"
    target = bdir / name
    files = ["config.yaml", "auth.json", ".env"]
    try:
        with tarfile.open(target, "w:gz") as tf:
            for f in files:
                src = home / f
                if src.exists():
                    tf.add(src, arcname=f)
        os.chmod(target, 0o600)
        return True, f"backup created: {name} ({target.stat().st_size} bytes)"
    except Exception as exc:
        return False, "backup failed: " + scrub_text(str(exc))

def _do_restore(name: str) -> Tuple[bool, str]:
    home = _home()
    snap = (home / "backups" / name)
    if not snap.exists() or not name.startswith("cpanel-") or not name.endswith(".tar.gz"):
        return False, "snapshot not found"
    ok, _ = _do_backup("-prerestore")  # fresh pre-restore backup first (distinct name)
    try:
        with tarfile.open(snap) as tf:
            for member in tf.getmembers():
                base = os.path.basename(member.name)
                if base not in ("config.yaml", "auth.json", ".env"):
                    continue
                extracted = tf.extractfile(member)
                if not extracted:
                    continue
                dst = home / base
                tmp = dst.with_suffix(dst.suffix + ".cpanel-tmp")
                tmp.write_bytes(extracted.read())
                if base in ("auth.json", ".env"):
                    os.chmod(tmp, 0o600)
                os.replace(tmp, dst)
                if base in ("auth.json", ".env"):
                    os.chmod(dst, 0o600)
        return True, f"restored from {name}"
    except Exception as exc:
        return False, "restore failed: " + scrub_text(str(exc))
