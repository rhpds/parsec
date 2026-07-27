"""Tests for share route handlers in src/routes/share.py."""

import json
from contextlib import asynccontextmanager

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

    monkeypatch.setattr("src.routes.share._check_user_allowed", _noop)


_TEST_SHARE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

_TEST_SHARE_DATA = {
    "id": _TEST_SHARE_ID,
    "shared_by": "alice@redhat.com",
    "title": "Shared investigation",
    "created_at": "2026-01-15T10:00:00+00:00",
    "messages": [
        {"role": "user", "content": "What costs the most?"},
        {"role": "assistant", "content": "EC2 in us-east-1."},
    ],
}


# ---------------------------------------------------------------------------
# POST /api/share
# ---------------------------------------------------------------------------


class TestCreateShare:
    def test_create_share_with_title(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        resp = client.post(
            "/api/share",
            json={
                "title": "My investigation",
                "messages": [
                    {"role": "user", "content": "What costs the most?"},
                    {"role": "assistant", "content": "EC2 in us-east-1."},
                ],
            },
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "url" in data
        assert data["id"] in data["url"]
        assert "?share=" in data["url"]

        # Verify file was written
        share_file = tmp_path / f"{data['id']}.json"
        assert share_file.exists()
        saved = json.loads(share_file.read_text())
        assert saved["title"] == "My investigation"
        assert saved["shared_by"] == "alice@redhat.com"
        assert len(saved["messages"]) == 2

    def test_create_share_auto_title(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        resp = client.post(
            "/api/share",
            json={
                "messages": [
                    {"role": "user", "content": "How much did sandbox ABC cost?"},
                ],
            },
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        # Check the file has the auto-generated title
        data = resp.json()
        share_file = tmp_path / f"{data['id']}.json"
        saved = json.loads(share_file.read_text())
        assert saved["title"] == "How much did sandbox ABC cost?"

    def test_create_share_anonymous(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        resp = client.post(
            "/api/share",
            json={
                "messages": [{"role": "user", "content": "Question"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        share_file = tmp_path / f"{data['id']}.json"
        saved = json.loads(share_file.read_text())
        assert saved["shared_by"] == "anonymous"

    def test_create_share_empty_messages(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        resp = client.post(
            "/api/share",
            json={"messages": []},
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        share_file = tmp_path / f"{data['id']}.json"
        saved = json.loads(share_file.read_text())
        assert saved["title"] == "Shared investigation"  # fallback title

    def test_create_share_missing_messages_returns_422(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        resp = client.post(
            "/api/share",
            json={"title": "No messages"},
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/share/{share_id}
# ---------------------------------------------------------------------------


class TestGetShare:
    def test_get_existing_share(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        fpath = tmp_path / f"{_TEST_SHARE_ID}.json"
        fpath.write_text(json.dumps(_TEST_SHARE_DATA))

        resp = client.get(
            f"/api/share/{_TEST_SHARE_ID}",
            headers={"X-Forwarded-Email": "bob@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == _TEST_SHARE_ID
        assert data["title"] == "Shared investigation"
        assert len(data["messages"]) == 2
        assert data["shared_by"] == "alice@redhat.com"

    def test_get_nonexistent_share_returns_404(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        resp = client.get(
            f"/api/share/{_TEST_SHARE_ID}",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_get_share_invalid_id_returns_422(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        resp = client.get(
            "/api/share/not-a-valid-uuid",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 422
        assert "Invalid share ID" in resp.json()["detail"]

    def test_share_readable_by_anyone(self, client, monkeypatch, tmp_path):
        """Shares are public read-only — anyone can view them."""
        monkeypatch.setattr("src.routes.share.SHARES_DIR", str(tmp_path))

        fpath = tmp_path / f"{_TEST_SHARE_ID}.json"
        fpath.write_text(json.dumps(_TEST_SHARE_DATA))

        # Different user from the creator
        resp = client.get(
            f"/api/share/{_TEST_SHARE_ID}",
            headers={"X-Forwarded-Email": "stranger@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == _TEST_SHARE_ID
