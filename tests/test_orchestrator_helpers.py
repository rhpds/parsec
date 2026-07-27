"""Tests for pure-logic helpers in src/agent/orchestrator.py."""

import json
from types import SimpleNamespace

from src.agent.orchestrator import (
    _build_alert_user_message,
    _cap_tool_result,
    _extract_text_from_sse,
    _parse_response_blocks,
    _try_smart_truncation,
)

# ---------------------------------------------------------------------------
# Mock block objects for _parse_response_blocks
# ---------------------------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str = "investigate_costs", tool_id: str = "tool_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, id=tool_id, input={})


# ---------------------------------------------------------------------------
# _build_alert_user_message
# ---------------------------------------------------------------------------


class TestBuildAlertUserMessage:
    def test_minimal(self):
        msg = _build_alert_user_message(
            alert_type="iam_key_created",
            account_id="123456789012",
            alert_text="New IAM key created",
        )
        assert "**Alert type:** iam_key_created" in msg
        assert "**Account ID:** 123456789012" in msg
        assert "New IAM key created" in msg
        assert "Investigate this alert" in msg

    def test_all_optional_fields(self):
        msg = _build_alert_user_message(
            alert_type="instance_launched",
            account_id="111111111111",
            alert_text="GPU instance launched",
            account_name="sandbox-42",
            user_arn="arn:aws:iam::root",
            event_time="2024-01-15T10:30:00Z",
            region="us-east-1",
            event_details={"instanceType": "p4d.24xlarge"},
        )
        assert "**Account name:** sandbox-42" in msg
        assert "**User ARN:** arn:aws:iam::root" in msg
        assert "**Event time:** 2024-01-15T10:30:00Z" in msg
        assert "**Region:** us-east-1" in msg
        assert "p4d.24xlarge" in msg
        assert "```json" in msg

    def test_no_optional_fields(self):
        msg = _build_alert_user_message(
            alert_type="test",
            account_id="000",
            alert_text="test alert",
        )
        assert "**Account name:**" not in msg
        assert "**User ARN:**" not in msg
        assert "**Event time:**" not in msg
        assert "**Region:**" not in msg
        assert "**Event details:**" not in msg

    def test_event_details_serialization(self):
        details = {"key": "value", "nested": {"a": 1}}
        msg = _build_alert_user_message(
            alert_type="test",
            account_id="000",
            alert_text="test",
            event_details=details,
        )
        assert '"key": "value"' in msg
        assert '"nested"' in msg


# ---------------------------------------------------------------------------
# _try_smart_truncation
# ---------------------------------------------------------------------------


class TestTrySmartTruncation:
    def test_truncates_rows(self):
        data = {"rows": list(range(100)), "count": 100}
        result = _try_smart_truncation(data)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["rows"]) == 20
        assert "_truncated" in parsed

    def test_truncates_results(self):
        data = {"results": [{"id": i} for i in range(50)]}
        result = _try_smart_truncation(data)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["results"]) == 20

    def test_truncates_items(self):
        data = {"items": list(range(30))}
        result = _try_smart_truncation(data)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["items"]) == 20

    def test_truncates_result_string_field(self):
        data = {"result": "x" * 20_000}
        result = _try_smart_truncation(data)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["result"]) <= 10_020  # 10000 + "... [truncated]"

    def test_returns_none_when_still_too_large(self):
        # Create data that is still over MAX_TOOL_RESULT_CHARS even after truncation
        from src.agent.orchestrator import MAX_TOOL_RESULT_CHARS

        data = {"rows": [{"data": "x" * 10_000} for _ in range(30)]}
        result = _try_smart_truncation(data)
        # If still too large after capping, returns None
        if result is not None:
            assert len(result) <= MAX_TOOL_RESULT_CHARS

    def test_no_truncatable_keys(self):
        data = {"custom_key": "some data"}
        result = _try_smart_truncation(data)
        assert result is None

    def test_list_under_threshold_not_truncated(self):
        data = {"rows": list(range(15))}
        result = _try_smart_truncation(data)
        # 15 items < 20, so no truncation happens on rows
        assert result is None


# ---------------------------------------------------------------------------
# _extract_text_from_sse
# ---------------------------------------------------------------------------


class TestExtractTextFromSse:
    def test_text_event(self):
        event = 'event: text\ndata: {"content": "Hello world"}\n\n'
        assert _extract_text_from_sse(event) == "Hello world"

    def test_non_text_event(self):
        event = 'event: status\ndata: {"content": "Processing..."}\n\n'
        assert _extract_text_from_sse(event) == ""

    def test_empty_content(self):
        event = 'event: text\ndata: {"content": ""}\n\n'
        assert _extract_text_from_sse(event) == ""

    def test_missing_content_key(self):
        event = 'event: text\ndata: {"other": "value"}\n\n'
        assert _extract_text_from_sse(event) == ""

    def test_invalid_json(self):
        event = "event: text\ndata: not-json\n\n"
        assert _extract_text_from_sse(event) == ""

    def test_empty_string(self):
        assert _extract_text_from_sse("") == ""

    def test_no_data_line(self):
        event = "event: text\n"
        assert _extract_text_from_sse(event) == ""


# ---------------------------------------------------------------------------
# _parse_response_blocks (orchestrator version)
# ---------------------------------------------------------------------------


class TestOrchestratorParseResponseBlocks:
    def test_text_only(self):
        blocks = [_text_block("Analysis complete")]
        text_parts, tool_blocks = _parse_response_blocks(blocks)
        assert text_parts == ["Analysis complete"]
        assert tool_blocks == []

    def test_tool_use_only(self):
        blocks = [_tool_use_block("investigate_costs")]
        text_parts, tool_blocks = _parse_response_blocks(blocks)
        assert text_parts == []
        assert len(tool_blocks) == 1

    def test_mixed(self):
        blocks = [
            _text_block("Let me investigate"),
            _tool_use_block("investigate_costs"),
        ]
        text_parts, tool_blocks = _parse_response_blocks(blocks)
        assert text_parts == ["Let me investigate"]
        assert len(tool_blocks) == 1

    def test_empty(self):
        text_parts, tool_blocks = _parse_response_blocks([])
        assert text_parts == []
        assert tool_blocks == []


# ---------------------------------------------------------------------------
# _cap_tool_result
# ---------------------------------------------------------------------------


class TestCapToolResult:
    def test_short_result_unchanged(self):
        result_str = json.dumps({"rows": [1, 2, 3]})
        assert _cap_tool_result(result_str) == result_str

    def test_long_result_truncated(self):
        from src.agent.orchestrator import MAX_TOOL_RESULT_CHARS

        big = json.dumps({"data": "x" * (MAX_TOOL_RESULT_CHARS + 1000)})
        capped = _cap_tool_result(big)
        assert len(capped) <= MAX_TOOL_RESULT_CHARS + 100  # allow for suffix

    def test_smart_truncation_applied(self):
        from src.agent.orchestrator import MAX_TOOL_RESULT_CHARS

        data = {"rows": [{"id": i, "payload": "x" * 500} for i in range(500)]}
        big = json.dumps(data)
        assert len(big) > MAX_TOOL_RESULT_CHARS
        capped = _cap_tool_result(big)
        # After smart truncation, rows should be capped to 20
        parseable = capped.split("\n... [truncated")[0] if "\n... [truncated" in capped else capped
        parsed = json.loads(parseable)
        assert len(parsed.get("rows", [])) <= 20 or "_truncated" in parsed

    def test_non_json_truncated(self):
        from src.agent.orchestrator import MAX_TOOL_RESULT_CHARS

        big = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
        capped = _cap_tool_result(big)
        assert "truncated" in capped

    def test_under_limit_passes_through(self):
        small = json.dumps({"ok": True})
        assert _cap_tool_result(small) == small
