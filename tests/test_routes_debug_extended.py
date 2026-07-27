"""Extended tests for src/routes/debug.py — covers diagnosis helpers
and route handlers with mocked AAP2 backends."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
    from contextlib import asynccontextmanager

    from src.app import app

    @asynccontextmanager
    async def _noop_lifespan(app_):
        yield

    app.router.lifespan_context = _noop_lifespan
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# _fetch_project_info_safe
# ---------------------------------------------------------------------------


class TestFetchProjectInfoSafe:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_project_id(self):
        from src.routes.debug import _fetch_project_info_safe

        result = await _fetch_project_info_safe("east", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_project_id_zero(self):
        from src.routes.debug import _fetch_project_info_safe

        result = await _fetch_project_info_safe("east", 0)
        assert result is None

    @pytest.mark.asyncio
    @patch("src.routes.debug.fetch_project_info", new_callable=AsyncMock)
    async def test_returns_project_info(self, mock_fetch):
        from src.routes.debug import _fetch_project_info_safe

        mock_fetch.return_value = {"name": "Test Project", "scmUrl": "https://github.com/org/repo"}
        result = await _fetch_project_info_safe("east", 42)
        assert result is not None
        assert result["name"] == "Test Project"
        mock_fetch.assert_called_once_with("east", 42)

    @pytest.mark.asyncio
    @patch("src.routes.debug.fetch_project_info", new_callable=AsyncMock)
    async def test_returns_none_on_exception(self, mock_fetch):
        from src.routes.debug import _fetch_project_info_safe

        mock_fetch.side_effect = Exception("Connection refused")
        result = await _fetch_project_info_safe("east", 42)
        assert result is None


# ---------------------------------------------------------------------------
# _diagnose_failed_job
# ---------------------------------------------------------------------------


class TestDiagnoseFailedJob:
    @pytest.mark.asyncio
    @patch("src.routes.debug.fetch_job_stdout", new_callable=AsyncMock)
    async def test_no_stdout_does_nothing(self, mock_stdout):
        from src.routes.debug import _diagnose_failed_job

        mock_stdout.return_value = ""
        result: dict = {"failingTask": None, "fix": None}
        metadata = {"extraVars": {}, "jobTemplateName": "test-template"}
        await _diagnose_failed_job("east", 123, metadata, result)
        assert result["failingTask"] is None
        assert result["fix"] is None

    @pytest.mark.asyncio
    @patch("src.routes.debug.recommend_fix", new_callable=AsyncMock)
    @patch("src.routes.debug.extract_failing_task")
    @patch("src.routes.debug.fetch_job_stdout", new_callable=AsyncMock)
    async def test_no_failing_task_does_nothing(self, mock_stdout, mock_extract, mock_fix):
        from src.routes.debug import _diagnose_failed_job

        mock_stdout.return_value = "PLAY [test]\nTASK [ok]\nPLAY RECAP"
        mock_extract.return_value = None
        result: dict = {"failingTask": None, "fix": None}
        metadata = {"extraVars": {}, "jobTemplateName": "test"}
        await _diagnose_failed_job("east", 123, metadata, result)
        assert result["failingTask"] is None
        mock_fix.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.routes.debug.recommend_fix", new_callable=AsyncMock)
    @patch("src.routes.debug.extract_failing_task")
    @patch("src.routes.debug.fetch_job_stdout", new_callable=AsyncMock)
    async def test_failing_task_with_fix(self, mock_stdout, mock_extract, mock_fix):
        from src.routes.debug import _diagnose_failed_job

        mock_stdout.return_value = "PLAY [test]\nfatal: [host]\nPLAY RECAP"
        failing = {"taskName": "Deploy", "errorMessage": "timed out"}
        mock_extract.return_value = failing
        mock_fix.return_value = {
            "source": "pattern",
            "explanation": "Timeout issue",
        }
        result: dict = {"failingTask": None, "fix": None}
        metadata = {"extraVars": {"ACTION": "provision"}, "jobTemplateName": "RHPDS test"}
        await _diagnose_failed_job("east", 123, metadata, result)
        assert result["failingTask"] == failing
        assert result["fix"]["source"] == "pattern"

    @pytest.mark.asyncio
    @patch("src.routes.debug.recommend_fix", new_callable=AsyncMock)
    @patch("src.routes.debug.extract_failing_task")
    @patch("src.routes.debug.fetch_job_stdout", new_callable=AsyncMock)
    async def test_failing_task_no_fix(self, mock_stdout, mock_extract, mock_fix):
        from src.routes.debug import _diagnose_failed_job

        mock_stdout.return_value = "PLAY [test]\nfatal: [host]\nPLAY RECAP"
        mock_extract.return_value = {"taskName": "Deploy", "errorMessage": "unknown"}
        mock_fix.return_value = None
        result: dict = {"failingTask": None, "fix": None}
        metadata = {"extraVars": {}}
        await _diagnose_failed_job("east", 123, metadata, result)
        assert result["failingTask"] is not None
        assert result["fix"] is None


# ---------------------------------------------------------------------------
# _diagnose_error_job
# ---------------------------------------------------------------------------


class TestDiagnoseErrorJob:
    @pytest.mark.asyncio
    async def test_pattern_match_on_job_explanation(self):
        from src.routes.debug import _diagnose_error_job

        metadata = {
            "jobExplanation": "Failed to JSON parse a line from worker stream",
            "executionEnvironment": None,
        }
        result: dict = {"fix": None, "eeInfo": None}
        await _diagnose_error_job("east", metadata, result)
        assert result["fix"] is not None
        assert result["fix"]["source"] == "pattern"

    @pytest.mark.asyncio
    async def test_no_job_explanation_no_ee(self):
        from src.routes.debug import _diagnose_error_job

        metadata = {
            "jobExplanation": "",
            "executionEnvironment": None,
        }
        result: dict = {"fix": None, "eeInfo": None}
        await _diagnose_error_job("east", metadata, result)
        assert result["fix"] is None
        assert result["eeInfo"] is None

    @pytest.mark.asyncio
    @patch("src.routes.debug.fetch_ee_info", new_callable=AsyncMock)
    async def test_ee_inspection_on_error(self, mock_ee):
        from src.routes.debug import _diagnose_error_job

        mock_ee.return_value = {"image": "quay.io/test/ee:latest", "id": 5}
        metadata = {
            "jobExplanation": "",
            "executionEnvironment": 5,
        }
        result: dict = {"fix": None, "eeInfo": None}
        await _diagnose_error_job("east", metadata, result)
        assert result["eeInfo"] is not None
        assert result["eeInfo"]["id"] == 5

    @pytest.mark.asyncio
    @patch("src.routes.debug.fetch_ee_info", new_callable=AsyncMock)
    async def test_ee_inspection_exception(self, mock_ee):
        from src.routes.debug import _diagnose_error_job

        mock_ee.side_effect = Exception("EE not found")
        metadata = {
            "jobExplanation": "",
            "executionEnvironment": 5,
        }
        result: dict = {"fix": None, "eeInfo": None}
        await _diagnose_error_job("east", metadata, result)
        assert result["eeInfo"] is None

    @pytest.mark.asyncio
    async def test_no_explanation_match(self):
        from src.routes.debug import _diagnose_error_job

        metadata = {
            "jobExplanation": "Some unmatched explanation text",
            "executionEnvironment": None,
        }
        result: dict = {"fix": None, "eeInfo": None}
        await _diagnose_error_job("east", metadata, result)
        assert result["fix"] is None


# ---------------------------------------------------------------------------
# Route endpoint tests
# ---------------------------------------------------------------------------


class TestDiagnoseEndpoint:
    @patch("src.routes.debug.fetch_job_metadata", new_callable=AsyncMock)
    @patch("src.routes.debug.find_controller_for_url")
    @patch("src.routes.debug.parse_job_url")
    def test_invalid_url_returns_400(self, mock_parse, mock_find, mock_meta, client):
        mock_parse.side_effect = ValueError("Invalid job URL format")
        resp = client.post("/api/debug/diagnose", json={"url": "not-a-url"})
        assert resp.status_code == 400

    @patch("src.routes.debug.fetch_job_metadata", new_callable=AsyncMock)
    @patch("src.routes.debug.find_controller_for_url")
    @patch("src.routes.debug.parse_job_url")
    def test_controller_not_found_returns_404(self, mock_parse, mock_find, mock_meta, client):
        mock_parse.return_value = ("https://unknown.controller.example.com", 123)
        mock_find.side_effect = LookupError("Controller not found")
        resp = client.post(
            "/api/debug/diagnose", json={"url": "https://unknown.example.com/#/jobs/playbook/123"}
        )
        assert resp.status_code == 404

    @patch("src.routes.debug._fetch_project_info_safe", new_callable=AsyncMock)
    @patch("src.routes.debug.fetch_job_metadata", new_callable=AsyncMock)
    @patch("src.routes.debug.find_controller_for_url")
    @patch("src.routes.debug.parse_job_url")
    def test_successful_diagnosis(self, mock_parse, mock_find, mock_meta, mock_proj, client):
        mock_parse.return_value = ("https://east.example.com", 123)
        mock_find.return_value = "east"
        mock_meta.return_value = {
            "status": "successful",
            "projectId": None,
            "extraVars": {},
            "jobTemplateName": "test",
            "jobExplanation": "",
            "executionEnvironment": None,
        }
        mock_proj.return_value = None
        resp = client.post(
            "/api/debug/diagnose",
            json={"url": "https://east.example.com/#/jobs/playbook/123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["metadata"]["status"] == "successful"


class TestCorrelationEndpoint:
    @patch("src.routes.debug.find_controller_for_url")
    @patch("src.routes.debug.parse_job_url")
    def test_invalid_url_returns_400(self, mock_parse, mock_find, client):
        mock_parse.side_effect = ValueError("Bad URL")
        resp = client.post(
            "/api/debug/correlation",
            json={"url": "bad", "job_id": 1},
        )
        assert resp.status_code == 400

    @patch("src.routes.debug.fetch_correlation", new_callable=AsyncMock)
    @patch("src.routes.debug.find_controller_for_url")
    @patch("src.routes.debug.parse_job_url")
    def test_successful_correlation(self, mock_parse, mock_find, mock_corr, client):
        mock_parse.return_value = ("https://east.example.com", 123)
        mock_find.return_value = "east"
        mock_corr.return_value = {"relatedJobs": [], "jobTemplate": {}}
        resp = client.post(
            "/api/debug/correlation",
            json={"url": "https://east.example.com/#/jobs/playbook/123", "job_id": 123},
        )
        assert resp.status_code == 200


class TestEEEndpoint:
    @patch("src.routes.debug.find_controller_for_url")
    @patch("src.routes.debug.parse_job_url")
    def test_invalid_url_returns_400(self, mock_parse, mock_find, client):
        mock_parse.side_effect = ValueError("Bad URL")
        resp = client.post(
            "/api/debug/ee",
            json={"url": "bad", "job_id": 1, "ee_id": 5},
        )
        assert resp.status_code == 400

    @patch("src.routes.debug.fetch_ee_info", new_callable=AsyncMock)
    @patch("src.routes.debug.find_controller_for_url")
    @patch("src.routes.debug.parse_job_url")
    def test_successful_ee_fetch(self, mock_parse, mock_find, mock_ee, client):
        mock_parse.return_value = ("https://east.example.com", 123)
        mock_find.return_value = "east"
        mock_ee.return_value = {"image": "quay.io/test/ee:latest", "id": 5}
        resp = client.post(
            "/api/debug/ee",
            json={"url": "https://east.example.com/#/jobs/playbook/123", "job_id": 123, "ee_id": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 5
