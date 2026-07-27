"""Coverage tests for remaining helpers in src/agent/runner.py.

Covers _EnteredSpan, _mark_sdk_unavailable edge cases,
and _set_agent_span_outputs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.agent.runner import _EnteredSpan, _mark_sdk_unavailable, _set_agent_span_outputs
from src.llm import SdkResult, SdkUsage

# ===================================================================
# _EnteredSpan
# ===================================================================


class TestEnteredSpan:
    def test_enter_returns_span(self):
        mock_cm = MagicMock()
        span = SimpleNamespace(set_outputs=MagicMock())
        entered = _EnteredSpan(mock_cm, span)
        result = entered.__enter__()
        assert result is span

    def test_exit_calls_cm_exit(self):
        mock_cm = MagicMock()
        span = SimpleNamespace()
        entered = _EnteredSpan(mock_cm, span)
        entered.__exit__(None, None, None)
        mock_cm.__exit__.assert_called_once_with(None, None, None)

    def test_exit_suppresses_exception_from_cm(self):
        mock_cm = MagicMock()
        mock_cm.__exit__.side_effect = RuntimeError("span close failed")
        span = SimpleNamespace()
        entered = _EnteredSpan(mock_cm, span)
        # Should not raise
        entered.__exit__(None, None, None)

    def test_used_as_context_manager(self):
        mock_cm = MagicMock()
        span = SimpleNamespace(set_outputs=MagicMock())
        entered = _EnteredSpan(mock_cm, span)
        with entered as s:
            assert s is span


# ===================================================================
# _mark_sdk_unavailable
# ===================================================================


class TestMarkSdkUnavailable:
    def test_noop_when_no_metrics(self):
        # Should not raise
        _mark_sdk_unavailable(None)

    def test_tags_metrics_with_sdk_error(self):
        from src.metrics.collector import MetricsCollector

        collector = MetricsCollector(conversation_id="test")
        _mark_sdk_unavailable(collector)
        assert collector.runtime == "sdk"
        assert collector.status == "error"


# ===================================================================
# _set_agent_span_outputs
# ===================================================================


class TestSetAgentSpanOutputs:
    def test_noop_when_span_is_none(self):
        result = SdkResult(
            text="answer",
            tool_invocations=[],
            model="claude-sonnet-4",
            usage=SdkUsage(
                input_tokens=10,
                output_tokens=5,
            ),
        )
        # Should not raise
        _set_agent_span_outputs(None, "icinga", result, 1.5)

    def test_sets_outputs_and_attributes(self):
        span = SimpleNamespace(
            set_outputs=MagicMock(),
            set_attributes=MagicMock(),
        )
        result = SdkResult(
            text="triage complete",
            tool_invocations=[
                {"name": "query_icinga", "input": {}, "is_error": False},
            ],
            model="claude-sonnet-4-5",
            usage=SdkUsage(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=5,
                total_cost_usd=0.05,
                num_turns=2,
            ),
        )
        _set_agent_span_outputs(span, "icinga", result, 3.5)
        span.set_outputs.assert_called_once()
        span.set_attributes.assert_called_once()
        # Verify output contents
        outputs = span.set_outputs.call_args[0][0]
        assert outputs["status"] == "success"
        assert outputs["tool_calls"] == 1
        assert "triage complete" in outputs["response_preview"]
        # Verify attribute contents
        attrs = span.set_attributes.call_args[0][0]
        assert attrs["runtime"] == "sdk"
        assert attrs["agent_type"] == "icinga"
        assert attrs["duration_seconds"] == 3.5
        assert attrs["gen_ai.usage.input_tokens"] == 100
        assert attrs["gen_ai.usage.cost_usd"] == 0.05
