"""Tests for health check route handlers in src/routes/health.py."""


import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _patch_lifespan(monkeypatch):
    """Disable the app lifespan to avoid connecting to real services."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        yield

    monkeypatch.setattr("src.app.lifespan", _noop_lifespan)


@pytest.fixture()
def client(_patch_lifespan):
    """Create a TestClient with patched lifespan."""
    # Import app AFTER lifespan is patched
    # Override the lifespan on the app instance directly
    from contextlib import asynccontextmanager

    from src.app import app

    @asynccontextmanager
    async def _noop_lifespan(app_):
        yield

    app.router.lifespan_context = _noop_lifespan
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_returns_200_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# GET /api/health/ready
# ---------------------------------------------------------------------------


class TestReadinessEndpoint:
    def test_ready_when_mcp_not_configured(self, client, monkeypatch):
        monkeypatch.setattr("src.routes.health.reporting_mcp.get_mcp_url", lambda: "")
        resp = client.get("/api/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["db"] == "reporting_mcp_not_configured"

    def test_ready_when_instructions_available(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_mcp_url",
            lambda: "http://localhost:8081/mcp",
        )
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_server_instructions",
            lambda: "Some instructions",
        )
        resp = client.get("/api/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["db"] == "via_reporting_mcp"

    def test_ready_when_tools_available(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_mcp_url",
            lambda: "http://localhost:8081/mcp",
        )
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_server_instructions",
            lambda: "",
        )
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_mcp_tools",
            lambda: [{"name": "query"}],
        )
        resp = client.get("/api/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["db"] == "via_reporting_mcp"

    def test_not_ready_when_mcp_uninitialized(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_mcp_url",
            lambda: "http://localhost:8081/mcp",
        )
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_server_instructions",
            lambda: "",
        )
        monkeypatch.setattr(
            "src.routes.health.reporting_mcp.get_mcp_tools",
            lambda: [],
        )
        resp = client.get("/api/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["db"] == "reporting_mcp_not_initialized"
