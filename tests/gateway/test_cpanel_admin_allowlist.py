"""Regression tests for Control Panel admin identity (``gateway.cpanel._is_admin``).

Background: ``gateway.pairing._configured_allowlist`` returns ``(env_var, ids)``. The
previous cpanel implementation iterated the *tuple itself* and compared the user id
against ``str()`` of its elements, so with TELEGRAM_ALLOWED_USERS configured (the
documented single-admin setup) NO user was ever recognized as admin: `/panel`, settings
and panel-driven model selection all answered "⛔ You are not authorized". The legacy
test fixture masked this by mocking the helper to return a bare list (old shape).

These tests therefore exercise the REAL pairing helper (no shape-mocking) plus the
canonical ``allow_admin_from`` config key shared with ``gateway.slash_access``.
"""
from __future__ import annotations

import gateway.cpanel as cp


def _no_config_allow_admin(monkeypatch):
    """Ensure no allow_admin_from config leaks into the fallback-path tests."""
    monkeypatch.setattr(cp, "_read_config", lambda: {})


class TestEnvAllowlistFallback:
    """No allow_admin_from: admin = TELEGRAM_ALLOWED_USERS ids (real helper)."""

    def test_listed_user_is_admin(self, monkeypatch):
        _no_config_allow_admin(monkeypatch)
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "449106486")
        assert cp._is_admin(None, "449106486") is True

    def test_unlisted_user_not_admin(self, monkeypatch):
        _no_config_allow_admin(monkeypatch)
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "449106486")
        assert cp._is_admin(None, "999999999") is False

    def test_comma_allowlist(self, monkeypatch):
        _no_config_allow_admin(monkeypatch)
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111, 222 ,333")
        assert cp._is_admin(None, "222") is True
        assert cp._is_admin(None, "444") is False

    def test_real_helper_contract_tuple(self, monkeypatch):
        """Contract guard: the fix must unpack (env_var, ids) from the REAL helper."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "7")
        from gateway.pairing import _configured_allowlist
        configured = _configured_allowlist("telegram")
        assert isinstance(configured, tuple) and len(configured) == 2
        env_var, ids = configured
        assert env_var == "TELEGRAM_ALLOWED_USERS"
        assert list(ids) == ["7"]


class TestAllowAdminFromCanonical:
    """allow_admin_from (platform extra) is the canonical admin list, same key as
    gateway.slash_access — it must take precedence over the env fallback."""

    def _cfg(self, extra):
        return {"gateway": {"platforms": {"telegram": {"extra": extra}}}}

    def test_admin_from_config(self, monkeypatch):
        monkeypatch.setattr(cp, "_read_config", lambda: self._cfg({"allow_admin_from": [12345]}))
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        assert cp._is_admin(None, "12345") is True
        assert cp._is_admin(None, "999") is False

    def test_config_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setattr(cp, "_read_config", lambda: self._cfg({"allow_admin_from": "12345"}))
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999")
        assert cp._is_admin(None, "999") is False  # env-only user is NOT admin anymore
        assert cp._is_admin(None, "12345") is True

    def test_empty_list_fail_closed(self, monkeypatch):
        monkeypatch.setattr(cp, "_read_config", lambda: self._cfg({"allow_admin_from": []}))
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999")
        assert cp._is_admin(None, "999") is False
        assert cp._is_admin(None, "12345") is False

    def test_root_bridged_key(self, monkeypatch):
        cfg = {"gateway": {"platforms": {"telegram": {"allow_admin_from": 555}}}}
        monkeypatch.setattr(cp, "_read_config", lambda: cfg)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        assert cp._is_admin(None, "555") is True

    def test_none_config_falls_back_to_pairing(self, monkeypatch):
        monkeypatch.setattr(cp, "_read_config", lambda: {})
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        import gateway.pairing as gp

        class _Store:
            def is_approved(self, platform, uid):
                return platform == "telegram" and uid == "abc"

        monkeypatch.setattr(gp, "PairingStore", lambda: _Store())
        assert cp._is_admin(None, "abc") is True
        assert cp._is_admin(None, "xyz") is False
