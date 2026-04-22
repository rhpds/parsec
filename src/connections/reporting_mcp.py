"""Reporting MCP connection — streamable HTTP client for the reporting-mcp server.

Provides dynamic MCP protocol capabilities discovered at startup:
- Tools: discovered via list_tools(), cached, and exposed to Claude
- Resources: discovered via list_resources(), exposed as db_read_knowledge tool
- Prompts: discovered via list_prompts(), exposed as db_get_prompt tool
- Instructions: fetched via initialize(), injected into agent system prompts
"""

import logging
from typing import Any

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)

_mcp_url: str = ""
_token: str = ""
_server_instructions: str = ""

_MCP_TIMEOUT_SECONDS = 60

_TOOL_PREFIX = "db_"

# Cached MCP tool schemas in Claude API format (populated at startup)
_mcp_tools: list[dict] = []
# Set of prefixed tool names for fast lookup
_mcp_tool_names: set[str] = set()


def init_reporting_mcp() -> None:
    """Read the Reporting MCP URL and token from config."""
    cfg = get_config()
    reporting_cfg = cfg.get("reporting_mcp", {})
    url = reporting_cfg.get("mcp_url", "")

    if not url:
        logger.info("No Reporting MCP URL configured — Reporting MCP disabled")
        return

    global _mcp_url, _token  # noqa: PLW0603
    _mcp_url = url
    _token = reporting_cfg.get("token", "")
    logger.info("Reporting MCP configured (url=%s)", _mcp_url)


def _build_http_client() -> httpx.AsyncClient:
    """Build an httpx client with auth headers and timeout."""
    headers: dict[str, str] = {}
    if _token:
        headers["Authorization"] = f"Bearer {_token}"
    return httpx.AsyncClient(headers=headers, timeout=_MCP_TIMEOUT_SECONDS)


def get_mcp_url() -> str:
    """Return the configured MCP URL, or empty string if not configured."""
    return _mcp_url


def get_server_instructions() -> str:
    """Return cached server instructions, or empty string."""
    return _server_instructions


def get_mcp_tools() -> list[dict]:
    """Return cached MCP tool schemas in Claude API format."""
    return _mcp_tools


def is_mcp_tool(name: str) -> bool:
    """Check if a tool name belongs to the Reporting MCP."""
    return name in _mcp_tool_names


def get_mcp_tool_original(prefixed_name: str) -> str:
    """Strip the db_ prefix to get the original MCP tool name."""
    if prefixed_name.startswith(_TOOL_PREFIX):
        return prefixed_name[len(_TOOL_PREFIX) :]
    return prefixed_name


def _mcp_schema_to_claude(tool) -> dict:
    """Convert an MCP tool schema to Claude API tool format."""
    input_schema = {"type": "object", "properties": {}, "required": []}

    if tool.inputSchema:
        props = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])
        input_schema["properties"] = props
        input_schema["required"] = required

    return {
        "name": f"{_TOOL_PREFIX}{tool.name}",
        "description": tool.description or f"MCP tool: {tool.name}",
        "input_schema": input_schema,
    }


def _build_knowledge_tool(domains: list[str]) -> dict:
    """Build a synthetic db_read_knowledge tool from discovered resources."""
    return {
        "name": "db_read_knowledge",
        "description": (
            "Read domain-specific business rules and pitfalls from the "
            "reporting database knowledge base. Use this before writing "
            "complex queries that involve business logic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": domains,
                    "description": "Knowledge domain to read.",
                },
            },
            "required": ["domain"],
        },
    }


def _build_prompt_tool(prompts_info: list[dict]) -> dict:
    """Build a synthetic db_get_prompt tool from discovered prompts."""
    names = [p["name"] for p in prompts_info]
    descriptions = [f"{p['name']}: {p.get('description', '')}" for p in prompts_info]

    return {
        "name": "db_get_prompt",
        "description": (
            "Get a structured investigation template from the reporting "
            "database. Templates contain step-by-step analysis plans with "
            "critical business rules. Available: " + "; ".join(descriptions)
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt_name": {
                    "type": "string",
                    "enum": names,
                    "description": "Template to retrieve.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Template-specific arguments.",
                },
            },
            "required": ["prompt_name"],
        },
    }


async def fetch_server_instructions() -> str:
    """Connect to the MCP server and discover all capabilities.

    Called once at startup. Fetches and caches:
    1. Server instructions (schema reference, JOIN patterns, pitfalls)
    2. Tool schemas (converted to Claude API format)
    3. Resource URIs (used to build db_read_knowledge tool)
    4. Prompt templates (used to build db_get_prompt tool)
    """
    global _server_instructions, _mcp_tools, _mcp_tool_names  # noqa: PLW0603

    if not _mcp_url:
        return ""

    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            streamable_http_client(
                url=_mcp_url,
                http_client=_build_http_client(),
            ) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            init_result = await session.initialize()

            # 1. Cache instructions
            instructions = init_result.instructions or ""
            if instructions:
                _server_instructions = instructions
                logger.info(
                    "Reporting MCP instructions fetched (%d chars)",
                    len(instructions),
                )
            else:
                logger.warning("Reporting MCP returned no instructions")

            # 2. Discover and cache tools
            tools_result = await session.list_tools()
            claude_tools: list[dict] = []
            for tool in tools_result.tools:
                if tool.name == "query":
                    # Handled by query_provisions_db wrapper (SQL validation)
                    continue
                claude_tools.append(_mcp_schema_to_claude(tool))

            # 3. Discover resources → build db_read_knowledge tool
            try:
                resources_result = await session.list_resources()
                knowledge_domains: list[str] = []
                for resource in resources_result.resources:
                    uri = str(resource.uri)
                    if uri.startswith("database://knowledge/"):
                        domain = uri.split("/")[-1]
                        knowledge_domains.append(domain)

                if knowledge_domains:
                    claude_tools.append(_build_knowledge_tool(sorted(knowledge_domains)))
                    logger.info(
                        "Reporting MCP knowledge domains: %s",
                        ", ".join(sorted(knowledge_domains)),
                    )
            except Exception:
                logger.warning(
                    "Could not discover MCP resources — db_read_knowledge will not be available"
                )

            # 4. Discover prompts → build db_get_prompt tool
            try:
                prompts_result = await session.list_prompts()
                prompts_info: list[dict] = []
                for prompt in prompts_result.prompts:
                    prompts_info.append(
                        {
                            "name": prompt.name,
                            "description": prompt.description or "",
                        }
                    )

                if prompts_info:
                    claude_tools.append(_build_prompt_tool(prompts_info))
                    logger.info(
                        "Reporting MCP prompts: %s",
                        ", ".join(p["name"] for p in prompts_info),
                    )
            except Exception:
                logger.warning(
                    "Could not discover MCP prompts — db_get_prompt will not be available"
                )

            # Cache the final tool list
            _mcp_tools = claude_tools
            _mcp_tool_names = {t["name"] for t in claude_tools}

            tool_names = [t["name"] for t in claude_tools]
            logger.info(
                "Reporting MCP tools discovered (%d): %s",
                len(tool_names),
                ", ".join(tool_names),
            )

            return instructions

    except Exception:
        logger.exception("Failed to initialize Reporting MCP")
        return ""


async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on the Reporting MCP server via streamable HTTP."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    if not _mcp_url:
        return {"error": "Reporting MCP not configured (set reporting_mcp.mcp_url)"}

    try:
        async with (
            streamable_http_client(
                url=_mcp_url,
                http_client=_build_http_client(),
            ) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                error_text = " ".join(
                    block.text for block in result.content if hasattr(block, "text")
                )
                return {"error": error_text or "Reporting MCP tool returned an error"}

            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)

            return {"result": "\n".join(parts)}

    except Exception as exc:
        logger.exception("Reporting MCP call failed (tool=%s)", tool_name)
        return {"error": f"Reporting MCP call failed: {exc}"}


async def read_resource(uri: str) -> dict[str, Any]:
    """Read a resource from the Reporting MCP server."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    if not _mcp_url:
        return {"error": "Reporting MCP not configured"}

    try:
        async with (
            streamable_http_client(
                url=_mcp_url,
                http_client=_build_http_client(),
            ) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            from pydantic import AnyUrl

            result = await session.read_resource(AnyUrl(uri))

            parts: list[str] = []
            for block in result.contents:
                if hasattr(block, "text"):
                    parts.append(block.text)

            return {"result": "\n".join(parts)}

    except Exception as exc:
        logger.exception("Reporting MCP resource read failed (uri=%s)", uri)
        return {"error": f"Resource read failed: {exc}"}


async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
    """Fetch a prompt template from the Reporting MCP server."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    if not _mcp_url:
        return {"error": "Reporting MCP not configured"}

    prompt_args = arguments or {}

    try:
        async with (
            streamable_http_client(
                url=_mcp_url,
                http_client=_build_http_client(),
            ) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.get_prompt(name, prompt_args)

            parts: list[str] = []
            for msg in result.messages:
                if hasattr(msg.content, "text"):
                    parts.append(msg.content.text)
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if hasattr(block, "text"):
                            parts.append(block.text)

            return {"result": "\n".join(parts)}

    except Exception as exc:
        logger.exception("Reporting MCP prompt fetch failed (name=%s)", name)
        return {"error": f"Prompt fetch failed: {exc}"}
