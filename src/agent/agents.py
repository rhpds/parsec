"""Sub-agent registry and execution for the orchestrator architecture.

Each sub-agent is a Claude tool-use loop with a focused prompt and tool set.
The orchestrator delegates to sub-agents via delegation tools, and sub-agents
return structured results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anthropic

if TYPE_CHECKING:
    from anthropic import AnthropicBedrock, AnthropicVertex

    from src.metrics.collector import MetricsCollector
    from src.metrics.tracing import ConversationTracer

from src.agent.streaming import (
    sse_event,
    sse_report,
    sse_status,
    sse_text,
    sse_tool_result,
    sse_tool_start,
)
from src.agent.system_prompt import get_agent_prompt
from src.agent.tool_definitions import (
    get_aap2_tools,
    get_babylon_tools,
    get_cost_tools,
    get_icinga_tools,
    get_ocpv_tools,
    get_security_tools,
)
from src.config import get_config

logger = logging.getLogger(__name__)

# Type alias for the SSE event callback used to forward sub-agent progress
EventCallback = Callable[[str], Coroutine[Any, Any, None]]


@dataclass
class AgentConfig:
    """Configuration for a sub-agent."""

    name: str
    agent_type: str
    tools_fn: Callable[[], list[dict]]
    prompt_file: str
    shared_prompt: str = "config/prompts/shared_context.md"
    max_rounds: int = 8
    description: str = ""
    slow_tool_labels: dict[str, str] = field(default_factory=dict)

    @property
    def tools(self) -> list[dict]:
        """Return tools, evaluating the function each time.

        This ensures dynamically discovered MCP tools are always current.
        """
        return self.tools_fn()


AGENTS: dict[str, AgentConfig] = {
    "cost": AgentConfig(
        name="Cost Investigation",
        agent_type="cost",
        tools_fn=get_cost_tools,
        prompt_file="config/prompts/cost_agent.md",
        max_rounds=8,
        description=(
            "Investigates cloud spending across AWS/Azure/GCP, GPU abuse, ODCR waste, and pricing."
        ),
        slow_tool_labels={
            "query_azure_costs": "Querying Azure billing",
        },
    ),
    "aap2": AgentConfig(
        name="AAP2 Investigation",
        agent_type="aap2",
        tools_fn=get_aap2_tools,
        prompt_file="config/prompts/aap2_agent.md",
        max_rounds=20,
        description=(
            "Investigates AAP2 job failures and traces configs through agnosticv/agnosticd."
        ),
        slow_tool_labels={
            "query_babylon_catalog": "Querying Babylon cluster",
            "query_aap2": "Querying AAP2 controller",
        },
    ),
    "babylon": AgentConfig(
        name="Babylon Investigation",
        agent_type="babylon",
        tools_fn=get_babylon_tools,
        prompt_file="config/prompts/babylon_agent.md",
        max_rounds=8,
        description=(
            "Investigates Babylon catalog items, deployments, lifecycle state, and workshops."
        ),
        slow_tool_labels={
            "query_babylon_catalog": "Querying Babylon cluster",
        },
    ),
    "security": AgentConfig(
        name="Security Investigation",
        agent_type="security",
        tools_fn=get_security_tools,
        prompt_file="config/prompts/security_agent.md",
        max_rounds=20,
        description=(
            "Investigates CloudTrail events, account security, "
            "marketplace subscriptions, and abuse."
        ),
        slow_tool_labels={
            "query_cloudtrail": "Scanning CloudTrail Lake",
            "query_aws_account": "Querying AWS account",
        },
    ),
    "ocpv": AgentConfig(
        name="OCPV Infrastructure",
        agent_type="ocpv",
        tools_fn=get_ocpv_tools,
        prompt_file="config/prompts/ocpv_agent.md",
        max_rounds=8,
        description=(
            "Inspects OCPV clusters: PVCs, PVs, VMs, pods, "
            "nodes, and storage classes for CNV infrastructure issues."
        ),
        slow_tool_labels={
            "query_ocpv_cluster": "Querying OCPV cluster",
            "query_babylon_catalog": "Querying Babylon cluster",
        },
    ),
    "icinga": AgentConfig(
        name="Icinga Monitoring",
        agent_type="icinga",
        tools_fn=get_icinga_tools,
        prompt_file="config/prompts/icinga_agent.md",
        max_rounds=15,
        description=(
            "Triages Icinga2 alerts by correlating live monitoring state "
            "with check script source and Icinga config from GitHub."
        ),
        slow_tool_labels={
            "query_icinga": "Querying Icinga monitoring",
            "fetch_github_file": "Fetching from GitHub",
            "search_github_repo": "Searching GitHub repo",
        },
    ),
}


# ---------------------------------------------------------------------------
# Fast-path classifier — skip orchestrator for obvious single-domain queries
# ---------------------------------------------------------------------------

_AAP2_PATTERNS = re.compile(
    r"""
    RHPDS\s                     # job template prefix
    | jobs/playbook/\d+         # AAP2 job URL
    | job\s+(failed|log|details|template)
    | failed?\s+provision
    | get_job_log
    | ansible.*(fail|error)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_BABYLON_PATTERNS = re.compile(
    r"""
    \bbabylon\b
    | catalog\s+item
    | resource.?claim
    | anarchy.?subject
    | resource.?pool
    | multi.?workshop
    | \bworkshop\b
    | deployment\s+state
    | what.*(?:deploy|provision)s?\b
    | \bsplunk\b.*\blog
    | \blog.*\bsplunk\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_COST_PATTERNS = re.compile(
    r"""
    \bcost\b | \bspend | \bspent\b | \bpricing\b | \bprice\b
    | \bodcr\b | \bcapacity\s+reserve
    | how\s+much\s+did | gpu\s+abuse
    | billing | budget
    | instance.type.*cost
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SECURITY_PATTERNS = re.compile(
    r"""
    \bcloudtrail\b | \biam\b.*key | access.key.*creat
    | marketplace.*subscript | who\s+(created|launched|ran)
    | compromised | abuse.*account | suspicious
    | running\s+instances | what.*running.*on
    """,
    re.IGNORECASE | re.VERBOSE,
)

_OCPV_PATTERNS = re.compile(
    r"""
    \bocpv\b | \bcnv\b.*(?:storage|pvc|volume|node|vm)
    | \bpvc\b.*(?:pending|stuck|fail|bound)
    | \bhostpath\b | volume.?binding
    | \bvirtualmachine\b | \bvmi\b
    | storage\s+class | node\s+resource
    | cnv\s+cluster | ocpv\d+
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ICINGA_PATTERNS = re.compile(
    r"""
    \bicinga\b | \bmonitoring\s+(alert|status|check|state|problem)
    | host\s+(down|unreachable|state|check)
    | service\s+(critical|warning|unknown|check|state)
    | \bdowntime\b.*(?:schedul|active|remov)
    | \backnowledg
    | monitoring\s+health | infra.*health\s+check
    """,
    re.IGNORECASE | re.VERBOSE,
)


def classify_fast(question: str) -> str | None:
    """Regex fast-path: returns agent type or None for orchestrator.

    For queries that clearly map to a single domain, this skips the
    orchestrator LLM call entirely. When multiple domains match, falls
    through to the orchestrator for routing.
    """
    aap2 = _AAP2_PATTERNS.search(question)
    babylon = _BABYLON_PATTERNS.search(question)

    if aap2 and not babylon:
        return "aap2"
    if babylon and not aap2:
        return "babylon"

    if _COST_PATTERNS.search(question) and not _SECURITY_PATTERNS.search(question):
        return "cost"
    if _SECURITY_PATTERNS.search(question) and not _COST_PATTERNS.search(question):
        return "security"
    if _OCPV_PATTERNS.search(question):
        return "ocpv"
    if _ICINGA_PATTERNS.search(question):
        return "icinga"
    return None


# ---------------------------------------------------------------------------
# Sub-agent execution
# ---------------------------------------------------------------------------


def _extract_user_context(history: list) -> str:
    """Extract user text messages from conversation history for sub-agent context.

    The orchestrator's history contains tool_use/tool_result blocks that
    reference orchestrator-level tools the sub-agent doesn't know about.
    We extract just the user's natural-language messages so the sub-agent
    has multi-turn context (e.g., follow-up questions, pasted logs).
    """
    if not history:
        return ""

    user_messages: list[str] = []
    for msg in history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            user_messages.append(content.strip())
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        user_messages.append(text)

    if not user_messages:
        return ""

    recent = user_messages[-3:]
    return "\n\n**Prior conversation context (user messages):**\n" + "\n---\n".join(recent)


def _maybe_inject_budget_warning(messages: list, current_round: int, max_rounds: int) -> None:
    """Inject a budget warning when the agent is 2 rounds from the limit.

    This nudges Claude to stop fetching and write its structured report
    before running out of rounds.
    """
    remaining = max_rounds - current_round - 1
    if remaining != 2:
        return

    last_content = messages[-1].get("content")
    if isinstance(last_content, list):
        last_content.append(
            {
                "type": "text",
                "text": (
                    "[SYSTEM: You have 2 tool rounds remaining. "
                    "You MUST write your full structured report (config trace, "
                    "failure analysis, root cause, recommendations) in your next "
                    "response. Do NOT call more tools unless absolutely critical. "
                    "A report with gaps is better than no report.]"
                ),
            }
        )


def _compute_confidence(tool_outcomes: list[dict]) -> tuple[str, list[str]]:
    """Compute confidence level and reasons from tool outcomes.

    Returns (level, reasons) where level is "high", "medium", or "low".
    """
    errors = [o for o in tool_outcomes if o["status"] == "error"]
    empties = [o for o in tool_outcomes if o["status"] == "empty"]
    if len(errors) >= 2:
        level = "low"
    elif errors or empties:
        level = "medium"
    else:
        level = "high"
    reasons = [f"{o['tool']}: {o['reason']}" for o in tool_outcomes if o["status"] != "success"]
    return level, reasons


async def run_sub_agent(  # noqa: C901
    agent_type: str,
    task: str,
    context: dict | None = None,
    client: anthropic.Anthropic | AnthropicVertex | AnthropicBedrock | None = None,
    event_queue: asyncio.Queue[str] | None = None,
    conversation_history: list | None = None,
    tracer: ConversationTracer | None = None,
) -> dict:
    """Run a sub-agent's Claude tool-use loop and return structured results.

    This is modeled on ``run_alert_investigation`` — a non-streaming inner
    loop that executes tools and collects findings.

    Args:
        agent_type: Key into AGENTS registry ("cost", "aap2", "babylon", "security").
        task: Natural language task description from the orchestrator.
        context: Optional context dict (account_ids, user info, etc.).
        client: Pre-built Anthropic client (shared with orchestrator).
        event_queue: Optional queue for forwarding SSE events to the outer stream.
        conversation_history: Prior orchestrator messages for multi-turn context.

    Returns:
        Structured result dict with summary, findings, and metadata.
    """
    from src.agent.orchestrator import _build_client, _trim_history

    start = _time.monotonic()
    agent_cfg = AGENTS.get(agent_type)
    if not agent_cfg:
        return {
            "agent": agent_type,
            "status": "error",
            "summary": f"Unknown agent type: {agent_type}",
            "findings": [],
            "data": {},
            "tool_calls": 0,
            "duration_seconds": 0,
        }

    cfg = get_config()
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
    max_tokens = cfg.anthropic.get("max_tokens", 4096)

    if client is None:
        client = _build_client(cfg)
    assert client is not None

    from datetime import UTC, datetime

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    system = f"{get_agent_prompt(agent_type)}\n\nToday's date is {today}."

    context_str = ""
    if context:
        context_str = (
            f"\n\n**Context from orchestrator:**\n```json\n{json.dumps(context, default=str)}\n```"
        )

    history_context = _extract_user_context(conversation_history) if conversation_history else ""

    messages: list[dict] = [{"role": "user", "content": f"{task}{context_str}{history_context}"}]
    investigation_log: list[str] = []
    tool_call_count = 0
    tool_outcomes: list[dict] = []  # Track tool results for confidence
    text_parts: list[str] = []
    _client = client

    async def _emit(event: str) -> None:
        if event_queue is not None:
            await event_queue.put(event)

    for _round in range(agent_cfg.max_rounds):
        from src.agent.orchestrator import _dump_api_request

        _dump_api_request(
            f"sub_{agent_type}_round_{_round}",
            system,
            messages,
            agent_cfg.tools,
            model,
        )

        try:
            # Capture LLM context for MLflow tracing
            if tracer:
                from src.agent.system_prompt import get_prompt_files

                tracer.set_llm_context(get_prompt_files(agent_type), agent_cfg.tools, messages)

            def _call_api() -> anthropic.types.Message:
                return _client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=agent_cfg.tools,  # type: ignore[arg-type]
                    messages=messages,  # type: ignore[arg-type]
                )

            await _emit(sse_status(f"{agent_cfg.name}: Analyzing..."))
            response = await asyncio.to_thread(_call_api)
        except anthropic.APIError as e:
            logger.exception("Claude API error in %s sub-agent", agent_type)
            return {
                "agent": agent_type,
                "status": "error",
                "summary": f"Claude API error: {e}",
                "findings": [],
                "data": {},
                "tool_calls": tool_call_count,
                "duration_seconds": round(_time.monotonic() - start, 1),
            }

        assistant_content = response.content
        tool_use_blocks = []
        response_text_parts = []

        for block in assistant_content:
            if block.type == "text" and block.text.strip():
                text_parts.append(block.text)
                response_text_parts.append(block.text)
                investigation_log.append(block.text)
                await _emit(sse_text(block.text))
                if tracer:
                    tracer.append_response(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        if tracer:
            tracer.record_llm_call(
                round_num=_round,
                label=f"sub_{agent_type}",
                input_tokens=response.usage.input_tokens if hasattr(response, "usage") else 0,
                output_tokens=response.usage.output_tokens if hasattr(response, "usage") else 0,
                model=response.model if hasattr(response, "model") else model,
                response_text="\n".join(response_text_parts),
                tool_use_names=[b.name for b in tool_use_blocks],
            )

        from src.agent.orchestrator import _clean_content_block

        messages.append(
            {"role": "assistant", "content": [_clean_content_block(b) for b in assistant_content]}
        )

        if not tool_use_blocks:
            break

        tool_results = []
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input
            tool_call_count += 1

            await _emit(sse_tool_start(tool_name, tool_input))

            investigation_log.append(
                f"[Tool: {tool_name}] input={json.dumps(tool_input, default=str)[:200]}"
            )

            result: dict = {}
            tool_start_time = _time.monotonic()
            cached = False
            try:
                from src.agent.orchestrator import (
                    _UNCACHEABLE_TOOLS,
                    _cache_key,
                    _execute_tool,
                    _tool_cache,
                )

                cache = _tool_cache.get(None)
                if cache is not None and tool_name not in _UNCACHEABLE_TOOLS:
                    key = _cache_key(tool_name, tool_input)
                    if key in cache:
                        result = cache[key]
                        cached = True
                        await _emit(sse_event("cache_hit", {"tool": tool_name}))

                if not cached:
                    task_coro = asyncio.create_task(_execute_tool(tool_name, tool_input))
                    elapsed = 0
                    while not task_coro.done():
                        done, _ = await asyncio.wait({task_coro}, timeout=10)
                        if not done:
                            elapsed += 10
                            label = agent_cfg.slow_tool_labels.get(
                                tool_name, f"Processing {tool_name}"
                            )
                            await _emit(sse_status(f"{agent_cfg.name}: {label}... ({elapsed}s)"))
                    result = task_coro.result()
                    if (
                        cache is not None
                        and tool_name not in _UNCACHEABLE_TOOLS
                        and "error" not in result
                    ):
                        cache[_cache_key(tool_name, tool_input)] = result
            except Exception as e:
                logger.exception("Tool %s failed in %s sub-agent", tool_name, agent_type)
                result = {"error": str(e)}

            tool_duration_ms = (_time.monotonic() - tool_start_time) * 1000

            if tracer:
                tracer.record_tool_call(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    result=result if "error" not in result else None,
                    error=result.get("error") if "error" in result else None,
                    duration_ms=tool_duration_ms,
                    cached=cached,
                )

            await _emit(sse_tool_result(tool_name, result))

            # Track outcome for confidence
            if "error" in result:
                tool_outcomes.append(
                    {"tool": tool_name, "status": "error", "reason": str(result["error"])[:100]}
                )
            elif isinstance(result, dict) and result.get("count", -1) == 0:
                tool_outcomes.append(
                    {"tool": tool_name, "status": "empty", "reason": "no results returned"}
                )
            else:
                tool_outcomes.append({"tool": tool_name, "status": "success"})

            if tool_name == "generate_report" and "error" not in result:
                download_url = f"/api/reports/{result['filename']}"
                await _emit(sse_report(result["filename"], result["format"], download_url))
            elif tool_name == "render_chart" and "error" not in result:
                await _emit(sse_event("chart", result))

            result_str = json.dumps(result, default=str)
            if len(result_str) > 300:
                investigation_log.append(f"[Tool: {tool_name}] result: {result_str[:300]}...")
            else:
                investigation_log.append(f"[Tool: {tool_name}] result: {result_str}")

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result, default=str),
                }
            )

        messages.append({"role": "user", "content": tool_results})
        messages[:] = _trim_history(messages)
        _maybe_inject_budget_warning(messages, _round, agent_cfg.max_rounds)

    elapsed_total = round(_time.monotonic() - start, 1)
    summary = "\n\n".join(text_parts) if text_parts else "Investigation completed without findings."

    logger.info(
        "Sub-agent %s complete: %d tool calls, %.1fs",
        agent_type,
        tool_call_count,
        elapsed_total,
    )

    # Compute and emit confidence
    from src.agent.streaming import sse_confidence

    confidence_level, reasons = _compute_confidence(tool_outcomes)
    if confidence_level != "high":
        await _emit(sse_confidence(confidence_level, reasons))

    return {
        "agent": agent_type,
        "status": "success",
        "summary": summary,
        "findings": investigation_log,
        "data": {},
        "tool_calls": tool_call_count,
        "tool_errors": sum(1 for t in tool_outcomes if t.get("status") == "error"),
        "rounds_used": _round + 1,
        "duration_seconds": elapsed_total,
    }


async def run_sub_agent_streaming(  # noqa: C901
    agent_type: str,
    task: str,
    context: dict | None = None,
    client: anthropic.Anthropic | AnthropicVertex | AnthropicBedrock | None = None,
    conversation_history: list | None = None,
    metrics: MetricsCollector | None = None,
    tracer: ConversationTracer | None = None,
) -> AsyncGenerator[str, None]:
    """Run a sub-agent as the top-level agent, yielding SSE events directly.

    Used in fast-path mode when the orchestrator is skipped entirely.
    The sub-agent streams text and tool events just like the monolithic agent.

    Args:
        conversation_history: Prior messages for multi-turn context. Required
            for fast-path mode so follow-up questions retain context.
    """
    from src.agent.orchestrator import (
        _UNCACHEABLE_TOOLS,
        _build_client,
        _cache_key,
        _execute_tool,
        _tool_cache,
        _trim_history,
    )
    from src.agent.streaming import (
        sse_done,
        sse_error,
        sse_event,
        sse_report,
        sse_text,
    )

    agent_cfg = AGENTS.get(agent_type)
    if not agent_cfg:
        yield sse_error(f"Unknown agent type: {agent_type}")
        yield sse_done()
        return

    # Initialize tool cache if not already set (fast-path enters here directly)
    if _tool_cache.get(None) is None:
        _tool_cache.set({})

    cfg = get_config()
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
    max_tokens = cfg.anthropic.get("max_tokens", 4096)

    try:
        if client is None:
            client = _build_client(cfg)
    except ValueError as e:
        yield sse_error(str(e))
        yield sse_done()
        return
    assert client is not None

    from datetime import UTC, datetime

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    system = f"{get_agent_prompt(agent_type)}\n\nToday's date is {today}."

    incoming_history = conversation_history or []

    def _serialize_messages(msgs: list) -> list:
        from src.agent.orchestrator import _serialize_messages

        return _serialize_messages(msgs)

    messages = _serialize_messages(_trim_history(incoming_history))
    messages.append({"role": "user", "content": task})

    logger.info(
        "Streaming sub-agent %s started (fast-path): %d history messages, task=%s",
        agent_type,
        len(incoming_history),
        task[:120],
    )
    tool_call_count = 0
    tool_outcomes: list[dict] = []  # Track tool results for confidence
    start_time = _time.monotonic()
    _client = client

    if tracer:
        tracer.start_agent_span(agent_type, agent_cfg.name)

    yield sse_event("agent_start", {"agent": agent_type, "name": agent_cfg.name})

    for _round in range(agent_cfg.max_rounds):
        from src.agent.orchestrator import _dump_api_request

        _dump_api_request(
            f"streaming_{agent_type}_round_{_round}",
            system,
            messages,
            agent_cfg.tools,
            model,
        )

        try:
            # Capture LLM context for MLflow tracing
            if tracer:
                from src.agent.system_prompt import get_prompt_files

                tracer.set_llm_context(get_prompt_files(agent_type), agent_cfg.tools, messages)

            def _call_api() -> anthropic.types.Message:
                return _client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=agent_cfg.tools,  # type: ignore[arg-type]
                    messages=messages,  # type: ignore[arg-type]
                )

            yield sse_status(f"{agent_cfg.name}: Analyzing...")
            api_task: asyncio.Task[anthropic.types.Message] = asyncio.ensure_future(
                asyncio.to_thread(_call_api)
            )
            elapsed = 0
            while not api_task.done():
                await asyncio.sleep(10)
                if not api_task.done():
                    elapsed += 10
                    yield sse_status(f"{agent_cfg.name}: Analyzing... ({elapsed}s)")
            response = api_task.result()
            if metrics and hasattr(response, "usage"):
                metrics.record_tokens(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                if not metrics.model:
                    metrics.record_model(response.model)
        except anthropic.APIError as e:
            logger.exception("Claude API error in %s streaming sub-agent", agent_type)
            yield sse_error(f"Claude API error: {e}")
            yield sse_done()
            return
        except Exception as e:
            logger.exception("Unexpected error in %s streaming sub-agent", agent_type)
            yield sse_error(f"Agent error: {e}")
            yield sse_done()
            return

        assistant_content = response.content
        tool_use_blocks = []
        response_text_parts: list[str] = []

        for block in assistant_content:
            if block.type == "text":
                yield sse_text(block.text)
                response_text_parts.append(block.text)
                if tracer:
                    tracer.append_response(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        if tracer:
            tracer.record_llm_call(
                round_num=_round,
                label=f"streaming_{agent_type}",
                input_tokens=response.usage.input_tokens if hasattr(response, "usage") else 0,
                output_tokens=response.usage.output_tokens if hasattr(response, "usage") else 0,
                model=response.model if hasattr(response, "model") else model,
                response_text="\n".join(response_text_parts),
                tool_use_names=[b.name for b in tool_use_blocks],
            )

        from src.agent.orchestrator import _clean_content_block

        messages.append(
            {"role": "assistant", "content": [_clean_content_block(b) for b in assistant_content]}
        )

        if not tool_use_blocks:
            logger.info(
                "Streaming sub-agent %s complete: %d tool calls, %.1fs",
                agent_type,
                tool_call_count,
                _time.monotonic() - start_time,
            )
            # Emit confidence
            from src.agent.streaming import sse_confidence

            confidence_level, reasons = _compute_confidence(tool_outcomes)
            if confidence_level != "high":
                yield sse_confidence(confidence_level, reasons)

            if metrics:
                metrics.record_sub_agent_result(
                    agent_type=agent_type,
                    duration_seconds=round(_time.monotonic() - start_time, 1),
                    tool_calls=tool_call_count,
                    tool_errors=sum(1 for t in tool_outcomes if t.get("status") == "error"),
                    rounds_used=_round + 1,
                    max_rounds=agent_cfg.max_rounds,
                    status="success",
                )
                metrics.record_confidence(confidence_level)

            if tracer:
                tracer.end_agent_span(
                    status="success",
                    tool_calls=tool_call_count,
                    duration_seconds=round(_time.monotonic() - start_time, 1),
                )

            yield sse_event("agent_done", {"agent": agent_type})
            yield sse_event("history", {"messages": _serialize_messages(messages)})
            yield sse_done()
            return

        tool_results = []
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input

            tool_call_count += 1

            yield sse_tool_start(tool_name, tool_input)

            result: dict = {}
            tool_start_t = _time.monotonic()
            cached = False
            try:
                cache = _tool_cache.get(None)
                if cache is not None and tool_name not in _UNCACHEABLE_TOOLS:
                    key = _cache_key(tool_name, tool_input)
                    if key in cache:
                        result = cache[key]
                        cached = True
                        yield sse_event("cache_hit", {"tool": tool_name})

                if not cached:
                    tool_task = asyncio.create_task(_execute_tool(tool_name, tool_input))
                    elapsed = 0
                    while not tool_task.done():
                        done, _ = await asyncio.wait({tool_task}, timeout=10)
                        if not done:
                            elapsed += 10
                            label = agent_cfg.slow_tool_labels.get(
                                tool_name, f"Processing {tool_name}"
                            )
                            yield sse_status(f"{agent_cfg.name}: {label}... ({elapsed}s)")
                    result = tool_task.result()
                    if (
                        cache is not None
                        and tool_name not in _UNCACHEABLE_TOOLS
                        and "error" not in result
                    ):
                        cache[_cache_key(tool_name, tool_input)] = result
            except Exception as e:
                logger.exception("Tool %s failed in %s streaming sub-agent", tool_name, agent_type)
                result = {"error": str(e)}

            tool_duration_ms = (_time.monotonic() - tool_start_t) * 1000

            if tracer:
                tracer.record_tool_call(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    result=result if "error" not in result else None,
                    error=result.get("error") if "error" in result else None,
                    duration_ms=tool_duration_ms,
                    cached=cached,
                )

            yield sse_tool_result(tool_name, result)

            # Track outcome for confidence
            if "error" in result:
                tool_outcomes.append(
                    {"tool": tool_name, "status": "error", "reason": str(result["error"])[:100]}
                )
            elif isinstance(result, dict) and result.get("count", -1) == 0:
                tool_outcomes.append(
                    {"tool": tool_name, "status": "empty", "reason": "no results returned"}
                )
            else:
                tool_outcomes.append({"tool": tool_name, "status": "success"})

            if tool_name == "generate_report" and "error" not in result:
                download_url = f"/api/reports/{result['filename']}"
                yield sse_report(result["filename"], result["format"], download_url)
            elif tool_name == "render_chart" and "error" not in result:
                yield sse_event("chart", result)

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result),
                }
            )

        messages.append({"role": "user", "content": tool_results})
        messages[:] = _trim_history(messages)
        yield sse_event("history", {"messages": _serialize_messages(messages)})

    logger.info(
        "Streaming sub-agent %s exhausted max rounds: %d tool calls, %.1fs",
        agent_type,
        tool_call_count,
        _time.monotonic() - start_time,
    )
    # Emit confidence
    from src.agent.streaming import sse_confidence

    confidence_level, reasons = _compute_confidence(tool_outcomes)
    if confidence_level != "high":
        yield sse_confidence(confidence_level, reasons)

    if metrics:
        metrics.record_sub_agent_result(
            agent_type=agent_type,
            duration_seconds=round(_time.monotonic() - start_time, 1),
            tool_calls=tool_call_count,
            tool_errors=sum(1 for t in tool_outcomes if t.get("status") == "error"),
            rounds_used=agent_cfg.max_rounds,
            max_rounds=agent_cfg.max_rounds,
            status="success",
        )
        metrics.record_confidence(confidence_level)

    if tracer:
        tracer.end_agent_span(
            status="max_rounds",
            tool_calls=tool_call_count,
            duration_seconds=round(_time.monotonic() - start_time, 1),
        )

    yield sse_event("agent_done", {"agent": agent_type})
    max_rounds_text = (
        "\n\nI've used all my planned tool calls but haven't finished. "
        "Would you like me to keep going?\n\n"
        "{{choices}}\n"
        "Keep investigating\n"
        "That's enough, thanks\n"
        "{{/choices}}"
    )
    yield sse_text(max_rounds_text)
    messages.append({"role": "assistant", "content": [{"type": "text", "text": max_rounds_text}]})
    yield sse_event("history", {"messages": _serialize_messages(messages)})
    yield sse_done()
