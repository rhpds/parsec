"""The SDK orchestrator must be a drop-in for the legacy loop.

Same SSE vocabulary, same ordering, same terminating `history` + `done` pair —
`routes/query.py` and the frontend are unchanged, so any divergence here is a
user-visible regression rather than an implementation detail.
"""

from __future__ import annotations

import json

import pytest

# ----------------------------------------------------- real SDK message types
#
# Constructed from the actual claude_agent_sdk dataclasses rather than look-alike
# stubs: the translator dispatches with isinstance, and a stub that merely shares
# a class name would pass here while failing against the real stream.
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.agent.sdk_stream import SdkEventTranslator


def _stream(event: dict, parent: str | None = None) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s-1", event=event, parent_tool_use_id=parent)


def _assistant(blocks: list) -> AssistantMessage:
    return AssistantMessage(content=blocks, model="claude-sonnet-4-6")


def _result(**kw) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="s-1",
    )
    base.update(kw)
    return ResultMessage(**base)


def _text_delta(text: str, parent: str | None = None) -> StreamEvent:
    return _stream(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        parent=parent,
    )


def _events(translator, message) -> list[str]:
    return list(translator.translate(message))


def _blob(events: list[str]) -> str:
    return "".join(events)


class _Collector:
    """Minimal MetricsCollector stand-in."""

    def __init__(self):
        self.model = None
        self.tokens = None
        self.cost = None

    def record_tokens(self, **kw):
        self.tokens = kw

    def record_cost(self, c):
        self.cost = c

    def record_model(self, m):
        self.model = m


@pytest.fixture
def tr():
    return SdkEventTranslator(question="why did costs spike?", history=[])


# ------------------------------------------------------------------ streaming


def test_text_deltas_stream_incrementally(tr):
    """The whole point of include_partial_messages: tokens, not one blob."""
    out = _events(tr, _text_delta("Costs ")) + _events(tr, _text_delta("rose 30%."))

    assert len(out) == 2, "each delta should be its own SSE event"
    assert "Costs " in out[0]
    assert "rose 30%." in out[1]
    assert tr.answer == "Costs rose 30%."


def test_subagent_narration_is_not_streamed_to_the_user(tr):
    """Only the top-level answer is user-facing; subagent prose would interleave."""
    assert _events(tr, _text_delta("internal", parent="tu-9")) == []
    assert tr.answer == ""


def test_non_text_stream_events_are_ignored(tr):
    assert _events(tr, _stream({"type": "content_block_start"})) == []
    assert (
        _events(
            tr,
            _stream({"type": "content_block_delta", "delta": {"type": "thinking_delta"}}),
        )
        == []
    )


# ----------------------------------------------------------------- delegation


def test_agent_tool_emits_agent_start(tr):
    msg = _assistant([ToolUseBlock(id="tu-1", name="Agent", input={"subagent_type": "cost"})])
    blob = _blob(_events(tr, msg))
    assert "agent_start" in blob
    assert "cost" in blob


def test_legacy_task_tool_name_still_recognised(tr):
    """Renamed Task -> Agent in CLI v2.1.63; the pin may predate that."""
    msg = _assistant([ToolUseBlock(id="tu-2", name="Task", input={"subagent_type": "aap2"})])
    assert "agent_start" in _blob(_events(tr, msg))


def test_agent_done_pairs_with_its_tool_result(tr):
    start = _assistant([ToolUseBlock(id="tu-7", name="Agent", input={"subagent_type": "ocpv"})])
    _events(tr, start)
    done = _blob(_events(tr, UserMessage(content=[ToolResultBlock(tool_use_id="tu-7")])))

    assert "agent_done" in done
    assert "ocpv" in done


def test_unrelated_tool_result_does_not_emit_agent_done(tr):
    out = _blob(_events(tr, UserMessage(content=[ToolResultBlock(tool_use_id="other")])))
    assert "agent_done" not in out


def test_skill_activation_is_surfaced_as_status(tr):
    msg = _assistant(
        [ToolUseBlock(id="tu-3", name="Skill", input={"command": "cost-anomaly-triage"})]
    )
    blob = _blob(_events(tr, msg))
    assert "status" in blob
    assert "cost-anomaly-triage" in blob


# ------------------------------------------------------------- bridged tools


async def test_bridge_events_are_drained_in_order(tr):
    """Tool events pushed from inside a handler must reach the browser."""
    await tr.push("event: tool_start\ndata: {}\n\n")
    await tr.push("event: tool_result\ndata: {}\n\n")

    drained = list(tr.drain())
    assert len(drained) == 2
    assert "tool_start" in drained[0]
    assert "tool_result" in drained[1]
    assert list(tr.drain()) == [], "drain must not replay"


# ---------------------------------------------------------------- terminating


def test_finish_emits_history_before_done(tr):
    _events(tr, _text_delta("the answer"))
    out = list(tr.finish(_Collector()))

    blob = "".join(out)
    assert blob.index("event: history") < blob.index("event: done")


def test_history_carries_the_turn_for_saveconversation(tr):
    _events(tr, _text_delta("spend rose in us-east-1"))
    out = list(tr.finish(_Collector()))

    history_ev = next(e for e in out if e.startswith("event: history"))
    msgs = json.loads(history_ev.split("data: ", 1)[1].strip())["messages"]
    assert msgs[-2] == {"role": "user", "content": "why did costs spike?"}
    assert msgs[-1]["role"] == "assistant"
    assert "us-east-1" in msgs[-1]["content"]


def test_result_text_used_when_nothing_streamed(tr):
    """A non-streamed turn must still produce an answer, not an empty one."""
    list(tr.translate(_result(result="fallback answer")))
    out = list(tr.finish(_Collector()))
    assert "fallback answer" in "".join(out)


def test_empty_answer_is_labelled_not_blank(tr):
    out = "".join(tr.finish(_Collector()))
    assert "(no output)" in out


# -------------------------------------------------------------------- metrics


def test_usage_is_recorded_from_the_result_message(tr):
    collector = _Collector()
    list(
        tr.translate(
            _result(
                result="x",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 7,
                },
                total_cost_usd=0.42,
            )
        )
    )
    list(tr.finish(collector))

    assert collector.tokens["input_tokens"] == 100
    assert collector.tokens["cache_read_tokens"] == 7
    assert collector.cost == 0.42


def test_metrics_failure_never_breaks_the_response(tr):
    class _Broken(_Collector):
        def record_tokens(self, **kw):
            raise RuntimeError("mlflow down")

    list(tr.translate(_result(result="answer", usage={"input_tokens": 1})))
    out = "".join(tr.finish(_Broken()))
    assert "event: done" in out
    assert "answer" in out


# -------------------------------------------------------------------- history


def test_prior_turns_are_carried_into_the_prompt():
    """A fresh SDK session has no history; without this multi-turn breaks."""
    tr = SdkEventTranslator(
        question="and last month?",
        history=[
            {"role": "user", "content": "what did we spend in June?"},
            {"role": "assistant", "content": "$12,000"},
        ],
    )
    prompt = tr.build_prompt()

    assert "and last month?" in prompt
    assert "what did we spend in June?" in prompt
    assert "$12,000" in prompt


def test_first_turn_prompt_is_just_the_question():
    tr = SdkEventTranslator(question="hello", history=[])
    assert tr.build_prompt() == "hello"


def test_structured_content_blocks_flatten_into_the_preamble():
    tr = SdkEventTranslator(
        question="follow up",
        history=[{"role": "user", "content": [{"type": "text", "text": "original ask"}]}],
    )
    assert "original ask" in tr.build_prompt()
