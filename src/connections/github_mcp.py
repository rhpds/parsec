"""GitHub MCP connection — streamable HTTP client for GitHub's remote MCP server."""

import logging
from typing import Any

from src.config import get_config
from src.connections.mcp_common import mcp_session

logger = logging.getLogger(__name__)

_mcp_url: str = ""
_token: str = ""


def init_github_mcp() -> None:
    """Read the GitHub MCP URL and token from config."""
    cfg = get_config()
    github_cfg = cfg.get("github", {})
    url = github_cfg.get("mcp_url", "")
    token = github_cfg.get("token", "")

    if not url:
        logger.info("No GitHub MCP URL configured — GitHub file lookups disabled")
        return

    if not token:
        logger.warning("GitHub MCP URL set but no token configured — calls will fail auth")

    global _mcp_url, _token  # noqa: PLW0603
    _mcp_url = url
    _token = token
    logger.info("GitHub MCP configured (url=%s)", _mcp_url)


def get_mcp_url() -> str:
    """Return the configured MCP URL, or empty string if not configured."""
    return _mcp_url


def get_token() -> str:
    """Return the configured GitHub token, or empty string if not configured."""
    return _token


async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on the GitHub remote MCP server via streamable HTTP."""
    if not _mcp_url:
        return {"error": "GitHub MCP not configured (set github.mcp_url)"}

    try:
        headers: dict[str, str] = {}
        if _token:
            headers["Authorization"] = f"Bearer {_token}"

        async with mcp_session(_mcp_url, headers) as (session, _):
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                error_text = " ".join(
                    block.text for block in result.content if hasattr(block, "text")
                )
                return {"error": error_text or "MCP tool returned an error"}

            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "resource") and hasattr(block.resource, "text"):
                    # EmbeddedResource with TextResourceContents (file content)
                    parts.append(block.resource.text)
                elif hasattr(block, "text") and not block.text.startswith(
                    "successfully downloaded"
                ):
                    parts.append(block.text)

            return {"content": "\n".join(parts)}

    except Exception as exc:
        logger.exception("GitHub MCP call failed (tool=%s)", tool_name)
        return {"error": f"GitHub MCP call failed: {exc}"}
