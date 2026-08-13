"""Extended tests for src/tools/aap2_fix.py — covers AI analysis,
role resolution, client construction, and response parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.aap2_fix import (
    _filter_sensitive_vars,
    _parse_ai_fix_response,
    _resolve_agnosticd_role,
    _resolve_role_source,
    ai_analyze_fix,
    recommend_fix,
)

# ---------------------------------------------------------------------------
# _filter_sensitive_vars
# ---------------------------------------------------------------------------


class TestFilterSensitiveVars:
    def test_none_input(self):
        assert _filter_sensitive_vars(None) == {}

    def test_empty_dict(self):
        assert _filter_sensitive_vars({}) == {}

    def test_removes_password_keys(self):
        result = _filter_sensitive_vars(
            {"username": "admin", "db_password": "secret123", "action": "deploy"}
        )
        assert "username" in result
        assert "action" in result
        assert "db_password" not in result

    def test_removes_secret_keys(self):
        result = _filter_sensitive_vars({"my_secret": "val", "name": "test"})
        assert "my_secret" not in result
        assert "name" in result

    def test_removes_token_keys(self):
        result = _filter_sensitive_vars({"api_token": "tok", "env": "prod"})
        assert "api_token" not in result
        assert "env" in result

    def test_removes_key_keys(self):
        result = _filter_sensitive_vars({"ssh_key": "rsa...", "host": "example.com"})
        assert "ssh_key" not in result
        assert "host" in result

    def test_removes_oversized_strings(self):
        result = _filter_sensitive_vars({"big": "x" * 300, "small": "ok"})
        assert "big" not in result
        assert "small" in result

    def test_keeps_non_string_large_values(self):
        result = _filter_sensitive_vars({"data": [1, 2, 3], "count": 999})
        assert "data" in result
        assert "count" in result


# ---------------------------------------------------------------------------
# _parse_ai_fix_response
# ---------------------------------------------------------------------------


class TestParseAiFixResponse:
    def test_valid_json_response(self):
        resp = json.dumps(
            {
                "file": "roles/my_role/tasks/main.yml",
                "repo": "agnosticd/cloud_deployer",
                "line": 42,
                "explanation": "Missing FQCN for uri module",
                "before": "  uri:",
                "after": "  ansible.builtin.uri:",
            }
        )
        result = _parse_ai_fix_response(resp)
        assert result is not None
        assert result["source"] == "ai"
        assert result["file"] == "roles/my_role/tasks/main.yml"
        assert result["repo"] == "agnosticd/cloud_deployer"
        assert result["line"] == 42
        assert "#L42" in result["githubUrl"]
        assert result["before"] == "  uri:"
        assert result["after"] == "  ansible.builtin.uri:"

    def test_json_in_markdown_code_fence(self):
        resp = '```json\n{"file": "test.yml", "repo": "org/repo", "line": null, "explanation": "env issue", "before": null, "after": null}\n```'
        result = _parse_ai_fix_response(resp)
        assert result is not None
        assert result["source"] == "ai"
        assert result["file"] == "test.yml"
        assert result["line"] is None
        assert "#L" not in result["githubUrl"]

    def test_no_json_in_response(self):
        resp = "I cannot determine the exact fix without more context."
        result = _parse_ai_fix_response(resp)
        assert result is not None
        assert result["source"] == "ai"
        assert result["file"] == "N/A"
        assert "non-JSON response" in result["explanation"]

    def test_json_with_extra_text(self):
        resp = 'Here is my analysis:\n{"file": "deploy.yml", "repo": "org/repo", "line": 10, "explanation": "Fix needed", "before": "old", "after": "new"}\nHope this helps!'
        result = _parse_ai_fix_response(resp)
        assert result is not None
        assert result["file"] == "deploy.yml"
        assert result["line"] == 10

    def test_file_na_produces_repo_only_url(self):
        resp = json.dumps(
            {
                "file": "N/A",
                "repo": "org/repo",
                "line": None,
                "explanation": "Environmental issue",
                "before": None,
                "after": None,
            }
        )
        result = _parse_ai_fix_response(resp)
        assert result is not None
        assert result["githubUrl"] == "https://github.com/org/repo"

    def test_line_not_integer_is_set_to_none(self):
        resp = json.dumps(
            {
                "file": "test.yml",
                "repo": "org/repo",
                "line": "unknown",
                "explanation": "test",
            }
        )
        result = _parse_ai_fix_response(resp)
        assert result is not None
        assert result["line"] is None

    def test_missing_fields_use_defaults(self):
        resp = json.dumps({"explanation": "something went wrong"})
        result = _parse_ai_fix_response(resp)
        assert result is not None
        assert result["file"] == "N/A"
        assert result["repo"] == "unknown"
        assert result["before"] is None
        assert result["after"] is None


# ---------------------------------------------------------------------------
# _resolve_role_source
# ---------------------------------------------------------------------------


class TestResolveRoleSource:
    @pytest.mark.asyncio
    async def test_empty_fqcn(self):
        hint, source = await _resolve_role_source("")
        assert hint == ""
        assert source == ""

    @pytest.mark.asyncio
    async def test_single_part_fqcn(self):
        hint, source = await _resolve_role_source("somerole")
        assert hint == ""
        assert source == ""

    @pytest.mark.asyncio
    async def test_two_part_fqcn(self):
        hint, source = await _resolve_role_source("community.general")
        assert "community" in hint
        assert "general" in hint

    @pytest.mark.asyncio
    async def test_two_part_with_role(self):
        hint, source = await _resolve_role_source("myns.mycol.myrole")
        assert "myns" in hint
        assert "mycol" in hint
        assert "myrole" in hint

    @pytest.mark.asyncio
    @patch("src.tools.aap2_fix._fetch_source_file", new_callable=AsyncMock)
    async def test_agnosticd_role_source_found(self, mock_fetch):
        mock_fetch.return_value = "   1: ---\n   2: - name: do stuff"
        hint, source = await _resolve_role_source("agnosticd.cloud_deployer.deploy_vm")
        assert "agnosticd/cloud_deployer" in hint
        assert "deploy_vm" in hint
        assert "Source file" in source

    @pytest.mark.asyncio
    @patch("src.tools.aap2_fix._fetch_source_file", new_callable=AsyncMock)
    async def test_agnosticd_role_source_not_found(self, mock_fetch):
        mock_fetch.return_value = None
        hint, source = await _resolve_role_source("agnosticd.cloud_deployer.deploy_vm")
        assert "agnosticd/cloud_deployer" in hint
        assert source == ""


# ---------------------------------------------------------------------------
# _resolve_agnosticd_role
# ---------------------------------------------------------------------------


class TestResolveAgnosticdRole:
    @pytest.mark.asyncio
    @patch("src.tools.aap2_fix._fetch_source_file", new_callable=AsyncMock)
    async def test_hyphen_fallback(self, mock_fetch):
        """When underscore repo 404s, try hyphenated name."""
        mock_fetch.side_effect = [None, "found via hyphen"]
        parts = ["agnosticd", "cloud_deployer", "my_role"]
        hint, source = await _resolve_agnosticd_role(parts)
        assert "agnosticd/cloud-deployer" in hint
        assert "Source file" in source

    @pytest.mark.asyncio
    @patch("src.tools.aap2_fix._fetch_source_file", new_callable=AsyncMock)
    async def test_no_hyphen_needed(self, mock_fetch):
        """When there's no underscore, no fallback needed."""
        mock_fetch.return_value = None
        parts = ["agnosticd", "mycollection", "myrole"]
        hint, source = await _resolve_agnosticd_role(parts)
        # No underscore so no hyphen attempt, just one call
        assert mock_fetch.call_count == 1
        assert "agnosticd/mycollection" in hint


# ---------------------------------------------------------------------------
# ai_analyze_fix
# ---------------------------------------------------------------------------


class TestAiAnalyzeFix:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_cfg = SimpleNamespace(
            anthropic={"backend": "api", "api_key": "", "model": "claude-sonnet-4-6"},
            gcp={},
            aws={},
        )
        with (
            patch("src.tools.aap2_fix.get_config", return_value=mock_cfg),
            patch(
                "src.tools.aap2_fix.build_client",
                side_effect=ValueError("No API key configured"),
            ),
        ):
            result = await ai_analyze_fix(
                {"taskName": "test", "errorMessage": "something broke"},
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_successful_ai_analysis(self, monkeypatch):
        ai_response = json.dumps(
            {
                "file": "roles/deploy/tasks/main.yml",
                "repo": "agnosticd/cloud_deployer",
                "line": 15,
                "explanation": "Module uri should be ansible.builtin.uri",
                "before": "  uri:",
                "after": "  ansible.builtin.uri:",
            }
        )
        mock_block = SimpleNamespace(text=ai_response)
        mock_response = SimpleNamespace(content=[mock_block])
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        mock_cfg = SimpleNamespace(
            anthropic={"backend": "api", "api_key": "test-key", "model": "claude-sonnet-4-6"},
            gcp={},
        )
        with (
            patch("src.tools.aap2_fix.get_config", return_value=mock_cfg),
            patch("src.tools.aap2_fix.build_client", return_value=mock_client),
            patch("src.tools.aap2_fix.resolve_model", return_value="claude-sonnet-4-6"),
            patch(
                "src.tools.aap2_fix._resolve_role_source",
                new_callable=AsyncMock,
                return_value=("", ""),
            ),
        ):
            result = await ai_analyze_fix(
                {"taskName": "deploy", "errorMessage": "module not found", "roleFqcn": ""},
                extra_vars={"ACTION": "provision"},
                job_template_name="RHPDS test.job.prod-1",
            )
            assert result is not None
            assert result["source"] == "ai"
            assert result["file"] == "roles/deploy/tasks/main.yml"

    @pytest.mark.asyncio
    async def test_ai_exception_returns_none(self):
        mock_cfg = SimpleNamespace(
            anthropic={"backend": "api", "api_key": "test-key", "model": "claude-sonnet-4-6"},
            gcp={},
        )
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")
        with (
            patch("src.tools.aap2_fix.get_config", return_value=mock_cfg),
            patch("src.tools.aap2_fix.build_client", return_value=mock_client),
            patch("src.tools.aap2_fix.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = await ai_analyze_fix(
                {"taskName": "test", "errorMessage": "fail"},
            )
            assert result is None


# ---------------------------------------------------------------------------
# recommend_fix (integration: pattern first, AI fallback)
# ---------------------------------------------------------------------------


class TestRecommendFix:
    @pytest.mark.asyncio
    async def test_pattern_match_takes_precedence(self):
        result = await recommend_fix(
            {"errorMessage": "InvalidClientTokenId: the security token is invalid"},
        )
        assert result is not None
        assert result["source"] == "pattern"

    @pytest.mark.asyncio
    async def test_ai_fallback_on_no_pattern(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_cfg = SimpleNamespace(
            anthropic={"backend": "api", "api_key": "", "model": "claude-sonnet-4-6"},
            gcp={},
            aws={},
        )
        with (
            patch("src.tools.aap2_fix.get_config", return_value=mock_cfg),
            patch(
                "src.tools.aap2_fix.build_client",
                side_effect=ValueError("No API key configured"),
            ),
        ):
            result = await recommend_fix(
                {"errorMessage": "some unknown error that matches no pattern"},
            )
            # AI returns None because no API key
            assert result is None
