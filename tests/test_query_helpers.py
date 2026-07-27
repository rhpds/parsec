"""Tests for pure-logic helpers in src/routes/query.py."""


from src.routes.query import _is_user_in_email_list, _parse_csv_set

# ---------------------------------------------------------------------------
# _parse_csv_set
# ---------------------------------------------------------------------------


class TestParseCsvSet:
    def test_simple_csv(self):
        result = _parse_csv_set("alice@redhat.com,bob@redhat.com")
        assert result == {"alice@redhat.com", "bob@redhat.com"}

    def test_lowercased(self):
        result = _parse_csv_set("Alice@Redhat.com,BOB@REDHAT.COM")
        assert result == {"alice@redhat.com", "bob@redhat.com"}

    def test_strips_whitespace(self):
        result = _parse_csv_set("  alice@redhat.com , bob@redhat.com  ")
        assert result == {"alice@redhat.com", "bob@redhat.com"}

    def test_skips_blanks(self):
        result = _parse_csv_set("alice@redhat.com,,,,bob@redhat.com,")
        assert result == {"alice@redhat.com", "bob@redhat.com"}

    def test_empty_string(self):
        result = _parse_csv_set("")
        assert result == set()

    def test_single_value(self):
        result = _parse_csv_set("solo@redhat.com")
        assert result == {"solo@redhat.com"}

    def test_whitespace_only_entries(self):
        result = _parse_csv_set("  ,  , alice@redhat.com ,  ")
        assert result == {"alice@redhat.com"}


# ---------------------------------------------------------------------------
# _is_user_in_email_list
# ---------------------------------------------------------------------------


class TestIsUserInEmailList:
    def test_user_in_list(self):
        assert _is_user_in_email_list("alice@redhat.com", "alice@redhat.com,bob@redhat.com") is True

    def test_user_not_in_list(self):
        assert (
            _is_user_in_email_list("charlie@redhat.com", "alice@redhat.com,bob@redhat.com") is False
        )

    def test_case_insensitive(self):
        assert _is_user_in_email_list("ALICE@REDHAT.COM", "alice@redhat.com") is True

    def test_empty_allowed_str(self):
        assert _is_user_in_email_list("alice@redhat.com", "") is False

    def test_user_with_spaces_in_list(self):
        assert (
            _is_user_in_email_list("alice@redhat.com", "  alice@redhat.com , bob@redhat.com")
            is True
        )

    def test_single_user_match(self):
        assert _is_user_in_email_list("alice@redhat.com", "alice@redhat.com") is True

    def test_single_user_no_match(self):
        assert _is_user_in_email_list("bob@redhat.com", "alice@redhat.com") is False
