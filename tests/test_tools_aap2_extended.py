"""Extended tests for src/tools/aap2.py — pure functions and async handlers."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.tools.aap2 import (
    _dispatch_aap2_action,
    _extract_guid,
    _get_job,
    _get_job_log,
    _parse_event,
    _parse_job_metadata,
    _strip_secrets,
    _trim_k8s_error,
    _validate_job_action,
)

# ---------------------------------------------------------------------------
# _validate_job_action
# ---------------------------------------------------------------------------


class TestValidateJobAction:
    def test_missing_job_id_returns_error(self):
        result = _validate_job_action("get_job", "east", None)
        assert isinstance(result, dict)
        assert "error" in result
        assert "job_id" in result["error"]
        assert "get_job" in result["error"]

    def test_missing_controller_returns_error(self):
        result = _validate_job_action("get_job", "", 123)
        assert isinstance(result, dict)
        assert "error" in result
        assert "controller" in result["error"]

    def test_zero_job_id_returns_error(self):
        result = _validate_job_action("get_job", "east", 0)
        assert isinstance(result, dict)
        assert "error" in result
        assert "job_id" in result["error"]

    @patch("src.tools.aap2.resolve_controller", return_value="east")
    def test_success_returns_cluster_name(self, mock_resolve):
        result = _validate_job_action("get_job", "east", 42)
        assert result == "east"
        mock_resolve.assert_called_once_with("east")

    @patch(
        "src.tools.aap2.resolve_controller",
        side_effect=ValueError("Unknown controller"),
    )
    def test_resolve_controller_raises(self, mock_resolve):
        with pytest.raises(ValueError, match="Unknown controller"):
            _validate_job_action("get_job", "bogus", 42)


# ---------------------------------------------------------------------------
# _extract_guid
# ---------------------------------------------------------------------------


class TestExtractGuid:
    def test_from_extra_vars(self):
        assert _extract_guid({"guid": "abc12"}, "some-job") == "abc12"

    def test_from_job_metadata(self):
        extra = {"job_metadata": {"guid": "xyz99"}}
        assert _extract_guid(extra, "some-job") == "xyz99"

    def test_from_job_name_provision(self):
        name = "RHPDS agd-v2.sovereign-cloud.prod-gm5ld-2-provision-xyz"
        assert _extract_guid({}, name) == "gm5ld"

    def test_from_job_name_destroy(self):
        name = "RHPDS agd-v2.ocp4-cluster.prod-ab3cd-1-destroy-xyz"
        assert _extract_guid({}, name) == "ab3cd"

    def test_from_job_name_stop(self):
        name = "RHPDS agd-v2.item.prod-qq1ww-3-stop-xyz"
        assert _extract_guid({}, name) == "qq1ww"

    def test_from_job_name_start(self):
        name = "RHPDS agd-v2.item.prod-aa2bb-5-start-xyz"
        assert _extract_guid({}, name) == "aa2bb"

    def test_no_guid_returns_unknown(self):
        assert _extract_guid({}, "random-job-name") == "unknown"

    def test_empty_extra_vars(self):
        assert _extract_guid({}, "") == "unknown"

    def test_none_extra_vars(self):
        assert _extract_guid(None, "job-name") == "unknown"

    def test_extra_vars_not_dict(self):
        assert _extract_guid("not-a-dict", "job-name") == "unknown"

    def test_extra_vars_guid_takes_priority_over_job_name(self):
        name = "RHPDS agd-v2.item.prod-gm5ld-2-provision-x"
        assert _extract_guid({"guid": "other"}, name) == "other"


# ---------------------------------------------------------------------------
# _parse_job_metadata
# ---------------------------------------------------------------------------


class TestParseJobMetadata:
    def _make_data(self, **overrides):
        base = {
            "id": 9999,
            "name": "test-job",
            "status": "successful",
            "failed": False,
            "started": "2025-01-01T00:00:00Z",
            "finished": "2025-01-01T00:05:00Z",
            "elapsed": "300.5",
            "launch_type": "manual",
            "job_explanation": "",
            "extra_vars": "{}",
            "summary_fields": {"job_template": {"name": "My Template"}},
        }
        base.update(overrides)
        return base

    def test_basic_fields(self):
        result = _parse_job_metadata(self._make_data(), "east")
        assert result["job_id"] == 9999
        assert result["job_name"] == "test-job"
        assert result["status"] == "successful"
        assert result["failed"] is False
        assert result["duration_seconds"] == 300
        assert result["template_name"] == "My Template"
        assert result["controller"] == "east"

    def test_extra_vars_as_json_string(self):
        ev = json.dumps({"guid": "abc12", "env_type": "ocp4-cluster", "ACTION": "provision"})
        result = _parse_job_metadata(self._make_data(extra_vars=ev), "west")
        assert result["guid"] == "abc12"
        assert result["env_type"] == "ocp4-cluster"
        assert result["action"] == "provision"

    def test_extra_vars_as_dict(self):
        ev = {"guid": "xyz99", "cloud_provider": "ec2"}
        result = _parse_job_metadata(self._make_data(extra_vars=ev), "east")
        assert result["guid"] == "xyz99"
        assert result["cloud_provider"] == "ec2"

    def test_invalid_json_extra_vars(self):
        result = _parse_job_metadata(self._make_data(extra_vars="{bad json}"), "east")
        assert result["guid"] == "unknown"
        assert result["env_type"] == "unknown"

    def test_git_context_from_deployer(self):
        ev = {
            "__meta__": {
                "deployer": {
                    "scm_url": "https://github.com/rhpds/agnosticd.git",
                    "scm_ref": "v1.2.3",
                    "scm_revision": "abc123def",
                }
            }
        }
        result = _parse_job_metadata(self._make_data(extra_vars=ev), "east")
        assert result["git_url"] == "https://github.com/rhpds/agnosticd.git"
        assert result["git_branch"] == "v1.2.3"
        assert result["git_revision"] == "abc123def"

    def test_git_context_scm_branch_fallback(self):
        ev = {
            "__meta__": {
                "deployer": {
                    "scm_url": "https://github.com/example.git",
                    "scm_branch": "main",
                }
            }
        }
        result = _parse_job_metadata(self._make_data(extra_vars=ev), "east")
        assert result["git_branch"] == "main"

    def test_no_deployer_no_git_keys(self):
        result = _parse_job_metadata(self._make_data(), "east")
        assert "git_url" not in result
        assert "git_branch" not in result

    def test_elapsed_none(self):
        result = _parse_job_metadata(self._make_data(elapsed=None), "east")
        assert result["duration_seconds"] is None

    def test_display_name_extraction(self):
        ev = json.dumps({"display_name": "My Demo Environment"})
        result = _parse_job_metadata(self._make_data(extra_vars=ev), "east")
        assert result["display_name"] == "My Demo Environment"

    def test_guid_from_job_name_fallback(self):
        data = self._make_data(
            name="RHPDS agd-v2.ocp4-cluster.prod-ab3cd-1-provision-x",
            extra_vars="{}",
        )
        result = _parse_job_metadata(data, "east")
        assert result["guid"] == "ab3cd"


# ---------------------------------------------------------------------------
# _parse_event
# ---------------------------------------------------------------------------


class TestParseEvent:
    def test_basic_event(self):
        event = {
            "event": "runner_on_ok",
            "task": "Install packages",
            "play": "Configure host",
            "role": "setup",
            "host_name": "bastion.example.com",
            "failed": False,
            "changed": True,
            "stdout": "ok: [bastion]",
            "counter": 5,
            "event_data": {},
        }
        result = _parse_event(event)
        assert result["event"] == "runner_on_ok"
        assert result["task"] == "Install packages"
        assert result["host"] == "bastion.example.com"
        assert result["failed"] is False
        assert result["changed"] is True
        assert result["counter"] == 5

    def test_event_data_as_json_string(self):
        event = {
            "event": "runner_on_failed",
            "task": "test",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": True,
            "changed": False,
            "stdout": "",
            "counter": 1,
            "event_data": json.dumps({"res": {"msg": "Something broke"}}),
        }
        result = _parse_event(event)
        assert result["error_msg"] == "Something broke"

    def test_event_data_as_dict(self):
        event = {
            "event": "runner_on_failed",
            "task": "t",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": True,
            "changed": False,
            "stdout": "",
            "counter": 1,
            "event_data": {"res": {"msg": "DNS lookup failed"}},
        }
        result = _parse_event(event)
        assert result["error_msg"] == "DNS lookup failed"

    def test_module_stderr_fallback(self):
        event = {
            "event": "runner_on_failed",
            "task": "t",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": True,
            "changed": False,
            "stdout": "",
            "counter": 1,
            "event_data": {"res": {"module_stderr": "permission denied"}},
        }
        result = _parse_event(event)
        assert result["error_msg"] == "permission denied"

    def test_invalid_event_data_json(self):
        event = {
            "event": "runner_on_ok",
            "task": "t",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": False,
            "changed": False,
            "stdout": "",
            "counter": 1,
            "event_data": "{invalid json",
        }
        result = _parse_event(event)
        assert result["error_msg"] == ""

    def test_stdout_truncation_non_failed(self):
        long_stdout = "x" * 1000
        event = {
            "event": "runner_on_ok",
            "task": "t",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": False,
            "changed": False,
            "stdout": long_stdout,
            "counter": 1,
            "event_data": {},
        }
        result = _parse_event(event)
        # Non-failed stdout limit is 500
        assert len(result["stdout"]) < len(long_stdout)
        assert "truncated" in result["stdout"]

    def test_stdout_truncation_failed_has_higher_limit(self):
        medium_stdout = "x" * 2000
        event = {
            "event": "runner_on_failed",
            "task": "t",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": True,
            "changed": False,
            "stdout": medium_stdout,
            "counter": 1,
            "event_data": {},
        }
        result = _parse_event(event)
        # Failed limit is 4000, so 2000 chars should not be truncated
        assert result["stdout"] == medium_stdout

    def test_stdout_none_treated_as_empty(self):
        event = {
            "event": "runner_on_ok",
            "task": "t",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": False,
            "changed": False,
            "stdout": None,
            "counter": 1,
            "event_data": {},
        }
        result = _parse_event(event)
        assert result["stdout"] == ""

    def test_no_error_msg_when_res_has_no_msg(self):
        event = {
            "event": "runner_on_ok",
            "task": "t",
            "play": "",
            "role": "",
            "host_name": "",
            "failed": False,
            "changed": False,
            "stdout": "",
            "counter": 1,
            "event_data": {"res": {"rc": 0}},
        }
        result = _parse_event(event)
        assert result["error_msg"] == ""


# ---------------------------------------------------------------------------
# _trim_k8s_error
# ---------------------------------------------------------------------------


class TestTrimK8sError:
    def test_short_message_unchanged(self):
        msg = "simple error"
        assert _trim_k8s_error(msg) == msg

    def test_under_limit_unchanged(self):
        msg = "a" * 7999
        assert _trim_k8s_error(msg) == msg

    def test_pod_status_summary_extracted(self):
        prefix = "Error in task X\n" * 10
        summary_section = "POD STATUS SUMMARY:\nPod is CrashLoopBackOff\nContainer exited with 1\n"
        yaml_dump = "FULL POD INFORMATION\n" + "managedFields: ...\n" * 500
        msg = prefix + summary_section + yaml_dump
        result = _trim_k8s_error(msg, limit=8000)
        assert "POD STATUS SUMMARY:" in result
        assert "CrashLoopBackOff" in result
        assert "managedFields" not in result
        assert "K8s metadata removed" in result

    def test_container_statuses_extracted(self):
        prefix = "Error occurred\n"
        status = "CONTAINER STATUSES:\nReady: false\nState: waiting\n"
        dump = "f:metadata:\n" + "noise: ...\n" * 2000
        msg = prefix + status + dump
        assert len(msg) > 8000  # ensure we exceed the limit
        result = _trim_k8s_error(msg, limit=8000)
        assert "CONTAINER STATUSES:" in result
        assert "f:metadata:" not in result

    def test_yaml_dump_fallback(self):
        useful = "Error: task failed\n" * 50
        dump = "FULL POD INFORMATION (YAML):\n" + "spec:\n  containers:\n" * 500
        msg = useful + dump
        result = _trim_k8s_error(msg, limit=8000)
        assert "Pod YAML trimmed" in result
        assert "K8s metadata removed" in result

    def test_json_dump_fallback(self):
        useful = "Error: check failed\n" * 200
        dump = 'FULL POD INFORMATION (JSON):\n{"kind": "Pod"}\n' + '{"x":"y"}\n' * 500
        msg = useful + dump
        assert len(msg) > 8000  # ensure we exceed the limit
        result = _trim_k8s_error(msg, limit=8000)
        assert "Pod YAML trimmed" in result

    def test_managed_fields_fallback(self):
        useful = "Something failed with the deployment\n" * 50
        dump = "managedFields:\n" + "  - manager: kubectl\n" * 500
        msg = useful + dump
        result = _trim_k8s_error(msg, limit=8000)
        assert "Pod YAML trimmed" in result

    def test_raw_truncation_as_last_resort(self):
        msg = "x" * 20000
        result = _trim_k8s_error(msg, limit=8000)
        assert len(result) < len(msg)
        assert "trimmed from" in result
        assert "20,000" in result

    def test_custom_limit(self):
        msg = "y" * 500
        result = _trim_k8s_error(msg, limit=100)
        assert "trimmed from" in result

    def test_summary_section_itself_too_long(self):
        prefix = "Err\n"
        summary_section = "POD STATUS SUMMARY:\n" + "line of status\n" * 2000
        msg = prefix + summary_section
        result = _trim_k8s_error(msg, limit=500)
        assert "container logs truncated" in result


# ---------------------------------------------------------------------------
# _strip_secrets
# ---------------------------------------------------------------------------


class TestStripSecrets:
    def test_exact_key_match(self):
        obj = {"ocp4_pull_secret": "s3cret", "name": "visible"}
        result = _strip_secrets(obj)
        assert result["ocp4_pull_secret"] == "[REDACTED]"
        assert result["name"] == "visible"

    def test_pattern_match(self):
        obj = {"my_api_key": "key123", "normal_field": "ok"}
        result = _strip_secrets(obj)
        assert result["my_api_key"] == "[REDACTED]"
        assert result["normal_field"] == "ok"

    def test_nested_dict(self):
        obj = {"outer": {"aws_secret_access_key": "hidden", "value": 42}}
        result = _strip_secrets(obj)
        assert result["outer"]["aws_secret_access_key"] == "[REDACTED]"
        assert result["outer"]["value"] == 42

    def test_list_of_dicts(self):
        obj = [{"password": "pw1"}, {"name": "safe"}]
        result = _strip_secrets(obj)
        assert result[0]["password"] == "[REDACTED]"
        assert result[1]["name"] == "safe"

    def test_non_dict_passthrough(self):
        assert _strip_secrets("hello") == "hello"
        assert _strip_secrets(42) == 42
        assert _strip_secrets(None) is None

    def test_empty_dict(self):
        assert _strip_secrets({}) == {}

    def test_deeply_nested(self):
        obj = {"a": {"b": {"c": {"client_secret": "deep"}}}}
        result = _strip_secrets(obj)
        assert result["a"]["b"]["c"]["client_secret"] == "[REDACTED]"

    def test_case_insensitive_pattern(self):
        obj = {"MY_ACCESS_KEY": "val"}
        result = _strip_secrets(obj)
        assert result["MY_ACCESS_KEY"] == "[REDACTED]"

    def test_mixed_list(self):
        obj = [1, "two", {"token": "t"}, [{"secret_key": "sk"}]]
        result = _strip_secrets(obj)
        assert result[0] == 1
        assert result[1] == "two"
        assert result[2]["token"] == "[REDACTED]"
        assert result[3][0]["secret_key"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# _get_job (async)
# ---------------------------------------------------------------------------


class TestGetJob:
    @pytest.mark.asyncio
    async def test_calls_api_get_and_parses(self):
        mock_data = {
            "id": 100,
            "name": "test-job-name",
            "status": "failed",
            "failed": True,
            "started": "2025-06-01T10:00:00Z",
            "finished": "2025-06-01T10:05:00Z",
            "elapsed": "300",
            "launch_type": "workflow",
            "job_explanation": "",
            "extra_vars": json.dumps({"guid": "ab1cd"}),
            "summary_fields": {"job_template": {"name": "Deploy Template"}},
        }
        with patch("src.tools.aap2.api_get", new_callable=AsyncMock, return_value=mock_data):
            result = await _get_job("east", 100)

        assert result["job_id"] == 100
        assert result["guid"] == "ab1cd"
        assert result["controller"] == "east"
        assert result["template_name"] == "Deploy Template"

    @pytest.mark.asyncio
    async def test_api_get_called_with_correct_path(self):
        mock_data = {
            "id": 55,
            "name": "",
            "status": "successful",
            "failed": False,
            "started": "",
            "finished": "",
            "elapsed": None,
            "launch_type": "",
            "job_explanation": "",
            "extra_vars": "{}",
            "summary_fields": {},
        }
        mock_api = AsyncMock(return_value=mock_data)
        with patch("src.tools.aap2.api_get", mock_api):
            await _get_job("west", 55)

        mock_api.assert_called_once_with("west", "/api/v2/jobs/55/")


# ---------------------------------------------------------------------------
# _get_job_log (async)
# ---------------------------------------------------------------------------


class TestGetJobLog:
    @pytest.mark.asyncio
    async def test_returns_metadata_plus_log(self):
        mock_job_data = {
            "id": 200,
            "name": "log-job",
            "status": "successful",
            "failed": False,
            "started": "2025-01-01T00:00:00Z",
            "finished": "2025-01-01T00:01:00Z",
            "elapsed": "60",
            "launch_type": "manual",
            "job_explanation": "",
            "extra_vars": "{}",
            "summary_fields": {"job_template": {"name": "T"}},
        }
        raw_stdout = "PLAY [all] ***\nTASK [test] ***\nok: [host1]\nPLAY RECAP ***\n"

        with (
            patch("src.tools.aap2.api_get", new_callable=AsyncMock, return_value=mock_job_data),
            patch(
                "src.tools.aap2.api_get_text",
                new_callable=AsyncMock,
                return_value=raw_stdout,
            ),
        ):
            result = await _get_job_log("east", 200)

        assert result["job_id"] == 200
        assert "log" in result
        assert result["log_original_size"] == len(raw_stdout)
        assert result["log_trimmed_size"] == len(result["log"])

    @pytest.mark.asyncio
    async def test_api_get_text_called_with_format_param(self):
        mock_job_data = {
            "id": 300,
            "name": "",
            "status": "successful",
            "failed": False,
            "started": "",
            "finished": "",
            "elapsed": None,
            "launch_type": "",
            "job_explanation": "",
            "extra_vars": "{}",
            "summary_fields": {},
        }
        mock_text = AsyncMock(return_value="some log text")
        with (
            patch("src.tools.aap2.api_get", new_callable=AsyncMock, return_value=mock_job_data),
            patch("src.tools.aap2.api_get_text", mock_text),
        ):
            await _get_job_log("west", 300)

        mock_text.assert_called_once_with("west", "/api/v2/jobs/300/stdout/", {"format": "txt"})

    @pytest.mark.asyncio
    async def test_non_ansible_log_truncated(self):
        mock_job_data = {
            "id": 400,
            "name": "",
            "status": "successful",
            "failed": False,
            "started": "",
            "finished": "",
            "elapsed": None,
            "launch_type": "",
            "job_explanation": "",
            "extra_vars": "{}",
            "summary_fields": {},
        }
        huge_text = "x" * 200_000

        with (
            patch("src.tools.aap2.api_get", new_callable=AsyncMock, return_value=mock_job_data),
            patch("src.tools.aap2.api_get_text", new_callable=AsyncMock, return_value=huge_text),
        ):
            result = await _get_job_log("east", 400)

        assert result["log_original_size"] == 200_000
        assert result["log_trimmed_size"] <= 100_000


# ---------------------------------------------------------------------------
# _dispatch_aap2_action (async)
# ---------------------------------------------------------------------------


class TestDispatchAap2Action:
    @pytest.mark.asyncio
    @patch("src.tools.aap2.resolve_controller", return_value="east")
    @patch("src.tools.aap2.api_get", new_callable=AsyncMock)
    async def test_get_job_dispatches(self, mock_api_get, mock_resolve):
        mock_api_get.return_value = {
            "id": 10,
            "name": "j",
            "status": "successful",
            "failed": False,
            "started": "",
            "finished": "",
            "elapsed": None,
            "launch_type": "",
            "job_explanation": "",
            "extra_vars": "{}",
            "summary_fields": {},
        }
        result = await _dispatch_aap2_action(
            "get_job", "east", 10, False, False, "", "", "", "", 50
        )
        assert result["job_id"] == 10
        mock_api_get.assert_called_once_with("east", "/api/v2/jobs/10/")

    @pytest.mark.asyncio
    async def test_get_job_no_job_id(self):
        result = await _dispatch_aap2_action(
            "get_job", "east", None, False, False, "", "", "", "", 50
        )
        assert "error" in result
        assert "job_id" in result["error"]

    @pytest.mark.asyncio
    async def test_get_job_no_controller(self):
        result = await _dispatch_aap2_action("get_job", "", 10, False, False, "", "", "", "", 50)
        assert "error" in result
        assert "controller" in result["error"]

    @pytest.mark.asyncio
    @patch("src.tools.aap2.resolve_controller", return_value="west")
    @patch("src.tools.aap2.api_get", new_callable=AsyncMock)
    @patch("src.tools.aap2.api_get_text", new_callable=AsyncMock, return_value="log text")
    async def test_get_job_log_dispatches(self, mock_text, mock_api, mock_resolve):
        mock_api.return_value = {
            "id": 20,
            "name": "",
            "status": "successful",
            "failed": False,
            "started": "",
            "finished": "",
            "elapsed": None,
            "launch_type": "",
            "job_explanation": "",
            "extra_vars": "{}",
            "summary_fields": {},
        }
        result = await _dispatch_aap2_action(
            "get_job_log", "west", 20, False, False, "", "", "", "", 50
        )
        assert "log" in result
        assert result["job_id"] == 20

    @pytest.mark.asyncio
    async def test_get_job_log_no_job_id(self):
        result = await _dispatch_aap2_action(
            "get_job_log", "east", None, False, False, "", "", "", "", 50
        )
        assert "error" in result

    @pytest.mark.asyncio
    @patch("src.tools.aap2.resolve_controller", return_value="east")
    @patch("src.tools.aap2.api_paginate", new_callable=AsyncMock, return_value=[])
    async def test_get_job_events_dispatches(self, mock_paginate, mock_resolve):
        result = await _dispatch_aap2_action(
            "get_job_events", "east", 30, True, False, "", "", "", "", 50
        )
        assert result["job_id"] == 30
        assert result["event_count"] == 0
        assert result["filters"]["failed_only"] is True

    @pytest.mark.asyncio
    async def test_get_job_events_no_controller(self):
        result = await _dispatch_aap2_action(
            "get_job_events", "", 30, False, False, "", "", "", "", 50
        )
        assert "error" in result

    @pytest.mark.asyncio
    @patch("src.tools.aap2.resolve_controller", return_value="east")
    @patch("src.tools.aap2.api_paginate", new_callable=AsyncMock, return_value=[])
    async def test_find_jobs_single_controller(self, mock_paginate, mock_resolve):
        result = await _dispatch_aap2_action(
            "find_jobs", "east", None, False, False, "failed", "", "", "deploy", 50
        )
        assert "jobs" in result
        assert result["total"] == 0

    @pytest.mark.asyncio
    @patch(
        "src.tools.aap2.get_configured_controllers",
        return_value=["east", "west"],
    )
    @patch("src.tools.aap2.api_paginate", new_callable=AsyncMock, return_value=[])
    async def test_find_jobs_all_controllers(self, mock_paginate, mock_controllers):
        result = await _dispatch_aap2_action(
            "find_jobs", "", None, False, False, "", "", "", "", 50
        )
        assert "jobs" in result
        assert mock_paginate.call_count == 2

    @pytest.mark.asyncio
    @patch("src.tools.aap2.get_configured_controllers", return_value=[])
    async def test_find_jobs_no_controllers_configured(self, mock_controllers):
        result = await _dispatch_aap2_action(
            "find_jobs", "", None, False, False, "", "", "", "", 50
        )
        assert "error" in result
        assert "No AAP2 controllers configured" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await _dispatch_aap2_action(
            "nonexistent_action", "", None, False, False, "", "", "", "", 50
        )
        assert "error" in result
        assert "Unknown action" in result["error"]
        assert "nonexistent_action" in result["error"]
