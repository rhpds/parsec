"""Tool: fetch_github_file — fetch files and directories from GitHub repos via MCP."""

import logging
import re

from src.connections.github_mcp import call_tool

logger = logging.getLogger(__name__)

_SECRET_PATTERNS = re.compile(
    r"(access_key|secret_key|password|token|pull_secret|hmac_key|eab_key|"
    r"ssh_pass|secret_access|api_key|client_secret|activationkey)",
    re.IGNORECASE,
)


def _redact_secrets(text: str) -> str:
    """Redact lines that look like they contain secrets."""
    lines = text.split("\n")
    redacted: list[str] = []
    for line in lines:
        if _SECRET_PATTERNS.search(line):
            key_part = line.split(":")[0] if ":" in line else line.split("=")[0]
            redacted.append(f"{key_part.rstrip()}: <REDACTED>")
        else:
            redacted.append(line)
    return "\n".join(redacted)


async def fetch_github_file(
    owner: str,
    repo: str,
    path: str,
    ref: str = "",
) -> dict:
    """Fetch a file or directory listing from a GitHub repository.

    Uses the GitHub remote MCP server's get_file_contents tool.

    Args:
        owner: Repository owner (e.g. "rhpds")
        repo:  Repository name (e.g. "agnosticv")
        path:  Path within the repo (e.g. "sandboxes-gpte/ANS_BU_WKSP_RHEL_90/common.yaml")
        ref:   Git ref — branch name, tag, or commit SHA (default: repo default branch)

    Returns:
        dict with "content" (file text or directory listing) or "error".
    """
    arguments: dict[str, str] = {
        "owner": owner,
        "repo": repo,
        "path": path,
    }
    if ref:
        arguments["ref"] = ref

    result = await call_tool("get_file_contents", arguments)

    if "error" in result:
        return result

    content = result.get("content", "")
    if content:
        result["content"] = _redact_secrets(content)

    return result
