"""Tests for pure-logic helpers in src/agent/agents.py."""

import json
from types import SimpleNamespace

from src.agent.agents import (
    _classify_tool_outcome,
    _format_log_entry,
    _parse_response_blocks,
)

# ---------------------------------------------------------------------------
# Mock block objects — _parse_response_blocks accesses block.type, block.text,
# block.name via attribute access (Anthropic SDK content blocks).
# ---------------------------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str = "query_aws_costs", tool_id: str = "tool_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, id=tool_id, input={})


# ---------------------------------------------------------------------------
# _parse_response_blocks
# ---------------------------------------------------------------------------


class TestParseResponseBlocks:
    def test_text_only(self):
        blocks = [_text_block("Hello"), _text_block("World")]
        text_parts, tool_blocks = _parse_response_blocks(blocks)
        assert text_parts == ["Hello", "World"]
        assert tool_blocks == []

    def test_tool_use_only(self):
        blocks = [_tool_use_block("query_aws_costs"), _tool_use_block("query_azure_costs")]
        text_parts, tool_blocks = _parse_response_blocks(blocks)
        assert text_parts == []
        assert len(tool_blocks) == 2
        assert tool_blocks[0].name == "query_aws_costs"
        assert tool_blocks[1].name == "query_azure_costs"

    def test_mixed_blocks(self):
        blocks = [
            _text_block("I will query costs"),
            _tool_use_block("query_aws_costs"),
            _text_block("And also Azure"),
            _tool_use_block("query_azure_costs"),
        ]
        text_parts, tool_blocks = _parse_response_blocks(blocks)
        assert text_parts == ["I will query costs", "And also Azure"]
        assert len(tool_blocks) == 2

    def test_empty_blocks(self):
        text_parts, tool_blocks = _parse_response_blocks([])
        assert text_parts == []
        assert tool_blocks == []

    def test_unknown_block_type_ignored(self):
        blocks = [
            _text_block("Hello"),
            SimpleNamespace(type="thinking", text="internal thought"),
        ]
        text_parts, tool_blocks = _parse_response_blocks(blocks)
        assert text_parts == ["Hello"]
        assert tool_blocks == []


# ---------------------------------------------------------------------------
# _classify_tool_outcome
# ---------------------------------------------------------------------------


class TestClassifyToolOutcome:
    def test_success(self):
        result = _classify_tool_outcome("query_aws_costs", {"rows": [1, 2, 3], "count": 3})
        assert result == {"tool": "query_aws_costs", "status": "success"}

    def test_error(self):
        result = _classify_tool_outcome("query_aws_costs", {"error": "Connection timed out"})
        assert result["tool"] == "query_aws_costs"
        assert result["status"] == "error"
        assert "Connection timed out" in result["reason"]

    def test_empty_count_zero(self):
        result = _classify_tool_outcome("query_provisions_db", {"rows": [], "count": 0})
        assert result["tool"] == "query_provisions_db"
        assert result["status"] == "empty"
        assert result["reason"] == "no results returned"

    def test_no_error_no_count(self):
        result = _classify_tool_outcome("query_babylon_catalog", {"items": ["a"]})
        assert result == {"tool": "query_babylon_catalog", "status": "success"}

    def test_error_reason_truncated(self):
        long_error = "x" * 200
        result = _classify_tool_outcome("test_tool", {"error": long_error})
        assert len(result["reason"]) <= 100

    def test_count_negative_one_treated_as_success(self):
        # When count is not present, .get("count", -1) returns -1 which is != 0
        result = _classify_tool_outcome("some_tool", {"data": "stuff"})
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# _format_log_entry
# ---------------------------------------------------------------------------


class TestFormatLogEntry:
    def test_short_result(self):
        result = {"rows": [1]}
        entry = _format_log_entry("query_provisions_db", result)
        assert entry.startswith("[Tool: query_provisions_db] result: ")
        assert "..." not in entry

    def test_long_result_truncated(self):
        result = {"rows": list(range(500))}
        entry = _format_log_entry("query_provisions_db", result, max_len=100)
        assert entry.endswith("...")
        # The truncated result_str portion is max_len chars
        result_str = json.dumps(result, default=str)
        expected_prefix = f"[Tool: query_provisions_db] result: {result_str[:100]}..."
        assert entry == expected_prefix

    def test_default_max_len(self):
        result = {"data": "x" * 1000}
        entry = _format_log_entry("big_tool", result)
        assert "..." in entry

    def test_non_serializable_values(self):
        from datetime import datetime

        result = {"timestamp": datetime(2024, 1, 1)}
        entry = _format_log_entry("test_tool", result)
        assert "[Tool: test_tool]" in entry
