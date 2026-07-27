"""Extended tests for src/agent/agents.py — covers uncovered helper functions.

Tests _get_special_tool_events, _execute_tool_cached_gen,
_try_sdk_streaming, _compute_confidence, _extract_user_context,
_maybe_inject_budget_warning, and _should_use_sdk.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.agents import (
    AgentConfig,
    _compute_confidence,
    _extract_user_context,
    _get_special_tool_events,
    _maybe_inject_budget_warning,
)

# ---------------------------------------------------------------------------
# Helper: build a minimal AgentConfig for tests
# ---------------------------------------------------------------------------


def _make_agent_cfg(**overrides) -> AgentConfig:
    defaults = dict(
        name="Test Agent",
        agent_type="test",
        tools_fn=lambda: [],
        prompt_file="test.md",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


# ---------------------------------------------------------------------------
# _get_special_tool_events
# ---------------------------------------------------------------------------


class TestGetSpecialToolEvents:
    def test_generate_report_success(self):
        result = {"filename": "report.csv", "format": "csv"}
        events = _get_special_tool_events("generate_report", result)
        assert len(events) == 1
        assert "report" in events[0]
        assert "report.csv" in events[0]
        assert "/api/reports/report.csv" in events[0]

    def test_generate_report_with_error(self):
        result = {"error": "failed to generate report"}
        events = _get_special_tool_events("generate_report", result)
        assert events == []

    def test_render_chart_success(self):
        result = {"chart_type": "bar", "data": [1, 2, 3]}
        events = _get_special_tool_events("render_chart", result)
        assert len(events) == 1
        assert "chart" in events[0]

    def test_render_chart_with_error(self):
        result = {"error": "chart rendering failed"}
        events = _get_special_tool_events("render_chart", result)
        assert events == []

    def test_other_tool_returns_empty(self):
        result = {"rows": [1, 2, 3]}
        events = _get_special_tool_events("query_aws_costs", result)
        assert events == []

    def test_empty_result_for_non_special_tool(self):
        events = _get_special_tool_events("query_provisions_db", {})
        assert events == []


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------


class TestComputeConfidence:
    def test_all_success(self):
        outcomes = [
            {"tool": "query_aws_costs", "status": "success"},
            {"tool": "query_provisions_db", "status": "success"},
        ]
        level, reasons = _compute_confidence(outcomes)
        assert level == "high"
        assert reasons == []

    def test_one_error(self):
        outcomes = [
            {"tool": "query_aws_costs", "status": "success"},
            {"tool": "query_provisions_db", "status": "error", "reason": "timeout"},
        ]
        level, reasons = _compute_confidence(outcomes)
        assert level == "medium"
        assert len(reasons) == 1
        assert "query_provisions_db: timeout" in reasons

    def test_two_errors(self):
        outcomes = [
            {"tool": "query_aws_costs", "status": "error", "reason": "auth failed"},
            {"tool": "query_provisions_db", "status": "error", "reason": "timeout"},
        ]
        level, reasons = _compute_confidence(outcomes)
        assert level == "low"
        assert len(reasons) == 2

    def test_one_empty(self):
        outcomes = [
            {"tool": "query_aws_costs", "status": "success"},
            {"tool": "query_provisions_db", "status": "empty", "reason": "no results"},
        ]
        level, reasons = _compute_confidence(outcomes)
        assert level == "medium"
        assert len(reasons) == 1
        assert "query_provisions_db: no results" in reasons

    def test_mix_of_errors_and_empties(self):
        outcomes = [
            {"tool": "query_aws_costs", "status": "error", "reason": "auth failed"},
            {"tool": "query_provisions_db", "status": "empty", "reason": "no results"},
            {"tool": "query_azure_costs", "status": "error", "reason": "timeout"},
        ]
        level, reasons = _compute_confidence(outcomes)
        assert level == "low"
        assert len(reasons) == 3

    def test_empty_outcomes(self):
        level, reasons = _compute_confidence([])
        assert level == "high"
        assert reasons == []


# ---------------------------------------------------------------------------
# _extract_user_context
# ---------------------------------------------------------------------------


class TestExtractUserContext:
    def test_empty_history(self):
        assert _extract_user_context([]) == ""

    def test_user_messages_extracted(self):
        history = [
            {"role": "user", "content": "What is the cost?"},
            {"role": "assistant", "content": "I'll check that."},
            {"role": "user", "content": "Also check Azure."},
        ]
        result = _extract_user_context(history)
        assert "What is the cost?" in result
        assert "Also check Azure." in result
        assert "I'll check that." not in result
        assert "Prior conversation context" in result

    def test_non_user_messages_excluded(self):
        history = [
            {"role": "assistant", "content": "Hello"},
            {"role": "assistant", "content": "Here are results"},
        ]
        assert _extract_user_context(history) == ""

    def test_list_content_with_text_blocks(self):
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "some tool data"},
                    {"type": "text", "text": "Follow up question"},
                ],
            },
        ]
        result = _extract_user_context(history)
        assert "Follow up question" in result
        # tool_result blocks should not appear
        assert "some tool data" not in result

    def test_keeps_last_three_user_messages(self):
        history = [
            {"role": "user", "content": "Message 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "user", "content": "Message 3"},
            {"role": "user", "content": "Message 4"},
            {"role": "user", "content": "Message 5"},
        ]
        result = _extract_user_context(history)
        assert "Message 1" not in result
        assert "Message 2" not in result
        assert "Message 3" in result
        assert "Message 4" in result
        assert "Message 5" in result

    def test_empty_string_content_skipped(self):
        history = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "   "},
        ]
        assert _extract_user_context(history) == ""

    def test_empty_text_blocks_skipped(self):
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ""},
                    {"type": "text", "text": "   "},
                ],
            },
        ]
        assert _extract_user_context(history) == ""


# ---------------------------------------------------------------------------
# _maybe_inject_budget_warning
# ---------------------------------------------------------------------------


class TestMaybeInjectBudgetWarning:
    def test_injects_when_remaining_is_two(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "content": "data"}]},
        ]
        original_len = len(messages[0]["content"])
        _maybe_inject_budget_warning(messages, current_round=5, max_rounds=8)
        # remaining = 8 - 5 - 1 = 2, should inject
        assert len(messages[0]["content"]) == original_len + 1
        injected = messages[0]["content"][-1]
        assert injected["type"] == "text"
        assert "2 tool rounds remaining" in injected["text"]

    def test_does_nothing_when_remaining_is_not_two(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "content": "data"}]},
        ]
        original_len = len(messages[0]["content"])
        _maybe_inject_budget_warning(messages, current_round=3, max_rounds=8)
        # remaining = 8 - 3 - 1 = 4, should NOT inject
        assert len(messages[0]["content"]) == original_len

    def test_does_nothing_when_remaining_is_one(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "content": "data"}]},
        ]
        original_len = len(messages[0]["content"])
        _maybe_inject_budget_warning(messages, current_round=6, max_rounds=8)
        # remaining = 8 - 6 - 1 = 1, should NOT inject
        assert len(messages[0]["content"]) == original_len

    def test_does_nothing_when_content_is_string(self):
        messages = [
            {"role": "user", "content": "plain text message"},
        ]
        _maybe_inject_budget_warning(messages, current_round=5, max_rounds=8)
        # remaining = 2, but content is a string, not a list — no injection
        assert messages[0]["content"] == "plain text message"

    def test_does_nothing_when_remaining_is_zero(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "content": "data"}]},
        ]
        original_len = len(messages[0]["content"])
        _maybe_inject_budget_warning(messages, current_round=7, max_rounds=8)
        # remaining = 8 - 7 - 1 = 0, should NOT inject
        assert len(messages[0]["content"]) == original_len


# ---------------------------------------------------------------------------
# _should_use_sdk
# ---------------------------------------------------------------------------


class TestShouldUseSdk:
    """_should_use_sdk does `from src.llm import RUNTIME_SDK, get_runtime` inside the function."""

    def test_icinga_with_sdk_runtime(self, monkeypatch):
        monkeypatch.setattr("src.llm.get_runtime", lambda cfg: "sdk")
        monkeypatch.setattr("src.llm.RUNTIME_SDK", "sdk")
        from src.agent.agents import _should_use_sdk

        assert _should_use_sdk("icinga", SimpleNamespace()) is True

    def test_icinga_with_legacy_runtime(self, monkeypatch):
        monkeypatch.setattr("src.llm.get_runtime", lambda cfg: "legacy")
        monkeypatch.setattr("src.llm.RUNTIME_SDK", "sdk")
        from src.agent.agents import _should_use_sdk

        assert _should_use_sdk("icinga", SimpleNamespace()) is False

    def test_non_icinga_with_sdk_runtime(self, monkeypatch):
        monkeypatch.setattr("src.llm.get_runtime", lambda cfg: "sdk")
        monkeypatch.setattr("src.llm.RUNTIME_SDK", "sdk")
        from src.agent.agents import _should_use_sdk

        assert _should_use_sdk("cost", SimpleNamespace()) is False

    def test_non_icinga_with_legacy_runtime(self, monkeypatch):
        monkeypatch.setattr("src.llm.get_runtime", lambda cfg: "legacy")
        monkeypatch.setattr("src.llm.RUNTIME_SDK", "sdk")
        from src.agent.agents import _should_use_sdk

        assert _should_use_sdk("cost", SimpleNamespace()) is False


# ---------------------------------------------------------------------------
# _execute_tool_cached_gen
# ---------------------------------------------------------------------------


class TestExecuteToolCachedGen:
    @pytest.mark.asyncio
    async def test_cache_hit(self, monkeypatch):
        from src.agent.agents import _execute_tool_cached_gen
        from src.agent.orchestrator import _tool_cache

        cache_key_val = "some_tool:" + json.dumps({"key": "val"}, sort_keys=True)
        cached_result = {"data": "cached_data"}
        token = _tool_cache.set({cache_key_val: cached_result})

        try:
            agent_cfg = _make_agent_cfg()
            events = []
            async for ev_type, ev_data in _execute_tool_cached_gen(
                "some_tool", {"key": "val"}, agent_cfg, "test"
            ):
                events.append((ev_type, ev_data))

            assert events[0] == ("cache_hit", "some_tool")
            assert events[1][0] == "done"
            assert events[1][1]["result"] == cached_result
            assert events[1][1]["cached"] is True
        finally:
            _tool_cache.reset(token)

    @pytest.mark.asyncio
    async def test_cache_miss_executes_tool(self, monkeypatch):
        from src.agent.agents import _execute_tool_cached_gen
        from src.agent.orchestrator import _tool_cache

        token = _tool_cache.set({})

        mock_result = {"rows": [1, 2, 3]}
        mock_execute = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)

        try:
            agent_cfg = _make_agent_cfg()
            events = []
            async for ev_type, ev_data in _execute_tool_cached_gen(
                "query_aws_costs", {"account": "123"}, agent_cfg, "cost"
            ):
                events.append((ev_type, ev_data))

            # Should NOT have a cache_hit event
            assert not any(ev[0] == "cache_hit" for ev in events)
            # Last event should be "done" with the result
            done_event = [ev for ev in events if ev[0] == "done"]
            assert len(done_event) == 1
            assert done_event[0][1]["result"] == mock_result
            assert done_event[0][1]["cached"] is False
            mock_execute.assert_awaited_once_with("query_aws_costs", {"account": "123"})
        finally:
            _tool_cache.reset(token)

    @pytest.mark.asyncio
    async def test_tool_error_yields_error_result(self, monkeypatch):
        from src.agent.agents import _execute_tool_cached_gen
        from src.agent.orchestrator import _tool_cache

        token = _tool_cache.set({})

        mock_execute = AsyncMock(side_effect=RuntimeError("connection refused"))
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)

        try:
            agent_cfg = _make_agent_cfg()
            events = []
            async for ev_type, ev_data in _execute_tool_cached_gen(
                "query_aws_costs", {"account": "123"}, agent_cfg, "cost"
            ):
                events.append((ev_type, ev_data))

            done_event = [ev for ev in events if ev[0] == "done"]
            assert len(done_event) == 1
            assert "error" in done_event[0][1]["result"]
            assert "connection refused" in done_event[0][1]["result"]["error"]
        finally:
            _tool_cache.reset(token)

    @pytest.mark.asyncio
    async def test_uncacheable_tool_skips_cache(self, monkeypatch):
        from src.agent.agents import _execute_tool_cached_gen
        from src.agent.orchestrator import _tool_cache

        # Pre-populate cache with a key that matches render_chart
        cache_key_val = "render_chart:" + json.dumps({"chart": "bar"}, sort_keys=True)
        token = _tool_cache.set({cache_key_val: {"cached": True}})

        mock_result = {"chart_url": "/charts/123.png"}
        mock_execute = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)

        try:
            agent_cfg = _make_agent_cfg()
            events = []
            async for ev_type, ev_data in _execute_tool_cached_gen(
                "render_chart", {"chart": "bar"}, agent_cfg, "cost"
            ):
                events.append((ev_type, ev_data))

            # render_chart is in _UNCACHEABLE_TOOLS, so no cache_hit
            assert not any(ev[0] == "cache_hit" for ev in events)
            done_event = [ev for ev in events if ev[0] == "done"]
            assert done_event[0][1]["result"] == mock_result
            mock_execute.assert_awaited_once()
        finally:
            _tool_cache.reset(token)

    @pytest.mark.asyncio
    async def test_cache_miss_stores_result(self, monkeypatch):
        from src.agent.agents import _execute_tool_cached_gen
        from src.agent.orchestrator import _tool_cache

        cache = {}
        token = _tool_cache.set(cache)

        mock_result = {"data": "fresh"}
        mock_execute = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)

        try:
            agent_cfg = _make_agent_cfg()
            async for _ in _execute_tool_cached_gen(
                "query_aws_costs", {"account": "456"}, agent_cfg, "cost"
            ):
                pass

            # Verify result was stored in cache
            expected_key = "query_aws_costs:" + json.dumps({"account": "456"}, sort_keys=True)
            assert expected_key in cache
            assert cache[expected_key] == mock_result
        finally:
            _tool_cache.reset(token)

    @pytest.mark.asyncio
    async def test_error_result_not_cached(self, monkeypatch):
        from src.agent.agents import _execute_tool_cached_gen
        from src.agent.orchestrator import _tool_cache

        cache = {}
        token = _tool_cache.set(cache)

        mock_result = {"error": "something broke"}
        mock_execute = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)

        try:
            agent_cfg = _make_agent_cfg()
            async for _ in _execute_tool_cached_gen(
                "query_aws_costs", {"account": "789"}, agent_cfg, "cost"
            ):
                pass

            # Error results should NOT be cached
            assert len(cache) == 0
        finally:
            _tool_cache.reset(token)

    @pytest.mark.asyncio
    async def test_no_cache_set_still_executes(self, monkeypatch):
        """When _tool_cache has no value (None), tool still executes normally."""
        from src.agent.agents import _execute_tool_cached_gen
        from src.agent.orchestrator import _tool_cache

        token = _tool_cache.set(None)

        mock_result = {"data": "no_cache"}
        mock_execute = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)

        try:
            agent_cfg = _make_agent_cfg()
            events = []
            async for ev_type, ev_data in _execute_tool_cached_gen(
                "query_aws_costs", {}, agent_cfg, "cost"
            ):
                events.append((ev_type, ev_data))

            done_event = [ev for ev in events if ev[0] == "done"]
            assert done_event[0][1]["result"] == mock_result
        finally:
            _tool_cache.reset(token)


# ---------------------------------------------------------------------------
# _try_sdk_streaming
# ---------------------------------------------------------------------------


class TestTrySdkStreaming:
    """_try_sdk_streaming uses deferred imports; patch on origin modules."""

    @pytest.mark.asyncio
    async def test_yields_expected_sse_events(self, monkeypatch):
        from src.agent.agents import _try_sdk_streaming

        sdk_result = {"summary": "Icinga host is DOWN due to network timeout"}

        mock_runner_instance = MagicMock()
        mock_runner_instance.run_sub_agent = AsyncMock(return_value=sdk_result)
        mock_runner_cls = MagicMock(return_value=mock_runner_instance)
        monkeypatch.setattr("src.agent.runner.AgentRunner", mock_runner_cls)

        # Mock orchestrator helpers used by _try_sdk_streaming
        monkeypatch.setattr(
            "src.agent.orchestrator._serialize_messages",
            lambda msgs: [{"role": "user", "content": "test"}],
        )
        monkeypatch.setattr(
            "src.agent.orchestrator._trim_history",
            lambda msgs: msgs,
        )

        agent_cfg = _make_agent_cfg(name="Icinga Monitoring", agent_type="icinga")
        cfg = SimpleNamespace()

        events = []
        async for event in _try_sdk_streaming(
            agent_type="icinga",
            agent_cfg=agent_cfg,
            cfg=cfg,
            task="Check host status",
            context=None,
            conversation_history=[],
            metrics=None,
        ):
            events.append(event)

        # Should have: agent_start, status, text, agent_done, history, done
        event_types = []
        for ev in events:
            for line in ev.split("\n"):
                if line.startswith("event: "):
                    event_types.append(line[7:])

        assert "agent_start" in event_types
        assert "status" in event_types
        assert "text" in event_types
        assert "agent_done" in event_types
        assert "history" in event_types
        assert "done" in event_types

    @pytest.mark.asyncio
    async def test_uses_error_field_when_no_summary(self, monkeypatch):
        from src.agent.agents import _try_sdk_streaming

        sdk_result = {"error": "SDK connection failed"}

        mock_runner_instance = MagicMock()
        mock_runner_instance.run_sub_agent = AsyncMock(return_value=sdk_result)
        mock_runner_cls = MagicMock(return_value=mock_runner_instance)
        monkeypatch.setattr("src.agent.runner.AgentRunner", mock_runner_cls)

        monkeypatch.setattr(
            "src.agent.orchestrator._serialize_messages",
            lambda msgs: [],
        )
        monkeypatch.setattr(
            "src.agent.orchestrator._trim_history",
            lambda msgs: msgs,
        )

        agent_cfg = _make_agent_cfg(name="Icinga Monitoring", agent_type="icinga")

        events = []
        async for event in _try_sdk_streaming(
            agent_type="icinga",
            agent_cfg=agent_cfg,
            cfg=SimpleNamespace(),
            task="Check host",
            context=None,
            conversation_history=None,
            metrics=None,
        ):
            events.append(event)

        # The text event should contain the error message
        text_events = [ev for ev in events if "SDK connection failed" in ev]
        assert len(text_events) >= 1

    @pytest.mark.asyncio
    async def test_fallback_to_no_output(self, monkeypatch):
        from src.agent.agents import _try_sdk_streaming

        sdk_result = {}

        mock_runner_instance = MagicMock()
        mock_runner_instance.run_sub_agent = AsyncMock(return_value=sdk_result)
        mock_runner_cls = MagicMock(return_value=mock_runner_instance)
        monkeypatch.setattr("src.agent.runner.AgentRunner", mock_runner_cls)

        monkeypatch.setattr(
            "src.agent.orchestrator._serialize_messages",
            lambda msgs: [],
        )
        monkeypatch.setattr(
            "src.agent.orchestrator._trim_history",
            lambda msgs: msgs,
        )

        agent_cfg = _make_agent_cfg(name="Icinga Monitoring", agent_type="icinga")

        events = []
        async for event in _try_sdk_streaming(
            agent_type="icinga",
            agent_cfg=agent_cfg,
            cfg=SimpleNamespace(),
            task="Check host",
            context=None,
            conversation_history=None,
            metrics=None,
        ):
            events.append(event)

        # Should contain "(no output)" in the text event
        text_events = [ev for ev in events if "(no output)" in ev]
        assert len(text_events) >= 1
