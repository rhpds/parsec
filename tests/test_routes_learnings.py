"""Tests for learnings admin route handlers in src/routes/learnings.py."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _patch_lifespan():
    """Disable the app lifespan to avoid connecting to real services."""
    from fastapi import FastAPI

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        yield

    import src.app

    src.app.app.router.lifespan_context = _noop_lifespan


@pytest.fixture()
def client():
    from src.app import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch):
    """Bypass _check_user_allowed for all tests in this module."""

    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr("src.routes.learnings._check_user_allowed", _noop)


# ---------------------------------------------------------------------------
# GET /api/learnings
# ---------------------------------------------------------------------------


class TestGetLearnings:
    def test_returns_learnings_for_admin(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "src.routes.learnings.get_learnings",
            lambda: "- Learning one\n- Learning two\n",
        )

        resp = client.get(
            "/api/learnings",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_learnings"] is True
        assert "Learning one" in data["content"]

    def test_empty_learnings(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr("src.routes.learnings.get_learnings", lambda: "")

        resp = client.get(
            "/api/learnings",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_learnings"] is False
        assert data["content"] == ""

    def test_whitespace_only_learnings_is_not_has_learnings(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr("src.routes.learnings.get_learnings", lambda: "   \n  \n")

        resp = client.get(
            "/api/learnings",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["has_learnings"] is False

    def test_non_admin_gets_403(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=False),
        )

        resp = client.get(
            "/api/learnings",
            headers={"X-Forwarded-Email": "user@redhat.com"},
        )
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /api/learnings
# ---------------------------------------------------------------------------


class TestDeleteLearnings:
    def test_clears_learnings_for_admin(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=True),
        )
        mock_clear = MagicMock()
        monkeypatch.setattr("src.routes.learnings.clear_learnings", mock_clear)

        resp = client.delete(
            "/api/learnings",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        mock_clear.assert_called_once()

    def test_non_admin_gets_403(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=False),
        )

        resp = client.delete(
            "/api/learnings",
            headers={"X-Forwarded-Email": "user@redhat.com"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/learnings/check
# ---------------------------------------------------------------------------


class TestCheckLearningsAdmin:
    def test_admin_returns_true(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=True),
        )

        resp = client.get(
            "/api/learnings/check",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True

    def test_non_admin_returns_false(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=False),
        )

        resp = client.get(
            "/api/learnings/check",
            headers={"X-Forwarded-Email": "user@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is False

    def test_no_email_header(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.learnings.is_admin_user_async",
            AsyncMock(return_value=False),
        )

        resp = client.get("/api/learnings/check")
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is False
