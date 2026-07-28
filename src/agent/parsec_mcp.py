"""In-process MCP server exposing Parsec's native tools to the Claude Agent SDK.

The SDK runs the ``claude`` CLI as a subprocess, so the model can only call
tools the CLI knows about: its own built-ins, or tools served over MCP. Icinga
was portable to the SDK without this module only because its backends already
*were* MCP servers (the monitoring-mcp sidecar and GitHub's remote MCP), so the
CLI could be pointed straight at them. Every other sub-agent's tools are plain
Python — boto3, the Azure SDK, BigQuery, httpx against AAP2/OCPV/Babylon — with
no MCP surface to point at.

This module creates that surface *in-process*, by reflecting the tool schemas an
:class:`~src.agent.agents.AgentConfig` already exposes onto
``orchestrator._execute_tool``. Registering the schemas is a loop rather than
23 hand-written wrappers because ``_execute_tool`` already does the per-tool
kwarg unpacking.

**Why in-process rather than a sidecar.** Four behaviours live in the app
process and a separate server would reach none of them:

* the per-request tool-result cache (``orchestrator._tool_cache``)
* the 100k-char result cap (``orchestrator._cap_tool_result``)
* GitHub secret redaction (``tools.github_files._redact_secrets``)
* Ansible log trimming (``tools.aap2.trim_ansible_log``)

A sidecar would also need its own copy of every cloud credential.

**Icinga routes through here too.** Under the raw-MCP profile the model called
``mcp__icinga__*`` directly, which bypassed ``src/tools/icinga.py`` — and with
it the write-action guardrails and the action-name alias map. Bridging Icinga
restores both and gives every agent one uniform tool surface.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

#: MCP server name. Tools surface to the model as ``mcp__parsec__<tool_name>``.
SERVER_NAME = "parsec"

#: Sink for SSE events emitted from inside a tool handler.
#:
#: Set by the caller **before** ``asyncio.create_task``: a task snapshots the
#: context at creation, so a ``.set()`` afterwards is invisible to it.
sse_sink: ContextVar[Callable[[str], Awaitable[None]] | None] = ContextVar(
    "parsec_mcp_sse_sink", default=None
)

#: The only state-mutating surface Parsec exposes. These are enum values of the
#: ``action`` argument on a single ``query_icinga`` tool, not separate tools, so
#: no ``allowed_tools`` / ``disallowed_tools`` list can express them — the gate
#: has to be Python.
WRITE_ACTIONS = frozenset(
    {
        "acknowledge_problem",
        "schedule_downtime",
        "reschedule_check",
        "add_comment",
        "remove_comment",
        "remove_downtime",
        "remove_acknowledgement",
        "send_custom_notification",
    }
)

#: MCP caps a tool description at a modest length; Parsec's are prose.
_MAX_DESCRIPTION_CHARS = 1024


def tool_names_for(schemas: list[dict]) -> list[str]:
    """Return the ``mcp__parsec__*`` names for ``schemas``.

    Used to build a profile's ``allowed_tools`` so it matches exactly the tools
    the server was built with.
    """
    return [f"mcp__{SERVER_NAME}__{s['name']}" for s in schemas]


def build_server(schemas: list[dict], *, allow_writes: bool = False) -> Any:
    """Build an SDK MCP server exposing ``schemas``.

    Args:
        schemas: Anthropic-style tool schemas, i.e. what ``AgentConfig.tools``
            returns. Pass that property (not a cached copy) so dynamically
            discovered Reporting-MCP ``db_*`` tools are included.
        allow_writes: When False (the default) the Icinga write actions in
            :data:`WRITE_ACTIONS` are refused inside the handler.

    Returns:
        An SDK MCP server instance for ``ClaudeAgentOptions(mcp_servers=...)``.

    Raises:
        AgentSdkUnavailableError: if ``claude_agent_sdk`` is not installed.

    Build this per request, never at import time: ``db_*`` schemas are
    discovered by a startup coroutine and the conditional tool set is
    evaluated late.
    """
    from src.llm.agent_sdk_client import AgentSdkUnavailableError

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError as e:  # pragma: no cover - exercised via the adapter
        raise AgentSdkUnavailableError(
            "claude_agent_sdk is required to build the Parsec MCP bridge"
        ) from e

    handlers = []
    for schema in schemas:
        name = schema["name"]
        description = (schema.get("description") or "")[:_MAX_DESCRIPTION_CHARS]
        input_schema = schema.get("input_schema") or {"type": "object", "properties": {}}
        handlers.append(tool(name, description, input_schema)(_make_handler(name, allow_writes)))

    logger.info(
        "Parsec MCP bridge: %d tools (writes %s)",
        len(handlers),
        "enabled" if allow_writes else "disabled",
    )
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=handlers)


def _make_handler(name: str, allow_writes: bool) -> Callable[[dict], Awaitable[dict]]:
    """Build the MCP handler for one tool.

    Bound as a default argument rather than a closure variable so each handler
    keeps its own name (the classic late-binding loop bug).
    """

    async def _handler(args: dict, _name: str = name) -> dict:
        refusal = _refuse_write(_name, args, allow_writes)
        if refusal is not None:
            return refusal

        sink = sse_sink.get()
        if sink is not None:
            await _emit(sink, "tool_start", _name, args)

        result = await _dispatch_cached(_name, args)

        if sink is not None:
            await _emit(sink, "tool_result", _name, result)

        payload = _cap(json.dumps(result, default=str))
        return {
            "content": [{"type": "text", "text": payload}],
            "is_error": isinstance(result, dict) and "error" in result,
        }

    return _handler


def _refuse_write(name: str, args: dict, allow_writes: bool) -> dict | None:
    """Refuse a state-mutating Icinga action unless writes are enabled."""
    if allow_writes or name != "query_icinga":
        return None
    action = str(args.get("action", ""))
    if action not in WRITE_ACTIONS:
        return None
    logger.warning("Refused Icinga write action %r (writes disabled for this agent)", action)
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Write action {action!r} is disabled. This agent is read-only; "
                    "ask an operator to perform it."
                ),
            }
        ],
        "is_error": True,
    }


async def _dispatch_cached(name: str, args: dict) -> dict:
    """Run the tool through the orchestrator, honouring the per-request cache.

    Mirrors the legacy loop: cache hits skip execution, uncacheable tools always
    execute, and an exception becomes an ``{"error": ...}`` result rather than
    propagating — the SDK would otherwise surface a transport failure instead of
    letting the model react to a failed tool.
    """
    from src.agent.orchestrator import (
        _UNCACHEABLE_TOOLS,
        _cache_key,
        _execute_tool,
        _tool_cache,
    )

    cache = _tool_cache.get(None)
    cacheable = cache is not None and name not in _UNCACHEABLE_TOOLS
    key = _cache_key(name, args) if cacheable else ""

    if cacheable and key in cache:
        return cache[key]

    try:
        result = await _execute_tool(name, args)
    except Exception as e:  # noqa: BLE001 - parity with the legacy tool loop
        logger.exception("Bridged tool %s failed", name)
        return {"error": str(e)}

    if cacheable and isinstance(result, dict) and "error" not in result:
        cache[key] = result
    return result


async def _emit(sink: Callable[[str], Awaitable[None]], kind: str, name: str, payload: Any) -> None:
    """Push an SSE event, never letting a streaming fault break the tool call."""
    from src.agent.streaming import sse_event, sse_report, sse_tool_result, sse_tool_start

    try:
        if kind == "tool_start":
            await sink(sse_tool_start(name, payload))
            return

        await sink(sse_tool_result(name, payload))
        if not isinstance(payload, dict) or "error" in payload:
            return
        # The legacy loops emit these alongside the tool result.
        if name == "render_chart":
            await sink(sse_event("chart", payload))
        elif name == "generate_report":
            await sink(
                sse_report(
                    payload["filename"],
                    payload["format"],
                    f"/api/reports/{payload['filename']}",
                )
            )
    except Exception:  # noqa: BLE001 - streaming is best-effort
        logger.exception("Failed to emit SSE %s for %s", kind, name)


def _cap(result_str: str) -> str:
    """Apply the orchestrator's 100k result cap."""
    from src.agent.orchestrator import _cap_tool_result

    return _cap_tool_result(result_str)
