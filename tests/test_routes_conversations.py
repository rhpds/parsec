"""Tests for conversation route handlers in src/routes/conversations.py."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

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

    monkeypatch.setattr("src.routes.conversations._check_user_allowed", _noop)


@pytest.fixture(autouse=True)
def _suppress_background_learn(monkeypatch):
    """Prevent background learning analysis from running during tests."""

    async def _noop_learn(messages):
        pass

    monkeypatch.setattr("src.routes.conversations._background_learn", _noop_learn)


_TEST_CONV_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

_TEST_CONV_DATA = {
    "id": _TEST_CONV_ID,
    "owner": "alice@redhat.com",
    "title": "Test conversation",
    "created_at": "2026-01-15T10:00:00+00:00",
    "updated_at": "2026-01-15T10:30:00+00:00",
    "messages": [
        {"role": "user", "content": "How much did account 123 spend?"},
        {"role": "assistant", "content": "The account spent $500."},
    ],
}


# ---------------------------------------------------------------------------
# POST /api/conversations
# ---------------------------------------------------------------------------


class TestSaveConversation:
    def test_save_new_conversation(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.post(
            "/api/conversations",
            json={
                "id": _TEST_CONV_ID,
                "title": "My investigation",
                "messages": [{"role": "user", "content": "What costs the most?"}],
            },
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == _TEST_CONV_ID
        assert data["title"] == "My investigation"

        # Verify file was written
        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        assert fpath.exists()
        saved = json.loads(fpath.read_text())
        assert saved["owner"] == "alice@redhat.com"
        assert len(saved["messages"]) == 1

    def test_save_generates_uuid_when_no_id(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.post(
            "/api/conversations",
            json={
                "messages": [{"role": "user", "content": "Generate an ID for me"}],
            },
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should have a valid UUID
        assert len(data["id"]) == 36
        assert data["id"].count("-") == 4

    def test_save_auto_generates_title(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.post(
            "/api/conversations",
            json={
                "id": _TEST_CONV_ID,
                "messages": [{"role": "user", "content": "How much did account 123 spend?"}],
            },
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "How much did account 123 spend?"

    def test_save_invalid_id_returns_422(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.post(
            "/api/conversations",
            json={
                "id": "not-a-valid-uuid",
                "messages": [{"role": "user", "content": "test"}],
            },
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 422
        assert "Invalid conversation ID" in resp.json()["detail"]

    def test_update_existing_preserves_created_at(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        # Pre-create the file
        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        fpath.write_text(json.dumps(_TEST_CONV_DATA))

        resp = client.post(
            "/api/conversations",
            json={
                "id": _TEST_CONV_ID,
                "messages": [
                    {"role": "user", "content": "Updated question"},
                    {"role": "assistant", "content": "Updated answer"},
                ],
            },
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200

        updated = json.loads(fpath.read_text())
        assert updated["created_at"] == _TEST_CONV_DATA["created_at"]
        assert updated["updated_at"] != _TEST_CONV_DATA["updated_at"]

    def test_update_wrong_owner_returns_403(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        # Pre-create the file owned by alice
        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        fpath.write_text(json.dumps(_TEST_CONV_DATA))

        resp = client.post(
            "/api/conversations",
            json={
                "id": _TEST_CONV_ID,
                "messages": [{"role": "user", "content": "Evil update"}],
            },
            headers={"X-Forwarded-Email": "bob@redhat.com"},
        )
        assert resp.status_code == 403
        assert "Not your conversation" in resp.json()["detail"]

    def test_anonymous_owner_when_no_header(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.post(
            "/api/conversations",
            json={
                "id": _TEST_CONV_ID,
                "messages": [{"role": "user", "content": "anonymous question"}],
            },
        )
        assert resp.status_code == 200

        saved = json.loads((tmp_path / f"{_TEST_CONV_ID}.json").read_text())
        assert saved["owner"] == "anonymous"


# ---------------------------------------------------------------------------
# GET /api/conversations
# ---------------------------------------------------------------------------


class TestListConversations:
    def test_list_own_conversations(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        # Write two conversations owned by alice, one by bob
        for i, owner in enumerate(["alice@redhat.com", "alice@redhat.com", "bob@redhat.com"]):
            conv = {
                "id": f"aaaaaaaa-0000-0000-0000-{i:012d}",
                "owner": owner,
                "title": f"Conv {i}",
                "created_at": f"2026-01-{15+i:02d}T10:00:00+00:00",
                "updated_at": f"2026-01-{15+i:02d}T10:30:00+00:00",
                "messages": [{"role": "user", "content": f"Q{i}"}],
            }
            fpath = tmp_path / f"{conv['id']}.json"
            fpath.write_text(json.dumps(conv))

        resp = client.get(
            "/api/conversations",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["conversations"]) == 2

    def test_list_empty_directory(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.get(
            "/api/conversations",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["conversations"] == []

    def test_list_nonexistent_directory(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.conversations.CONVERSATIONS_DIR",
            "/tmp/parsec-test-nonexistent-dir",
        )

        resp = client.get(
            "/api/conversations",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["conversations"] == []

    def test_all_users_requires_admin(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.routes.conversations.is_admin_user_async",
            AsyncMock(return_value=False),
        )

        resp = client.get(
            "/api/conversations?all_users=true",
            headers={"X-Forwarded-Email": "user@redhat.com"},
        )
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]

    def test_all_users_admin_sees_all(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.routes.conversations.is_admin_user_async",
            AsyncMock(return_value=True),
        )

        # Write one conv by alice, one by bob
        for i, owner in enumerate(["alice@redhat.com", "bob@redhat.com"]):
            conv = {
                "id": f"aaaaaaaa-0000-0000-0000-{i:012d}",
                "owner": owner,
                "title": f"Conv {i}",
                "created_at": f"2026-01-{15+i:02d}T10:00:00+00:00",
                "updated_at": f"2026-01-{15+i:02d}T10:30:00+00:00",
                "messages": [{"role": "user", "content": f"Q{i}"}],
            }
            (tmp_path / f"{conv['id']}.json").write_text(json.dumps(conv))

        resp = client.get(
            "/api/conversations?all_users=true",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["conversations"]) == 2
        # Admin mode includes owner field
        assert all("owner" in c for c in data["conversations"])

    def test_conversations_sorted_by_updated_at(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        for i in range(3):
            conv = {
                "id": f"aaaaaaaa-0000-0000-0000-{i:012d}",
                "owner": "alice@redhat.com",
                "title": f"Conv {i}",
                "created_at": f"2026-01-{15+i:02d}T10:00:00+00:00",
                "updated_at": f"2026-01-{15+i:02d}T10:00:00+00:00",
                "messages": [{"role": "user", "content": f"Q{i}"}],
            }
            (tmp_path / f"{conv['id']}.json").write_text(json.dumps(conv))

        resp = client.get(
            "/api/conversations",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        convs = resp.json()["conversations"]
        # Most recently updated first
        dates = [c["updated_at"] for c in convs]
        assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# GET /api/conversations/export
# ---------------------------------------------------------------------------


class TestExportConversations:
    def test_admin_can_export(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.routes.conversations.is_admin_user_async",
            AsyncMock(return_value=True),
        )

        conv = {
            "id": _TEST_CONV_ID,
            "owner": "alice@redhat.com",
            "title": "Test",
            "created_at": "2026-01-15T10:00:00+00:00",
            "updated_at": "2026-01-15T10:30:00+00:00",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        (tmp_path / f"{_TEST_CONV_ID}.json").write_text(json.dumps(conv))

        resp = client.get(
            "/api/conversations/export",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["conversations"]) == 1
        # Export includes full messages
        assert "messages" in data["conversations"][0]

    def test_non_admin_cannot_export(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.routes.conversations.is_admin_user_async",
            AsyncMock(return_value=False),
        )

        resp = client.get(
            "/api/conversations/export",
            headers={"X-Forwarded-Email": "user@redhat.com"},
        )
        assert resp.status_code == 403

    def test_export_empty_dir(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.routes.conversations.is_admin_user_async",
            AsyncMock(return_value=True),
        )

        resp = client.get(
            "/api/conversations/export",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["conversations"] == []


# ---------------------------------------------------------------------------
# GET /api/conversations/{conv_id}
# ---------------------------------------------------------------------------


class TestGetConversation:
    def test_get_own_conversation(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        fpath.write_text(json.dumps(_TEST_CONV_DATA))

        resp = client.get(
            f"/api/conversations/{_TEST_CONV_ID}",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == _TEST_CONV_ID
        assert data["title"] == "Test conversation"
        assert len(data["messages"]) == 2

    def test_get_nonexistent_returns_404(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.get(
            f"/api/conversations/{_TEST_CONV_ID}",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_get_invalid_id_returns_422(self, client, monkeypatch):
        resp = client.get(
            "/api/conversations/not-a-uuid",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 422
        assert "Invalid conversation ID" in resp.json()["detail"]

    def test_get_other_users_conversation_returns_403(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.routes.conversations.is_admin_user_async",
            AsyncMock(return_value=False),
        )

        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        fpath.write_text(json.dumps(_TEST_CONV_DATA))

        resp = client.get(
            f"/api/conversations/{_TEST_CONV_ID}",
            headers={"X-Forwarded-Email": "bob@redhat.com"},
        )
        assert resp.status_code == 403
        assert "Not your conversation" in resp.json()["detail"]

    def test_admin_can_get_others_conversation(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.routes.conversations.is_admin_user_async",
            AsyncMock(return_value=True),
        )

        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        fpath.write_text(json.dumps(_TEST_CONV_DATA))

        resp = client.get(
            f"/api/conversations/{_TEST_CONV_ID}",
            headers={"X-Forwarded-Email": "admin@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == _TEST_CONV_ID


# ---------------------------------------------------------------------------
# DELETE /api/conversations/{conv_id}
# ---------------------------------------------------------------------------


class TestDeleteConversation:
    def test_delete_own_conversation(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        fpath.write_text(json.dumps(_TEST_CONV_DATA))

        resp = client.delete(
            f"/api/conversations/{_TEST_CONV_ID}",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert not fpath.exists()

    def test_delete_nonexistent_returns_404(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        resp = client.delete(
            f"/api/conversations/{_TEST_CONV_ID}",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 404

    def test_delete_invalid_id_returns_422(self, client, monkeypatch):
        resp = client.delete(
            "/api/conversations/bad-id",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 422

    def test_delete_other_users_conversation_returns_403(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("src.routes.conversations.CONVERSATIONS_DIR", str(tmp_path))

        fpath = tmp_path / f"{_TEST_CONV_ID}.json"
        fpath.write_text(json.dumps(_TEST_CONV_DATA))

        resp = client.delete(
            f"/api/conversations/{_TEST_CONV_ID}",
            headers={"X-Forwarded-Email": "bob@redhat.com"},
        )
        assert resp.status_code == 403
        assert "Not your conversation" in resp.json()["detail"]
        # File should still exist
        assert fpath.exists()
