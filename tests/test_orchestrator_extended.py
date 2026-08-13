"""Extended tests for uncovered functions in src/agent/orchestrator.py.

Covers tool dispatch helpers, history/serialization helpers, client construction,
alert helpers, and cache helpers.
"""

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.client_factory import build_client
from src.agent.orchestrator import (
    _cache_key,
    _check_tool_cache,
    _drop_oldest_turn,
    _execute_cloud_tool,
    _execute_cost_tool,
    _execute_db_tool,
    _execute_github_tool,
    _execute_infra_tool,
    _execute_tool,
    _make_error_verdict,
    _process_alert_tool_call,
    _serialize_content_block,
    _store_tool_cache,
    _tool_cache,
    _truncate_old_message,
    _truncate_tool_result_content,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_mlflow_span():
    """Return a mock mlflow.start_span that yields a SimpleNamespace span."""
    span = SimpleNamespace(
        set_inputs=MagicMock(),
        set_outputs=MagicMock(),
        set_attribute=MagicMock(),
        set_attributes=MagicMock(),
    )

    @contextmanager
    def _ctx(*args, **kwargs):
        yield span

    return _ctx


# ===================================================================
# 1. Tool dispatch helpers
# ===================================================================


class TestExecuteDbTool:
    @pytest.mark.asyncio
    async def test_returns_none_for_non_db_tool(self, monkeypatch):
        monkeypatch.setattr("src.agent.orchestrator._is_reporting_mcp_tool", lambda name: False)
        result = await _execute_db_tool("query_aws_costs", {"sql": "SELECT 1"})
        assert result is None

    @pytest.mark.asyncio
    async def test_routes_query_provisions_db(self, monkeypatch):
        mock_execute = AsyncMock(return_value={"rows": [{"id": 1}]})
        monkeypatch.setattr("src.agent.orchestrator.execute_query", mock_execute)
        result = await _execute_db_tool("query_provisions_db", {"sql": "SELECT 1"})
        assert result == {"rows": [{"id": 1}]}
        mock_execute.assert_awaited_once_with("SELECT 1")

    @pytest.mark.asyncio
    async def test_routes_db_read_knowledge(self, monkeypatch):
        mock_read = AsyncMock(return_value={"content": "knowledge data"})
        monkeypatch.setattr("src.connections.reporting_mcp.read_resource", mock_read)
        result = await _execute_db_tool("db_read_knowledge", {"domain": "users"})
        assert result == {"content": "knowledge data"}
        mock_read.assert_awaited_once_with("database://knowledge/users")

    @pytest.mark.asyncio
    async def test_routes_db_get_prompt(self, monkeypatch):
        mock_prompt = AsyncMock(return_value={"messages": []})
        monkeypatch.setattr("src.connections.reporting_mcp.get_prompt", mock_prompt)
        result = await _execute_db_tool(
            "db_get_prompt",
            {"prompt_name": "my_prompt", "arguments": {"key": "val"}},
        )
        assert result == {"messages": []}
        mock_prompt.assert_awaited_once_with("my_prompt", {"key": "val"})

    @pytest.mark.asyncio
    async def test_routes_db_get_prompt_no_arguments(self, monkeypatch):
        mock_prompt = AsyncMock(return_value={"messages": []})
        monkeypatch.setattr("src.connections.reporting_mcp.get_prompt", mock_prompt)
        result = await _execute_db_tool("db_get_prompt", {"prompt_name": "simple"})
        assert result == {"messages": []}
        mock_prompt.assert_awaited_once_with("simple", {})

    @pytest.mark.asyncio
    async def test_routes_mcp_tool(self, monkeypatch):
        monkeypatch.setattr("src.agent.orchestrator._is_reporting_mcp_tool", lambda name: True)
        mock_call = AsyncMock(return_value={"data": "mcp_result"})
        monkeypatch.setattr("src.connections.reporting_mcp.call_tool", mock_call)
        monkeypatch.setattr(
            "src.connections.reporting_mcp.get_mcp_tool_original",
            lambda name: "original_tool",
        )
        result = await _execute_db_tool("db_some_tool", {"arg": "value"})
        assert result == {"data": "mcp_result"}
        mock_call.assert_awaited_once_with("original_tool", {"arg": "value"})


class TestExecuteCostTool:
    @pytest.mark.asyncio
    async def test_routes_query_aws_costs(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"costs": []})
        monkeypatch.setattr("src.agent.orchestrator.query_aws_costs", mock_fn)
        result = await _execute_cost_tool(
            "query_aws_costs",
            {
                "account_ids": ["123"],
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
            },
        )
        assert result == {"costs": []}
        mock_fn.assert_awaited_once_with(
            account_ids=["123"],
            start_date="2024-01-01",
            end_date="2024-01-31",
            group_by="SERVICE",
        )

    @pytest.mark.asyncio
    async def test_routes_query_azure_costs(self, monkeypatch):
        mock_fn = MagicMock(return_value={"azure": True})
        monkeypatch.setattr("src.agent.orchestrator.query_azure_costs", mock_fn)
        result = await _execute_cost_tool(
            "query_azure_costs",
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        )
        assert result == {"azure": True}
        mock_fn.assert_called_once_with(
            start_date="2024-01-01",
            end_date="2024-01-31",
            subscription_names=None,
            meter_filter=None,
        )

    @pytest.mark.asyncio
    async def test_routes_query_gcp_costs(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"gcp": True})
        monkeypatch.setattr("src.agent.orchestrator.query_gcp_costs", mock_fn)
        result = await _execute_cost_tool(
            "query_gcp_costs",
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        )
        assert result == {"gcp": True}
        mock_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_routes_query_aws_pricing(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"price": "0.10"})
        monkeypatch.setattr("src.agent.orchestrator.query_aws_pricing", mock_fn)
        result = await _execute_cost_tool("query_aws_pricing", {"instance_type": "m5.large"})
        assert result == {"price": "0.10"}
        mock_fn.assert_awaited_once_with(
            instance_type="m5.large",
            region="us-east-1",
            os_type="Linux",
        )

    @pytest.mark.asyncio
    async def test_routes_query_cost_monitor(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"data": []})
        monkeypatch.setattr("src.agent.orchestrator.query_cost_monitor", mock_fn)
        result = await _execute_cost_tool(
            "query_cost_monitor",
            {
                "endpoint": "summary",
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
            },
        )
        assert result == {"data": []}

    @pytest.mark.asyncio
    async def test_falls_through_to_capacity_manager(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"odcr": True})
        monkeypatch.setattr("src.agent.orchestrator.query_aws_capacity_manager", mock_fn)
        result = await _execute_cost_tool("query_aws_capacity_manager", {"metric": "utilization"})
        assert result == {"odcr": True}
        mock_fn.assert_awaited_once()


class TestExecuteCloudTool:
    @pytest.mark.asyncio
    async def test_routes_query_azure_pools(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"pools": []})
        monkeypatch.setattr("src.agent.orchestrator.query_azure_pools", mock_fn)
        result = await _execute_cloud_tool("query_azure_pools", {"action": "list_pools"})
        assert result == {"pools": []}

    @pytest.mark.asyncio
    async def test_routes_query_gcp_projects(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"projects": []})
        monkeypatch.setattr("src.agent.orchestrator.query_gcp_projects", mock_fn)
        result = await _execute_cloud_tool("query_gcp_projects", {"action": "list"})
        assert result == {"projects": []}

    @pytest.mark.asyncio
    async def test_routes_query_cloudtrail(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"events": []})
        monkeypatch.setattr("src.agent.orchestrator.query_cloudtrail", mock_fn)
        result = await _execute_cloud_tool("query_cloudtrail", {"query": "SELECT * FROM events"})
        assert result == {"events": []}

    @pytest.mark.asyncio
    async def test_routes_query_aws_account(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"instances": []})
        monkeypatch.setattr("src.agent.orchestrator.query_aws_account", mock_fn)
        result = await _execute_cloud_tool(
            "query_aws_account",
            {"account_id": "123", "action": "list_instances"},
        )
        assert result == {"instances": []}

    @pytest.mark.asyncio
    async def test_routes_query_aws_account_db(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"accounts": []})
        monkeypatch.setattr("src.agent.orchestrator.query_aws_account_db", mock_fn)
        result = await _execute_cloud_tool("query_aws_account_db", {"account_id": "123"})
        assert result == {"accounts": []}

    @pytest.mark.asyncio
    async def test_falls_through_to_marketplace_agreements(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"agreements": []})
        monkeypatch.setattr("src.agent.orchestrator.query_marketplace_agreements", mock_fn)
        result = await _execute_cloud_tool("query_marketplace_agreements", {"account_id": "123"})
        assert result == {"agreements": []}


class TestExecuteInfraTool:
    @pytest.mark.asyncio
    async def test_routes_query_babylon_catalog(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"items": []})
        monkeypatch.setattr("src.agent.orchestrator.query_babylon_catalog", mock_fn)
        result = await _execute_infra_tool("query_babylon_catalog", {"action": "list"})
        assert result == {"items": []}

    @pytest.mark.asyncio
    async def test_routes_query_ocpv_cluster(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"nodes": []})
        monkeypatch.setattr("src.agent.orchestrator.query_ocpv_cluster", mock_fn)
        result = await _execute_infra_tool("query_ocpv_cluster", {"action": "list_vms"})
        assert result == {"nodes": []}

    @pytest.mark.asyncio
    async def test_routes_query_aap2(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"jobs": []})
        monkeypatch.setattr("src.agent.orchestrator.query_aap2", mock_fn)
        result = await _execute_infra_tool("query_aap2", {"action": "list_jobs"})
        assert result == {"jobs": []}

    @pytest.mark.asyncio
    async def test_routes_query_splunk(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"results": []})
        monkeypatch.setattr("src.agent.orchestrator.query_splunk", mock_fn)
        result = await _execute_infra_tool("query_splunk", {"action": "search"})
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_falls_through_to_query_icinga(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"hosts": []})
        monkeypatch.setattr("src.agent.orchestrator.query_icinga", mock_fn)
        result = await _execute_infra_tool("query_icinga", {"action": "get_hosts"})
        assert result == {"hosts": []}


class TestExecuteGithubTool:
    @pytest.mark.asyncio
    async def test_routes_fetch_github_file(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"content": "file data"})
        monkeypatch.setattr("src.agent.orchestrator.fetch_github_file", mock_fn)
        result = await _execute_github_tool(
            "fetch_github_file",
            {"owner": "rhpds", "repo": "parsec", "path": "README.md"},
        )
        assert result == {"content": "file data"}
        mock_fn.assert_awaited_once_with(owner="rhpds", repo="parsec", path="README.md", ref="")

    @pytest.mark.asyncio
    async def test_routes_lookup_catalog_item(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"items": []})
        monkeypatch.setattr("src.agent.orchestrator.lookup_catalog_item", mock_fn)
        result = await _execute_github_tool("lookup_catalog_item", {"search": "ocp4-workshop"})
        assert result == {"items": []}
        mock_fn.assert_awaited_once_with(search="ocp4-workshop")

    @pytest.mark.asyncio
    async def test_routes_search_github_repo(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"files": []})
        monkeypatch.setattr("src.agent.orchestrator.search_github_repo", mock_fn)
        result = await _execute_github_tool(
            "search_github_repo",
            {"owner": "rhpds", "repo": "agnosticd", "search": "deploy"},
        )
        assert result == {"files": []}

    @pytest.mark.asyncio
    async def test_falls_through_to_search_agnosticv_prs(self, monkeypatch):
        mock_fn = AsyncMock(return_value={"prs": []})
        monkeypatch.setattr("src.agent.orchestrator.search_agnosticv_prs", mock_fn)
        result = await _execute_github_tool("search_agnosticv_prs", {"search": "fix deploy"})
        assert result == {"prs": []}
        mock_fn.assert_awaited_once_with(search="fix deploy", state="open", max_results=10)


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_delegates_to_db_tools_first(self, monkeypatch):
        mock_db = AsyncMock(return_value={"db": True})
        monkeypatch.setattr("src.agent.orchestrator._execute_db_tool", mock_db)
        result = await _execute_tool("query_provisions_db", {"sql": "SELECT 1"})
        assert result == {"db": True}

    @pytest.mark.asyncio
    async def test_routes_cost_tools(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.orchestrator._execute_db_tool",
            AsyncMock(return_value=None),
        )
        mock_cost = AsyncMock(return_value={"cost": True})
        monkeypatch.setattr("src.agent.orchestrator._execute_cost_tool", mock_cost)
        result = await _execute_tool(
            "query_aws_costs",
            {
                "account_ids": [],
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
            },
        )
        assert result == {"cost": True}

    @pytest.mark.asyncio
    async def test_routes_cloud_tools(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.orchestrator._execute_db_tool",
            AsyncMock(return_value=None),
        )
        mock_cloud = AsyncMock(return_value={"cloud": True})
        monkeypatch.setattr("src.agent.orchestrator._execute_cloud_tool", mock_cloud)
        result = await _execute_tool("query_cloudtrail", {"query": "SELECT *"})
        assert result == {"cloud": True}

    @pytest.mark.asyncio
    async def test_routes_infra_tools(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.orchestrator._execute_db_tool",
            AsyncMock(return_value=None),
        )
        mock_infra = AsyncMock(return_value={"infra": True})
        monkeypatch.setattr("src.agent.orchestrator._execute_infra_tool", mock_infra)
        result = await _execute_tool("query_babylon_catalog", {"action": "list"})
        assert result == {"infra": True}

    @pytest.mark.asyncio
    async def test_routes_github_tools(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.orchestrator._execute_db_tool",
            AsyncMock(return_value=None),
        )
        mock_gh = AsyncMock(return_value={"github": True})
        monkeypatch.setattr("src.agent.orchestrator._execute_github_tool", mock_gh)
        result = await _execute_tool(
            "fetch_github_file",
            {"owner": "o", "repo": "r", "path": "p"},
        )
        assert result == {"github": True}

    @pytest.mark.asyncio
    async def test_render_chart_returns_input(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.orchestrator._execute_db_tool",
            AsyncMock(return_value=None),
        )
        tool_input = {"type": "bar", "data": [1, 2, 3]}
        result = await _execute_tool("render_chart", tool_input)
        assert result is tool_input

    @pytest.mark.asyncio
    async def test_generate_report_calls_save_report(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.orchestrator._execute_db_tool",
            AsyncMock(return_value=None),
        )
        mock_save = MagicMock(return_value={"filename": "test.md"})
        monkeypatch.setattr("src.agent.orchestrator._save_report", mock_save)
        result = await _execute_tool(
            "generate_report",
            {"title": "Test", "content": "# Report"},
        )
        assert result == {"filename": "test.md"}
        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.orchestrator._execute_db_tool",
            AsyncMock(return_value=None),
        )
        result = await _execute_tool("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]


# ===================================================================
# 2. History/serialization helpers
# ===================================================================


class TestTruncateToolResultContent:
    def test_truncates_long_string_content(self):
        block = {"content": "x" * 3000}
        _truncate_tool_result_content(block)
        assert len(block["content"]) < 3000
        assert "truncated" in block["content"]

    def test_truncates_non_dict_json_content(self):
        block = {"content": json.dumps([1, 2, 3] * 1000)}
        _truncate_tool_result_content(block)
        assert "truncated" in block["content"]

    def test_truncates_dict_with_rows(self):
        # Content must be > 2000 chars to trigger truncation
        data = {"rows": [{"payload": "x" * 50} for _ in range(100)], "count": 100}
        block = {"content": json.dumps(data)}
        assert len(block["content"]) > 2000
        _truncate_tool_result_content(block)
        parsed = json.loads(block["content"])
        assert len(parsed["rows"]) == 5
        assert parsed.get("_truncated_for_context") is True

    def test_truncates_dict_with_results(self):
        data = {"results": [{"id": i, "payload": "y" * 50} for i in range(100)]}
        block = {"content": json.dumps(data)}
        assert len(block["content"]) > 2000
        _truncate_tool_result_content(block)
        parsed = json.loads(block["content"])
        assert len(parsed["results"]) == 5
        assert parsed.get("_truncated_for_context") is True

    def test_truncates_dict_with_long_result_string(self):
        data = {"result": "y" * 5000}
        block = {"content": json.dumps(data)}
        assert len(block["content"]) > 2000
        _truncate_tool_result_content(block)
        parsed = json.loads(block["content"])
        assert len(parsed["result"]) <= 2020  # 2000 + "\n... [truncated]"
        assert parsed.get("_truncated_for_context") is True

    def test_does_nothing_for_short_content(self):
        block = {"content": json.dumps({"ok": True})}
        original = block["content"]
        _truncate_tool_result_content(block)
        assert block["content"] == original

    def test_does_nothing_for_non_string_content(self):
        block = {"content": 42}
        _truncate_tool_result_content(block)
        assert block["content"] == 42


class TestTruncateOldMessage:
    def test_truncates_list_content_with_tool_result_blocks(self):
        block = {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": "x" * 5000,
        }
        msg = {"role": "user", "content": [block]}
        _truncate_old_message(msg)
        assert len(msg["content"][0]["content"]) < 5000

    def test_truncates_list_content_with_text_blocks(self):
        block = {"type": "text", "text": "a" * 5000}
        msg = {"role": "assistant", "content": [block]}
        _truncate_old_message(msg)
        assert len(msg["content"][0]["text"]) < 5000
        assert "truncated" in msg["content"][0]["text"]

    def test_truncates_assistant_string_content(self):
        msg = {"role": "assistant", "content": "b" * 5000}
        _truncate_old_message(msg)
        assert len(msg["content"]) < 5000
        assert "truncated" in msg["content"]

    def test_does_nothing_for_short_content(self):
        msg = {"role": "assistant", "content": "short text"}
        _truncate_old_message(msg)
        assert msg["content"] == "short text"

    def test_does_nothing_for_short_list_content(self):
        block = {"type": "text", "text": "short"}
        msg = {"role": "assistant", "content": [block]}
        _truncate_old_message(msg)
        assert msg["content"][0]["text"] == "short"


class TestDropOldestTurn:
    def test_drops_first_user_message(self):
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        _drop_oldest_turn(messages)
        # Drops user q1 + assistant a1
        assert len(messages) == 1
        assert messages[0]["content"] == "q2"

    def test_drops_user_and_assistant(self):
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        _drop_oldest_turn(messages)
        assert len(messages) == 2
        assert messages[0]["content"] == "q2"
        assert messages[1]["content"] == "a2"

    def test_drops_orphaned_tool_result(self):
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}],
            },
            {"role": "user", "content": "q2"},
        ]
        _drop_oldest_turn(messages)
        # Drops user q1, assistant a1, and orphaned tool_result user
        assert len(messages) == 1
        assert messages[0]["content"] == "q2"

    def test_handles_empty_list_after_pop(self):
        messages = [{"role": "user", "content": "q1"}]
        _drop_oldest_turn(messages)
        assert messages == []

    def test_single_user_assistant_pair(self):
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        _drop_oldest_turn(messages)
        assert messages == []


class TestSerializeContentBlock:
    def test_handles_dict_input_tool_use(self):
        block = {
            "type": "tool_use",
            "id": "t1",
            "name": "query_aws_costs",
            "input": {"sql": "SELECT 1"},
            "caller": None,  # extra field that should be stripped
        }
        result = _serialize_content_block(block)
        assert result["type"] == "tool_use"
        assert result["id"] == "t1"
        assert result["name"] == "query_aws_costs"
        assert result["input"] == {"sql": "SELECT 1"}
        assert "caller" not in result

    def test_handles_dict_input_text(self):
        block = {"type": "text", "text": "Hello world"}
        result = _serialize_content_block(block)
        assert result == {"type": "text", "text": "Hello world"}

    def test_handles_dict_input_tool_result(self):
        block = {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": '{"ok": true}',
            "is_error": False,
        }
        result = _serialize_content_block(block)
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "t1"
        assert result["content"] == '{"ok": true}'

    def test_handles_object_with_model_dump(self):
        obj = SimpleNamespace(
            model_dump=lambda: {
                "type": "text",
                "text": "from model_dump",
            }
        )
        result = _serialize_content_block(obj)
        assert result == {"type": "text", "text": "from model_dump"}

    def test_handles_object_with_to_dict(self):
        obj = SimpleNamespace(to_dict=lambda: {"type": "text", "text": "from to_dict"})
        result = _serialize_content_block(obj)
        assert result == {"type": "text", "text": "from to_dict"}

    def test_handles_other_objects(self):
        obj = 42
        result = _serialize_content_block(obj)
        assert result == {"type": "text", "text": "42"}

    def test_handles_string_object(self):
        result = _serialize_content_block("some string")
        assert result == {"type": "text", "text": "some string"}

    def test_dict_tool_result_with_is_error_true(self):
        block = {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": "error details",
            "is_error": True,
        }
        result = _serialize_content_block(block)
        assert result["is_error"] is True

    def test_dict_tool_result_without_content(self):
        block = {
            "type": "tool_result",
            "tool_use_id": "t1",
        }
        result = _serialize_content_block(block)
        assert "content" not in result

    def test_dict_unknown_type_passes_through(self):
        block = {"type": "image", "source": "data:image/png;base64,abc"}
        result = _serialize_content_block(block)
        assert result == block


# ===================================================================
# 3. Client construction
# ===================================================================


class TestBuildClient:
    def test_direct_api_backend(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = SimpleNamespace(
            anthropic={"backend": "api", "api_key": "sk-test-key"},
            gcp={},
            aws={},
        )
        client = build_client(cfg)
        import anthropic

        assert isinstance(client, anthropic.Anthropic)

    def test_vertex_backend(self, monkeypatch):
        cfg = SimpleNamespace(
            anthropic={
                "backend": "vertex",
                "vertex_project_id": "my-project",
                "vertex_region": "us-east5",
            },
            gcp={"project_id": "fallback-project"},
            aws={},
        )
        with patch("anthropic.AnthropicVertex") as mock_vertex:
            mock_vertex.return_value = MagicMock()
            build_client(cfg)
            mock_vertex.assert_called_once_with(project_id="my-project", region="us-east5")

    def test_bedrock_backend(self, monkeypatch):
        cfg = SimpleNamespace(
            anthropic={"backend": "bedrock", "bedrock_region": "us-west-2"},
            gcp={},
            aws={"region": "us-east-1"},
        )
        with patch("anthropic.AnthropicBedrock") as mock_bedrock:
            mock_bedrock.return_value = MagicMock()
            build_client(cfg)
            mock_bedrock.assert_called_once_with(aws_region="us-west-2")

    def test_raises_when_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = SimpleNamespace(
            anthropic={"backend": "api", "api_key": ""},
            gcp={},
            aws={},
        )
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY not configured"):
            build_client(cfg)

    def test_raises_when_vertex_project_id_missing(self):
        cfg = SimpleNamespace(
            anthropic={
                "backend": "vertex",
                "vertex_project_id": "",
                "vertex_region": "us-east5",
            },
            gcp={"project_id": ""},
            aws={},
        )
        with pytest.raises(ValueError, match="vertex_project_id"):
            build_client(cfg)

    def test_vertex_uses_gcp_project_id_fallback(self, monkeypatch):
        cfg = SimpleNamespace(
            anthropic={
                "backend": "vertex",
                "vertex_project_id": "",
                "vertex_region": "us-east5",
            },
            gcp={"project_id": "gcp-fallback"},
            aws={},
        )
        with patch("anthropic.AnthropicVertex") as mock_vertex:
            mock_vertex.return_value = MagicMock()
            build_client(cfg)
            mock_vertex.assert_called_once_with(project_id="gcp-fallback", region="us-east5")

    def test_direct_api_uses_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-key")
        cfg = SimpleNamespace(
            anthropic={"backend": "api", "api_key": ""},
            gcp={},
            aws={},
        )
        client = build_client(cfg)
        import anthropic

        assert isinstance(client, anthropic.Anthropic)


# ===================================================================
# 4. Alert helpers
# ===================================================================


class TestMakeErrorVerdict:
    def test_returns_correct_structure(self):
        start = time.monotonic() - 2.5
        result = _make_error_verdict(
            "Something failed",
            ["log1", "log2"],
            start,
        )
        assert result["should_alert"] is True
        assert result["severity"] == "medium"
        assert result["summary"] == "Something failed"
        assert result["investigation_log"] == "log1\nlog2"
        assert result["duration_seconds"] >= 2.0

    def test_includes_error_msg_in_summary(self):
        start = time.monotonic()
        result = _make_error_verdict("API timeout", [], start)
        assert "API timeout" in result["summary"]

    def test_joins_investigation_log(self):
        start = time.monotonic()
        result = _make_error_verdict("err", ["step1", "step2", "step3"], start)
        assert result["investigation_log"] == "step1\nstep2\nstep3"

    def test_calculates_duration(self):
        start = time.monotonic() - 5.0
        result = _make_error_verdict("err", [], start)
        assert result["duration_seconds"] >= 4.5

    def test_empty_log(self):
        start = time.monotonic()
        result = _make_error_verdict("err", [], start)
        assert result["investigation_log"] == ""


class TestProcessAlertToolCall:
    @pytest.mark.asyncio
    async def test_submit_alert_verdict(self, monkeypatch):
        monkeypatch.setattr("mlflow.start_span", _fake_mlflow_span())
        tool_block = SimpleNamespace(
            name="submit_alert_verdict",
            id="tool_1",
            input={
                "should_alert": False,
                "severity": "low",
                "summary": "Benign activity",
            },
        )
        investigation_log = []
        verdict, tool_result = await _process_alert_tool_call(tool_block, investigation_log)
        assert verdict is not None
        assert verdict["should_alert"] is False
        assert verdict["severity"] == "low"
        assert verdict["summary"] == "Benign activity"
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "tool_1"
        assert "verdict_recorded" in tool_result["content"]
        assert len(investigation_log) == 1
        assert "Verdict" in investigation_log[0]

    @pytest.mark.asyncio
    async def test_regular_tool_execution(self, monkeypatch):
        monkeypatch.setattr("mlflow.start_span", _fake_mlflow_span())
        mock_execute = AsyncMock(return_value={"data": "result"})
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)
        tool_block = SimpleNamespace(
            name="query_aws_costs",
            id="tool_2",
            input={"account_ids": ["123"], "start_date": "2024-01-01", "end_date": "2024-01-31"},
        )
        investigation_log = []
        verdict, tool_result = await _process_alert_tool_call(tool_block, investigation_log)
        assert verdict is None
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "tool_2"
        assert "data" in tool_result["content"]
        assert len(investigation_log) == 2  # input log + result log

    @pytest.mark.asyncio
    async def test_tool_raises_exception(self, monkeypatch):
        monkeypatch.setattr("mlflow.start_span", _fake_mlflow_span())
        mock_execute = AsyncMock(side_effect=RuntimeError("connection refused"))
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)
        tool_block = SimpleNamespace(
            name="query_aws_costs",
            id="tool_3",
            input={"account_ids": []},
        )
        investigation_log = []
        verdict, tool_result = await _process_alert_tool_call(tool_block, investigation_log)
        assert verdict is None
        assert "error" in tool_result["content"]
        assert "connection refused" in tool_result["content"]


# ===================================================================
# 5. Cache helpers
# ===================================================================


class TestCacheKey:
    def test_returns_deterministic_key(self):
        key1 = _cache_key("query_aws_costs", {"a": 1, "b": 2})
        key2 = _cache_key("query_aws_costs", {"b": 2, "a": 1})
        assert key1 == key2  # sort_keys=True

    def test_different_tools_different_keys(self):
        key1 = _cache_key("tool_a", {"x": 1})
        key2 = _cache_key("tool_b", {"x": 1})
        assert key1 != key2

    def test_different_inputs_different_keys(self):
        key1 = _cache_key("tool_a", {"x": 1})
        key2 = _cache_key("tool_a", {"x": 2})
        assert key1 != key2


class TestCheckToolCache:
    def test_returns_none_false_when_no_cache(self):
        # Ensure ContextVar has no cache set
        token = _tool_cache.set(None)
        try:
            result, hit = _check_tool_cache("query_aws_costs", {"a": 1})
            assert result is None
            assert hit is False
        finally:
            _tool_cache.reset(token)

    def test_returns_none_false_for_uncacheable_tools(self):
        token = _tool_cache.set({})
        try:
            result, hit = _check_tool_cache("render_chart", {"type": "bar"})
            assert result is None
            assert hit is False
        finally:
            _tool_cache.reset(token)

    def test_returns_cached_result(self):
        key = _cache_key("query_aws_costs", {"a": 1})
        cached_data = {"costs": [100, 200]}
        token = _tool_cache.set({key: cached_data})
        try:
            result, hit = _check_tool_cache("query_aws_costs", {"a": 1})
            assert result == cached_data
            assert hit is True
        finally:
            _tool_cache.reset(token)

    def test_returns_none_false_for_cache_miss(self):
        token = _tool_cache.set({})
        try:
            result, hit = _check_tool_cache("query_aws_costs", {"a": 1})
            assert result is None
            assert hit is False
        finally:
            _tool_cache.reset(token)


class TestStoreToolCache:
    def test_stores_result_in_cache(self):
        token = _tool_cache.set({})
        try:
            _store_tool_cache("query_aws_costs", {"a": 1}, {"costs": [100]})
            cache = _tool_cache.get()
            key = _cache_key("query_aws_costs", {"a": 1})
            assert cache[key] == {"costs": [100]}
        finally:
            _tool_cache.reset(token)

    def test_does_not_store_for_uncacheable_tools(self):
        token = _tool_cache.set({})
        try:
            _store_tool_cache("render_chart", {"type": "bar"}, {"ok": True})
            cache = _tool_cache.get()
            assert len(cache) == 0
        finally:
            _tool_cache.reset(token)

    def test_does_not_store_error_results(self):
        token = _tool_cache.set({})
        try:
            _store_tool_cache("query_aws_costs", {"a": 1}, {"error": "timeout"})
            cache = _tool_cache.get()
            assert len(cache) == 0
        finally:
            _tool_cache.reset(token)

    def test_does_not_store_when_no_cache_context(self):
        token = _tool_cache.set(None)
        try:
            # Should not raise
            _store_tool_cache("query_aws_costs", {"a": 1}, {"costs": []})
        finally:
            _tool_cache.reset(token)

    def test_does_not_store_generate_report(self):
        token = _tool_cache.set({})
        try:
            _store_tool_cache(
                "generate_report",
                {"title": "t"},
                {"filename": "report.md"},
            )
            cache = _tool_cache.get()
            assert len(cache) == 0
        finally:
            _tool_cache.reset(token)

    def test_does_not_store_query_icinga(self):
        token = _tool_cache.set({})
        try:
            _store_tool_cache("query_icinga", {"action": "get_hosts"}, {"hosts": []})
            cache = _tool_cache.get()
            assert len(cache) == 0
        finally:
            _tool_cache.reset(token)
