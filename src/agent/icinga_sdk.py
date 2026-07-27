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
        profile["allowed_tools"] = _allowed_tools(mcp_servers, icinga_cfg)

    logger.info(
        "Icinga SDK profile: skill=%s servers=%s allowed_tools=%s",
        ICINGA_SKILL,
        list(mcp_servers),
        profile.get("allowed_tools", []),
    )
    return profile


#: Read-only Icinga/GitHub tools the ``icinga-triage`` skill uses. Names match
#: the monitoring-mcp sidecar surface the SKILL.md was reconciled against in the
#: #34 review.
_READ_ONLY_TOOLS = (
    "mcp__icinga__get_problems",
    "mcp__icinga__get_services",
    "mcp__icinga__get_hosts",
    "mcp__icinga__get_downtimes",
    "mcp__icinga__get_comments",
    "mcp__github__get_file_contents",
)

#: State-mutating Icinga tools. The SKILL.md marks these "gated write — only
#: when the user explicitly requests them", but that is prose addressed to the
#: model, not an enforced control. They are omitted from ``allowed_tools``
#: unless ``icinga.sdk_allow_writes`` is set, so the gate is real.
_WRITE_TOOLS = (
    "mcp__icinga__acknowledge_problem",
    "mcp__icinga__schedule_downtime",
    "mcp__icinga__reschedule_check",
    "mcp__icinga__add_comment",
    "mcp__icinga__remove_comment",
    "mcp__icinga__remove_downtime",
)


def _allowed_tools(mcp_servers: dict[str, Any], icinga_cfg: dict[str, Any]) -> list[str]:
    """Enumerate the tools the Icinga SDK agent may call.

    Previously this returned bare ``["mcp__icinga", "mcp__github"]``. Per the
    Claude Code permission rules a bare ``mcp__<server>`` entry matches *any*
    tool that server provides, so it also admitted the six state-mutating Icinga
    operations — acknowledging problems, scheduling and removing downtime, and
    adding or removing comments — on a monitoring system RHDP ops relies on.
    This addresses the #32 review finding "enumerate read-only tools explicitly
    and require a separate confirmation flow for write actions".

    Only servers that are actually configured contribute tools, so a partial
    config still degrades gracefully. ``icinga.sdk_allowed_tools`` overrides the
    list outright, so a sidecar that names its tools differently is a config fix
    rather than a redeploy.
    """
    override = icinga_cfg.get("sdk_allowed_tools") or []
    if override:
        return [str(t) for t in override]

    names = [t for t in _READ_ONLY_TOOLS if t.split("__")[1] in mcp_servers]
    if icinga_cfg.get("sdk_allow_writes"):
        names += [t for t in _WRITE_TOOLS if t.split("__")[1] in mcp_servers]
        logger.warning("Icinga SDK profile: WRITE actions enabled (icinga.sdk_allow_writes)")
    return names


# NOTE: `_section` duplicates `_get_section` in `agent_sdk_client.py`. PR #31
# extracts a shared `src/llm/config_section.py`; once #31 merges, rebase and
# import `section()` from there instead of keeping this copy.  [PR #34 review]
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
