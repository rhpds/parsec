"""Icinga MCP connection — SSE client for the monitoring-mcp sidecar server."""

import logging
from typing import Any

from src.config import get_config

logger = logging.getLogger(__name__)

_mcp_url: str = ""


def init_icinga_mcp() -> None:
    """Read the Icinga MCP URL from config."""
    cfg = get_config()
    icinga_cfg = cfg.get("icinga", {})
    url = icinga_cfg.get("mcp_url", "")

    if not url:
        logger.info("No Icinga MCP URL configured — Icinga monitoring disabled")
        return

    global _mcp_url  # noqa: PLW0603
    _mcp_url = url
    logger.info("Icinga MCP configured (url=%s)", _mcp_url)


def get_mcp_url() -> str:
    """Return the configured MCP URL, or empty string if not configured."""
    return _mcp_url


async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on the Icinga MCP server via SSE transport.

    Opens a fresh connection per call to avoid managing persistent sessions.
    """
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    if not _mcp_url:
        return {"error": "Icinga MCP not configured (set icinga.mcp_url)"}

    try:
        async with (
            sse_client(url=_mcp_url) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                error_text = " ".join(
                    block.text for block in result.content if hasattr(block, "text")
                )
                return {"error": error_text or "Icinga MCP tool returned an error"}

            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)

            return {"result": "\n".join(parts)}

    except Exception as exc:
        logger.exception("Icinga MCP call failed (tool=%s)", tool_name)
        return {"error": f"Icinga MCP call failed: {exc}"}
