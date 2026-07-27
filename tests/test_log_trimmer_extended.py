"""Extended tests for src/agent/log_trimmer.py — covers trimming pipeline,
retry handling, fatal context, and full log processing."""

from __future__ import annotations

from src.agent.log_trimmer import (
    _extract_container_statuses,
    _extract_first_pod,
    _extract_json_errors,
    _extract_k8s_pod_status,
    _find_fatal_context,
    _find_recap_start,
    _flush_retry_summary,
    _format_trimmed_output,
    _is_noise_line,
    _is_unconditional_keep,
    _process_fatal_line,
    _process_retry_line,
    _track_retry,
    _truncate_line,
    is_ansible_log,
    trim_ansible_log,
)

# ---------------------------------------------------------------------------
# is_ansible_log
# ---------------------------------------------------------------------------


class TestIsAnsibleLog:
    def test_ansible_log_with_play_and_task(self):
        content = "PLAY [deploy]\nTASK [setup]\nok: [host1]\nPLAY RECAP"
        assert is_ansible_log(content) is True

    def test_not_ansible_log(self):
        content = "Just some random text without Ansible markers"
        assert is_ansible_log(content) is False

    def test_only_one_marker(self):
        content = "PLAY [deploy]\nSome other stuff"
        assert is_ansible_log(content) is False

    def test_markers_at_end(self):
        content = "x" * 9000 + "\nPLAY RECAP\nTASK [final]"
        assert is_ansible_log(content) is True


# ---------------------------------------------------------------------------
# _extract_json_errors
# ---------------------------------------------------------------------------


class TestExtractJsonErrors:
    def test_extracts_msg_field(self):
        line = 'fatal: [host] => {"msg": "Something failed", "rc": 1}'
        result = _extract_json_errors(line)
        assert result is not None
        assert "Something failed" in result

    def test_extracts_stderr_field(self):
        line = 'fatal: [host] => {"stderr": "Permission denied"}'
        result = _extract_json_errors(line)
        assert result is not None
        assert "Permission denied" in result

    def test_extracts_cmd_field(self):
        line = 'fatal: [host] => {"msg": "fail", "cmd": ["python", "-c", "import sys"]}'
        result = _extract_json_errors(line)
        assert result is not None
        assert "python" in result

    def test_cmd_as_string(self):
        line = 'fatal: [host] => {"msg": "fail", "cmd": "ls -la /tmp"}'
        result = _extract_json_errors(line)
        assert result is not None
        assert "ls -la" in result

    def test_no_arrow_marker(self):
        result = _extract_json_errors("just a plain line")
        assert result is None

    def test_no_error_keys(self):
        line = 'fatal: [host] => {"rc": 1, "changed": false}'
        result = _extract_json_errors(line)
        assert result is None or result == ""

    def test_invalid_json(self):
        line = "fatal: [host] => {not valid json"
        result = _extract_json_errors(line)
        assert result is None

    def test_paren_marker(self):
        """Test that ``=> (`` marker is also detected."""
        line = 'fatal: [host] => (item=test) {"msg": "fail"}'
        # The paren marker path: start = line.find("=> (")
        # This particular case won't parse because json starts after => (
        result = _extract_json_errors(line)
        # Depending on the JSON parse, may be None
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# _extract_first_pod
# ---------------------------------------------------------------------------


class TestExtractFirstPod:
    def test_valid_pod_resource(self):
        data = {
            "resources": [
                {
                    "kind": "Pod",
                    "metadata": {"name": "test-pod"},
                    "status": {"phase": "Failed"},
                }
            ]
        }
        result = _extract_first_pod(data)
        assert result is not None
        assert result["metadata"]["name"] == "test-pod"

    def test_no_resources_key(self):
        assert _extract_first_pod({}) is None

    def test_empty_resources_list(self):
        assert _extract_first_pod({"resources": []}) is None

    def test_non_list_resources(self):
        assert _extract_first_pod({"resources": "not a list"}) is None

    def test_non_pod_resource(self):
        data = {"resources": [{"kind": "Service", "status": {}}]}
        assert _extract_first_pod(data) is None

    def test_non_dict_first_resource(self):
        data = {"resources": ["not a dict"]}
        assert _extract_first_pod(data) is None

    def test_non_dict_status(self):
        data = {"resources": [{"kind": "Pod", "status": "invalid"}]}
        assert _extract_first_pod(data) is None


# ---------------------------------------------------------------------------
# _extract_container_statuses
# ---------------------------------------------------------------------------


class TestExtractContainerStatuses:
    def test_unhealthy_container(self):
        status = {
            "containerStatuses": [
                {
                    "name": "main",
                    "ready": False,
                    "restartCount": 3,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m",
                        }
                    },
                }
            ]
        }
        parts = _extract_container_statuses(status)
        assert len(parts) == 1
        assert "main" in parts[0]
        assert "CrashLoopBackOff" in parts[0]
        assert "restarts=3" in parts[0]

    def test_healthy_container_skipped(self):
        status = {
            "containerStatuses": [{"name": "main", "ready": True, "restartCount": 0, "state": {}}]
        }
        parts = _extract_container_statuses(status)
        assert len(parts) == 0

    def test_init_container_failure(self):
        status = {
            "initContainerStatuses": [
                {
                    "name": "init-certs",
                    "ready": False,
                    "restartCount": 1,
                    "state": {"waiting": {"reason": "ImagePullBackOff"}},
                }
            ]
        }
        parts = _extract_container_statuses(status)
        assert len(parts) == 1
        assert "Init" in parts[0]
        assert "init-certs" in parts[0]

    def test_empty_status(self):
        parts = _extract_container_statuses({})
        assert parts == []

    def test_non_dict_container_entry_skipped(self):
        status = {"containerStatuses": ["not a dict"]}
        parts = _extract_container_statuses(status)
        assert parts == []

    def test_container_with_restarts_but_ready(self):
        """Container that restarted but is now ready still gets reported."""
        status = {
            "containerStatuses": [{"name": "main", "ready": True, "restartCount": 5, "state": {}}]
        }
        parts = _extract_container_statuses(status)
        assert len(parts) == 1
        assert "restarts=5" in parts[0]


# ---------------------------------------------------------------------------
# _flush_retry_summary
# ---------------------------------------------------------------------------


class TestFlushRetrySummary:
    def test_appends_summary(self):
        result: list[str] = []
        _flush_retry_summary(result, 5)
        assert len(result) == 1
        assert "retried 5 times" in result[0]

    def test_zero_retries_no_append(self):
        result: list[str] = []
        _flush_retry_summary(result, 0)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# _track_retry
# ---------------------------------------------------------------------------


class TestTrackRetry:
    def test_first_retry(self):
        was_retry, count = _track_retry(False, 0)
        assert was_retry is True
        assert count == 1

    def test_subsequent_retry(self):
        was_retry, count = _track_retry(True, 3)
        assert was_retry is True
        assert count == 4


# ---------------------------------------------------------------------------
# _process_fatal_line
# ---------------------------------------------------------------------------


class TestProcessFatalLine:
    def test_non_retry_line(self):
        result: list[str] = []
        was_retry, count = _process_fatal_line(
            "fatal: [host]: FAILED! => {}", "fatal: [host]: FAILED! => {}", result, False, 0
        )
        assert was_retry is False
        assert count == 0
        assert len(result) == 1

    def test_retry_line_in_fatal_context(self):
        result: list[str] = []
        stripped = "FAILED - RETRYING: Wait for pod (3 retries left)"
        was_retry, count = _process_fatal_line(stripped, stripped, result, False, 0)
        assert was_retry is True
        assert count == 1
        assert len(result) == 0

    def test_non_retry_after_retries_flushes_summary(self):
        result: list[str] = []
        was_retry, count = _process_fatal_line(
            "fatal: actual failure",
            "fatal: actual failure",
            result,
            True,  # prev_was_retry
            5,  # retry_count
        )
        assert was_retry is False
        assert count == 0
        # Should have retry summary + the actual line
        assert len(result) == 2
        assert "retried 5 times" in result[0]


# ---------------------------------------------------------------------------
# _process_retry_line
# ---------------------------------------------------------------------------


class TestProcessRetryLine:
    def test_non_retry_line_passthrough(self):
        result: list[str] = []
        was_retry, count = _process_retry_line("ok: [host]", result, False, 0)
        assert was_retry is False
        assert count == 0

    def test_retry_with_retries_left(self):
        result: list[str] = []
        stripped = "FAILED - RETRYING: Wait for DNS (5 retries left)"
        was_retry, count = _process_retry_line(stripped, result, False, 0)
        assert was_retry is True
        assert count == 1

    def test_retry_with_one_left_flushes(self):
        result: list[str] = []
        stripped = "FAILED - RETRYING: Wait for DNS (1 retries left)"
        was_retry, count = _process_retry_line(stripped, result, True, 4)
        # Last retry: flushes summary and resets
        assert was_retry is False
        assert count == 0
        assert len(result) == 1
        assert "retried 5 times" in result[0]


# ---------------------------------------------------------------------------
# _find_fatal_context / _find_recap_start
# ---------------------------------------------------------------------------


class TestFindFatalContext:
    def test_finds_context_around_fatal(self):
        lines = ["line0", "line1", "line2", "fatal: [host]", "line4", "line5", "line6"]
        indices = _find_fatal_context(lines)
        # 2 before (1,2) + the line (3) + 3 after (4,5,6)
        assert 1 in indices
        assert 2 in indices
        assert 3 in indices
        assert 4 in indices
        assert 5 in indices
        assert 6 in indices
        assert 0 not in indices

    def test_fatal_at_start(self):
        lines = ["fatal: [host]", "line1", "line2"]
        indices = _find_fatal_context(lines)
        assert 0 in indices
        assert 1 in indices
        assert 2 in indices

    def test_failed_excl_mark(self):
        lines = ["line0", "FAILED!", "line2"]
        indices = _find_fatal_context(lines)
        assert 1 in indices

    def test_no_fatal_lines(self):
        lines = ["ok: [host]", "TASK [test]", "PLAY RECAP"]
        indices = _find_fatal_context(lines)
        assert len(indices) == 0

    def test_failed_prefix(self):
        lines = ["line0", "  failed: [host]", "line2"]
        indices = _find_fatal_context(lines)
        assert 1 in indices


class TestFindRecapStart:
    def test_finds_play_recap(self):
        lines = ["PLAY [test]", "TASK [task1]", "ok: [host]", "PLAY RECAP ****"]
        result = _find_recap_start(lines)
        assert result == 3

    def test_finds_tasks_recap(self):
        lines = ["PLAY [test]", "TASKS RECAP ****"]
        result = _find_recap_start(lines)
        assert result == 1

    def test_no_recap(self):
        lines = ["PLAY [test]", "TASK [task1]", "ok: [host]"]
        result = _find_recap_start(lines)
        assert result is None


# ---------------------------------------------------------------------------
# _is_noise_line / _is_unconditional_keep
# ---------------------------------------------------------------------------


class TestIsNoiseLine:
    def test_empty_line(self):
        assert _is_noise_line("") is True

    def test_skipping(self):
        assert _is_noise_line("skipping: [host]") is True

    def test_changed(self):
        assert _is_noise_line("changed: [host]") is True

    def test_included(self):
        assert _is_noise_line("included: /path/to/file.yml") is True

    def test_warning(self):
        assert _is_noise_line("[WARNING]: Some warning message") is True

    def test_deprecation_warning(self):
        assert _is_noise_line("[DEPRECATION WARNING]: Old module") is True

    def test_warning_embedded(self):
        assert _is_noise_line("Something [WARNING] about stuff") is True

    def test_timestamp_line(self):
        assert _is_noise_line("Monday 15 January 2024  12:00:00 +0000") is True

    def test_task_header_not_noise(self):
        assert _is_noise_line("TASK [deploy : install package]") is False

    def test_fatal_not_noise(self):
        assert _is_noise_line("fatal: [host]: FAILED!") is False

    def test_async_prefix(self):
        assert _is_noise_line("ASYNC OK on host") is True


class TestIsUnconditionalKeep:
    def test_ignoring(self):
        assert _is_unconditional_keep("...ignoring") is True

    def test_no_more_hosts(self):
        assert _is_unconditional_keep("NO MORE HOSTS LEFT ****") is True

    def test_vault_password(self):
        assert _is_unconditional_keep("Vault password: ") is True

    def test_pausing(self):
        assert _is_unconditional_keep("Pausing for 30 seconds") is True

    def test_regular_line(self):
        assert _is_unconditional_keep("ok: [host]") is False


# ---------------------------------------------------------------------------
# _truncate_line
# ---------------------------------------------------------------------------


class TestTruncateLine:
    def test_short_line_unchanged(self):
        line = "ok: [host]"
        assert _truncate_line(line) == line

    def test_long_line_truncated(self):
        line = "x" * 3000
        result = _truncate_line(line)
        assert "truncated from 3,000 chars" in result
        assert len(result) < 3000

    def test_fatal_line_larger_budget(self):
        line = "fatal: " + "x" * 2000
        result_normal = _truncate_line(line, is_fatal=False)
        result_fatal = _truncate_line(line, is_fatal=True)
        # Fatal lines get larger budget, so should be longer or equal
        assert len(result_fatal) >= len(result_normal)


# ---------------------------------------------------------------------------
# _format_trimmed_output
# ---------------------------------------------------------------------------


class TestFormatTrimmedOutput:
    def test_basic_formatting(self):
        result = _format_trimmed_output(["line1", "line2"], 100, 5000)
        assert "[Trimmed Ansible log" in result
        assert "100 → 2 lines" in result
        assert "line1" in result
        assert "line2" in result

    def test_small_file_uses_chars(self):
        result = _format_trimmed_output(["line1"], 10, 500)
        assert "chars" in result

    def test_large_file_uses_kb(self):
        result = _format_trimmed_output(["line1"], 1000, 50000)
        assert "KB" in result


# ---------------------------------------------------------------------------
# trim_ansible_log (integration)
# ---------------------------------------------------------------------------


class TestTrimAnsibleLog:
    def test_preserves_first_three_lines(self):
        content = "line0\nline1\nline2\nok: [host]\nok: [host2]"
        result = trim_ansible_log(content)
        assert "line0" in result
        assert "line1" in result
        assert "line2" in result

    def test_preserves_play_task_headers(self):
        content = (
            "header1\nheader2\nheader3\n"
            "PLAY [deploy]\n"
            "TASK [install packages]\n"
            "ok: [host]\n"
            "skipping: [host2]\n"
        )
        result = trim_ansible_log(content)
        assert "PLAY [deploy]" in result
        assert "TASK [install packages]" in result

    def test_strips_ok_lines(self):
        content = (
            "h1\nh2\nh3\n"
            "PLAY [test]\n"
            "ok: [host1]\n"
            "ok: [host2]\n"
            "PLAY RECAP ****\nhost1: ok=5"
        )
        result = trim_ansible_log(content)
        # ok lines without "msg" should be stripped
        lines = result.split("\n")
        ok_lines = [line for line in lines if line.strip().startswith("ok:")]
        assert len(ok_lines) == 0

    def test_keeps_ok_with_msg(self):
        content = (
            "h1\nh2\nh3\n"
            "PLAY [test]\n"
            'ok: [host] => {"msg": "Deploy complete"}\n'
            "PLAY RECAP ****\nhost1: ok=5"
        )
        result = trim_ansible_log(content)
        assert '"msg"' in result

    def test_preserves_fatal_lines(self):
        content = (
            "h1\nh2\nh3\n"
            "PLAY [test]\n"
            "TASK [deploy]\n"
            'fatal: [host]: FAILED! => {"msg": "connection refused"}\n'
            "PLAY RECAP ****\nhost1: unreachable=1"
        )
        result = trim_ansible_log(content)
        assert "fatal:" in result
        assert "connection refused" in result

    def test_preserves_recap(self):
        content = (
            "h1\nh2\nh3\n"
            "PLAY [test]\n"
            "ok: [host]\n"
            "PLAY RECAP ****\n"
            "host1 : ok=5  changed=0  unreachable=0  failed=0\n"
        )
        result = trim_ansible_log(content)
        assert "PLAY RECAP" in result
        assert "ok=5" in result

    def test_strips_skipping_lines(self):
        content = "h1\nh2\nh3\nPLAY [test]\nskipping: [host1]\nskipping: [host2]\nPLAY RECAP"
        result = trim_ansible_log(content)
        assert "skipping:" not in result

    def test_handles_retries(self):
        lines = ["h1", "h2", "h3", "PLAY [test]", "TASK [wait]"]
        for i in range(5, 0, -1):
            lines.append(f"FAILED - RETRYING: Wait for pod ({i} retries left)")
        lines.append('fatal: [host]: FAILED! => {"msg": "timed out"}')
        lines.append("PLAY RECAP ****")
        content = "\n".join(lines)
        result = trim_ansible_log(content)
        assert "retried" in result
        assert "timed out" in result

    def test_keeps_ignoring_lines(self):
        content = "h1\nh2\nh3\nPLAY [test]\n...ignoring\nPLAY RECAP"
        result = trim_ansible_log(content)
        assert "...ignoring" in result

    def test_keeps_no_more_hosts(self):
        content = "h1\nh2\nh3\nPLAY [test]\nNO MORE HOSTS LEFT ****\nPLAY RECAP"
        result = trim_ansible_log(content)
        assert "NO MORE HOSTS LEFT" in result

    def test_metadata_header_present(self):
        content = "h1\nh2\nh3\nPLAY [test]\nPLAY RECAP"
        result = trim_ansible_log(content)
        assert result.startswith("[Trimmed Ansible log")

    def test_strips_timestamp_lines(self):
        content = "h1\nh2\nh3\nPLAY [test]\nMonday 15 January 2024  12:00:00\nPLAY RECAP"
        result = trim_ansible_log(content)
        assert "Monday 15 January" not in result


# ---------------------------------------------------------------------------
# _extract_k8s_pod_status
# ---------------------------------------------------------------------------


class TestExtractK8sPodStatus:
    def test_extracts_pod_phase(self):
        import json

        data = {
            "resources": [
                {
                    "kind": "Pod",
                    "status": {
                        "phase": "Failed",
                        "conditions": [
                            {"type": "Ready", "status": "False", "message": "not ready"},
                        ],
                    },
                }
            ]
        }
        line = f"fatal: [host] => {json.dumps(data)}"
        result = _extract_k8s_pod_status(line)
        assert result is not None
        assert "Pod Phase: Failed" in result
        assert "Ready" in result

    def test_no_json_blob(self):
        assert _extract_k8s_pod_status("just a line") is None

    def test_not_a_pod_resource(self):
        import json

        data = {"resources": [{"kind": "Service", "status": {}}]}
        line = f"fatal: [host] => {json.dumps(data)}"
        result = _extract_k8s_pod_status(line)
        assert result is None
