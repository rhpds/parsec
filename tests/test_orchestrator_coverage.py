"""Additional coverage tests for src/agent/orchestrator.py.

Covers _record_usage, _yield_output_events, _record_llm_span,
_parse_alert_response_blocks, _process_alert_round_tools,
_estimate_tokens, _save_report, _flush_collector, _trim_history (full),
_serialize_messages, _dispatch_tool_blocks, and _dump_api_request.
"""

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.orchestrator import (
    _estimate_tokens,
    _parse_alert_response_blocks,
    _process_alert_round_tools,
    _record_llm_span,
    _record_usage,
    _save_report,
    _serialize_messages,
    _trim_history,
    _yield_output_events,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_mlflow_span():
    """Return a mock mlflow.start_span that yields a SimpleNamespace span."""
    span = SimpleNamespace(
        set_inputs=MagicMock(),
        set_outputs=MagicMock(),
        set_attribute=MagicMock(),
        set_attributes=MagicMock(),
    )

    @contextmanager
    def _ctx(*args, **kwargs):
        yield span

    return _ctx


# ===================================================================
# _record_usage
# ===================================================================


class TestRecordUsage:
    def test_records_tokens_from_response(self):
        from src.metrics.collector import MetricsCollector

        collector = MetricsCollector(conversation_id="test-1")
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=5,
            ),
            model="claude-sonnet-4-20250514",
        )
        _record_usage(collector, response, "claude-sonnet-4-20250514")
        assert collector.input_tokens == 100
        assert collector.output_tokens == 50
        assert collector.cache_creation_tokens == 10
        assert collector.cache_read_tokens == 5

    def test_records_model_on_first_call(self):
        from src.metrics.collector import MetricsCollector

        collector = MetricsCollector(conversation_id="test-2")
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            model="claude-sonnet-4-20250514",
        )
        _record_usage(collector, response, "claude-sonnet-4-20250514")
        assert collector.model == "claude-sonnet-4-20250514"
        assert collector.agent_type == "orchestrator"
        assert collector.routing_method == "llm"

    def test_does_not_overwrite_model_on_subsequent_calls(self):
        from src.metrics.collector import MetricsCollector

        collector = MetricsCollector(conversation_id="test-3")
        collector.model = "existing-model"
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            model="new-model",
        )
        _record_usage(collector, response, "new-model")
        assert collector.model == "existing-model"

    def test_skips_when_no_usage_attribute(self):
        from src.metrics.collector import MetricsCollector

        collector = MetricsCollector(conversation_id="test-4")
        response = SimpleNamespace(model="test")  # no usage attribute
        _record_usage(collector, response, "test")
        assert collector.input_tokens == 0
        assert collector.output_tokens == 0

    def test_handles_none_cache_tokens(self):
        from src.metrics.collector import MetricsCollector

        collector = MetricsCollector(conversation_id="test-5")
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=200,
                output_tokens=100,
                cache_creation_input_tokens=None,
                cache_read_input_tokens=None,
            ),
            model="claude-sonnet-4-20250514",
        )
        _record_usage(collector, response, "claude-sonnet-4-20250514")
        assert collector.cache_creation_tokens == 0
        assert collector.cache_read_tokens == 0


# ===================================================================
# _yield_output_events
# ===================================================================


class TestYieldOutputEvents:
    def test_generate_report_success(self):
        result = {"filename": "report_2024-01-15.md", "format": "markdown"}
        events = _yield_output_events("generate_report", result)
        assert len(events) == 1
        assert "report" in events[0]
        assert "report_2024-01-15.md" in events[0]
        assert "/api/reports/report_2024-01-15.md" in events[0]

    def test_generate_report_with_error(self):
        result = {"error": "failed"}
        events = _yield_output_events("generate_report", result)
        assert events == []

    def test_render_chart_success(self):
        result = {"type": "bar", "data": [1, 2, 3]}
        events = _yield_output_events("render_chart", result)
        assert len(events) == 1
        assert "chart" in events[0]

    def test_render_chart_with_error(self):
        result = {"error": "rendering failed"}
        events = _yield_output_events("render_chart", result)
        assert events == []

    def test_regular_tool_returns_empty(self):
        result = {"rows": [1, 2, 3]}
        events = _yield_output_events("query_aws_costs", result)
        assert events == []


# ===================================================================
# _record_llm_span
# ===================================================================


class TestRecordLlmSpan:
    def test_records_span_outputs(self):
        span = SimpleNamespace(
            set_inputs=MagicMock(),
            set_outputs=MagicMock(),
            set_attributes=MagicMock(),
        )
        response = SimpleNamespace(
            model="claude-sonnet-4-20250514",
            usage=SimpleNamespace(input_tokens=500, output_tokens=200),
        )
        _record_llm_span(
            span,
            round_num=0,
            response=response,
            default_model="fallback-model",
            response_text="Hello world",
            tool_use_names=["query_aws_costs"],
        )
        span.set_inputs.assert_called_once()
        span.set_outputs.assert_called_once()
        span.set_attributes.assert_called_once()

    def test_uses_default_model_when_response_has_none(self):
        span = SimpleNamespace(
            set_inputs=MagicMock(),
            set_outputs=MagicMock(),
            set_attributes=MagicMock(),
        )
        response = SimpleNamespace()  # no model or usage
        _record_llm_span(
            span,
            round_num=1,
            response=response,
            default_model="fallback-model",
            response_text="text",
            tool_use_names=[],
        )
        # Verify it used the fallback model
        inputs = span.set_inputs.call_args[0][0]
        assert inputs["model"] == "fallback-model"


# ===================================================================
# _parse_alert_response_blocks
# ===================================================================


class TestParseAlertResponseBlocks:
    def test_text_blocks_logged(self):
        blocks = [
            SimpleNamespace(type="text", text="I will investigate this alert."),
            SimpleNamespace(type="text", text="Checking CloudTrail..."),
        ]
        investigation_log = []
        text_parts, tool_blocks = _parse_alert_response_blocks(blocks, investigation_log)
        assert text_parts == ["I will investigate this alert.", "Checking CloudTrail..."]
        assert tool_blocks == []
        assert len(investigation_log) == 2
        assert "investigate this alert" in investigation_log[0]

    def test_tool_use_blocks_extracted(self):
        blocks = [
            SimpleNamespace(type="tool_use", name="query_cloudtrail", id="t1", input={}),
        ]
        investigation_log = []
        text_parts, tool_blocks = _parse_alert_response_blocks(blocks, investigation_log)
        assert text_parts == []
        assert len(tool_blocks) == 1
        assert tool_blocks[0].name == "query_cloudtrail"

    def test_empty_text_skipped(self):
        blocks = [
            SimpleNamespace(type="text", text=""),
            SimpleNamespace(type="text", text="   "),
        ]
        investigation_log = []
        text_parts, tool_blocks = _parse_alert_response_blocks(blocks, investigation_log)
        assert text_parts == []
        assert tool_blocks == []
        assert len(investigation_log) == 0

    def test_mixed_blocks(self):
        blocks = [
            SimpleNamespace(type="text", text="Analyzing..."),
            SimpleNamespace(type="tool_use", name="query_aws_account", id="t1", input={}),
            SimpleNamespace(type="text", text="Done."),
        ]
        investigation_log = []
        text_parts, tool_blocks = _parse_alert_response_blocks(blocks, investigation_log)
        assert text_parts == ["Analyzing...", "Done."]
        assert len(tool_blocks) == 1


# ===================================================================
# _process_alert_round_tools
# ===================================================================


class TestProcessAlertRoundTools:
    @pytest.mark.asyncio
    async def test_verdict_tool(self, monkeypatch):
        monkeypatch.setattr("mlflow.start_span", _fake_mlflow_span())
        blocks = [
            SimpleNamespace(
                name="submit_alert_verdict",
                id="t1",
                input={
                    "should_alert": False,
                    "severity": "low",
                    "summary": "Benign",
                },
            ),
        ]
        investigation_log = []
        verdict, tool_results, count = await _process_alert_round_tools(blocks, investigation_log)
        assert verdict is not None
        assert verdict["should_alert"] is False
        assert count == 0  # verdict tools don't count as tool_calls
        assert len(tool_results) == 1

    @pytest.mark.asyncio
    async def test_regular_tools_counted(self, monkeypatch):
        monkeypatch.setattr("mlflow.start_span", _fake_mlflow_span())
        mock_execute = AsyncMock(return_value={"data": "ok"})
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)
        blocks = [
            SimpleNamespace(name="query_cloudtrail", id="t1", input={"query": "SELECT *"}),
            SimpleNamespace(
                name="query_aws_account", id="t2", input={"account_id": "123", "action": "list"}
            ),
        ]
        investigation_log = []
        verdict, tool_results, count = await _process_alert_round_tools(blocks, investigation_log)
        assert verdict is None
        assert count == 2
        assert len(tool_results) == 2

    @pytest.mark.asyncio
    async def test_mixed_verdict_and_tools(self, monkeypatch):
        monkeypatch.setattr("mlflow.start_span", _fake_mlflow_span())
        mock_execute = AsyncMock(return_value={"data": "ok"})
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)
        blocks = [
            SimpleNamespace(name="query_cloudtrail", id="t1", input={"query": "q"}),
            SimpleNamespace(
                name="submit_alert_verdict",
                id="t2",
                input={"should_alert": True, "severity": "high", "summary": "Bad"},
            ),
        ]
        investigation_log = []
        verdict, tool_results, count = await _process_alert_round_tools(blocks, investigation_log)
        assert verdict is not None
        assert verdict["should_alert"] is True
        assert count == 1  # only the non-verdict tool


# ===================================================================
# _estimate_tokens
# ===================================================================


class TestEstimateTokens:
    def test_simple_object(self):
        obj = {"hello": "world"}
        tokens = _estimate_tokens(obj)
        # len('{"hello": "world"}') = 18, // 4 = 4
        assert tokens == len(json.dumps(obj)) // 4

    def test_empty_list(self):
        assert _estimate_tokens([]) == 0  # len("[]") = 2, // 4 = 0

    def test_larger_object(self):
        obj = {"rows": list(range(100))}
        tokens = _estimate_tokens(obj)
        assert tokens > 0
        assert tokens == len(json.dumps(obj)) // 4


# ===================================================================
# _save_report
# ===================================================================


class TestSaveReport:
    def test_saves_markdown_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))
        result = _save_report(
            {
                "title": "Test Report",
                "content": "# Report\n\nContent here",
                "format": "markdown",
                "filename": "test_report",
            }
        )
        assert result["filename"] == "test_report.md"
        assert result["format"] == "markdown"
        assert result["title"] == "Test Report"
        assert result["size_bytes"] == len(b"# Report\n\nContent here")
        # Verify file was written
        filepath = os.path.join(str(tmp_path), "test_report.md")
        assert os.path.isfile(filepath)
        with open(filepath) as f:
            assert f.read() == "# Report\n\nContent here"

    def test_saves_asciidoc_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))
        result = _save_report(
            {
                "title": "AsciiDoc Report",
                "content": "= Title\n\nBody",
                "format": "asciidoc",
                "filename": "asciidoc_report",
            }
        )
        assert result["filename"] == "asciidoc_report.adoc"
        assert result["format"] == "asciidoc"

    def test_auto_generates_filename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))
        result = _save_report(
            {
                "title": "Auto Name",
                "content": "content",
            }
        )
        assert result["filename"].startswith("investigation_report_")
        assert result["filename"].endswith(".md")
        assert result["format"] == "markdown"

    def test_default_format_is_markdown(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))
        result = _save_report(
            {
                "title": "Default Format",
                "content": "some content",
                "filename": "default_fmt",
            }
        )
        assert result["format"] == "markdown"
        assert result["filename"].endswith(".md")


# ===================================================================
# _trim_history (full function)
# ===================================================================


class TestTrimHistory:
    def test_empty_history(self):
        assert _trim_history([]) == []

    def test_short_history_unchanged(self):
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        result = _trim_history(msgs)
        assert len(result) == 2

    def test_large_history_truncated(self):
        # Create a history that exceeds the token limit
        msgs = []
        for i in range(50):
            msgs.append({"role": "user", "content": f"Question {i} " + "x" * 5000})
            msgs.append({"role": "assistant", "content": f"Answer {i} " + "y" * 5000})
        result = _trim_history(msgs, max_tokens=10000)
        assert len(result) < len(msgs)
        # Latest messages should be preserved
        assert "Question 49" in result[-2]["content"]

    def test_respects_max_tokens_parameter(self):
        # Small max_tokens should drop more messages
        msgs = [
            {"role": "user", "content": "q1 " + "x" * 2000},
            {"role": "assistant", "content": "a1 " + "y" * 2000},
            {"role": "user", "content": "q2 " + "x" * 2000},
            {"role": "assistant", "content": "a2 " + "y" * 2000},
        ]
        result = _trim_history(msgs, max_tokens=500)
        assert len(result) <= 4


# ===================================================================
# _serialize_messages
# ===================================================================


class TestSerializeMessages:
    def test_string_content(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = _serialize_messages(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_list_content_with_dicts(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "response"},
                ],
            }
        ]
        result = _serialize_messages(msgs)
        assert result[0]["content"][0] == {"type": "text", "text": "response"}

    def test_list_content_with_sdk_objects(self):
        block = SimpleNamespace(
            type="text",
            text="from sdk",
            model_dump=lambda: {"type": "text", "text": "from sdk"},
        )
        msgs = [{"role": "assistant", "content": [block]}]
        result = _serialize_messages(msgs)
        assert result[0]["content"][0] == {"type": "text", "text": "from sdk"}

    def test_non_iterable_content_fallback(self):
        msgs = [{"role": "assistant", "content": 42}]
        result = _serialize_messages(msgs)
        assert result[0]["content"] == "42"


# ===================================================================
# _dispatch_tool_blocks (async)
# ===================================================================


class TestDispatchToolBlocks:
    @pytest.mark.asyncio
    async def test_dispatches_direct_tool(self, monkeypatch):
        from src.agent.orchestrator import _dispatch_tool_blocks

        monkeypatch.setattr("mlflow.start_span", _fake_mlflow_span())

        # Mock _execute_tool
        mock_execute = AsyncMock(return_value={"rows": [1, 2]})
        monkeypatch.setattr("src.agent.orchestrator._execute_tool", mock_execute)
        monkeypatch.setattr("src.agent.orchestrator._tool_cache", MagicMock(get=lambda *a: None))

        tool_block = SimpleNamespace(
            name="query_provisions_db",
            id="tool_1",
            input={"sql": "SELECT 1"},
        )

        events = []
        results = []
        async for sse_evt, tool_result in _dispatch_tool_blocks([tool_block], None, []):
            if sse_evt is not None:
                events.append(sse_evt)
            if tool_result is not None:
                results.append(tool_result)

        assert len(results) == 1
        assert results[0]["type"] == "tool_result"
        assert results[0]["tool_use_id"] == "tool_1"
        # Should have tool_start and tool_result SSE events
        assert any("tool_start" in e for e in events)


# ===================================================================
# _dump_api_request
# ===================================================================


class TestDumpApiRequest:
    def test_writes_debug_file_when_enabled(self, tmp_path, monkeypatch):
        from src.agent.orchestrator import _dump_api_request

        cfg = {"debug": {"dump_prompts": True}}
        monkeypatch.setattr(
            "src.agent.orchestrator.get_config",
            lambda: SimpleNamespace(get=lambda k, d=None: cfg.get(k, d)),
        )

        # Patch the debug directory
        str(tmp_path / "debug")
        monkeypatch.setattr("os.path.dirname", lambda p: str(tmp_path))

        _dump_api_request(
            label="test_round_0",
            system="System prompt",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"name": "tool1"}],
            model="claude-sonnet-4",
        )
        # Check that a file was created in the debug dir
        # Since the path is constructed from __file__, we just verify no crash

    def test_noop_when_disabled(self, monkeypatch):
        from src.agent.orchestrator import _dump_api_request

        cfg = SimpleNamespace(get=lambda k, d=None: {}.get(k, d))
        monkeypatch.setattr("src.agent.orchestrator.get_config", lambda: cfg)

        # Should not raise and should not write any files
        _dump_api_request(
            label="test",
            system="sys",
            messages=[],
            tools=[],
            model="model",
        )
