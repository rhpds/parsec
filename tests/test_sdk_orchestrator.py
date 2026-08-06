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


# ------------------------------------------------ orchestrator options wiring


@pytest.fixture
def _sdk_stub(monkeypatch):
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "tool", lambda n, d, s: (lambda fn: fn), raising=False)
    monkeypatch.setattr(
        claude_agent_sdk,
        "create_sdk_mcp_server",
        lambda name, version, tools: {"name": name, "count": len(tools)},
        raising=False,
    )


def _opts(_sdk_stub):
    from src.agent.sdk_orchestrator import build_orchestrator_options

    cfg = {"agent": {"runtime": "sdk", "sdk": {"enabled_agents": ["all"]}}}
    return build_orchestrator_options(cfg, system="sys")


def test_every_subagent_tool_is_pre_approved(_sdk_stub):
    """Regression: subagents were denied every tool call.

    `permission_mode="dontAsk"` denies anything absent from the session-wide
    `allowed_tools`. Approving only the orchestrator's own tools meant a
    delegated agent could call nothing — the icinga agent answered "unable to
    access the monitoring system due to permission restrictions" having made
    zero tool calls, which looks like a plausible answer rather than a failure.
    """
    from src.agent.agents import AGENTS
    from src.agent.parsec_mcp import tool_names_for

    approved = set(_opts(_sdk_stub).allowed_tools)

    for agent_type, agent_cfg in AGENTS.items():
        for name in tool_names_for(list(agent_cfg.tools)):
            assert name in approved, f"{agent_type} may call {name} but it is not pre-approved"


def test_agent_tool_is_available_and_approved(_sdk_stub):
    """Without `Agent` in both lists, delegation cannot happen at all."""
    o = _opts(_sdk_stub)
    assert "Agent" in o.tools
    assert "Agent" in o.allowed_tools


def test_per_agent_availability_stays_narrow(_sdk_stub):
    """Widening approval must not widen what an individual agent can reach."""
    from src.agent.agents import AGENTS
    from src.agent.parsec_mcp import tool_names_for

    o = _opts(_sdk_stub)
    ocpv_tools = set(o.agents["ocpv"].tools)
    assert ocpv_tools == set(tool_names_for(list(AGENTS["ocpv"].tools)))
    # The cost agent has tools ocpv does not; they must not leak into ocpv.
    cost_only = set(tool_names_for(list(AGENTS["cost"].tools))) - ocpv_tools
    assert cost_only, "expected cost to have tools ocpv lacks"
    assert not (cost_only & ocpv_tools)


def test_streaming_and_isolation_flags(_sdk_stub):
    o = _opts(_sdk_stub)
    assert o.include_partial_messages is True, "token streaming requires this"
    assert o.strict_mcp_config is True
    assert set(o.mcp_servers) == {"parsec"}


def test_delegation_instructions_are_translated_for_the_sdk():
    """The prompt's `investigate_*` tools do not exist on this runtime.

    Left untranslated, the orchestrator is told to delegate with tools it does
    not have, cannot, and silently does everything inline instead — no
    agent_start, no sub-agent prompt, no skill. The only visible symptom is a
    missing event.
    """
    from src.agent.sdk_orchestrator import _orchestrator_system

    cfg = {"agent": {"runtime": "sdk", "sdk": {"enabled_agents": ["all"]}}}
    sys_prompt = _orchestrator_system(cfg)

    assert "Agent" in sys_prompt and "subagent_type" in sys_prompt
    for agent_type in ("cost", "aap2", "babylon", "security", "ocpv", "icinga"):
        assert f'subagent_type="{agent_type}"' in sys_prompt
    # and it explicitly retires the legacy names the prompt file still advertises
    assert "investigate_costs" in sys_prompt
    assert "do NOT exist here" in sys_prompt
    assert "Today's date is" in sys_prompt


def test_no_delegation_block_when_no_agents_enabled():
    from src.agent.sdk_orchestrator import _orchestrator_system

    cfg = {"agent": {"runtime": "sdk", "sdk": {"enabled_agents": []}}}
    assert "subagent_type" not in _orchestrator_system(cfg)


# ------------------------------------------------------------- skill visibility


def test_preloaded_skills_are_surfaced_on_delegation(tr, monkeypatch):
    """Preloaded skills never produce a Skill tool call.

    They are in the agent's context from turn one, so without an explicit event
    a SKILL.md can steer an entire investigation with zero UI trace.
    """
    monkeypatch.setattr(
        "src.agent.sdk_profiles.skills_for",
        lambda a: ["icinga-triage"] if a == "icinga" else [],
    )
    msg = _assistant([ToolUseBlock(id="tu-1", name="Agent", input={"subagent_type": "icinga"})])
    blob = _blob(_events(tr, msg))

    assert "skill_used" in blob
    assert "icinga-triage" in blob
    assert "preloaded" in blob


def test_explicitly_invoked_skill_is_surfaced(tr):
    msg = _assistant([ToolUseBlock(id="tu-2", name="Skill", input={"command": "provision-lookup"})])
    blob = _blob(_events(tr, msg))

    assert "skill_used" in blob
    assert "provision-lookup" in blob
    assert "invoked" in blob


def test_skill_reported_once_per_turn(tr):
    """Repeated use must not spam the transcript with duplicate badges."""
    m = _assistant([ToolUseBlock(id="t", name="Skill", input={"command": "provision-lookup"})])
    first = _blob(_events(tr, m))
    second = _blob(_events(tr, m))

    assert first.count("skill_used") == 1
    assert "skill_used" not in second


def test_skill_lookup_failure_does_not_break_delegation(tr, monkeypatch):
    def _boom(_):
        raise RuntimeError("skills root unreadable")

    monkeypatch.setattr("src.agent.sdk_profiles.skills_for", _boom)
    blob = _blob(
        _events(
            tr, _assistant([ToolUseBlock(id="x", name="Agent", input={"subagent_type": "cost"})])
        )
    )
    assert "agent_start" in blob  # delegation still reported


def test_every_agent_with_skills_resolves_them():
    """The map must name skills that are actually shipped."""
    from pathlib import Path

    from src.agent.sdk_profiles import _AGENT_SKILLS

    shipped = {
        p.name for p in (Path(__file__).resolve().parent.parent / "skills").iterdir() if p.is_dir()
    }
    for agent, skills in _AGENT_SKILLS.items():
        for s in skills:
            assert s in shipped, f"{agent} references skill {s!r} which is not shipped"


# ------------------------------------------------------- answer-detail parity


def test_orchestrator_is_told_to_relay_specialist_findings_verbatim():
    """The measured parity gap was answer compression, not worse investigation.

    Tool counts were comparable (legacy 208 vs SDK 154) but SDK answers ran 659
    chars against legacy's 10,785 on the same question, scoring 3.00 vs 4.43 on
    actionability. The SDK docs note the parent "may summarise" a subagent
    result unless told otherwise.
    """
    from src.agent.sdk_orchestrator import _orchestrator_system

    cfg = {"agent": {"runtime": "sdk", "sdk": {"enabled_agents": ["all"]}}}
    p = _orchestrator_system(cfg)

    assert "Relay specialist findings in full" in p
    assert "verbatim" in p
    for token in ("job IDs", "hostnames", "dollar amounts", "citations"):
        assert token in p, f"prompt should name {token!r} as detail to preserve"


def test_subagents_are_told_their_final_message_is_the_whole_output(_sdk_stub):
    """Only a subagent's final message returns; its tool results do not."""
    from src.agent.sdk_orchestrator import _agent_definitions

    defs = _agent_definitions({"agent": {"runtime": "sdk", "sdk": {"enabled_agents": ["all"]}}})
    assert defs, "expected subagent definitions"
    for agent_type, d in defs.items():
        assert "Reporting your findings" in d.prompt, f"{agent_type} lacks the output contract"
        assert "ONLY thing that leaves" in d.prompt
        assert "owner/repo:path:line" in d.prompt


# ------------------------------------------------------------ SSE keepalive


async def test_keepalive_emitted_during_silence():
    """A long quiet stretch must not look like a dead connection.

    HAProxy drops a route connection after 30s of silence, which is exactly what
    an investigation looks like while tools run and while the model composes.
    """
    import asyncio

    from src.agent.streaming import with_keepalive

    async def slow():
        yield "event: text\ndata: {}\n\n"
        await asyncio.sleep(0.25)
        yield "event: done\ndata: {}\n\n"

    out = [chunk async for chunk in with_keepalive(slow(), interval=0.05)]

    assert any(c.startswith(":") for c in out), "expected keepalive comments"
    assert out[0].startswith("event: text")
    assert out[-1].startswith("event: done")


async def test_keepalive_preserves_event_order_and_content():
    from src.agent.streaming import with_keepalive

    async def fast():
        for i in range(4):
            yield f"event: text\ndata: {i}\n\n"

    out = [c async for c in with_keepalive(fast(), interval=5)]
    assert out == [
        f"event: text\ndata: {i}\n\n" for i in range(4)
    ], "events must pass through intact"


async def test_keepalive_is_an_sse_comment():
    """Clients must ignore it — a stray event would render in the transcript."""
    from src.agent.streaming import sse_keepalive

    k = sse_keepalive()
    assert k.startswith(":")
    assert "event:" not in k and "data:" not in k


async def test_keepalive_propagates_upstream_errors():
    from src.agent.streaming import with_keepalive

    async def boom():
        yield "event: text\ndata: {}\n\n"
        raise RuntimeError("upstream died")

    got = []
    with pytest.raises(RuntimeError, match="upstream died"):
        async for c in with_keepalive(boom(), interval=5):
            got.append(c)
    assert got, "events before the failure should still have been delivered"


async def test_keepalive_preserves_contextvar_set_reset_pairing():
    """The source must run in ONE context, or teardown raises.

    Regression: stepping the generator with a task per `__anext__` copies the
    context each step, so a ContextVar set early cannot be reset in the
    generator's `finally` — it raised "Token was created in a different
    Context" *after* a complete answer had streamed, killing the terminating
    done/history events and dropping the browser connection.
    """
    from contextvars import ContextVar

    from src.agent.streaming import with_keepalive

    probe: ContextVar = ContextVar("probe", default=None)
    reset_ok = {"value": False}

    async def source():
        token = probe.set("sink")
        try:
            yield "event: text\ndata: {}\n\n"
            yield "event: done\ndata: {}\n\n"
        finally:
            probe.reset(token)  # raises if set/reset straddle contexts
            reset_ok["value"] = True

    out = [c async for c in with_keepalive(source(), interval=5)]

    assert reset_ok["value"], "ContextVar reset did not complete — contexts straddled"
    assert out[-1].startswith("event: done"), "terminating event must survive"
