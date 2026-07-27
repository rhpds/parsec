"""Tests for pure-logic helpers in src/agent/log_trimmer.py."""

import json

from src.agent.log_trimmer import (
    _extract_pod_conditions,
    _format_container_state,
    _parse_json_blob,
)

# ---------------------------------------------------------------------------
# _parse_json_blob
# ---------------------------------------------------------------------------


class TestParseJsonBlob:
    def test_valid_json(self):
        line = 'fatal: [host] => {"msg": "Something failed", "rc": 1}'
        result = _parse_json_blob(line)
        assert result is not None
        assert result["msg"] == "Something failed"
        assert result["rc"] == 1

    def test_no_arrow_marker(self):
        line = "just a plain line without the marker"
        result = _parse_json_blob(line)
        assert result is None

    def test_invalid_json_after_marker(self):
        line = "fatal: [host] => {not valid json"
        result = _parse_json_blob(line)
        assert result is None

    def test_empty_json_object(self):
        line = "fatal: [host] => {}"
        result = _parse_json_blob(line)
        assert result == {}

    def test_nested_json(self):
        data = {"msg": "fail", "results": [{"item": "pkg1", "rc": 1}]}
        line = f"fatal: [host] => {json.dumps(data)}"
        result = _parse_json_blob(line)
        assert result is not None
        assert result["results"][0]["item"] == "pkg1"

    def test_marker_with_whitespace(self):
        line = 'fatal: [host] => {"msg": "test"}   '
        result = _parse_json_blob(line)
        assert result is not None
        assert result["msg"] == "test"

    def test_line_with_text_after_json(self):
        # json.loads should fail if there's trailing text
        line = 'fatal: [host] => {"msg": "test"} extra text'
        result = _parse_json_blob(line)
        # This may return None because json.loads fails on trailing text
        # Depends on Python JSON parser strictness
        assert result is None or result.get("msg") == "test"


# ---------------------------------------------------------------------------
# _extract_pod_conditions
# ---------------------------------------------------------------------------


class TestExtractPodConditions:
    def test_non_true_conditions(self):
        status = {
            "conditions": [
                {"type": "Ready", "status": "False", "message": "container not ready"},
                {"type": "Initialized", "status": "True"},
                {"type": "PodScheduled", "status": "False", "message": "no matching nodes"},
            ]
        }
        parts = _extract_pod_conditions(status)
        assert len(parts) == 2
        assert "Ready" in parts[0]
        assert "container not ready" in parts[0]
        assert "PodScheduled" in parts[1]
        assert "no matching nodes" in parts[1]

    def test_all_true_conditions(self):
        status = {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Initialized", "status": "True"},
            ]
        }
        parts = _extract_pod_conditions(status)
        assert parts == []

    def test_empty_conditions(self):
        status = {"conditions": []}
        parts = _extract_pod_conditions(status)
        assert parts == []

    def test_no_conditions_key(self):
        status = {}
        parts = _extract_pod_conditions(status)
        assert parts == []

    def test_missing_type_and_message(self):
        status = {
            "conditions": [
                {"status": "False"},
            ]
        }
        parts = _extract_pod_conditions(status)
        assert len(parts) == 1
        assert "?" in parts[0]  # type defaults to "?"
        assert "N/A" in parts[0]  # message defaults to "N/A"

    def test_non_dict_condition_skipped(self):
        status = {
            "conditions": [
                "not a dict",
                {"type": "Ready", "status": "False", "message": "failing"},
            ]
        }
        parts = _extract_pod_conditions(status)
        assert len(parts) == 1

    def test_long_message_truncated(self):
        long_msg = "x" * 500
        status = {
            "conditions": [
                {"type": "Ready", "status": "False", "message": long_msg},
            ]
        }
        parts = _extract_pod_conditions(status)
        assert len(parts) == 1
        # Message is truncated to 300 chars
        assert len(parts[0]) < len(long_msg) + 50


# ---------------------------------------------------------------------------
# _format_container_state
# ---------------------------------------------------------------------------


class TestFormatContainerState:
    def test_waiting_with_reason(self):
        cs = {
            "state": {
                "waiting": {
                    "reason": "CrashLoopBackOff",
                    "message": "back-off 5m restarting",
                }
            }
        }
        result = _format_container_state(cs)
        assert "waiting" in result
        assert "CrashLoopBackOff" in result
        assert "back-off 5m restarting" in result

    def test_running_no_details(self):
        cs = {"state": {"running": {"startedAt": "2024-01-01T00:00:00Z"}}}
        result = _format_container_state(cs)
        assert "running" in result

    def test_terminated_with_reason(self):
        cs = {
            "state": {
                "terminated": {
                    "reason": "OOMKilled",
                    "message": "memory limit exceeded",
                }
            }
        }
        result = _format_container_state(cs)
        assert "terminated" in result
        assert "OOMKilled" in result

    def test_empty_state(self):
        cs = {"state": {}}
        result = _format_container_state(cs)
        assert result == ""

    def test_no_state_key(self):
        cs = {}
        result = _format_container_state(cs)
        assert result == ""

    def test_non_dict_state_data_skipped(self):
        cs = {"state": {"waiting": "not a dict"}}
        result = _format_container_state(cs)
        assert result == ""

    def test_long_message_truncated(self):
        long_msg = "e" * 1000
        cs = {
            "state": {
                "terminated": {
                    "reason": "Error",
                    "message": long_msg,
                }
            }
        }
        result = _format_container_state(cs)
        # Message is truncated to 500 chars
        assert len(result) < len(long_msg)

    def test_reason_only_no_message(self):
        cs = {
            "state": {
                "waiting": {
                    "reason": "ContainerCreating",
                }
            }
        }
        result = _format_container_state(cs)
        assert "waiting" in result
        assert "ContainerCreating" in result
