"""Extended tests for src/agent/learnings.py — uncovered helper functions and pipeline."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.agent.learnings import (
    _extract_tool_calls,
    _find_similar_entry,
    _merge_entries,
    _parse_analysis_response,
    _summarize_assistant_content,
    _summarize_user_content,
    analyze_and_learn,
)

# ---------------------------------------------------------------------------
# _summarize_user_content
# ---------------------------------------------------------------------------


class TestSummarizeUserContent:
    def test_string_content(self):
        result = _summarize_user_content("hello world")
        assert result == ["User: hello world"]

    def test_list_with_text_blocks(self):
        content = [{"text": "What is going on?"}]
        result = _summarize_user_content(content)
        assert result == ["User: What is going on?"]

    def test_list_with_tool_result_block(self):
        content = [{"type": "tool_result", "content": "some result data"}]
        result = _summarize_user_content(content)
        assert result == ["[tool_result: some result data]"]

    def test_tool_result_long_content_truncated(self):
        long_text = "x" * 300
        content = [{"type": "tool_result", "content": long_text}]
        result = _summarize_user_content(content)
        assert len(result) == 1
        assert result[0] == f"[tool_result: {'x' * 200}...]"

    def test_tool_result_exactly_200_chars_not_truncated(self):
        text_200 = "y" * 200
        content = [{"type": "tool_result", "content": text_200}]
        result = _summarize_user_content(content)
        assert result == [f"[tool_result: {text_200}]"]

    def test_mixed_blocks(self):
        content = [
            {"text": "Tell me about costs"},
            {"type": "tool_result", "content": "AWS: $500"},
            {"text": "And Azure?"},
        ]
        result = _summarize_user_content(content)
        assert result == [
            "User: Tell me about costs",
            "[tool_result: AWS: $500]",
            "User: And Azure?",
        ]

    def test_non_dict_items_skipped(self):
        content = ["raw string", 42, None, {"text": "kept"}]
        result = _summarize_user_content(content)
        assert result == ["User: kept"]

    def test_empty_list(self):
        result = _summarize_user_content([])
        assert result == []

    def test_non_list_non_string(self):
        result = _summarize_user_content(12345)
        assert result == ["User: 12345"]

    def test_block_without_text_or_tool_result_skipped(self):
        content = [{"type": "image", "source": "..."}]
        result = _summarize_user_content(content)
        assert result == []


# ---------------------------------------------------------------------------
# _summarize_assistant_content
# ---------------------------------------------------------------------------


class TestSummarizeAssistantContent:
    def test_string_content(self):
        result = _summarize_assistant_content("The cost is $500.")
        assert result == ["Assistant: The cost is $500."]

    def test_long_string_truncated_at_300(self):
        long_text = "a" * 500
        result = _summarize_assistant_content(long_text)
        assert len(result) == 1
        assert result[0] == f"Assistant: {'a' * 300}"

    def test_list_with_text_blocks(self):
        content = [{"type": "text", "text": "Here is the answer."}]
        result = _summarize_assistant_content(content)
        assert result == ["Assistant: Here is the answer."]

    def test_list_text_block_truncated_at_300(self):
        long_text = "b" * 500
        content = [{"type": "text", "text": long_text}]
        result = _summarize_assistant_content(content)
        assert result == [f"Assistant: {'b' * 300}"]

    def test_list_with_tool_use_blocks(self):
        content = [{"type": "tool_use", "name": "query_costs", "input": {"account": "123"}}]
        result = _summarize_assistant_content(content)
        assert len(result) == 1
        assert result[0].startswith("Tool call: query_costs(")
        assert '"account": "123"' in result[0]

    def test_tool_use_long_input_truncated(self):
        big_input = {"data": "z" * 300}
        content = [{"type": "tool_use", "name": "big_tool", "input": big_input}]
        result = _summarize_assistant_content(content)
        assert len(result) == 1
        serialized_input = json.dumps(big_input)[:150]
        assert result[0] == f"Tool call: big_tool({serialized_input})"

    def test_non_list_non_string_returns_empty(self):
        result = _summarize_assistant_content(42)
        assert result == []

    def test_none_returns_empty(self):
        result = _summarize_assistant_content(None)
        assert result == []

    def test_non_dict_items_in_list_skipped(self):
        content = ["raw", 99, {"type": "text", "text": "ok"}]
        result = _summarize_assistant_content(content)
        assert result == ["Assistant: ok"]

    def test_text_block_with_empty_text_skipped(self):
        content = [{"type": "text", "text": ""}]
        result = _summarize_assistant_content(content)
        assert result == []

    def test_tool_use_missing_input_defaults(self):
        content = [{"type": "tool_use", "name": "simple_tool"}]
        result = _summarize_assistant_content(content)
        assert result == ["Tool call: simple_tool({})"]


# ---------------------------------------------------------------------------
# _extract_tool_calls
# ---------------------------------------------------------------------------


class TestExtractToolCalls:
    def test_empty_messages(self):
        assert _extract_tool_calls([]) == []

    def test_no_assistant_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "system prompt"},
        ]
        assert _extract_tool_calls(messages) == []

    def test_assistant_with_tool_use(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "query_costs", "input": {"year": 2026}},
                    {"type": "tool_use", "name": "query_db", "input": {"sql": "SELECT 1"}},
                ],
            }
        ]
        result = _extract_tool_calls(messages)
        assert len(result) == 2
        assert result[0] == {"name": "query_costs", "input": {"year": 2026}}
        assert result[1] == {"name": "query_db", "input": {"sql": "SELECT 1"}}

    def test_assistant_with_text_blocks_ignored(self):
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Here is your answer."}],
            }
        ]
        assert _extract_tool_calls(messages) == []

    def test_non_dict_messages_skipped(self):
        messages = ["not a dict", 42, None]
        assert _extract_tool_calls(messages) == []

    def test_assistant_with_string_content_skipped(self):
        messages = [{"role": "assistant", "content": "just a string"}]
        assert _extract_tool_calls(messages) == []

    def test_mixed_messages(self):
        messages = [
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {"type": "tool_use", "name": "lookup", "input": {}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "content": "data"}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        ]
        result = _extract_tool_calls(messages)
        assert len(result) == 1
        assert result[0] == {"name": "lookup", "input": {}}

    def test_tool_use_missing_name_and_input(self):
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use"}],
            }
        ]
        result = _extract_tool_calls(messages)
        assert result == [{"name": "", "input": {}}]


# ---------------------------------------------------------------------------
# _find_similar_entry
# ---------------------------------------------------------------------------


class TestFindSimilarEntry:
    def test_empty_new_text(self):
        existing = [{"text": "some entry", "count": 1}]
        assert _find_similar_entry(existing, "") is None

    def test_whitespace_only_new_text(self):
        existing = [{"text": "some entry", "count": 1}]
        assert _find_similar_entry(existing, "   ") is None

    def test_no_similar_entries(self):
        existing = [
            {"text": "AWS costs are high for GPU instances", "count": 1},
            {"text": "Check CloudTrail for API events", "count": 2},
        ]
        result = _find_similar_entry(existing, "azure billing csv import failed")
        assert result is None

    def test_high_overlap_returns_match(self):
        entry = {"text": "Always check the destroy job logs first", "count": 3}
        existing = [entry]
        # Same words, different order -> high overlap
        result = _find_similar_entry(existing, "check the destroy job logs first always")
        assert result is entry

    def test_low_overlap_returns_none(self):
        existing = [{"text": "GPU instances cost a lot on AWS", "count": 1}]
        result = _find_similar_entry(existing, "azure billing database is slow today")
        assert result is None

    def test_exact_match(self):
        entry = {"text": "destroy jobs fail with timeout", "count": 5}
        existing = [entry]
        result = _find_similar_entry(existing, "destroy jobs fail with timeout")
        assert result is entry

    def test_multiple_entries_returns_first_match(self):
        entry_a = {"text": "check the logs for errors always", "count": 1}
        entry_b = {"text": "check the logs for errors and warnings always", "count": 2}
        existing = [entry_a, entry_b]
        result = _find_similar_entry(existing, "check the logs for errors always")
        assert result is entry_a

    def test_empty_existing_list(self):
        assert _find_similar_entry([], "some new learning") is None

    def test_existing_entry_with_empty_text(self):
        existing = [{"text": "", "count": 1}]
        result = _find_similar_entry(existing, "some new text")
        assert result is None


# ---------------------------------------------------------------------------
# _parse_analysis_response
# ---------------------------------------------------------------------------


class TestParseAnalysisResponse:
    def test_valid_json_array(self):
        text = '["learning one", "learning two"]'
        result = _parse_analysis_response(text)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert len(result) == 2
        assert result[0]["text"] == "learning one"
        assert result[0]["count"] == 1
        assert result[0]["last_seen"] == today
        assert result[1]["text"] == "learning two"

    def test_json_in_markdown_code_fence(self):
        text = '```json\n["inside fence"]\n```'
        result = _parse_analysis_response(text)
        assert len(result) == 1
        assert result[0]["text"] == "inside fence"

    def test_json_with_surrounding_text(self):
        text = 'Here are the learnings:\n["extracted"]\nEnd of response.'
        result = _parse_analysis_response(text)
        assert len(result) == 1
        assert result[0]["text"] == "extracted"

    def test_invalid_json(self):
        text = '["broken", "json'
        result = _parse_analysis_response(text)
        assert result == []

    def test_empty_array(self):
        text = "[]"
        result = _parse_analysis_response(text)
        assert result == []

    def test_non_string_items_filtered(self):
        text = '["valid", 42, {"key": "val"}, "also valid"]'
        result = _parse_analysis_response(text)
        assert len(result) == 2
        assert result[0]["text"] == "valid"
        assert result[1]["text"] == "also valid"

    def test_whitespace_only_strings_filtered(self):
        text = '["good", "  ", "", "also good"]'
        result = _parse_analysis_response(text)
        assert len(result) == 2
        assert result[0]["text"] == "good"
        assert result[1]["text"] == "also good"

    def test_no_array_in_text(self):
        text = "No JSON here, just plain text about learnings."
        result = _parse_analysis_response(text)
        assert result == []

    def test_whitespace_stripped_from_entries(self):
        text = '["  has leading spaces  "]'
        result = _parse_analysis_response(text)
        assert result[0]["text"] == "has leading spaces"

    def test_pure_whitespace_text(self):
        result = _parse_analysis_response("   ")
        assert result == []


# ---------------------------------------------------------------------------
# _merge_entries
# ---------------------------------------------------------------------------


class TestMergeEntries:
    def test_new_into_empty_existing(self):
        new_entries = [
            {"text": "learning A", "count": 1, "last_seen": "2026-07-01"},
            {"text": "learning B", "count": 1, "last_seen": "2026-07-01"},
        ]
        result = _merge_entries([], new_entries)
        assert len(result) == 2

    def test_similar_entry_increases_count(self):
        existing = [{"text": "check destroy job logs first", "count": 2, "last_seen": "2026-06-01"}]
        new_entries = [
            {"text": "check destroy job logs first always", "count": 1, "last_seen": "2026-07-01"}
        ]
        result = _merge_entries(existing, new_entries)
        assert len(result) == 1
        assert result[0]["count"] == 3
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert result[0]["last_seen"] == today

    def test_dissimilar_entry_appended(self):
        existing = [{"text": "AWS GPU costs are high", "count": 1, "last_seen": "2026-06-01"}]
        new_entries = [
            {
                "text": "Azure billing CSV import sometimes fails",
                "count": 1,
                "last_seen": "2026-07-01",
            }
        ]
        result = _merge_entries(existing, new_entries)
        assert len(result) == 2

    def test_capped_at_50(self):
        # Use fully distinct texts so nothing triggers the >60% overlap merge
        existing = [
            {
                "text": f"unique-pattern-{i} is important to remember",
                "count": 100 - i,
                "last_seen": "2026-01-01",
            }
            for i in range(48)
        ]
        new_entries = [
            {
                "text": "completely unrelated alpha observation",
                "count": 1,
                "last_seen": "2026-07-01",
            },
            {"text": "different beta discovery insight", "count": 1, "last_seen": "2026-07-01"},
            {"text": "separate gamma finding noted", "count": 1, "last_seen": "2026-07-01"},
        ]
        result = _merge_entries(existing, new_entries)
        assert len(result) == 50

    def test_sorted_by_count_then_last_seen(self):
        existing = []
        # Use fully distinct texts to avoid >60% word overlap triggering merges
        new_entries = [
            {
                "text": "azure billing csv import sometimes fails silently",
                "count": 1,
                "last_seen": "2026-01-01",
            },
            {
                "text": "GPU instances on AWS are extremely expensive always",
                "count": 5,
                "last_seen": "2026-03-01",
            },
            {
                "text": "cloudtrail lake queries need proper date filtering",
                "count": 1,
                "last_seen": "2026-07-01",
            },
        ]
        result = _merge_entries(existing, new_entries)
        assert len(result) == 3
        # Highest count first
        assert result[0]["text"] == "GPU instances on AWS are extremely expensive always"
        # Among count=1, more recent last_seen sorts first
        assert result[1]["text"] == "cloudtrail lake queries need proper date filtering"
        assert result[2]["text"] == "azure billing csv import sometimes fails silently"

    def test_similar_entry_keeps_longer_text(self):
        # 5/7 words overlap = 0.71 > 0.6, so these merge; new is longer
        existing = [
            {"text": "always check destroy job logs first", "count": 1, "last_seen": "2026-06-01"}
        ]
        new_entries = [
            {
                "text": "always check destroy job logs first for timeout errors",
                "count": 1,
                "last_seen": "2026-07-01",
            }
        ]
        result = _merge_entries(existing, new_entries)
        assert len(result) == 1
        # Longer text replaces shorter
        assert result[0]["text"] == "always check destroy job logs first for timeout errors"

    def test_similar_entry_keeps_shorter_text_when_existing_longer(self):
        # 5/7 words overlap = 0.71 > 0.6, so these merge; existing is longer
        existing = [
            {
                "text": "always check destroy job logs first for timeout errors",
                "count": 1,
                "last_seen": "2026-06-01",
            }
        ]
        new_entries = [
            {"text": "always check destroy job logs first", "count": 1, "last_seen": "2026-07-01"}
        ]
        result = _merge_entries(existing, new_entries)
        assert len(result) == 1
        # Existing text is longer, so it stays
        assert result[0]["text"] == "always check destroy job logs first for timeout errors"


# ---------------------------------------------------------------------------
# analyze_and_learn (async pipeline)
# ---------------------------------------------------------------------------


class TestAnalyzeAndLearn:
    @pytest.mark.asyncio
    async def test_too_few_user_messages_returns_early(self, monkeypatch):
        mock_save = patch("src.agent.learnings._save_entries").start()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        await analyze_and_learn(messages)
        mock_save.assert_not_called()
        patch.stopall()

    @pytest.mark.asyncio
    async def test_too_few_tool_calls_returns_early(self, monkeypatch):
        mock_save = patch("src.agent.learnings._save_entries").start()
        messages = [
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            {"role": "user", "content": "question 2"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "tool_a", "input": {}},
                    {"type": "tool_use", "name": "tool_b", "input": {}},
                ],
            },
        ]
        await analyze_and_learn(messages)
        mock_save.assert_not_called()
        patch.stopall()

    @pytest.mark.asyncio
    async def test_ai_analyze_returns_entries_merges_and_saves(self, monkeypatch):
        messages = [
            {"role": "user", "content": "question 1"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "t1", "input": {}},
                    {"type": "tool_use", "name": "t2", "input": {}},
                    {"type": "tool_use", "name": "t3", "input": {}},
                ],
            },
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        mock_ai = AsyncMock(return_value=[{"text": "new learning", "count": 1, "last_seen": today}])
        monkeypatch.setattr("src.agent.learnings._ai_analyze", mock_ai)
        monkeypatch.setattr("src.agent.learnings._load_entries", lambda: [])

        saved = {}

        def capture_save(entries):
            saved["entries"] = entries

        monkeypatch.setattr("src.agent.learnings._save_entries", capture_save)

        await analyze_and_learn(messages)

        mock_ai.assert_awaited_once()
        assert "entries" in saved
        assert len(saved["entries"]) == 1
        assert saved["entries"][0]["text"] == "new learning"

    @pytest.mark.asyncio
    async def test_ai_analyze_returns_empty_no_save(self, monkeypatch):
        messages = [
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "a", "input": {}},
                    {"type": "tool_use", "name": "b", "input": {}},
                    {"type": "tool_use", "name": "c", "input": {}},
                ],
            },
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]

        monkeypatch.setattr("src.agent.learnings._ai_analyze", AsyncMock(return_value=[]))

        save_called = []
        monkeypatch.setattr("src.agent.learnings._save_entries", lambda e: save_called.append(e))

        await analyze_and_learn(messages)
        assert save_called == []

    @pytest.mark.asyncio
    async def test_exception_logged_not_raised(self, monkeypatch):
        messages = [
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "a", "input": {}},
                    {"type": "tool_use", "name": "b", "input": {}},
                    {"type": "tool_use", "name": "c", "input": {}},
                ],
            },
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]

        monkeypatch.setattr(
            "src.agent.learnings._ai_analyze",
            AsyncMock(side_effect=RuntimeError("API down")),
        )

        # Should not raise
        await analyze_and_learn(messages)

    @pytest.mark.asyncio
    async def test_merges_with_existing_entries(self, monkeypatch):
        messages = [
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "x", "input": {}},
                    {"type": "tool_use", "name": "y", "input": {}},
                    {"type": "tool_use", "name": "z", "input": {}},
                ],
            },
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        existing_entry = {"text": "existing pattern", "count": 3, "last_seen": "2026-06-01"}

        monkeypatch.setattr(
            "src.agent.learnings._ai_analyze",
            AsyncMock(return_value=[{"text": "brand new insight", "count": 1, "last_seen": today}]),
        )
        monkeypatch.setattr("src.agent.learnings._load_entries", lambda: [existing_entry.copy()])

        saved = {}
        monkeypatch.setattr("src.agent.learnings._save_entries", lambda e: saved.update(entries=e))

        await analyze_and_learn(messages)

        assert len(saved["entries"]) == 2
        texts = [e["text"] for e in saved["entries"]]
        assert "existing pattern" in texts
        assert "brand new insight" in texts
