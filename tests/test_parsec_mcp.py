"""The in-process MCP bridge must preserve the legacy tool loop's behaviour.

The bridge is what lets a non-Icinga sub-agent run on the SDK at all: it turns
Parsec's Python tools into MCP tools the ``claude`` subprocess can call. If it
loses the cache, the result cap, or the Icinga write gate, then "the agent runs
on the SDK" is a regression rather than a migration.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agent import parsec_mcp

SCHEMAS = [
    {
        "name": "query_icinga",
        "description": "Query Icinga.",
        "input_schema": {"type": "object", "properties": {"action": {"type": "string"}}},
    },
    {
        "name": "query_aws_costs",
        "description": "Query AWS costs.",
        "input_schema": {"type": "object", "properties": {"account_ids": {"type": "array"}}},
    },
]


@pytest.fixture(autouse=True)
def _clear_sink():
    token = parsec_mcp.sse_sink.set(None)
    yield
    parsec_mcp.sse_sink.reset(token)


def _handler(name: str, *, allow_writes: bool = False):
    return parsec_mcp._make_handler(name, allow_writes)


def _text(out: dict) -> str:
    return out["content"][0]["text"]


# ------------------------------------------------------------------ naming


def test_tool_names_are_namespaced():
    assert parsec_mcp.tool_names_for(SCHEMAS) == [
        "mcp__parsec__query_icinga",
        "mcp__parsec__query_aws_costs",
    ]


# ------------------------------------------------------------- write gate


@pytest.mark.parametrize("action", sorted(parsec_mcp.WRITE_ACTIONS))
async def test_icinga_write_actions_refused_by_default(action, monkeypatch):
    """These are enum values on one tool, so no allow-list can express them."""
    called = []
    monkeypatch.setattr(
        "src.agent.orchestrator._execute_tool",
        lambda n, a: called.append(n),
    )

    out = await _handler("query_icinga")({"action": action})

    assert out["is_error"] is True
    assert action in _text(out)
    assert called == [], "a refused write must never reach the tool layer"


async def test_icinga_reads_are_allowed(monkeypatch):
    async def _exec(name, args):
        return {"problems": []}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    out = await _handler("query_icinga")({"action": "get_problems"})
    assert out["is_error"] is False
    assert json.loads(_text(out)) == {"problems": []}


async def test_writes_permitted_when_explicitly_enabled(monkeypatch):
    async def _exec(name, args):
        return {"ok": True}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    out = await _handler("query_icinga", allow_writes=True)({"action": "acknowledge_problem"})
    assert out["is_error"] is False


async def test_write_gate_only_applies_to_icinga(monkeypatch):
    """A same-named argument on another tool must not be swept up."""

    async def _exec(name, args):
        return {"ok": True}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    out = await _handler("query_aws_costs")({"action": "add_comment"})
    assert out["is_error"] is False


# ----------------------------------------------------------------- errors


async def test_tool_exception_becomes_an_error_result(monkeypatch):
    """The model should see a failed tool, not a transport crash."""

    async def _boom(name, args):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _boom)
    out = await _handler("query_aws_costs")({})

    assert out["is_error"] is True
    assert "backend exploded" in _text(out)


async def test_error_dict_is_flagged(monkeypatch):
    async def _exec(name, args):
        return {"error": "no such account"}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    out = await _handler("query_aws_costs")({})
    assert out["is_error"] is True


# ------------------------------------------------------------------ cache


async def test_cache_hit_skips_execution(monkeypatch):
    from src.agent.orchestrator import _tool_cache

    calls = []

    async def _exec(name, args):
        calls.append(name)
        return {"rows": [1]}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    token = _tool_cache.set({})
    try:
        h = _handler("query_aws_costs")
        first = await h({"account_ids": ["1"]})
        second = await h({"account_ids": ["1"]})
    finally:
        _tool_cache.reset(token)

    assert calls == ["query_aws_costs"], "second identical call should hit the cache"
    assert _text(first) == _text(second)


async def test_uncacheable_tools_always_execute(monkeypatch):
    """query_icinga is in _UNCACHEABLE_TOOLS — live state must not be cached."""
    from src.agent.orchestrator import _tool_cache

    calls = []

    async def _exec(name, args):
        calls.append(name)
        return {"problems": []}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    token = _tool_cache.set({})
    try:
        h = _handler("query_icinga")
        await h({"action": "get_problems"})
        await h({"action": "get_problems"})
    finally:
        _tool_cache.reset(token)

    assert len(calls) == 2


async def test_errors_are_not_cached(monkeypatch):
    from src.agent.orchestrator import _tool_cache

    calls = []

    async def _exec(name, args):
        calls.append(name)
        return {"error": "transient"}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    token = _tool_cache.set({})
    try:
        h = _handler("query_aws_costs")
        await h({})
        await h({})
    finally:
        _tool_cache.reset(token)

    assert len(calls) == 2, "a failed call must not poison the cache"


# -------------------------------------------------------------- result cap


async def test_oversized_results_are_capped(monkeypatch):
    from src.agent.orchestrator import MAX_TOOL_RESULT_CHARS

    async def _exec(name, args):
        return {"blob": "x" * (MAX_TOOL_RESULT_CHARS * 2)}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    out = await _handler("query_aws_costs")({})

    assert len(_text(out)) <= MAX_TOOL_RESULT_CHARS + 200


# ------------------------------------------------------------------- SSE


async def test_sse_events_are_emitted_around_the_call(monkeypatch):
    async def _exec(name, args):
        return {"ok": True}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)

    seen: list[str] = []

    async def _sink(ev: str) -> None:
        seen.append(ev)

    token = parsec_mcp.sse_sink.set(_sink)
    try:
        await _handler("query_aws_costs")({})
    finally:
        parsec_mcp.sse_sink.reset(token)

    blob = "".join(seen)
    assert "tool_start" in blob
    assert "tool_result" in blob


async def test_streaming_failure_does_not_break_the_tool(monkeypatch):
    """SSE is best-effort; a broken client must not fail the investigation."""

    async def _exec(name, args):
        return {"ok": True}

    async def _sink(ev: str) -> None:
        raise RuntimeError("client went away")

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    token = parsec_mcp.sse_sink.set(_sink)
    try:
        out = await _handler("query_aws_costs")({})
    finally:
        parsec_mcp.sse_sink.reset(token)

    assert out["is_error"] is False


async def test_no_sink_is_fine(monkeypatch):
    async def _exec(name, args):
        return {"ok": True}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    out = await _handler("query_aws_costs")({})
    assert out["is_error"] is False


# ------------------------------------------------------- server construction


def test_build_server_registers_every_schema(monkeypatch):
    """Schemas pass through verbatim; one handler per tool, names preserved."""
    captured: dict[str, Any] = {}

    def _fake_tool(name, description, input_schema):
        def _wrap(fn):
            captured.setdefault("tools", []).append((name, description, input_schema))
            return fn

        return _wrap

    def _fake_create(name, version, tools):
        captured["server"] = {"name": name, "count": len(tools)}
        return captured["server"]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "tool", _fake_tool, raising=False)
    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", _fake_create, raising=False)

    parsec_mcp.build_server(SCHEMAS)

    names = [t[0] for t in captured["tools"]]
    assert names == ["query_icinga", "query_aws_costs"]
    assert captured["tools"][0][2] == SCHEMAS[0]["input_schema"]
    assert captured["server"] == {"name": "parsec", "count": 2}


def test_long_descriptions_are_truncated(monkeypatch):
    captured: list = []
    import claude_agent_sdk

    monkeypatch.setattr(
        claude_agent_sdk,
        "tool",
        lambda n, d, s: (captured.append(d) or (lambda fn: fn)),
        raising=False,
    )
    monkeypatch.setattr(
        claude_agent_sdk, "create_sdk_mcp_server", lambda **kw: None, raising=False
    )

    parsec_mcp.build_server([{**SCHEMAS[0], "description": "y" * 5000}])
    assert len(captured[0]) <= parsec_mcp._MAX_DESCRIPTION_CHARS
