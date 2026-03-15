"""GitHub MCP sidecar connection — SSE client for the supergateway bridge."""

import logging
from typing import Any

from src.config import get_config

logger = logging.getLogger(__name__)

_mcp_url: str = ""


def init_github_mcp() -> None:
    """Read the GitHub MCP sidecar URL from config."""
    cfg = get_config()
    github_cfg = cfg.get("github", {})
    url = github_cfg.get("mcp_url", "")

    if not url:
        logger.info("No GitHub MCP URL configured — GitHub file lookups disabled")
        return

    global _mcp_url  # noqa: PLW0603
    _mcp_url = url
    logger.info("GitHub MCP configured (url=%s)", _mcp_url)


def get_mcp_url() -> str:
    """Return the configured MCP SSE URL, or empty string if not configured."""
    return _mcp_url


async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on the GitHub MCP sidecar via SSE.

    Opens a fresh SSE connection per call.  The sidecar runs on localhost so
    the overhead is negligible, and this avoids having to manage a persistent
    session with reconnection logic.
    """
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    if not _mcp_url:
        return {"error": "GitHub MCP sidecar not configured (set github.mcp_url)"}

    try:
        async with sse_client(url=_mcp_url) as streams, ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                error_text = " ".join(
                    block.text for block in result.content if hasattr(block, "text")
                )
                return {"error": error_text or "MCP tool returned an error"}

            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)

            return {"content": "\n".join(parts)}

    except Exception as exc:
        logger.exception("GitHub MCP call failed (tool=%s)", tool_name)
        return {"error": f"GitHub MCP call failed: {exc}"}
