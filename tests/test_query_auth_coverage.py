"""Coverage tests for auth branches in src/routes/query.py.

Covers _check_user_allowed, _check_group_access, and _raise_no_identity
with various configuration combinations.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.routes.query import (
    _check_group_access,
    _check_user_allowed,
    _raise_no_identity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request():
    """Create a minimal fake Request object with empty headers."""
    return SimpleNamespace(headers={})


def _make_config(allowed_groups="", allowed_users=""):
    """Create a fake config namespace."""
    return SimpleNamespace(
        auth={
            "allowed_groups": allowed_groups,
            "allowed_users": allowed_users,
        }
    )


# ===================================================================
# _raise_no_identity
# ===================================================================


class TestRaiseNoIdentity:
    def test_raises_403(self):
        with pytest.raises(HTTPException) as exc_info:
            _raise_no_identity()
        assert exc_info.value.status_code == 403
        assert "no user identity" in exc_info.value.detail.lower()


# ===================================================================
# _check_group_access
# ===================================================================


class TestCheckGroupAccess:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_groups_configured(self, monkeypatch):
        cfg = _make_config(allowed_groups="")
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        result = await _check_group_access(cfg, "alice@redhat.com")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_user_in_group(self, monkeypatch):
        cfg = _make_config(allowed_groups="rhpds-admins")
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr(
            "src.routes.query._get_user_groups",
            AsyncMock(return_value={"rhpds-admins"}),
        )
        result = await _check_group_access(cfg, "alice@redhat.com")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_via_email_fallback(self, monkeypatch):
        cfg = _make_config(
            allowed_groups="rhpds-admins",
            allowed_users="bob@redhat.com",
        )
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr(
            "src.routes.query._get_user_groups",
            AsyncMock(return_value=set()),  # not in any group
        )
        result = await _check_group_access(cfg, "bob@redhat.com")
        assert result is True

    @pytest.mark.asyncio
    async def test_raises_403_when_not_in_group_or_email(self, monkeypatch):
        cfg = _make_config(
            allowed_groups="rhpds-admins",
            allowed_users="admin@redhat.com",
        )
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr(
            "src.routes.query._get_user_groups",
            AsyncMock(return_value=set()),
        )
        with pytest.raises(HTTPException) as exc_info:
            await _check_group_access(cfg, "hacker@evil.com")
        assert exc_info.value.status_code == 403
        assert "not in an allowed group" in exc_info.value.detail


# ===================================================================
# _check_user_allowed — all branches
# ===================================================================


class TestCheckUserAllowed:
    @pytest.mark.asyncio
    async def test_groups_configured_no_user_raises(self, monkeypatch):
        """Groups configured but no user identity -> 403."""
        cfg = _make_config(allowed_groups="rhpds-admins")
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr("src.routes.query._log_identity_debug", lambda r: None)
        with pytest.raises(HTTPException) as exc_info:
            await _check_user_allowed(_make_request(), None)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_groups_configured_user_allowed(self, monkeypatch):
        """Groups configured and user in allowed group -> pass."""
        cfg = _make_config(allowed_groups="rhpds-admins")
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr("src.routes.query._log_identity_debug", lambda r: None)
        monkeypatch.setattr(
            "src.routes.query._check_group_access",
            AsyncMock(return_value=True),
        )
        # Should not raise
        await _check_user_allowed(_make_request(), "alice@redhat.com")

    @pytest.mark.asyncio
    async def test_no_groups_no_emails_allows_all(self, monkeypatch):
        """No groups, no email list -> allow all."""
        cfg = _make_config(allowed_groups="", allowed_users="")
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr("src.routes.query._log_identity_debug", lambda r: None)
        # Should not raise even with no user
        await _check_user_allowed(_make_request(), None)

    @pytest.mark.asyncio
    async def test_email_only_user_allowed(self, monkeypatch):
        """No groups, email list configured, user in list -> pass."""
        cfg = _make_config(
            allowed_groups="",
            allowed_users="alice@redhat.com,bob@redhat.com",
        )
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr("src.routes.query._log_identity_debug", lambda r: None)
        # Should not raise
        await _check_user_allowed(_make_request(), "alice@redhat.com")

    @pytest.mark.asyncio
    async def test_email_only_user_denied(self, monkeypatch):
        """No groups, email list configured, user NOT in list -> 403."""
        cfg = _make_config(
            allowed_groups="",
            allowed_users="alice@redhat.com",
        )
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr("src.routes.query._log_identity_debug", lambda r: None)
        with pytest.raises(HTTPException) as exc_info:
            await _check_user_allowed(_make_request(), "hacker@evil.com")
        assert exc_info.value.status_code == 403
        assert "not in the allowed users list" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_email_only_no_user_raises(self, monkeypatch):
        """No groups, email list configured, no user -> 403."""
        cfg = _make_config(
            allowed_groups="",
            allowed_users="alice@redhat.com",
        )
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr("src.routes.query._log_identity_debug", lambda r: None)
        with pytest.raises(HTTPException) as exc_info:
            await _check_user_allowed(_make_request(), None)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_whitespace_only_email_list_allows_all(self, monkeypatch):
        """Email list with only whitespace entries -> allow all."""
        cfg = _make_config(allowed_groups="", allowed_users="  ,  , ")
        monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)
        monkeypatch.setattr("src.routes.query._log_identity_debug", lambda r: None)
        # After parsing, the set is empty -> no restriction
        await _check_user_allowed(_make_request(), None)
