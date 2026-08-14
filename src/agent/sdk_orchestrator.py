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
from collections.abc import AsyncGenerator, Iterable
from typing import Any, cast

from claude_agent_sdk.types import PermissionMode, SettingSource

logger = logging.getLogger(__name__)

#: Tools the orchestrator itself may call. Delegation is deliberately excluded —
#: the SDK's ``Agent`` tool replaces the ``investigate_*`` wrappers.
#: The SDK types these as Literals. Config is free-form strings, so validate
#: rather than cast: a typo in ``agent.sdk.permission_mode`` should fall back to
#: the safe default with a warning, not be silently coerced into an invalid
#: value the SDK then rejects at runtime.
_PERMISSION_MODES: tuple[PermissionMode, ...] = (
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "dontAsk",
    "auto",
)
_SETTING_SOURCES: tuple[SettingSource, ...] = ("user", "project", "local")


def _permission_mode(raw: object, fallback: str) -> PermissionMode:
    for candidate in (str(raw or "").strip(), str(fallback or "").strip()):
        if candidate in _PERMISSION_MODES:
            return cast(PermissionMode, candidate)
        if candidate:
            logger.warning(
                "Unknown agent.sdk.permission_mode %r — falling back to 'dontAsk'", candidate
            )
    return "dontAsk"


def _setting_sources(raw: Iterable[str]) -> list[SettingSource]:
    out: list[SettingSource] = []
    for item in raw:
        name = str(item).strip()
        if name in _SETTING_SOURCES:
            out.append(cast(SettingSource, name))
        elif name:
            logger.warning("Unknown agent.sdk.setting_sources entry %r — ignored", name)
    return out


_ORCHESTRATOR_EXTRA_TOOLS = ("Agent",)


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


#: Appended to every subagent prompt.
#:
#: Only a subagent's FINAL message returns to the orchestrator — its tool
#: results stay in its own context. So whatever it omits from that message is
#: lost for good, and the orchestrator then summarises what it does receive.
#: Measured effect of that double compression: SDK answers ran 659 characters
#: against legacy's 10,785 on the same question, and an LLM judge scored the SDK
#: 3.00 on actionability against legacy's 4.43 — not because it investigated
#: less (tool counts were comparable) but because the findings never survived
#: the hand-off.
_SUBAGENT_OUTPUT_CONTRACT = """

## Reporting your findings

Your final message is the ONLY thing that leaves this investigation — your tool
results are not visible to anyone else. Write it as the complete answer for a
Red Hat SRE who will act on it, not as a summary for another agent.

Include every specific you found: job IDs, hostnames, cluster and account names,
error strings, dollar amounts, counts, dates, SQL you ran, file paths and
`owner/repo:path:line` citations. Keep your tables. Keep your section headings.
State the root cause and the concrete next steps.

Length is not a virtue, but omitting specifics is a defect — a tidy answer that
drops the identifiers an SRE needs is worse than a long one that keeps them.
"""


def _agent_definitions(config: Any) -> dict[str, Any]:
    """Turn Parsec's ``AGENTS`` registry into SDK subagent definitions.

    ``description`` is what the SDK matches on when deciding to delegate, so the
    registry's own description — the same text the legacy ``investigate_*`` tool
    schemas advertised — carries over verbatim.
    """
    from claude_agent_sdk import AgentDefinition

    from src.agent.agents import AGENTS
    from src.agent.parsec_mcp import tool_names_for
    from src.agent.sdk_profiles import TURN_HEADROOM, enabled_sdk_agents, skills_for
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
            "prompt": (
                f"{get_agent_prompt(agent_type)}"
                f"\n\nToday's date is {_today()}."
                f"{_SUBAGENT_OUTPUT_CONTRACT}"
            ),
            "tools": tool_names_for(list(agent_cfg.tools)),
            "maxTurns": agent_cfg.max_rounds + TURN_HEADROOM,
        }
        skills = skills_for(agent_type)
        if skills:
            kwargs["skills"] = skills
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
    from src.llm.agent_sdk_client import (
        AgentSdkConfig,
        backend_cli_env,
        build_subprocess_env,
        default_cli_path,
    )

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
    # Pin the binary here too. Without it the SDK picks the CLI bundled in its
    # own wheel, which floats with claude-agent-sdk: the image's 2.1.185 sends
    # an `anthropic-beta: thinking-token-count-*` header that the LiteLLM
    # gateway's Vertex upstream rejects with a 400, so every orchestrated turn
    # died on a backend the sub-agent path had already been fixed for.
    cli_path = str(sdk_cfg.get("cli_path", "") or "").strip() or default_cli_path()
    return ClaudeAgentOptions(
        model=str(model),
        cli_path=cli_path,
        system_prompt=system,
        max_turns=max_turns,
        agents=_agent_definitions(config),
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[*approved_tools, *_ORCHESTRATOR_EXTRA_TOOLS],
        # Availability, not just auto-approval — see agent_sdk_client._build_options.
        # "Agent" must be present or delegation cannot happen at all.
        tools=[*defaults.builtin_tools, *_ORCHESTRATOR_EXTRA_TOOLS],
        permission_mode=_permission_mode(sdk_cfg.get("permission_mode"), defaults.permission_mode),
        strict_mcp_config=True,
        setting_sources=_setting_sources(defaults.setting_sources),
        env=build_subprocess_env(_tracing_env(config), backend_cli_env(config)),
        cwd=str(sdk_cfg.get("cwd") or "") or None,
        # Token-level deltas, so the UI streams like the legacy path.
        include_partial_messages=True,
    )


def _delegation_addendum(config: Any) -> str:
    """Translate the prompt's ``investigate_*`` instructions onto the Agent tool.

    ``config/prompts/orchestrator.md`` tells the model to delegate by calling
    ``investigate_costs``, ``investigate_icinga`` and friends. Those tools do not
    exist on this path — the SDK's own ``Agent`` tool replaces them — so without
    this the model is instructed to use tools it does not have. It cannot, so it
    silently does the whole investigation inline with the bridged tools instead:
    delegation never fires, the sub-agent prompts and skills are never loaded,
    and the only visible symptom is that no ``agent_start`` event is emitted.

    Rewriting the prompt itself is not an option while the legacy runtime still
    reads the same file, so the mapping is appended for the SDK path only.
    """
    from src.agent.agents import AGENTS
    from src.agent.sdk_profiles import enabled_sdk_agents

    enabled = sorted(enabled_sdk_agents(config))
    if not enabled:
        return ""

    lines = [
        "",
        "## Delegation on this runtime",
        "",
        "The `investigate_*` tools described above do NOT exist here. Delegate with",
        "the `Agent` tool instead, passing `subagent_type`. Each specialist has its",
        "own prompt, tools and skill, and returns a finished answer:",
        "",
    ]
    legacy_names = {
        "cost": "investigate_costs",
        "aap2": "investigate_aap2_job",
        "babylon": "investigate_babylon",
        "security": "investigate_security",
        "ocpv": "investigate_ocpv",
        "icinga": "investigate_icinga",
    }
    for agent_type in enabled:
        cfg = AGENTS.get(agent_type)
        if not cfg:
            continue
        was = legacy_names.get(agent_type)
        lines.append(
            f'- `subagent_type="{agent_type}"` — {cfg.name}'
            + (f" (replaces `{was}`)" if was else "")
        )
    lines += [
        "",
        "Delegate whenever a question falls in one of those domains, exactly as the",
        "instructions above intend. Handle only cross-domain synthesis and your own",
        "direct tools yourself.",
        "",
        "### Relay specialist findings in full",
        "",
        "A specialist's reply is the finished answer for its domain. **Relay it in",
        "full.** Do not condense it, do not replace its tables with a summary, and do",
        "not drop its specifics — job IDs, hostnames, error strings, dollar amounts,",
        "counts, SQL, file paths and `owner/repo:path:line` citations must all survive",
        "into your response verbatim. Reproduce its structure and section headings.",
        "",
        "Add only what the specialist could not: cross-domain synthesis when you",
        "consulted several, and a short lead sentence. If you consulted exactly one",
        "specialist, your answer should be essentially its answer.",
        "",
        "Detail is what makes an answer actionable. An SRE reading this needs the",
        "specific values in order to act; a tidy precis of them is worse than useless",
        "because it looks complete while omitting what they need.",
    ]
    return "\n".join(lines)


def _orchestrator_system(config: Any) -> str:
    """The orchestrator prompt, adapted for the SDK runtime.

    Two additions over the raw prompt file: the delegation mapping above, and the
    date grounding the legacy path appends at ``orchestrator.py:1248``. Without
    the date, SDK answers reason about relative windows ("last 30 days") with no
    idea what today is — an asymmetry that also silently confounded every
    legacy-vs-SDK comparison run so far.
    """
    from datetime import UTC, datetime

    from src.agent.system_prompt import get_agent_prompt

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return (
        f"{get_agent_prompt('orchestrator')}"
        f"{_delegation_addendum(config)}"
        f"\n\nToday's date is {today}."
    )


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
        options = build_orchestrator_options(cfg, system=_orchestrator_system(cfg))
    except Exception as e:
        logger.exception("Failed to build SDK orchestrator options")
        yield sse_error(f"SDK orchestrator unavailable: {e}")
        yield sse_done()
        return

    prompt = translator.build_prompt()

    # The bridge pushes tool events onto the same queue the translator drains,
    # so a tool call inside a subagent still reaches the browser.
    # The bridge's per-request tool-result cache lives in a ContextVar that the
    # legacy path sets after this function returns, so on the SDK path it was
    # never initialised and every repeated tool call re-executed. Set it here,
    # in the same place and for the same lifetime as sse_sink.
    from src.agent.orchestrator import _tool_cache  # local: avoids a cycle

    cache_token = _tool_cache.set({})

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
        _tool_cache.reset(cache_token)

    for event in translator.finish(collector):
        yield event
