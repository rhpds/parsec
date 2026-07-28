"""The Agent SDK as Parsec's orchestration harness.

The legacy orchestrator is a hand-written tool-use loop: call the model, inspect
``tool_use`` blocks, dispatch, append ``tool_result``, repeat — and delegation to
a sub-agent is itself a tool (``investigate_costs`` and friends) that runs a
second hand-written loop inside the first.

This module inverts that. One :class:`ClaudeSDKClient` session *is* the
orchestrator, and everything else plugs into it:

* the six sub-agents become native SDK subagents (``ClaudeAgentOptions.agents``),
  so delegation is the SDK's own ``Agent`` tool rather than a Parsec tool wrapping
  a nested loop;
* every Parsec tool is served by the in-process bridge in
  :mod:`src.agent.parsec_mcp`, so ``src/tools/*`` — the guardrails, redaction and
  trimming — still runs;
* skills mount through ``AgentDefinition.skills``.

Each subagent gets a fresh context and returns only its final message, which is
what the legacy delegation tool did by hand.

**Streaming.** ``include_partial_messages`` makes the CLI emit raw Anthropic
stream events, so text arrives as token deltas rather than one block at the end.
:func:`run_agent_via_sdk` translates the SDK message stream into exactly the SSE
events the existing frontend consumes, so the UI cannot tell which runtime
produced them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

logger = logging.getLogger(__name__)

#: Tools the orchestrator itself may call. Delegation is deliberately excluded —
#: the SDK's ``Agent`` tool replaces the ``investigate_*`` wrappers.
_ORCHESTRATOR_EXTRA_TOOLS = ("Agent",)


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def _agent_definitions(config: Any) -> dict[str, Any]:
    """Turn Parsec's ``AGENTS`` registry into SDK subagent definitions.

    ``description`` is what the SDK matches on when deciding to delegate, so the
    registry's own description — the same text the legacy ``investigate_*`` tool
    schemas advertised — carries over verbatim.
    """
    from claude_agent_sdk import AgentDefinition

    from src.agent.agents import AGENTS
    from src.agent.parsec_mcp import tool_names_for
    from src.agent.sdk_profiles import _AGENT_SKILLS, TURN_HEADROOM, enabled_sdk_agents
    from src.agent.system_prompt import get_agent_prompt

    enabled = enabled_sdk_agents(config)
    definitions: dict[str, Any] = {}

    for agent_type, agent_cfg in AGENTS.items():
        if agent_type not in enabled:
            # Not enabled for the SDK: leave it out so the orchestrator cannot
            # delegate to a half-migrated agent.
            continue

        kwargs: dict[str, Any] = {
            "description": agent_cfg.description or f"{agent_cfg.name} specialist",
            # Same date grounding the legacy sub-agent loop appends
            # (agents.py:579/828); without it an SDK agent reasons about
            # "the last 30 days" with no idea what today is.
            "prompt": f"{get_agent_prompt(agent_type)}\n\nToday's date is {_today()}.",
            "tools": tool_names_for(list(agent_cfg.tools)),
            "maxTurns": agent_cfg.max_rounds + TURN_HEADROOM,
        }
        skill = _AGENT_SKILLS.get(agent_type)
        if skill:
            kwargs["skills"] = [skill]
        definitions[agent_type] = AgentDefinition(**kwargs)

    logger.info("SDK orchestrator: %d subagents (%s)", len(definitions), ", ".join(definitions))
    return definitions


def _union_tool_schemas() -> list[dict]:
    """Every tool any agent might call, de-duplicated by name.

    The bridge is built once per request and shared by the orchestrator and all
    subagents; each ``AgentDefinition.tools`` then narrows it per agent.
    """
    from src.agent.agents import AGENTS
    from src.agent.tool_definitions import get_orchestrator_direct_tools

    seen: dict[str, dict] = {}
    for schema in get_orchestrator_direct_tools():
        seen.setdefault(schema["name"], schema)
    for agent_cfg in AGENTS.values():
        for schema in agent_cfg.tools:
            seen.setdefault(schema["name"], schema)
    return list(seen.values())


def build_orchestrator_options(config: Any, *, system: str) -> Any:
    """Assemble ``ClaudeAgentOptions`` for one orchestrator turn."""
    from claude_agent_sdk import ClaudeAgentOptions

    from src.agent.parsec_mcp import SERVER_NAME, build_server, tool_names_for
    from src.agent.sdk_profiles import _sdk_section
    from src.llm.agent_sdk_client import AgentSdkConfig, build_subprocess_env

    sdk_cfg = _sdk_section(config)
    allow_writes = bool(sdk_cfg.get("allow_writes", False))

    schemas = _union_tool_schemas()
    server = build_server(schemas, allow_writes=allow_writes)

    # Auto-approve every bridged tool, not just the orchestrator's own.
    #
    # `allowed_tools` is the session-wide *approval* list and `permission_mode`
    # is "dontAsk" (deny anything not pre-approved), so a tool missing from here
    # is denied even when a subagent is explicitly allowed to use it. Listing
    # only the orchestrator's direct tools meant every delegated call was
    # refused: the icinga agent reported "unable to access the monitoring system
    # due to permission restrictions" and answered with no tool calls at all.
    #
    # This does not widen what any individual agent can reach — availability is
    # still per-agent via `AgentDefinition.tools` (see `_agent_definitions`).
    # Approval is session-wide; availability is per-agent.
    approved_tools = tool_names_for(schemas)

    anthropic_cfg = _section_get(config, "anthropic")
    model = sdk_cfg.get("model") or anthropic_cfg.get("model") or "claude-sonnet-4-6"
    max_turns = int(sdk_cfg.get("max_turns") or anthropic_cfg.get("max_tool_rounds") or 10)

    defaults = AgentSdkConfig(model=str(model))
    return ClaudeAgentOptions(
        model=str(model),
        system_prompt=system,
        max_turns=max_turns,
        agents=_agent_definitions(config),
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[*approved_tools, *_ORCHESTRATOR_EXTRA_TOOLS],
        # Availability, not just auto-approval — see agent_sdk_client._build_options.
        # "Agent" must be present or delegation cannot happen at all.
        tools=[*defaults.builtin_tools, *_ORCHESTRATOR_EXTRA_TOOLS],
        permission_mode=str(sdk_cfg.get("permission_mode") or defaults.permission_mode),
        strict_mcp_config=True,
        setting_sources=list(defaults.setting_sources),
        env=build_subprocess_env(_tracing_env(config)),
        cwd=str(sdk_cfg.get("cwd") or "") or None,
        # Token-level deltas, so the UI streams like the legacy path.
        include_partial_messages=True,
    )


def _orchestrator_system() -> str:
    """The orchestrator prompt, with the same date grounding the legacy path adds.

    ``orchestrator.py:1248`` appends ``Today's date is {today}`` and the SDK path
    did not, so SDK answers were reasoning about relative windows ("last 30
    days") without knowing the date. That asymmetry also silently confounded
    every legacy-vs-SDK comparison run so far.
    """
    from datetime import UTC, datetime

    from src.agent.system_prompt import get_agent_prompt

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"{get_agent_prompt('orchestrator')}\n\nToday's date is {today}."


def _section_get(config: Any, key: str) -> dict:
    from src.llm.config_section import section

    return section(config, key) or {}


def _tracing_env(config: Any) -> dict[str, str]:
    from src.llm.sdk_tracing import build_tracing_env

    sdk_cfg = _sdk_section_safe(config)
    return {**build_tracing_env(config), **(sdk_cfg.get("env") or {})}


def _sdk_section_safe(config: Any) -> dict:
    from src.agent.sdk_profiles import _sdk_section

    return _sdk_section(config)


async def run_agent_via_sdk(
    question: str,
    conversation_history: list | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Run one orchestrator turn on the SDK, yielding Parsec SSE events.

    Mirrors :func:`src.agent.orchestrator.run_agent`'s contract exactly: same
    event names, same ordering, same terminating ``history`` + ``done`` pair, so
    ``routes/query.py`` and the frontend are unchanged.
    """
    from src.agent.parsec_mcp import sse_sink
    from src.agent.sdk_stream import SdkEventTranslator
    from src.agent.streaming import sse_done, sse_error
    from src.config import get_config
    from src.metrics.collector import MetricsCollector

    cfg = get_config()
    collector = MetricsCollector(conversation_id=conversation_id or session_id or "")
    collector.record_runtime("sdk")
    collector.record_agent_dispatch("orchestrator", routing_method="sdk")

    translator = SdkEventTranslator(question=question, history=conversation_history or [])

    try:
        options = build_orchestrator_options(cfg, system=_orchestrator_system())
    except Exception as e:
        logger.exception("Failed to build SDK orchestrator options")
        yield sse_error(f"SDK orchestrator unavailable: {e}")
        yield sse_done()
        return

    prompt = translator.build_prompt()

    # The bridge pushes tool events onto the same queue the translator drains,
    # so a tool call inside a subagent still reaches the browser.
    token = sse_sink.set(translator.push)
    try:
        from claude_agent_sdk import ClaudeSDKClient

        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                for event in translator.translate(message):
                    yield event
                # Drain anything the bridge queued while that message was handled.
                for event in translator.drain():
                    yield event
    except Exception as e:
        logger.exception("SDK orchestrator failed")
        yield sse_error(str(e))
    finally:
        sse_sink.reset(token)

    for event in translator.finish(collector):
        yield event
