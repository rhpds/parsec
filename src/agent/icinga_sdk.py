"""SDK invocation profile for the Icinga sub-agent.

When Icinga runs on the Agent SDK (``agent.runtime: sdk``), it loads the
``icinga-triage`` SKILL.md and talks to the **same backends** the legacy
``query_icinga`` / GitHub tools use: the ``monitoring-mcp`` sidecar and the
GitHub MCP server. Both are real MCP servers, so the SDK can consume them
directly via ``ClaudeAgentOptions(mcp_servers=...)`` — no per-tool shim.

This module builds the ``skills`` / ``allowed_tools`` / ``mcp_servers`` kwargs
that :meth:`AgentSdkClient.complete` passes through. It is config-only and
import-light (no SDK dependency) so it is unit-testable without the SDK; the
exact MCP-server wire format is verified in-cluster.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Sub-agent that has an SDK profile today (the Phase-2 pilot).
ICINGA_AGENT = "icinga"
ICINGA_SKILL = "icinga-triage"


def sdk_profile_for(agent_type: str, config: Any) -> dict[str, Any]:
    """Return the ``complete()`` profile kwargs for ``agent_type``, or ``{}``.

    Only Icinga has an SDK profile in Phase 2; every other agent runs the SDK
    with no skill/tool specialization (``{}``), so the runner stays generic.
    """
    if agent_type == ICINGA_AGENT:
        return build_icinga_sdk_profile(config)
    return {}


def build_icinga_sdk_profile(config: Any) -> dict[str, Any]:
    """Build the Icinga SDK profile: the skill + the Icinga/GitHub MCP servers.

    Reads ``icinga.mcp_url`` (the monitoring-mcp sidecar, SSE) and ``github.mcp_url``
    (+ ``github.token`` if present, for auth). A server is only added when its URL
    is configured, so a partial config degrades gracefully.
    """
    icinga_cfg = _section(config, "icinga")
    github_cfg = _section(config, "github")

    mcp_servers: dict[str, Any] = {}

    icinga_url = str(icinga_cfg.get("mcp_url", "") or "").strip()
    if icinga_url:
        mcp_servers["icinga"] = {"type": "sse", "url": icinga_url}

    github_url = str(github_cfg.get("mcp_url", "") or "").strip()
    if github_url:
        server: dict[str, Any] = {"type": "http", "url": github_url}
        token = str(github_cfg.get("token", "") or "").strip()
        if token:
            server["headers"] = {"Authorization": f"Bearer {token}"}
        mcp_servers["github"] = server

    profile: dict[str, Any] = {"skills": [ICINGA_SKILL]}
    if mcp_servers:
        profile["mcp_servers"] = mcp_servers
        profile["allowed_tools"] = _allowed_tools(mcp_servers)

    logger.debug("Icinga SDK profile: skill=%s servers=%s", ICINGA_SKILL, list(mcp_servers))
    return profile


def _allowed_tools(mcp_servers: dict[str, Any]) -> list[str]:
    """Whitelist the configured MCP servers' tools (server-level prefixes)."""
    return [f"mcp__{name}" for name in mcp_servers]


def _section(config: Any, key: str) -> dict[str, Any]:
    """Return config sub-section ``key`` as a plain dict (``{}`` if missing)."""
    if config is None:
        return {}
    raw = config.get(key, {}) if hasattr(config, "get") else getattr(config, key, {})
    if raw is None:
        return {}
    if hasattr(raw, "to_dict"):
        return raw.to_dict()
    return dict(raw)
