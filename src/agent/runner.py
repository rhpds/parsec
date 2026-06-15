"""AgentRunner — route a sub-agent task to the legacy loop or the Claude Agent SDK.

Phase-2 foundation. The orchestrator currently calls :func:`src.agent.agents.run_sub_agent`
directly. This module introduces a single dispatch seam that reads ``agent.runtime``
(``legacy|sdk``, default ``legacy``) once and routes each sub-agent task to the matching
runtime, returning the **same structured result dict** either way so callers don't care
which runtime answered.

With the default ``legacy`` runtime, :meth:`AgentRunner.run_sub_agent` is a transparent
pass-through to the existing loop — **zero behavior change at the shipped default**. The
Icinga sub-agent dispatches through this seam when ``agent.runtime: sdk`` is set:
:func:`src.agent.agents.run_sub_agent` and ``run_sub_agent_streaming`` route
``agent_type == "icinga"`` here under that flag (every other agent stays legacy).

The SDK branch here is deliberately minimal: it runs the agent's system prompt through
:meth:`AgentSdkClient.complete` and normalizes the outcome. Per-agent *skill + tool*
wiring (e.g. the Icinga ``query_icinga`` tool surface and ``icinga-triage`` SKILL.md)
lands in the follow-up PR; this module only owns the routing.
"""

from __future__ import annotations

import asyncio
import contextlib
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
        metrics: Any = None,
    ) -> dict:
        """Run one sub-agent task on the active runtime.

        Signature mirrors :func:`src.agent.agents.run_sub_agent` so the runner
        is a drop-in seam. The ``client``/``event_queue``/``conversation_history``
        arguments are only meaningful for the legacy loop and are ignored by the
        SDK path (the SDK runs its own loop and streams internally).

        ``metrics`` is an optional :class:`~src.metrics.collector.MetricsCollector`
        from the caller (the orchestrator's per-turn collector). When provided,
        the SDK path records its token/cache/cost usage into it — tagged
        ``runtime=sdk`` — so legacy and SDK land as comparable runs in the same
        MLflow experiment. The caller owns the flush. When ``None``, the SDK path
        self-emits a run (only if MLflow is configured). The legacy path ignores
        it (the legacy loop already records into the same collector upstream).

        Comparison notes (important for the benchmark):

        * The **fast-path** (``run_sub_agent_streaming``) passes its collector, so
          an Icinga query yields exactly ONE ``runtime=sdk`` run — symmetric with
          the legacy arm's single run. This is the path Icinga uses in practice.
        * The **full-orchestrator delegation** path passes no collector, so the
          SDK sub-agent self-emits its own ``runtime=sdk`` run while the (genuinely
          legacy) orchestrator loop emits its own ``runtime=legacy`` run. That two-
          run split is correct — they are different units of work — but means a
          legacy-vs-SDK pivot should compare the per-sub-agent runs (filter on
          ``agent_type`` + ``runtime``), not the orchestrator run.
        * For the SDK arm, ``cost_usd`` is the SDK's authoritative billed total;
          ``input/output/cache`` token counts come from the final ``ResultMessage``
          snapshot and may under-count when ``num_turns > 1``. Prefer ``cost_usd``
          (and ``num_turns``) over raw token deltas for multi-turn SDK runs.

        Returns:
            The legacy result dict (``agent``/``status``/``summary``/``findings``/
            ``data``/``tool_calls``/``duration_seconds`` …). The SDK path produces
            the same shape, with token/cost/cache usage surfaced under ``data``.
        """
        if self._runtime == RUNTIME_SDK:
            return await self._run_via_sdk(
                agent_type=agent_type, task=task, context=context, metrics=metrics
            )
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

    async def _run_via_sdk(
        self, *, agent_type: str, task: str, context: dict | None, metrics: Any = None
    ) -> dict:
        """Run the task through the Claude Agent SDK adapter and normalize it.

        The agent's system prompt is loaded the same way the legacy loop loads
        it, so the two paths share prompt content for a fair benchmark. Per-agent
        skill + MCP-tool specialization comes from :func:`sdk_profile_for`
        (Icinga loads ``icinga-triage`` + the monitoring-mcp/GitHub servers;
        other agents get an empty profile).

        Observability: the call is wrapped in an MLflow AGENT span (so the SDK
        sub-agent shows up in the trace tree like a legacy sub-agent), and its
        token/cache/cost usage is recorded for cross-validation against the
        legacy arm — see :meth:`run_sub_agent`'s ``metrics`` note.
        """
        from src.agent.icinga_sdk import sdk_profile_for
        from src.agent.system_prompt import get_agent_prompt
        from src.llm import AgentSdkClient, AgentSdkUnavailableError

        start = time.monotonic()
        system = get_agent_prompt(agent_type) or None
        prompt = _with_context(task, context)
        profile = sdk_profile_for(agent_type, self._config)

        span_cm = _agent_span(agent_type, task)
        span = span_cm.__enter__()
        try:
            try:
                sdk_client = AgentSdkClient.from_config(self._config)
                result = await sdk_client.complete(prompt=prompt, system=system, **profile)
            except AgentSdkUnavailableError as exc:
                logger.warning("SDK runtime requested but unavailable: %s", exc)
                _mark_sdk_unavailable(metrics)
                return _error_result(agent_type, str(exc), round(time.monotonic() - start, 1))
            except (ValueError, TypeError) as exc:
                # Malformed agent.sdk.* config (e.g. a non-numeric timeout/max_turns
                # coerced in from_config) must still yield the normalized,
                # legacy-shaped error dict — not raise out of the SDK arm and abort
                # the SSE stream. Mirrors the legacy fast-path, which converts a
                # ValueError from _build_client into an sse_error.
                logger.warning("SDK runtime setup failed (bad config?): %s", exc)
                _mark_sdk_unavailable(metrics)
                return _error_result(agent_type, str(exc), round(time.monotonic() - start, 1))

            duration = round(time.monotonic() - start, 1)
            # Observability is a non-fatal side effect: a tracing/metrics failure
            # must never stop the already-computed SDK answer from reaching the
            # caller. (The helpers are internally guarded too; this is defense in
            # depth so a future change can't regress that invariant.)
            with contextlib.suppress(Exception):
                _record_sdk_metrics(agent_type, result, duration, context, metrics)
            with contextlib.suppress(Exception):
                _set_agent_span_outputs(span, agent_type, result, duration)
            return _sdk_result_to_dict(agent_type, result, duration)
        finally:
            span_cm.__exit__(None, None, None)


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
    out = {
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
    }
    # Match the legacy success dict, which omits "error" entirely on success
    # (only present on failure) — keep the result-dict shapes congruent.
    if result.error_message:
        out["error"] = result.error_message
    return out


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


# ---------------------------------------------------------------- observability
#
# These mirror the legacy sub-agent instrumentation in agents.py/orchestrator.py
# (an AGENT span + a MetricsCollector run) so SDK and legacy turns are directly
# comparable in one MLflow experiment. Everything here is gated on MLflow being
# configured (``get_mlflow_client()`` non-None), so it's a true no-op — no spans,
# no runs, no side effects — when tracing is disabled or in unit tests.


def _tracing_enabled() -> bool:
    try:
        from src.connections.mlflow_tracking import get_mlflow_client

        return get_mlflow_client() is not None
    except Exception:  # pragma: no cover - defensive
        return False


def _agent_span(agent_type: str, task: str) -> Any:
    """An MLflow AGENT span context manager, or a null context when disabled.

    Nests under whatever root trace the request already opened (the orchestrator's
    ``parsec:orchestrator`` CHAIN span), matching the legacy ``streaming_agent:*``
    span shape.
    """
    if not _tracing_enabled():
        return contextlib.nullcontext(None)
    try:
        import mlflow

        from src.metrics.tracing import SpanType

        cm = mlflow.start_span(name=f"sdk_agent:{agent_type}", span_type=SpanType.AGENT)
        span = cm.__enter__()
        with contextlib.suppress(Exception):
            span.set_inputs({"agent_type": agent_type, "task": task[:500], "runtime": "sdk"})
        # Re-wrap so the caller's __enter__ returns the already-entered span and
        # __exit__ closes the real span.
        return _EnteredSpan(cm, span)
    except Exception:  # pragma: no cover - defensive
        return contextlib.nullcontext(None)


class _EnteredSpan:
    """Adapts an already-entered MLflow span to the context-manager protocol."""

    def __init__(self, cm: Any, span: Any) -> None:
        self._cm = cm
        self._span = span

    def __enter__(self) -> Any:
        return self._span

    def __exit__(self, *exc: Any) -> None:
        with contextlib.suppress(Exception):
            self._cm.__exit__(*exc)


def _set_agent_span_outputs(span: Any, agent_type: str, result: Any, duration: float) -> None:
    if span is None:
        return
    usage = result.usage
    with contextlib.suppress(Exception):
        span.set_outputs(
            {
                "response_preview": (result.text or "")[:2000],
                "status": "success" if result.succeeded else "error",
                "tool_calls": len(result.tool_invocations),
            }
        )
        span.set_attributes(
            {
                "runtime": "sdk",
                "agent_type": agent_type,
                "model": result.model or "",
                "duration_seconds": duration,
                "gen_ai.usage.input_tokens": usage.input_tokens,
                "gen_ai.usage.output_tokens": usage.output_tokens,
                "gen_ai.usage.cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "gen_ai.usage.cache_read_input_tokens": usage.cache_read_input_tokens,
                "gen_ai.usage.cost_usd": usage.total_cost_usd,
                "num_turns": usage.num_turns,
            }
        )


def _record_sdk_metrics(
    agent_type: str, result: Any, duration: float, context: dict | None, metrics: Any
) -> None:
    """Record the SDK turn into a MetricsCollector run tagged ``runtime=sdk``.

    Into the caller's ``metrics`` collector when provided (the caller flushes —
    one run, symmetric with the legacy arm); otherwise self-emit a run, but only
    when MLflow is configured.
    """
    owns = metrics is None
    if owns and not _tracing_enabled():
        return

    from src.metrics.collector import MetricsCollector

    collector = metrics
    if owns:
        collector = MetricsCollector(conversation_id=_sdk_conversation_id(context, agent_type))
        collector.record_agent_dispatch(agent_type, routing_method="sdk")

    usage = result.usage
    collector.record_runtime("sdk")
    if result.model and not collector.model:
        collector.record_model(result.model)
    collector.record_tokens(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_tokens=usage.cache_creation_input_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
    )
    collector.record_cost(usage.total_cost_usd)
    collector.record_sub_agent_result(
        agent_type=agent_type,
        duration_seconds=duration,
        tool_calls=len(result.tool_invocations),
        tool_errors=sum(1 for inv in result.tool_invocations if inv.get("is_error")),
        rounds_used=usage.num_turns,
        max_rounds=usage.num_turns,
        status="success" if result.succeeded else "error",
    )

    if owns:
        # Self-emitted run: stamp the real wall-clock latency (this collector's
        # own timer was never started) and flush our own run.
        collector.total_latency_ms = duration * 1000
        asyncio.create_task(collector.flush_to_mlflow())


def _mark_sdk_unavailable(metrics: Any) -> None:
    """Tag a failed SDK *init* as a ``runtime=sdk`` error on the caller's
    collector, so the orchestrator doesn't flush it as a phantom ``runtime=legacy``
    run. No-op when no shared collector was passed (the self-emit path simply
    emits nothing for a failed init)."""
    if metrics is None:
        return
    with contextlib.suppress(Exception):
        metrics.record_runtime("sdk")
        metrics.status = "error"


def _sdk_conversation_id(context: dict | None, agent_type: str) -> str:
    if context:
        for key in ("session_id", "conversation_id"):
            value = context.get(key)
            if value:
                return str(value)
    return f"sdk-{agent_type}"
