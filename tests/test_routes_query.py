"""Tests for query route handlers in src/routes/query.py.

Covers GET /api/auth/check and GET /api/reports/{filename}.
POST /api/query (SSE streaming) is intentionally excluded — its streaming
nature makes it complex to test via TestClient.
"""

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


# ---------------------------------------------------------------------------
# GET /api/auth/check
# ---------------------------------------------------------------------------


class TestAuthCheck:
    def test_authorized_user(self, client, monkeypatch):
        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)

        resp = client.get(
            "/api/auth/check",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authorized"] is True
        assert data["user"] == "alice@redhat.com"

    def test_authorized_via_user_header(self, client, monkeypatch):
        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)

        resp = client.get(
            "/api/auth/check",
            headers={"X-Forwarded-User": "bob"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authorized"] is True
        assert data["user"] == "bob"

    def test_email_takes_precedence_over_user(self, client, monkeypatch):
        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)

        resp = client.get(
            "/api/auth/check",
            headers={
                "X-Forwarded-Email": "alice@redhat.com",
                "X-Forwarded-User": "bob",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["user"] == "alice@redhat.com"

    def test_no_identity_no_restrictions(self, client, monkeypatch):
        """When no auth restrictions are configured, anonymous access is allowed."""

        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)

        resp = client.get("/api/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authorized"] is True
        assert data["user"] is None

    def test_forbidden_user(self, client, monkeypatch):
        from fastapi import HTTPException

        async def _deny(*args, **kwargs):
            raise HTTPException(status_code=403, detail="Access denied")

        monkeypatch.setattr("src.routes.query._check_user_allowed", _deny)

        resp = client.get(
            "/api/auth/check",
            headers={"X-Forwarded-Email": "hacker@evil.com"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/reports/{filename}
# ---------------------------------------------------------------------------


class TestDownloadReport:
    def test_report_found(self, client, monkeypatch, tmp_path):
        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)

        # Create a temporary report file
        report_content = "# Test Report\n\nSome content"
        report_file = tmp_path / "test-report.md"
        report_file.write_text(report_content)

        # Point REPORTS_DIR to our temp directory
        monkeypatch.setattr("src.routes.query.REPORTS_DIR", str(tmp_path))

        resp = client.get(
            "/api/reports/test-report.md",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        assert report_content in resp.text

    def test_report_not_found(self, client, monkeypatch, tmp_path):
        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)
        monkeypatch.setattr("src.routes.query.REPORTS_DIR", str(tmp_path))

        resp = client.get(
            "/api/reports/nonexistent.md",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 404
        assert "Report not found" in resp.json()["detail"]

    def test_path_traversal_rejected(self, client, monkeypatch, tmp_path):
        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)
        monkeypatch.setattr("src.routes.query.REPORTS_DIR", str(tmp_path))

        resp = client.get(
            "/api/reports/..%2F..%2Fetc%2Fpasswd",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        # Should be either 400 (invalid filename) or 404 (not found)
        assert resp.status_code in (400, 404)

    def test_adoc_report_media_type(self, client, monkeypatch, tmp_path):
        async def _noop(*args, **kwargs):
            pass

        monkeypatch.setattr("src.routes.query._check_user_allowed", _noop)

        report_file = tmp_path / "report.adoc"
        report_file.write_text("= AsciiDoc Report")

        monkeypatch.setattr("src.routes.query.REPORTS_DIR", str(tmp_path))

        resp = client.get(
            "/api/reports/report.adoc",
            headers={"X-Forwarded-Email": "alice@redhat.com"},
        )
        assert resp.status_code == 200
        assert "asciidoc" in resp.headers.get("content-type", "")

    def test_forbidden_user_on_reports(self, client, monkeypatch):
        from fastapi import HTTPException

        async def _deny(*args, **kwargs):
            raise HTTPException(status_code=403, detail="Access denied")

        monkeypatch.setattr("src.routes.query._check_user_allowed", _deny)

        resp = client.get(
            "/api/reports/test.md",
            headers={"X-Forwarded-Email": "hacker@evil.com"},
        )
        assert resp.status_code == 403
