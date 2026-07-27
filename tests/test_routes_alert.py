"""Tests for alert investigation route handlers in src/routes/alert.py."""

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


# ---------------------------------------------------------------------------
# POST /api/alert/investigate
# ---------------------------------------------------------------------------

_VALID_ALERT = {
    "alert_type": "unauthorized_api_call",
    "account_id": "123456789012",
    "alert_text": "Suspicious API call detected",
    "account_name": "test-sandbox",
    "user_arn": "arn:aws:iam::123456789012:user/testuser",
    "event_time": "2026-01-15T10:30:00Z",
    "region": "us-east-1",
}


class TestInvestigateAlert:
    def test_missing_api_key_returns_401(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.alert.get_config",
            lambda: _FakeConfig(alert_api_key="secret-key-123"),
        )
        resp = client.post("/api/alert/investigate", json=_VALID_ALERT)
        assert resp.status_code == 401
        assert "Invalid or missing API key" in resp.json()["detail"]

    def test_wrong_api_key_returns_401(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.alert.get_config",
            lambda: _FakeConfig(alert_api_key="secret-key-123"),
        )
        resp = client.post(
            "/api/alert/investigate",
            json=_VALID_ALERT,
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_unconfigured_endpoint_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.alert.get_config",
            lambda: _FakeConfig(alert_api_key=""),
        )
        resp = client.post(
            "/api/alert/investigate",
            json=_VALID_ALERT,
            headers={"X-API-Key": "anything"},
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]

    def test_valid_request_returns_investigation(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.alert.get_config",
            lambda: _FakeConfig(alert_api_key="secret-key-123"),
        )
        mock_investigate = AsyncMock(
            return_value={
                "should_alert": True,
                "severity": "high",
                "summary": "Suspicious activity found",
                "investigation_log": "Checked account...",
                "duration_seconds": 2.5,
            }
        )
        monkeypatch.setattr("src.routes.alert.run_alert_investigation", mock_investigate)

        resp = client.post(
            "/api/alert/investigate",
            json=_VALID_ALERT,
            headers={"X-API-Key": "secret-key-123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["should_alert"] is True
        assert data["severity"] == "high"
        assert data["summary"] == "Suspicious activity found"
        assert data["investigation_log"] == "Checked account..."
        assert data["duration_seconds"] == 2.5

        # Verify the mock was called with the right arguments
        mock_investigate.assert_called_once_with(
            alert_type="unauthorized_api_call",
            account_id="123456789012",
            alert_text="Suspicious API call detected",
            account_name="test-sandbox",
            user_arn="arn:aws:iam::123456789012:user/testuser",
            event_time="2026-01-15T10:30:00Z",
            region="us-east-1",
            event_details=None,
        )

    def test_investigation_failure_returns_fallback(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.alert.get_config",
            lambda: _FakeConfig(alert_api_key="secret-key-123"),
        )
        mock_investigate = AsyncMock(side_effect=RuntimeError("API call failed"))
        monkeypatch.setattr("src.routes.alert.run_alert_investigation", mock_investigate)

        resp = client.post(
            "/api/alert/investigate",
            json=_VALID_ALERT,
            headers={"X-API-Key": "secret-key-123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # On exception, should_alert defaults to True as a precaution
        assert data["should_alert"] is True
        assert data["severity"] == "medium"
        assert "error" in data["summary"].lower() or "precaution" in data["summary"].lower()

    def test_missing_required_fields_returns_422(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.alert.get_config",
            lambda: _FakeConfig(alert_api_key="secret-key-123"),
        )
        # Missing alert_type, account_id, alert_text
        resp = client.post(
            "/api/alert/investigate",
            json={"account_name": "test"},
            headers={"X-API-Key": "secret-key-123"},
        )
        assert resp.status_code == 422

    def test_event_details_passed_through(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.routes.alert.get_config",
            lambda: _FakeConfig(alert_api_key="secret-key-123"),
        )
        mock_investigate = AsyncMock(
            return_value={
                "should_alert": False,
                "severity": "low",
                "summary": "Benign activity",
                "investigation_log": "",
                "duration_seconds": 1.0,
            }
        )
        monkeypatch.setattr("src.routes.alert.run_alert_investigation", mock_investigate)

        alert_with_details = {
            **_VALID_ALERT,
            "event_details": {"source_ip": "10.0.0.1", "action": "RunInstances"},
        }
        resp = client.post(
            "/api/alert/investigate",
            json=alert_with_details,
            headers={"X-API-Key": "secret-key-123"},
        )
        assert resp.status_code == 200
        # Verify event_details were passed
        call_kwargs = mock_investigate.call_args[1]
        assert call_kwargs["event_details"] == {
            "source_ip": "10.0.0.1",
            "action": "RunInstances",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Minimal fake config for alert route tests."""

    def __init__(self, alert_api_key=""):
        self._alert_api_key = alert_api_key

    def get(self, key, default=""):
        if key == "alert_api_key":
            return self._alert_api_key
        return default
