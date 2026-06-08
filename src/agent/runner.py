"""AgentRunner — route a sub-agent task to the legacy loop or the Claude Agent SDK.

Phase-2 foundation. The orchestrator currently calls :func:`src.agent.agents.run_sub_agent`
directly. This module introduces a single dispatch seam that reads ``agent.runtime``
(``legacy|sdk``, default ``legacy``) once and routes each sub-agent task to the matching
runtime, returning the **same structured result dict** either way so callers don't care
which runtime answered.

It is intentionally *additive and dormant*: nothing in the request path imports it yet
(mirroring how the #24 adapter shipped behind the flag). A later PR wires a specific
sub-agent (Icinga) to dispatch through it. With the default ``legacy`` runtime,
:meth:`AgentRunner.run_sub_agent` is a transparent pass-through to the existing loop —
zero behavior change.

The SDK branch here is deliberately minimal: it runs the agent's system prompt through
:meth:`AgentSdkClient.complete` and normalizes the outcome. Per-agent *skill + tool*
wiring (e.g. the Icinga ``query_icinga`` tool surface and ``icinga-triage`` SKILL.md)
lands in the follow-up PR; this module only owns the routing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.llm import RUNTIME_SDK, RuntimeName, get_runtime

logger = logging.getLogger(__name__)


class AgentRunner:
    """Dispatches a sub-agent task to the configured runtime.

    Resolve the runtime once (at construction) so a single request doesn't
    re-read config per sub-agent call. Stateless apart from the resolved
    runtime + config handle, so it's safe to build per request or reuse.
    """

    def __init__(self, config: Any, runtime: RuntimeName | None = None) -> None:
        """Args:
        config: Dynaconf-style config (or plain dict in tests).
        runtime: Force a runtime, bypassing ``agent.runtime``. Mainly for
            tests and benchmark harnesses that want to drive both paths from
            one config; ``None`` resolves the flag via :func:`get_runtime`.
        """
        self._config = config
        self._runtime: RuntimeName = runtime or get_runtime(config)
        logger.debug("AgentRunner initialized with runtime=%s", self._runtime)

    @property
    def runtime(self) -> RuntimeName:
        """The resolved runtime (``legacy`` or ``sdk``)."""
        return self._runtime

    async def run_sub_agent(
        self,
        agent_type: str,
        task: str,
        context: dict | None = None,
        client: Any = None,
        event_queue: Any = None,
        conversation_history: list | None = None,
    ) -> dict:
        """Run one sub-agent task on the active runtime.

        Signature mirrors :func:`src.agent.agents.run_sub_agent` so the runner
        is a drop-in seam. The ``client``/``event_queue``/``conversation_history``
        arguments are only meaningful for the legacy loop and are ignored by the
        SDK path (the SDK runs its own loop and streams internally).

        Returns:
            The legacy result dict (``agent``/``status``/``summary``/``findings``/
            ``data``/``tool_calls``/``duration_seconds`` …). The SDK path produces
            the same shape, with token/cost/cache usage surfaced under ``data``.
        """
        if self._runtime == RUNTIME_SDK:
            return await self._run_via_sdk(agent_type=agent_type, task=task, context=context)
        return await self._run_via_legacy(
            agent_type=agent_type,
            task=task,
            context=context,
            client=client,
            event_queue=event_queue,
            conversation_history=conversation_history,
        )

    # ----------------------------------------------------------------- legacy

    async def _run_via_legacy(
        self,
        *,
        agent_type: str,
        task: str,
        context: dict | None,
        client: Any,
        event_queue: Any,
        conversation_history: list | None,
    ) -> dict:
        """Pass-through to the existing Anthropic tool-use loop (unchanged)."""
        # Imported lazily: agents.py pulls in the full tool/orchestrator graph,
        # which we don't want to import just to construct an SDK-runtime runner.
        from src.agent.agents import run_sub_agent as legacy_run_sub_agent

        return await legacy_run_sub_agent(
            agent_type,
            task,
            context=context,
            client=client,
            event_queue=event_queue,
            conversation_history=conversation_history,
        )

    # -------------------------------------------------------------------- sdk

    async def _run_via_sdk(self, *, agent_type: str, task: str, context: dict | None) -> dict:
        """Run the task through the Claude Agent SDK adapter and normalize it.

        The agent's system prompt is loaded the same way the legacy loop loads
        it, so the two paths share prompt content for a fair benchmark. Per-agent
        skill + MCP-tool specialization comes from :func:`sdk_profile_for`
        (Icinga loads ``icinga-triage`` + the monitoring-mcp/GitHub servers;
        other agents get an empty profile).
        """
        from src.agent.icinga_sdk import sdk_profile_for
        from src.agent.system_prompt import get_agent_prompt
        from src.llm import AgentSdkClient, AgentSdkUnavailableError

        start = time.monotonic()
        system = get_agent_prompt(agent_type) or None
        prompt = _with_context(task, context)
        profile = sdk_profile_for(agent_type, self._config)

        try:
            sdk_client = AgentSdkClient.from_config(self._config)
            result = await sdk_client.complete(prompt=prompt, system=system, **profile)
        except AgentSdkUnavailableError as exc:
            logger.warning("SDK runtime requested but unavailable: %s", exc)
            return _error_result(agent_type, str(exc), round(time.monotonic() - start, 1))

        return _sdk_result_to_dict(agent_type, result, round(time.monotonic() - start, 1))


# --------------------------------------------------------------------- helpers


def _with_context(task: str, context: dict | None) -> str:
    """Append orchestrator context to the task, matching the legacy framing."""
    if not context:
        return task
    import json

    return f"{task}\n\n**Context from orchestrator:**\n```json\n{json.dumps(context, default=str)}\n```"


def _sdk_result_to_dict(agent_type: str, result: Any, duration_seconds: float) -> dict:
    """Map an :class:`SdkResult` onto the legacy ``run_sub_agent`` result dict.

    Token/cost/cache usage is surfaced under ``data.usage`` — exactly the
    fields the Phase-2 cost benchmark compares against the legacy path.
    """
    usage = result.usage
    return {
        "agent": agent_type,
        "status": "success" if result.succeeded else "error",
        "summary": result.text or (result.error_message or ""),
        "findings": [result.text] if result.text else [],
        "data": {
            "runtime": "sdk",
            "model": result.model,
            "session_id": result.session_id,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "total_cost_usd": usage.total_cost_usd,
                "num_turns": usage.num_turns,
            },
        },
        "tool_calls": len(result.tool_invocations),
        "tool_errors": sum(1 for inv in result.tool_invocations if inv.get("is_error")),
        "rounds_used": result.usage.num_turns,
        "duration_seconds": duration_seconds,
        "error": result.error_message,
    }


def _error_result(agent_type: str, message: str, duration_seconds: float = 0.0) -> dict:
    """A legacy-shaped error result for failures before/around the SDK call."""
    return {
        "agent": agent_type,
        "status": "error",
        "summary": message,
        "findings": [],
        "data": {"runtime": "sdk"},
        "tool_calls": 0,
        "tool_errors": 0,
        "rounds_used": 0,
        "duration_seconds": duration_seconds,
        "error": message,
    }
