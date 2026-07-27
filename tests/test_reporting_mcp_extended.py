"""Extended tests for src/connections/reporting_mcp.py — covers discovery
functions, schema conversion, and synthetic tool builders."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.connections.reporting_mcp import (
    _build_knowledge_tool,
    _build_prompt_tool,
    _discover_prompts,
    _discover_resources,
    _discover_tools,
    _mcp_schema_to_claude,
    get_mcp_tool_original,
    get_mcp_tools,
    get_mcp_url,
    get_server_instructions,
    is_mcp_tool,
)

# ---------------------------------------------------------------------------
# _mcp_schema_to_claude
# ---------------------------------------------------------------------------


class TestMcpSchemaToClaude:
    def test_basic_tool_conversion(self):
        tool = SimpleNamespace(
            name="query",
            description="Run a SQL query",
            inputSchema={
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        )
        result = _mcp_schema_to_claude(tool)
        assert result["name"] == "db_query"
        assert result["description"] == "Run a SQL query"
        assert result["input_schema"]["properties"]["sql"]["type"] == "string"
        assert result["input_schema"]["required"] == ["sql"]

    def test_tool_with_no_input_schema(self):
        tool = SimpleNamespace(
            name="list_tables",
            description="List all tables",
            inputSchema=None,
        )
        result = _mcp_schema_to_claude(tool)
        assert result["name"] == "db_list_tables"
        assert result["input_schema"]["properties"] == {}
        assert result["input_schema"]["required"] == []

    def test_tool_with_no_description(self):
        tool = SimpleNamespace(
            name="my_tool",
            description=None,
            inputSchema={"properties": {}, "required": []},
        )
        result = _mcp_schema_to_claude(tool)
        assert result["description"] == "MCP tool: my_tool"

    def test_tool_with_empty_schema_properties(self):
        tool = SimpleNamespace(
            name="info",
            description="Get info",
            inputSchema={"properties": {}, "required": []},
        )
        result = _mcp_schema_to_claude(tool)
        assert result["input_schema"]["properties"] == {}


# ---------------------------------------------------------------------------
# _build_knowledge_tool
# ---------------------------------------------------------------------------


class TestBuildKnowledgeTool:
    def test_creates_tool_with_domains(self):
        result = _build_knowledge_tool(["billing", "provisioning", "users"])
        assert result["name"] == "db_read_knowledge"
        assert "domain" in result["input_schema"]["properties"]
        assert result["input_schema"]["properties"]["domain"]["enum"] == [
            "billing",
            "provisioning",
            "users",
        ]
        assert result["input_schema"]["required"] == ["domain"]

    def test_single_domain(self):
        result = _build_knowledge_tool(["provisioning"])
        assert result["input_schema"]["properties"]["domain"]["enum"] == ["provisioning"]


# ---------------------------------------------------------------------------
# _build_prompt_tool
# ---------------------------------------------------------------------------


class TestBuildPromptTool:
    def test_creates_tool_with_prompts(self):
        prompts = [
            {"name": "investigate_cost", "description": "Cost analysis template"},
            {"name": "investigate_user", "description": "User investigation template"},
        ]
        result = _build_prompt_tool(prompts)
        assert result["name"] == "db_get_prompt"
        assert result["input_schema"]["properties"]["prompt_name"]["enum"] == [
            "investigate_cost",
            "investigate_user",
        ]
        assert "Cost analysis" in result["description"]
        assert "User investigation" in result["description"]

    def test_single_prompt(self):
        prompts = [{"name": "analyze", "description": ""}]
        result = _build_prompt_tool(prompts)
        assert result["input_schema"]["properties"]["prompt_name"]["enum"] == ["analyze"]


# ---------------------------------------------------------------------------
# _discover_tools
# ---------------------------------------------------------------------------


class TestDiscoverTools:
    @pytest.mark.asyncio
    async def test_skips_query_tool(self):
        mock_session = AsyncMock()
        tool1 = SimpleNamespace(
            name="query",
            description="Run a SQL query",
            inputSchema={"properties": {}, "required": []},
        )
        tool2 = SimpleNamespace(
            name="list_tables",
            description="List tables",
            inputSchema={"properties": {}, "required": []},
        )
        mock_session.list_tools.return_value = SimpleNamespace(tools=[tool1, tool2])
        result = await _discover_tools(mock_session)
        names = [t["name"] for t in result]
        assert "db_query" not in names
        assert "db_list_tables" in names

    @pytest.mark.asyncio
    async def test_empty_tools_list(self):
        mock_session = AsyncMock()
        mock_session.list_tools.return_value = SimpleNamespace(tools=[])
        result = await _discover_tools(mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_tools_converted(self):
        mock_session = AsyncMock()
        tools = [
            SimpleNamespace(
                name=f"tool_{i}",
                description=f"Tool {i}",
                inputSchema={"properties": {}, "required": []},
            )
            for i in range(5)
        ]
        mock_session.list_tools.return_value = SimpleNamespace(tools=tools)
        result = await _discover_tools(mock_session)
        assert len(result) == 5
        assert all(t["name"].startswith("db_") for t in result)


# ---------------------------------------------------------------------------
# _discover_resources
# ---------------------------------------------------------------------------


class TestDiscoverResources:
    @pytest.mark.asyncio
    async def test_adds_knowledge_tool_for_knowledge_resources(self):
        mock_session = AsyncMock()
        mock_session.list_resources.return_value = SimpleNamespace(
            resources=[
                SimpleNamespace(uri="database://knowledge/billing"),
                SimpleNamespace(uri="database://knowledge/provisioning"),
                SimpleNamespace(uri="database://other/something"),
            ]
        )
        claude_tools: list[dict] = []
        await _discover_resources(mock_session, claude_tools)
        assert len(claude_tools) == 1
        assert claude_tools[0]["name"] == "db_read_knowledge"
        domains = claude_tools[0]["input_schema"]["properties"]["domain"]["enum"]
        assert "billing" in domains
        assert "provisioning" in domains

    @pytest.mark.asyncio
    async def test_no_knowledge_resources(self):
        mock_session = AsyncMock()
        mock_session.list_resources.return_value = SimpleNamespace(
            resources=[
                SimpleNamespace(uri="database://tables/users"),
            ]
        )
        claude_tools: list[dict] = []
        await _discover_resources(mock_session, claude_tools)
        assert len(claude_tools) == 0

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self):
        mock_session = AsyncMock()
        mock_session.list_resources.side_effect = Exception("Connection refused")
        claude_tools: list[dict] = []
        await _discover_resources(mock_session, claude_tools)
        assert len(claude_tools) == 0


# ---------------------------------------------------------------------------
# _discover_prompts
# ---------------------------------------------------------------------------


class TestDiscoverPrompts:
    @pytest.mark.asyncio
    async def test_adds_prompt_tool(self):
        mock_session = AsyncMock()
        mock_session.list_prompts.return_value = SimpleNamespace(
            prompts=[
                SimpleNamespace(name="investigate_cost", description="Cost analysis"),
                SimpleNamespace(name="investigate_user", description="User lookup"),
            ]
        )
        claude_tools: list[dict] = []
        await _discover_prompts(mock_session, claude_tools)
        assert len(claude_tools) == 1
        assert claude_tools[0]["name"] == "db_get_prompt"

    @pytest.mark.asyncio
    async def test_no_prompts(self):
        mock_session = AsyncMock()
        mock_session.list_prompts.return_value = SimpleNamespace(prompts=[])
        claude_tools: list[dict] = []
        await _discover_prompts(mock_session, claude_tools)
        assert len(claude_tools) == 0

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self):
        mock_session = AsyncMock()
        mock_session.list_prompts.side_effect = Exception("Timeout")
        claude_tools: list[dict] = []
        await _discover_prompts(mock_session, claude_tools)
        assert len(claude_tools) == 0

    @pytest.mark.asyncio
    async def test_prompt_with_no_description(self):
        mock_session = AsyncMock()
        mock_session.list_prompts.return_value = SimpleNamespace(
            prompts=[
                SimpleNamespace(name="quick_check", description=None),
            ]
        )
        claude_tools: list[dict] = []
        await _discover_prompts(mock_session, claude_tools)
        assert len(claude_tools) == 1
        prompt_names = claude_tools[0]["input_schema"]["properties"]["prompt_name"]["enum"]
        assert "quick_check" in prompt_names


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_get_mcp_tool_original_strips_prefix(self):
        assert get_mcp_tool_original("db_query") == "query"

    def test_get_mcp_tool_original_no_prefix(self):
        assert get_mcp_tool_original("query") == "query"

    def test_get_mcp_tool_original_partial_prefix(self):
        assert get_mcp_tool_original("db_") == ""

    def test_is_mcp_tool_empty_set(self):
        # Default state: _mcp_tool_names is empty unless populated
        assert is_mcp_tool("nonexistent") is False

    def test_get_mcp_url_default(self):
        # Module-level _mcp_url default is ""
        url = get_mcp_url()
        assert isinstance(url, str)

    def test_get_server_instructions_default(self):
        instructions = get_server_instructions()
        assert isinstance(instructions, str)

    def test_get_mcp_tools_default(self):
        tools = get_mcp_tools()
        assert isinstance(tools, list)
