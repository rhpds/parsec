"""Per-agent SDK invocation profiles, and which agents may use the SDK.

Replaces ``src/agent/icinga_sdk.py``, which hardcoded a single agent: it built a
profile for ``icinga`` and returned ``{}`` for everything else, so flipping any
other agent to ``agent.runtime: sdk`` would have run it with no tools at all.

A profile is the set of ``AgentSdkClient.complete()`` kwargs for one sub-agent:
the tool surface (the in-process bridge from :mod:`src.agent.parsec_mcp`), the
skill to load, the tool allow-list, and the turn budget.

Two config keys drive this:

``agent.sdk.enabled_agents``
    Which sub-agents may run on the SDK, *in addition to* ``agent.runtime``
    being ``sdk``. Both must agree, so ``agent.runtime: legacy`` remains a
    universal kill switch — no agent is ever SDK-only.

``agent.sdk.allow_writes``
    Whether state-mutating tool actions are permitted. Off by default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.llm.config_section import section

logger = logging.getLogger(__name__)

#: Agents allowed on the SDK when config says nothing. Icinga is the Phase-2
#: pilot and the only one verified in-cluster.
DEFAULT_ENABLED_AGENTS: frozenset[str] = frozenset({"icinga"})

#: Extra turns beyond an agent's legacy ``max_rounds``. The model spends turns
#: on ``ToolSearch`` loading deferred MCP schemas before it can call a bridged
#: tool, so an SDK turn budget equal to ``max_rounds`` is not comparable to the
#: legacy round budget — it is strictly tighter.
TURN_HEADROOM = 3


@dataclass(frozen=True)
class SdkProfile:
    """Resolved SDK invocation profile for one sub-agent."""

    agent_type: str
    skill: str | None = None
    max_turns: int | None = None
    allow_writes: bool = False
    #: Extra remote MCP servers, e.g. ``{"github": {"type": "http", ...}}``.
    extra_mcp_servers: dict[str, Any] = field(default_factory=dict)


#: Skills each agent may use. Every entry is preloaded into that agent's context
#: via ``AgentDefinition.skills``; the SDK also keeps unlisted skills invocable
#: through the ``Skill`` tool, so this is the "definitely available" set rather
#: than a whitelist.
#:
#: Previously one skill per agent, which left three shipped skills
#: (``cost-spike-investigation``, ``aap2-job-failure-triage``,
#: ``provision-lookup``) attached to nothing — they loaded, appeared in the
#: Skills tab, and no agent could ever use them.
_AGENT_SKILLS: dict[str, tuple[str, ...]] = {
    "icinga": ("icinga-triage",),
    "cost": ("cost-anomaly-triage", "cost-spike-investigation", "provision-lookup"),
    "aap2": ("aap2-job-failure-rca", "aap2-job-failure-triage", "root-cause-analysis"),
    "security": ("abuse-account-detection", "provision-lookup"),
    "babylon": ("provision-lookup",),
    "ocpv": (),
}


def skills_for(agent_type: str) -> list[str]:
    """Skills to preload for ``agent_type``, filtered to those actually shipped.

    A name that is not on disk would be silently ignored by the SDK, so it is
    dropped here instead and logged — an agent quietly missing its skill is the
    failure mode this whole path is meant to make visible.
    """
    wanted = _AGENT_SKILLS.get(agent_type, ())
    if not wanted:
        return []
    available = discoverable_skill_names()
    if not available:
        # Discovery unavailable (e.g. unit tests with no skills root): trust the
        # map rather than silently dropping everything.
        return list(wanted)
    present = [s for s in wanted if s in available]
    missing = [s for s in wanted if s not in available]
    if missing:
        logger.warning(
            "Agent %s: skills not found on disk, dropped: %s (available: %s)",
            agent_type,
            ", ".join(missing),
            ", ".join(sorted(available)),
        )
    return present


def discoverable_skill_names() -> frozenset[str]:
    """Names the SDK can actually load, i.e. what is under its discovery root."""
    try:
        from src.skills.sdk_root import sdk_skills_root

        root = sdk_skills_root()
        if not root.is_dir():
            return frozenset()
        return frozenset(p.name for p in root.iterdir() if (p / "SKILL.md").is_file())
    except Exception:
        logger.exception("Could not enumerate SDK-visible skills")
        return frozenset()


def enabled_sdk_agents(config: Any) -> frozenset[str]:
    """Return the sub-agents permitted to run on the SDK.

    Fail-safe: a malformed value yields the default rather than raising or
    enabling everything, matching ``src.llm.runtime.get_runtime``'s posture.
    Unknown agent names are dropped with a warning instead of failing startup —
    a hand-edited ConfigMap should not CrashLoopBackOff a single-replica pod.
    """
    from src.agent.agents import AGENTS

    sdk_cfg = _sdk_section(config)
    raw = sdk_cfg.get("enabled_agents", None)
    if raw is None:
        return DEFAULT_ENABLED_AGENTS
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list | tuple | set | frozenset):
        logger.warning(
            "agent.sdk.enabled_agents must be a list or comma-separated string, got %s — "
            "falling back to %s",
            type(raw).__name__,
            sorted(DEFAULT_ENABLED_AGENTS),
        )
        return DEFAULT_ENABLED_AGENTS

    names = {str(x).strip() for x in raw if str(x).strip()}
    if "all" in names:
        names = set(AGENTS)

    known = {n for n in names if n in AGENTS}
    for unknown in sorted(names - known):
        logger.warning("agent.sdk.enabled_agents lists unknown agent %r — ignoring", unknown)
    return frozenset(known)


def sdk_profile_for(agent_type: str, config: Any) -> dict[str, Any]:
    """Build the ``complete()`` kwargs for ``agent_type``.

    Returns the bridge MCP server, its allow-list, the agent's skill if one
    exists, and a turn budget derived from the agent's legacy ``max_rounds``.
    Returns ``{}`` for an unknown agent so the caller degrades to a plain SDK
    call rather than crashing.
    """
    from src.agent.agents import AGENTS
    from src.agent.parsec_mcp import SERVER_NAME, build_server, tool_names_for

    agent_cfg = AGENTS.get(agent_type)
    if agent_cfg is None:
        logger.warning("No AgentConfig for %r; SDK profile is empty", agent_type)
        return {}

    sdk_cfg = _sdk_section(config)
    allow_writes = bool(sdk_cfg.get("allow_writes", False))

    # Evaluate the late-bound property so dynamically discovered db_* tools are
    # included in whatever this request's tool surface actually is.
    schemas = list(agent_cfg.tools)

    profile: dict[str, Any] = {
        "mcp_servers": {SERVER_NAME: build_server(schemas, allow_writes=allow_writes)},
        "allowed_tools": tool_names_for(schemas),
        "max_turns": agent_cfg.max_rounds + TURN_HEADROOM,
    }

    skills = skills_for(agent_type)
    if skills:
        profile["skills"] = skills

    extra = _extra_mcp_servers(agent_type, config)
    if extra:
        profile["mcp_servers"].update(extra)
        profile["allowed_tools"] = list(profile["allowed_tools"]) + [
            f"mcp__{name}" for name in extra
        ]

    logger.info(
        "SDK profile for %s: %d bridged tools, skills=%s, max_turns=%d, writes=%s",
        agent_type,
        len(schemas),
        ", ".join(skills) or "none",
        profile["max_turns"],
        allow_writes,
    )
    return profile


def _extra_mcp_servers(agent_type: str, config: Any) -> dict[str, Any]:
    """Remote MCP servers to expose *in addition to* the bridge.

    Empty by design. Every Parsec backend — including Icinga and GitHub, which
    genuinely are remote MCP servers — is reached through the bridge so that
    ``src/tools/*`` keeps running: the Icinga write guardrails and action-alias
    map, and ``_redact_secrets`` on GitHub file contents. Wiring a remote server
    in here would hand the model results that never passed through Parsec code.

    Kept as a seam because a future backend may have no Python wrapper worth
    preserving; anything added here must be reviewed against that rule.
    """
    return {}


def _sdk_section(config: Any) -> dict[str, Any]:
    agent_section = section(config, "agent")
    return section(agent_section, "sdk") if agent_section else {}
