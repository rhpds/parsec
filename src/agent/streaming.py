"""SSE (Server-Sent Events) helpers for streaming agent responses."""

import asyncio
import contextlib
import json
import logging

logger = logging.getLogger(__name__)


def sse_event(event: str, data: dict | str) -> str:
    """Format a single SSE event."""
    if isinstance(data, dict):
        try:
            data = json.dumps(data)
        except (TypeError, ValueError):
            logger.exception("Failed to serialize SSE %s event", event)
            data = json.dumps(data, default=str)
    return f"event: {event}\ndata: {data}\n\n"


def sse_text(text: str) -> str:
    """Stream a text chunk to the client."""
    return sse_event("text", {"content": text})


def sse_tool_start(tool_name: str, tool_input: dict) -> str:
    """Notify client that a tool call is starting."""
    return sse_event("tool_start", {"tool": tool_name, "input": tool_input})


def sse_tool_result(tool_name: str, result: dict) -> str:
    """Send tool call result to client."""
    return sse_event("tool_result", {"tool": tool_name, "result": result})


def sse_report(filename: str, format: str, download_url: str) -> str:
    """Notify client that a report is available for download."""
    return sse_event("report", {"filename": filename, "format": format, "url": download_url})


def sse_status(message: str) -> str:
    """Send a status update (shown as a subtle progress indicator)."""
    return sse_event("status", {"message": message})


def sse_agent_start(agent_type: str, agent_name: str) -> str:
    """Signal that a sub-agent has started execution."""
    return sse_event("agent_start", {"agent": agent_type, "name": agent_name})


def sse_agent_done(agent_type: str) -> str:
    """Signal that a sub-agent has finished execution."""
    return sse_event("agent_done", {"agent": agent_type})


#: Seconds of silence before a keepalive comment is emitted. OpenShift's HAProxy
#: router defaults ``timeout server`` to 30s and drops a connection that goes
#: quiet for longer, so this must stay comfortably under it.
SSE_KEEPALIVE_SECONDS = 10.0


def sse_keepalive() -> str:
    """An SSE comment line. Resets router/proxy idle timers; clients ignore it.

    Per the SSE spec a line beginning with ``:`` is a comment and is discarded by
    ``EventSource`` and by the hand-rolled parser in ``static/app.js``, so this is
    invisible to the UI.
    """
    return ": keepalive\n\n"


async def with_keepalive(stream, interval: float = SSE_KEEPALIVE_SECONDS):
    """Wrap an SSE generator so it never goes silent for longer than ``interval``.

    An investigation is mostly quiet on the wire: tool calls run for tens of
    seconds, and after the last one the model composes the answer with nothing
    to send. Through an OpenShift route that silence exceeds HAProxy's 30s
    ``timeout server`` and the connection is dropped — the browser shows
    "network error" after a long, apparently-healthy run.

    This never reproduced in testing because the harness drove the app directly
    on ``localhost:8000``, bypassing the router and the oauth-proxy entirely.
    Both runtimes are affected; this wraps the shared entry point so both are
    covered.

    The source runs as ONE task feeding a queue, rather than a task per step.
    That matters: ``ensure_future`` copies the current context, so stepping the
    generator that way puts each ``__anext__`` in a *different* context and a
    ``ContextVar`` set in one step cannot be reset in another. Parsec sets
    ``parsec_mcp_sse_sink`` for the life of a request and resets it in a
    ``finally``, so the per-step form raised

        ValueError: <Token ...> was created in a different Context

    at teardown — after a complete answer had streamed — which killed the
    terminating ``done``/``history`` events and dropped the connection.
    """
    done_marker = object()
    queue: asyncio.Queue = asyncio.Queue()

    async def _pump() -> None:
        try:
            async for chunk in stream:
                await queue.put(chunk)
        except Exception as exc:  # noqa: BLE001 - re-raised on the consumer side
            await queue.put(exc)
        else:
            await queue.put(done_marker)

    pump = asyncio.ensure_future(_pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield sse_keepalive()
                continue
            if item is done_marker:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump


def sse_skill_used(skill: str, agent_type: str | None = None, source: str = "invoked") -> str:
    """Signal that an agent skill shaped this answer.

    Without this a skill is invisible: preloaded skills never produce a tool
    call, so a SKILL.md could steer an entire investigation with no trace in the
    UI at all. ``source`` distinguishes the two ways that happens:

    ``preloaded``
        Passed via ``AgentDefinition.skills``, so it is in the agent's context
        from the first turn.
    ``invoked``
        The model called the ``Skill`` tool for it mid-run.
    """
    return sse_event("skill_used", {"skill": skill, "agent": agent_type, "source": source})


def sse_confidence(level: str, reasons: list[str]) -> str:
    """Send a confidence level indicator (only emitted for medium/low)."""
    return sse_event("confidence", {"level": level, "reasons": reasons})


def sse_error(message: str) -> str:
    """Send an error event."""
    return sse_event("error", {"message": message})


def sse_done() -> str:
    """Signal that the stream is complete."""
    return sse_event("done", {})
