"""Translate the Agent SDK message stream into Parsec's SSE events.

The frontend consumes a fixed event vocabulary — ``text``, ``tool_start``,
``tool_result``, ``agent_start``, ``agent_done``, ``chart``, ``report``,
``history``, ``done`` — and the legacy loop emits all of it. Nothing about that
contract changes when the runtime does, so this module maps SDK messages onto
the same events in the same order.

Two SDK details make real streaming possible:

* ``include_partial_messages=True`` makes the CLI forward raw Anthropic stream
  events, so assistant text arrives as ``content_block_delta`` chunks. Without
  it, an answer only appears once the whole message completes — which is what
  made the Phase-2 SDK path feel like a spinner followed by a wall of text.
* Delegation is the SDK's ``Agent`` tool. A ``tool_use`` block named ``Agent``
  carries ``subagent_type``, which maps onto ``agent_start`` / ``agent_done``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

from src.agent.sdk_profiles import discoverable_skill_names

logger = logging.getLogger(__name__)

#: SDK tool name for delegation. Renamed from "Task" in Claude Code v2.1.63;
#: both are matched so a pinned older CLI still works.
_AGENT_TOOL_NAMES = frozenset({"Agent", "Task"})

#: Runtime failures that are really deployment failures, and what to say instead.
#: Matched case-insensitively against the ``ResultMessage`` text.
_CLI_ERROR_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("not logged in", "please run /login", "invalid api key", "authentication_error"),
        "the agent runtime could not authenticate to the model backend — this is a "
        "Parsec deployment problem, not something to fix from this chat; check "
        "anthropic.backend and its credentials reach the agent subprocess",
    ),
    (
        ("invalid model name", "model not found"),
        "the agent runtime asked the model gateway for a model it does not serve — "
        "check anthropic.model against the gateway's model list",
    ),
)


class SdkEventTranslator:
    """Stateful translator for one orchestrator turn."""

    def __init__(self, *, question: str, history: list) -> None:
        self._question = question
        self._history = history or []
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._text_parts: list[str] = []
        self._active_agents: dict[str, str] = {}
        self._skills_seen: set[str] = set()
        self._usage: Any | None = None
        self._session_id: str | None = None

    # ------------------------------------------------------------- input

    def build_prompt(self) -> str:
        """Build the user prompt, carrying prior turns forward.

        The legacy loop replays the whole message list to the model each turn.
        A fresh SDK session has no history, so prior turns are summarised into
        the prompt — without this the SDK path silently loses multi-turn
        context, which the UI still displays.
        """
        from src.agent.orchestrator import _trim_history

        if not self._history:
            return self._question

        lines: list[str] = ["Earlier in this conversation:"]
        for msg in _trim_history(list(self._history)):
            role = msg.get("role") if isinstance(msg, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            text = _flatten_content(content)
            if role and text:
                lines.append(f"{role}: {text}")
        lines.append("")
        lines.append(f"Current question: {self._question}")
        return "\n".join(lines)

    async def push(self, event: str) -> None:
        """Sink for events emitted from inside a bridged tool handler."""
        await self._queue.put(event)

    def drain(self) -> Iterator[str]:
        while not self._queue.empty():
            yield self._queue.get_nowait()

    # --------------------------------------------------------- translate

    def translate(self, message: Any) -> Iterator[str]:
        """Yield the SSE events for one SDK message."""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            UserMessage,
        )

        if isinstance(message, StreamEvent):
            yield from self._translate_stream_event(message)
        elif isinstance(message, AssistantMessage):
            yield from self._translate_assistant(message)
        elif isinstance(message, UserMessage):
            yield from self._translate_user(message)
        elif isinstance(message, ResultMessage):
            self._capture_result(message)

        sid = getattr(message, "session_id", None)
        if sid:
            self._session_id = sid

    def _translate_stream_event(self, message: Any) -> Iterator[str]:
        """Token-level text deltas — the reason streaming works at all."""
        from src.agent.streaming import sse_text

        event = getattr(message, "event", None) or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return
        chunk = delta.get("text") or ""
        if not chunk:
            return
        # Only the top-level agent's prose is the user-facing answer; a
        # subagent's internal narration would interleave incoherently.
        if getattr(message, "parent_tool_use_id", None):
            return
        self._text_parts.append(chunk)
        yield sse_text(chunk)

    def _translate_assistant(self, message: Any) -> Iterator[str]:
        from claude_agent_sdk import ToolUseBlock

        for block in getattr(message, "content", None) or []:
            if not isinstance(block, ToolUseBlock):
                continue
            name = getattr(block, "name", "")
            tool_input = getattr(block, "input", None) or {}

            if name in _AGENT_TOOL_NAMES:
                agent_type = str(tool_input.get("subagent_type") or "").strip()
                if agent_type:
                    yield from self._start_agent(agent_type, getattr(block, "id", ""))
            elif name == "Skill":
                # Parsec's own tools already emit tool_start/tool_result from
                # inside the bridge handler, so only the built-in Skill tool
                # needs surfacing here.
                yield from self._translate_skill_use(tool_input)

    def _translate_skill_use(self, tool_input: dict) -> Iterator[str]:
        """Surface a built-in Skill tool call as ``skill_used`` + a status line."""
        from src.agent.streaming import sse_status

        # The name comes straight out of the model's tool_use block, so validate
        # it against what is actually on disk before it reaches the UI — the
        # preloaded path already filters the same way via skills_for(). An unknown
        # name means the model asked for a skill that does not exist; the CLI will
        # reject it, and echoing it would put unvalidated model output into the page.
        skill = str(tool_input.get("command") or tool_input.get("name") or "").strip()
        if not skill:
            return
        # Mirror skills_for(): filter against what is on disk when discovery works,
        # and trust the name when it does not (unit tests have no skills root). The
        # client builds these nodes with textContent, so an unverified name is a
        # wrong label rather than an injection.
        available = discoverable_skill_names()
        if available and skill not in available:
            logger.warning("Model invoked unknown skill %r — not surfaced", skill)
            return
        yield from self._note_skill(skill, source="invoked")
        yield sse_status(f"Using skill: {skill}")

    def _note_skill(
        self, skill: str, *, source: str, agent_type: str | None = None
    ) -> Iterator[str]:
        """Emit ``skill_used`` once per skill per turn."""
        from src.agent.streaming import sse_skill_used

        if skill in self._skills_seen:
            return
        self._skills_seen.add(skill)
        yield sse_skill_used(skill, agent_type=agent_type, source=source)

    def _start_agent(self, agent_type: str, tool_use_id: str) -> Iterator[str]:
        from src.agent.agents import AGENTS
        from src.agent.sdk_profiles import skills_for
        from src.agent.streaming import sse_agent_start

        if tool_use_id:
            self._active_agents[tool_use_id] = agent_type
        agent_cfg = AGENTS.get(agent_type)
        name = agent_cfg.name if agent_cfg else agent_type
        yield sse_agent_start(agent_type, name)

        # Skills passed via AgentDefinition.skills are in the agent's context
        # from turn one and never produce a Skill tool call, so nothing else
        # would ever reveal them. Surface them at delegation time.
        try:
            for skill in skills_for(agent_type):
                yield from self._note_skill(skill, source="preloaded", agent_type=agent_type)
        except Exception:
            logger.exception("Could not resolve preloaded skills for %s", agent_type)

    def _translate_user(self, message: Any) -> Iterator[str]:
        """A UserMessage carries tool_result blocks back from the model."""
        from claude_agent_sdk import ToolResultBlock

        from src.agent.streaming import sse_agent_done

        for block in getattr(message, "content", None) or []:
            if not isinstance(block, ToolResultBlock):
                continue
            tool_use_id = getattr(block, "tool_use_id", "")
            agent_type = self._active_agents.pop(tool_use_id, None)
            if agent_type:
                yield sse_agent_done(agent_type)

    def _capture_result(self, message: Any) -> None:
        self._usage = message
        if not self._text_parts:
            # Some turns produce no streamed text (e.g. the model answered in a
            # single non-streamed block); fall back to the result payload.
            text = getattr(message, "result", None)
            if isinstance(text, str) and text.strip():
                self._text_parts.append(text)

    def _failure_reason(self) -> str:
        """Human-readable reason this turn failed, or "" if it succeeded.

        The runtime's own wording is written for someone sitting at a terminal,
        and it reaches the chat box verbatim. ``Not logged in · Please run
        /login`` did exactly what it says on the tin: an investigator read it as
        an instruction and typed ``/login`` into Parsec, which is not a thing.
        So misconfiguration is named as misconfiguration, with the raw text kept
        alongside for whoever reads the log.
        """
        msg = self._usage
        if msg is None:
            return "the agent runtime produced no result"
        if not getattr(msg, "is_error", False):
            # A clean run that still said nothing is a failure worth surfacing.
            if not "".join(self._text_parts).strip():
                return "the agent finished without producing an answer"
            return ""
        subtype = str(getattr(msg, "subtype", "") or "")
        if subtype == "error_max_turns":
            turns = getattr(msg, "num_turns", None)
            return (
                f"the investigation hit its turn limit after {turns} turns "
                "without finishing — raise agent.sdk.max_turns or narrow the question"
            )
        detail = str(getattr(msg, "result", None) or subtype or "unknown error")
        lowered = detail.lower()
        for needles, hint in _CLI_ERROR_HINTS:
            if any(needle in lowered for needle in needles):
                return f"{hint} (the runtime said: {detail})"
        return f"the agent runtime failed: {detail}"

    # ------------------------------------------------------------ finish

    def finish(self, collector: Any) -> Iterator[str]:
        """Emit the terminating events the frontend needs.

        ``history`` must precede ``done``: the browser's ``saveConversation()``
        runs on it, and without it the answer is lost on refresh.
        """
        from src.agent.streaming import sse_done, sse_event

        answer = "".join(self._text_parts).strip()
        self._record_metrics(collector)

        # A run that errored or ran out of turns must not read as a successful
        # empty answer. ResultMessage carries is_error and a subtype
        # ("error_max_turns", "error_during_execution"); without this the UI
        # showed "(no output)" and a normal `done`, which is indistinguishable
        # from the model legitimately having nothing to say.
        failure = self._failure_reason()
        if failure:
            from src.agent.streaming import sse_error

            logger.warning("SDK run did not complete: %s", failure)
            yield sse_error(failure)

        history = _serialize(self._history)
        history.append({"role": "user", "content": self._question})
        history.append({"role": "assistant", "content": answer or "(no output)"})
        yield sse_event("history", {"messages": history})
        yield sse_done()

    def _record_metrics(self, collector: Any) -> None:
        """Record usage, then flush. Never let telemetry break the response."""
        try:
            usage = getattr(self._usage, "usage", None) or {}
            if isinstance(usage, dict):
                collector.record_tokens(
                    input_tokens=usage.get("input_tokens", 0) or 0,
                    output_tokens=usage.get("output_tokens", 0) or 0,
                    cache_creation_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0) or 0,
                )
            cost = getattr(self._usage, "total_cost_usd", None)
            if cost:
                collector.record_cost(cost)
            model = getattr(self._usage, "model", None)
            if model and not getattr(collector, "model", None):
                collector.record_model(model)
        except Exception:
            logger.exception("Failed to record SDK orchestrator metrics")

        try:
            from src.agent.orchestrator import _flush_collector

            _flush_collector(collector)
        except Exception:
            logger.exception("Failed to flush SDK orchestrator metrics")

    @property
    def answer(self) -> str:
        return "".join(self._text_parts).strip()

    @property
    def session_id(self) -> str | None:
        return self._session_id


def _flatten_content(content: Any) -> str:
    """Reduce a message's content to plain text for the history preamble."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return " ".join(p for p in parts if p).strip()


def _serialize(history: list) -> list[dict]:
    from src.agent.orchestrator import _serialize_messages

    try:
        return _serialize_messages(list(history))
    except Exception:
        logger.exception("Failed to serialize history; returning empty")
        return []
