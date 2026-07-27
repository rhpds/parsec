"""Tests for pure-logic helpers in src/tools/splunk.py."""

from src.tools.splunk import (
    AAP_INDEX,
    OCP_APP_INDEX,
    _build_aap2_query,
    _build_guid_query,
    _build_namespace_query,
)

# ---------------------------------------------------------------------------
# _build_guid_query
# ---------------------------------------------------------------------------


class TestBuildGuidQuery:
    def test_basic_guid(self):
        result = _build_guid_query("abc123", "", False, "")
        assert isinstance(result, tuple)
        query, index = result
        assert '"abc123"' in query
        assert f"index={OCP_APP_INDEX}" in query
        assert index == OCP_APP_INDEX

    def test_empty_guid_returns_error(self):
        result = _build_guid_query("", "", False, "")
        assert isinstance(result, dict)
        assert "error" in result

    def test_with_cluster_name(self):
        result = _build_guid_query("abc123", "east.cluster.example.com", False, "")
        query, _ = result
        assert '"east.cluster.example.com"' in query

    def test_with_errors_only(self):
        result = _build_guid_query("abc123", "", True, "")
        query, _ = result
        assert '"error"' in query or '"fatal"' in query

    def test_with_search_terms(self):
        result = _build_guid_query("abc123", "", False, "provision failed")
        query, _ = result
        assert '"provision failed"' in query

    def test_all_options(self):
        result = _build_guid_query("abc123", "west", True, "timeout")
        query, _ = result
        assert '"abc123"' in query
        assert '"west"' in query
        assert '"error"' in query or '"fatal"' in query
        assert '"timeout"' in query

    def test_query_ends_with_spath_sort(self):
        result = _build_guid_query("abc123", "", False, "")
        query, _ = result
        assert query.endswith("| spath | sort -_time")

    def test_sanitizes_input(self):
        # Quotes are stripped by _sanitize_query_value
        result = _build_guid_query('abc"123', "", False, "")
        query, _ = result
        assert '"' not in query.replace(f"index={OCP_APP_INDEX}", "").replace(
            "| spath | sort -_time", ""
        ).replace('"abc123"', "SAFE")


# ---------------------------------------------------------------------------
# _build_namespace_query
# ---------------------------------------------------------------------------


class TestBuildNamespaceQuery:
    def test_basic_namespace(self):
        result = _build_namespace_query("my-namespace", "", False, "")
        assert isinstance(result, tuple)
        query, index = result
        assert '"my-namespace"' in query
        assert index == OCP_APP_INDEX

    def test_empty_namespace_returns_error(self):
        result = _build_namespace_query("", "", False, "")
        assert isinstance(result, dict)
        assert "error" in result

    def test_with_cluster_name(self):
        result = _build_namespace_query("test-ns", "east.cluster.example.com", False, "")
        query, _ = result
        assert '"east.cluster.example.com"' in query

    def test_with_errors_only(self):
        result = _build_namespace_query("test-ns", "", True, "")
        query, _ = result
        assert '"error"' in query or '"fatal"' in query

    def test_with_search_terms(self):
        result = _build_namespace_query("test-ns", "", False, "crash loop")
        query, _ = result
        assert '"crash loop"' in query


# ---------------------------------------------------------------------------
# _build_aap2_query
# ---------------------------------------------------------------------------


class TestBuildAap2Query:
    def test_basic_controller(self):
        result = _build_aap2_query("east.controller.example.com", "", False, "")
        assert isinstance(result, tuple)
        query, index = result
        assert '"east.controller.example.com"' in query
        assert f"index={AAP_INDEX}" in query
        assert index == AAP_INDEX

    def test_empty_controller_returns_error(self):
        result = _build_aap2_query("", "", False, "")
        assert isinstance(result, dict)
        assert "error" in result

    def test_with_guid(self):
        result = _build_aap2_query("east.ctrl", "guid-abc", False, "")
        query, _ = result
        assert '"guid-abc"' in query

    def test_with_errors_only(self):
        result = _build_aap2_query("east.ctrl", "", True, "")
        query, _ = result
        assert '"ERROR"' in query or '"CRITICAL"' in query

    def test_with_search_terms(self):
        result = _build_aap2_query("east.ctrl", "", False, "agnosticd")
        query, _ = result
        assert '"agnosticd"' in query

    def test_all_options(self):
        result = _build_aap2_query("east.ctrl", "guid-abc", True, "deploy failed")
        query, _ = result
        assert '"east.ctrl"' in query
        assert '"guid-abc"' in query
        assert '"ERROR"' in query
        assert '"deploy failed"' in query

    def test_query_ends_with_spath_sort(self):
        result = _build_aap2_query("ctrl", "", False, "")
        query, _ = result
        assert query.endswith("| spath | sort -_time")
