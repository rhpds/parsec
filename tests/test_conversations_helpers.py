"""Tests for pure-logic helpers in src/routes/conversations.py."""


from src.routes.conversations import (
    _auto_title,
    _count_user_messages,
    _extract_first_user_text,
    _truncate_title,
)

# ---------------------------------------------------------------------------
# _extract_first_user_text
# ---------------------------------------------------------------------------


class TestExtractFirstUserText:
    def test_simple_string_content(self):
        messages = [{"role": "user", "content": "Hello world"}]
        assert _extract_first_user_text(messages) == "Hello world"

    def test_list_content_with_text_blocks(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"text": "Part one"},
                    {"text": "Part two"},
                ],
            }
        ]
        assert _extract_first_user_text(messages) == "Part one Part two"

    def test_skips_assistant_messages(self):
        messages = [
            {"role": "assistant", "content": "I am Claude"},
            {"role": "user", "content": "User question"},
        ]
        assert _extract_first_user_text(messages) == "User question"

    def test_skips_tool_result_role(self):
        messages = [
            {"role": "tool_result", "content": "tool output"},
            {"role": "user", "content": "Real question"},
        ]
        assert _extract_first_user_text(messages) == "Real question"

    def test_empty_messages(self):
        assert _extract_first_user_text([]) == ""

    def test_no_user_messages(self):
        messages = [{"role": "assistant", "content": "Hello"}]
        assert _extract_first_user_text(messages) == ""

    def test_empty_user_content(self):
        messages = [{"role": "user", "content": ""}]
        assert _extract_first_user_text(messages) == ""

    def test_whitespace_only_user_content(self):
        messages = [
            {"role": "user", "content": "   "},
            {"role": "user", "content": "Second question"},
        ]
        assert _extract_first_user_text(messages) == "Second question"

    def test_non_dict_in_messages_list(self):
        messages = ["not a dict", {"role": "user", "content": "Valid"}]
        assert _extract_first_user_text(messages) == "Valid"

    def test_list_content_with_non_dict_blocks(self):
        messages = [
            {
                "role": "user",
                "content": [
                    "not a dict",
                    {"text": "Valid block"},
                ],
            }
        ]
        assert _extract_first_user_text(messages) == "Valid block"

    def test_missing_content_key(self):
        messages = [{"role": "user"}]
        assert _extract_first_user_text(messages) == ""

    def test_none_in_messages(self):
        messages = [None, {"role": "user", "content": "Hello"}]
        assert _extract_first_user_text(messages) == "Hello"


# ---------------------------------------------------------------------------
# _truncate_title
# ---------------------------------------------------------------------------


class TestTruncateTitle:
    def test_short_text_unchanged(self):
        assert _truncate_title("Short title", 80) == "Short title"

    def test_exact_max_len(self):
        text = "x" * 80
        assert _truncate_title(text, 80) == text

    def test_truncates_at_word_boundary(self):
        text = "This is a longer sentence that should be truncated at a word boundary for display"
        result = _truncate_title(text, 40)
        assert result.endswith("...")
        assert len(result) <= 44  # 40 + "..."

    def test_truncates_without_space_fallback(self):
        # When last_space is before max_len // 2, it falls through to hard truncation
        text = "x" * 100
        result = _truncate_title(text, 20)
        assert result.endswith("...")
        assert result == "x" * 20 + "..."

    def test_default_max_len_not_passed(self):
        # The function signature has max_len with no default in definition,
        # but _auto_title calls it with 80
        short = "Hello"
        assert _truncate_title(short, 80) == "Hello"

    def test_word_boundary_near_end(self):
        text = "Hello world this is exactly enough text to test the boundary case right here ok"
        result = _truncate_title(text, 50)
        assert result.endswith("...")
        # Should truncate at a space near the 50-char mark
        without_ellipsis = result[:-3]
        assert " " not in without_ellipsis or without_ellipsis.endswith(" ") is False

    def test_empty_string(self):
        assert _truncate_title("", 80) == ""


# ---------------------------------------------------------------------------
# _auto_title
# ---------------------------------------------------------------------------


class TestAutoTitle:
    def test_generates_title_from_first_message(self):
        messages = [{"role": "user", "content": "How much did account 123 spend?"}]
        assert _auto_title(messages) == "How much did account 123 spend?"

    def test_fallback_when_no_user_message(self):
        messages = [{"role": "assistant", "content": "Hello"}]
        assert _auto_title(messages) == "New conversation"

    def test_empty_messages(self):
        assert _auto_title([]) == "New conversation"

    def test_long_question_truncated(self):
        long_question = "What is the total cost of " + "all services " * 20
        messages = [{"role": "user", "content": long_question}]
        result = _auto_title(messages)
        assert len(result) <= 84  # 80 + "..."

    def test_whitespace_only_user_content(self):
        messages = [{"role": "user", "content": "   "}]
        assert _auto_title(messages) == "New conversation"


# ---------------------------------------------------------------------------
# _count_user_messages
# ---------------------------------------------------------------------------


class TestCountUserMessages:
    def test_counts_user_and_assistant(self):
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
        ]
        assert _count_user_messages(messages) == 4

    def test_skips_tool_result(self):
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "tool_result", "content": "data"},
        ]
        assert _count_user_messages(messages) == 2

    def test_empty_messages(self):
        assert _count_user_messages([]) == 0

    def test_non_dict_items_skipped(self):
        messages = [
            "not a dict",
            {"role": "user", "content": "Q1"},
        ]
        assert _count_user_messages(messages) == 1

    def test_missing_role_key(self):
        messages = [{"content": "no role"}, {"role": "user", "content": "Q1"}]
        assert _count_user_messages(messages) == 1
