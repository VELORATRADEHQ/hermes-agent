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

def _disabled_set(cfg: Dict[str, Any]) -> set:
    raw = ((cfg.get("model") or {}).get("disabled_providers") or [])
    return {str(x).lower() for x in raw if x}

def _provider_rows() -> List[Dict[str, Any]]:
    cfg = _read_config()
    model = cfg.get("model") or {}
    active_provider = str(model.get("provider") or "")
    active_model = str(model.get("default") or "")
    disabled = _disabled_set(cfg)
    rows = []
    for name, prof in sorted(_profiles().items()):
        env_vars = tuple(getattr(prof, "env_vars", ()) or ())
        key_var = env_vars[0] if env_vars else ""
        key_val = _env_get(key_var) if key_var else None
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
    lines += ["", _T("cpanel.prov.legend", "🟢 enabled · ⚪ disabled · ⭐ default · tap a provider for actions")]
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

def screen_models(adapter):
    cfg = _read_config()
    model = cfg.get("model") or {}
    active_m = str(model.get("default") or "")
    pname = str(model.get("provider") or "")
    candidates = _model_candidates(pname)
    lines = [ _T("cpanel.models.title", "🤖 Model Management"), "─" * 26,
              f"{_T('cpanel.models.provider', 'Provider')}: {pname or '—'}",
              f"{_T('cpanel.models.active', 'Active model')}: {active_m or '—'}", "" ]
    kb_rows = []
    if candidates:
        lines.append(_T("cpanel.models.pick", "Tap to set as active (saved to config; no AI call):"))
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
    prof = _profiles().get(provider_name)
    ts = time.time()
    if not prof:
        return {"ok": False, "kind": "unknown_provider", "provider": provider_name, "ts": ts}
    env_vars = tuple(getattr(prof, "env_vars", ()) or ())
    key = _env_get(env_vars[0]) if env_vars else None
    base_url = (getattr(prof, "base_url", "") or "").rstrip("/")
    models_url = getattr(prof, "models_url", "") or (base_url + "/models" if base_url else "")
    if not base_url and not models_url:
        res = {"ok": None, "kind": "unsupported", "reason": "no HTTP models endpoint for this provider", "provider": provider_name, "ts": ts}
        _save_test(_decorate(res)); return res
    host = re.sub(r"^(https?://[^/]+).*$", r"\1", models_url or base_url)
    if env_vars and not key:
        res = {"ok": False, "kind": "no_key", "reason": f"credential {env_vars[0]} not configured", "provider": provider_name, "ts": ts}
        _save_test(_decorate(res)); return res
    headers: Dict[str, str] = {}
    url = models_url
    auth_type = str(getattr(prof, "auth_type", "api_key"))
    pname = provider_name.lower()
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
        elif code in (401, 403):
            res = {"ok": False, "kind": "auth", "http": code, "provider": provider_name, "host": host, "ts": ts}
        elif code == 429:
            res = {"ok": False, "kind": "quota", "http": code, "provider": provider_name, "host": host, "ts": ts, "detail": "RESOURCE_EXHAUSTED / rate limit"}
        elif 500 <= code < 600:
            res = {"ok": False, "kind": "provider_error", "http": code, "provider": provider_name, "host": host, "ts": ts}
        else:
            res = {"ok": False, "kind": "other", "http": code, "provider": provider_name, "host": host, "ts": ts}
    except Exception as exc:
        res = {"ok": False, "kind": "network", "provider": provider_name, "host": host, "ts": ts,
               "detail": scrub_text(type(exc).__name__)}
    decorated = _decorate(res)
    _save_test(decorated)
    return decorated

def _decorate(res: Dict[str, Any]) -> Dict[str, Any]:
    kind = res.get("kind")
    prov = res.get("provider", "?")
    if res.get("ok") is True:
        icon, summary = "🟢", f"{prov}: reachable (HTTP {res.get('http')})"
    elif kind == "auth":
        icon, summary = "🔴", f"{prov}: authentication failed (HTTP {res.get('http')})"
    elif kind == "quota":
        icon, summary = "🔴", f"{prov}: quota/rate limit (HTTP 429)"
    elif kind == "network":
        icon, summary = "🔴", f"{prov}: network failure ({res.get('detail', '')})".strip()
    elif kind == "no_key":
        icon, summary = "🔴", f"{prov}: no credential configured"
    elif kind == "unsupported":
        icon, summary = "—", f"{prov}: connectivity test unsupported for this provider type"
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

def _add_start(adapter):
    profs = list(_profiles().keys())
    rows = []
    labels = {"google": "Google Gemini", "openai": "OpenAI", "anthropic": "Anthropic"}
    for name in profs:
        disp = labels.get(name, name)
        rows.append([(disp, f"hctl:add:pick:{name}")][:1] if False else [(disp, f"hctl:add:pick:{name}")])
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

def _is_admin(adapter, user_id: str) -> bool:
    """Admin = configured DM admin list when present, else any pairing-approved user.

    Uses the native pairing allowlist helper; never weakens the pairing gate
    (unauthorized users are rejected earlier by the callback auth check)."""
    try:
        from gateway.pairing import _configured_allowlist, PairingStore  # native
        allow = _configured_allowlist("telegram")
        if allow:
            return user_id in {str(x) for x in allow}
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
                cfg = _read_config()
                dis = sorted(_disabled_set(cfg))
                nm = arg.lower()
                if arg2 == "off" and nm not in dis:
                    dis.append(nm)
                if arg2 == "on" and nm in dis:
                    dis.remove(nm)
                _write_config_key(adapter, "model.disabled_providers", dis)
                text, kb = screen_provider_detail(adapter, arg)
            elif op == "mkdefault" and arg:
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
                labels = {"google": "Google Gemini", "openai": "OpenAI", "anthropic": "Anthropic"}
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
            if op == "set" and arg:
                _write_config_key(adapter, "model.default", arg)
                text, kb = screen_models(adapter)
                text = f"✅ active model = {arg}\n\n" + text
            elif op == "type":
                _pending_set(chat_id, "models", "type", {})
                text = _T("cpanel.models.typeask", "Send the model id as a normal message now.")
                kb = _back_cancel(adapter, "hctl:models")
            else:
                text, kb = screen_models(adapter)
        elif screen == "test":
            if op == "sel" and arg:
                text, kb = screen_test(adapter, arg)
            elif op == "go" and arg:
                await query.answer(_T("cpanel.test.running", "Testing…"))
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
    p = _pending_get(chat_id)
    if not p:
        return False
    uid = str(getattr(getattr(update, "effective_user", None), "id", ""))
    if not _is_admin(adapter, uid):
        return False
    text_in = msg.text.strip()
    flow, step, data = p["flow"], p["step"], p["data"]

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

    if flow == "models" and step == "type":
        _pending_pop(chat_id)
        model_id = re.sub(r"\s+", "", text_in)[:80]
        try:
            await msg.delete()
        except Exception:
            pass
        ok = _write_config_key(adapter, "model.default", model_id)
        kb_text, kb = screen_models(adapter)
        try:
            await adapter._send_control_message(chat_id, ("✅ active model = " + model_id + "\n\n" if ok else "⚠️ save failed\n\n") + kb_text,
                                                parse_mode=None, thread_id=getattr(msg, "message_thread_id", None), metadata=None, reply_markup=kb)
        except Exception:
            pass
        return True
    return True

# ── backup/restore ────────────────────────────────────────────────────────────

def _do_backup() -> Tuple[bool, str]:
    home = _home()
    bdir = home / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    name = f"cpanel-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}.tar.gz"
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
    ok, _ = _do_backup()  # fresh pre-restore backup first
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
