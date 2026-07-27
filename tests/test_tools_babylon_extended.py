"""Extended tests for helpers in src/tools/babylon.py.

Covers functions NOT tested in test_babylon_helpers.py:
  _extract_anarchy_state, _extract_provision_data, _resolve_cluster_target,
  _fetch_resource_claims, _strip_secrets, _normalize_ci_name,
  _extract_instance_info, _filter_job_vars
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.tools.babylon import (
    _extract_anarchy_state,
    _extract_instance_info,
    _extract_provision_data,
    _fetch_resource_claims,
    _filter_job_vars,
    _normalize_ci_name,
    _resolve_cluster_target,
    _strip_secrets,
)

# ---------------------------------------------------------------------------
# _extract_anarchy_state
# ---------------------------------------------------------------------------


class TestExtractAnarchyState:
    def test_returns_empty_when_kind_is_not_anarchy_subject(self):
        state = {"kind": "ConfigMap", "spec": {"vars": {}}}
        assert _extract_anarchy_state(state) == {}

    def test_returns_empty_when_kind_is_missing(self):
        state = {"spec": {"vars": {}}}
        assert _extract_anarchy_state(state) == {}

    def test_returns_empty_for_empty_dict(self):
        assert _extract_anarchy_state({}) == {}

    def test_extracts_basic_fields(self):
        state = {
            "kind": "AnarchySubject",
            "spec": {
                "vars": {
                    "current_state": "started",
                    "desired_state": "started",
                    "job_vars": {
                        "guid": "abc12",
                        "cloud_provider": "ec2",
                    },
                },
            },
            "status": {},
        }
        result = _extract_anarchy_state(state)
        assert result["current_state"] == "started"
        assert result["desired_state"] == "started"
        assert result["guid"] == "abc12"
        assert result["cloud_provider"] == "ec2"
        assert "tower_jobs" not in result

    def test_extracts_tower_jobs(self):
        state = {
            "kind": "AnarchySubject",
            "spec": {
                "vars": {
                    "current_state": "provisioning",
                    "desired_state": "started",
                    "job_vars": {"guid": "xyz99", "cloud_provider": "azure"},
                },
            },
            "status": {
                "towerJobs": {
                    "provision": {
                        "towerHost": "east.controller.example.com",
                        "deployerJob": "55555",
                        "jobStatus": "successful",
                        "completeTimestamp": "2024-06-01T12:00:00Z",
                    },
                },
            },
        }
        result = _extract_anarchy_state(state)
        assert "tower_jobs" in result
        tj = result["tower_jobs"]
        assert "provision" in tj
        assert tj["provision"]["controller"] == "east.controller.example.com"
        assert tj["provision"]["job_id"] == "55555"
        assert tj["provision"]["status"] == "successful"
        assert tj["provision"]["completed"] == "2024-06-01T12:00:00Z"

    def test_handles_missing_spec_vars(self):
        state = {"kind": "AnarchySubject", "spec": {}, "status": {}}
        result = _extract_anarchy_state(state)
        assert result["current_state"] == ""
        assert result["desired_state"] == ""
        assert result["guid"] == ""
        assert result["cloud_provider"] == ""

    def test_handles_missing_job_vars(self):
        state = {
            "kind": "AnarchySubject",
            "spec": {
                "vars": {
                    "current_state": "started",
                    "desired_state": "started",
                },
            },
            "status": {},
        }
        result = _extract_anarchy_state(state)
        assert result["guid"] == ""
        assert result["cloud_provider"] == ""

    def test_empty_tower_jobs_not_included(self):
        state = {
            "kind": "AnarchySubject",
            "spec": {"vars": {"job_vars": {}}},
            "status": {"towerJobs": {}},
        }
        result = _extract_anarchy_state(state)
        assert "tower_jobs" not in result

    def test_multiple_tower_job_actions(self):
        state = {
            "kind": "AnarchySubject",
            "spec": {"vars": {"job_vars": {"guid": "multi1"}}},
            "status": {
                "towerJobs": {
                    "provision": {"towerHost": "ctrl1", "deployerJob": "100"},
                    "stop": {"towerHost": "ctrl1", "deployerJob": "101"},
                    "start": {"towerHost": "ctrl1", "deployerJob": "102"},
                    "destroy": {"towerHost": "ctrl2", "deployerJob": "103"},
                },
            },
        }
        result = _extract_anarchy_state(state)
        tj = result["tower_jobs"]
        assert len(tj) == 4
        assert set(tj.keys()) == {"provision", "stop", "start", "destroy"}


# ---------------------------------------------------------------------------
# _extract_provision_data
# ---------------------------------------------------------------------------


class TestExtractProvisionData:
    def test_basic_extraction(self):
        summary = {
            "state": "started",
            "agnosticv": {"repo": "agnosticd-v2", "path": "configs/foo"},
            "runtime_default": "8h",
            "runtime_maximum": "24h",
        }
        result = _extract_provision_data(summary)
        assert result["state"] == "started"
        assert result["agnosticv"] == {"repo": "agnosticd-v2", "path": "configs/foo"}
        assert result["runtime_default"] == "8h"
        assert result["runtime_maximum"] == "24h"
        assert "provision_data" not in result

    def test_empty_provision_data_returns_no_key(self):
        summary = {"state": "started", "provision_data": {}}
        result = _extract_provision_data(summary)
        assert "provision_data" not in result

    def test_none_provision_data_returns_no_key(self):
        summary = {"state": "started", "provision_data": None}
        result = _extract_provision_data(summary)
        assert "provision_data" not in result

    def test_extracts_cloud_provider_and_guid(self):
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "ec2",
                "guid": "abc12",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["cloud_provider"] == "ec2"
        assert pd["guid"] == "abc12"

    def test_aws_sandbox_fields(self):
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "ec2",
                "guid": "def34",
                "aws_sandbox_account_id": "123456789012",
                "aws_default_region": "us-east-1",
                "aws_route53_domain": "sandbox123.example.com.",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["sandbox_account_id"] == "123456789012"
        assert pd["aws_region"] == "us-east-1"
        assert pd["sandbox_name"] == "sandbox123"

    def test_aws_region_fallback(self):
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "ec2",
                "guid": "reg01",
                "aws_sandbox_account_id": "111111111111",
                "aws_region": "eu-west-1",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["aws_region"] == "eu-west-1"

    def test_aws_route53_domain_without_trailing_dot(self):
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "ec2",
                "guid": "dom01",
                "aws_sandbox_account_id": "222222222222",
                "aws_route53_domain": "sandbox456.example.com",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["sandbox_name"] == "sandbox456"

    def test_cnv_specific_fields(self):
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "openshift_cnv",
                "guid": "cnv01",
                "sandbox_openshift_cluster": "ocpv07.infra.demo.redhat.com",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["cloud_provider"] == "openshift_cnv"
        assert pd["cnv_cluster"] == "ocpv07.infra.demo.redhat.com"

    def test_cnv_fallback_to_ingress_domain(self):
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "openshift_cnv",
                "guid": "cnv02",
                "openshift_cluster_ingress_domain": "apps.ocpv08.infra.demo.redhat.com",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["cnv_cluster"] == "apps.ocpv08.infra.demo.redhat.com"

    def test_cnv_no_cluster_field(self):
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "openshift_cnv",
                "guid": "cnv03",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["cnv_cluster"] == ""

    def test_cnv_with_aws_sandbox(self):
        """CNV items can also have AWS sandbox accounts for spoke clusters."""
        summary = {
            "state": "started",
            "provision_data": {
                "cloud_provider": "openshift_cnv",
                "guid": "cnvaws",
                "aws_sandbox_account_id": "333333333333",
                "aws_default_region": "us-west-2",
                "sandbox_openshift_cluster": "ocpv05.infra.demo.redhat.com",
            },
        }
        result = _extract_provision_data(summary)
        pd = result["provision_data"]
        assert pd["cloud_provider"] == "openshift_cnv"
        assert pd["sandbox_account_id"] == "333333333333"
        assert pd["aws_region"] == "us-west-2"
        assert pd["cnv_cluster"] == "ocpv05.infra.demo.redhat.com"

    def test_missing_state_defaults_to_unknown(self):
        summary = {"provision_data": {"cloud_provider": "ec2", "guid": "g1"}}
        result = _extract_provision_data(summary)
        assert result["state"] == "unknown"

    def test_empty_summary(self):
        result = _extract_provision_data({})
        assert result["state"] == "unknown"
        assert result["agnosticv"] == {}
        assert result["runtime_default"] == ""
        assert result["runtime_maximum"] == ""


# ---------------------------------------------------------------------------
# _resolve_cluster_target
# ---------------------------------------------------------------------------


class TestResolveClusterTarget:
    def test_returns_cluster_if_provided(self):
        assert _resolve_cluster_target("east", "") == "east"

    def test_returns_cluster_even_with_sandbox_comment(self):
        assert _resolve_cluster_target("west", "some-comment") == "west"

    @patch("src.tools.babylon.resolve_cluster_from_comment", return_value="east")
    def test_resolves_from_sandbox_comment(self, mock_resolve):
        result = _resolve_cluster_target("", "sandbox-api https://console.apps.east.example.com")
        assert result == "east"
        mock_resolve.assert_called_once_with("sandbox-api https://console.apps.east.example.com")

    def test_returns_empty_when_neither_provided(self):
        assert _resolve_cluster_target("", "") == ""


# ---------------------------------------------------------------------------
# _fetch_resource_claims
# ---------------------------------------------------------------------------


class TestFetchResourceClaims:
    @pytest.mark.asyncio
    async def test_fetches_from_dict_keys(self):
        mock_rc = {
            "status": {
                "summary": {"state": "started"},
                "healthy": True,
                "ready": True,
                "resources": [
                    {
                        "name": "comp1",
                        "healthy": True,
                        "ready": True,
                        "reference": {"name": "as-abc12", "namespace": "babylon-anarchy-0"},
                        "state": {
                            "kind": "AnarchySubject",
                            "spec": {
                                "vars": {
                                    "current_state": "started",
                                    "desired_state": "started",
                                    "job_vars": {"guid": "abc12", "cloud_provider": "ec2"},
                                },
                            },
                            "status": {},
                        },
                    },
                ],
            },
        }
        with patch("src.tools.babylon.k8s_get_resource", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_rc
            result = await _fetch_resource_claims("east", "user-ns", {"rc-one": {}, "rc-two": {}})
        assert len(result) == 2
        assert mock_get.call_count == 2

    @pytest.mark.asyncio
    async def test_fetches_from_list(self):
        mock_rc = {
            "status": {
                "summary": {"state": "started"},
                "healthy": True,
                "ready": True,
                "resources": [],
            },
        }
        with patch("src.tools.babylon.k8s_get_resource", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_rc
            result = await _fetch_resource_claims("east", "user-ns", ["rc-alpha"])
        assert len(result) == 1
        assert result[0]["name"] == "rc-alpha"
        assert result[0]["state"] == "started"

    @pytest.mark.asyncio
    async def test_handles_fetch_error_per_rc(self):
        with patch("src.tools.babylon.k8s_get_resource", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("404 Not Found")
            result = await _fetch_resource_claims("east", "user-ns", ["missing-rc"])
        assert len(result) == 1
        assert result[0]["name"] == "missing-rc"
        assert "error" in result[0]
        assert "404" in result[0]["error"]

    @pytest.mark.asyncio
    async def test_mixed_success_and_error(self):
        good_rc = {
            "status": {
                "summary": {"state": "started"},
                "healthy": True,
                "ready": True,
                "resources": [],
            },
        }
        with patch("src.tools.babylon.k8s_get_resource", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [good_rc, Exception("timeout")]
            result = await _fetch_resource_claims("east", "user-ns", ["rc-ok", "rc-bad"])
        assert len(result) == 2
        assert result[0]["state"] == "started"
        assert "error" not in result[0]
        assert "error" in result[1]

    @pytest.mark.asyncio
    async def test_extracts_resource_components(self):
        mock_rc = {
            "status": {
                "summary": {"state": "started"},
                "healthy": True,
                "ready": True,
                "resources": [
                    {
                        "name": "sandbox",
                        "healthy": True,
                        "ready": True,
                        "reference": {"name": "as-sandbox", "namespace": "babylon-anarchy-0"},
                        "state": {
                            "kind": "AnarchySubject",
                            "spec": {"vars": {"job_vars": {"guid": "snd01"}}},
                            "status": {},
                        },
                    },
                    {
                        "name": "lab",
                        "healthy": False,
                        "ready": False,
                        "reference": {"name": "as-lab", "namespace": "babylon-anarchy-0"},
                        "state": {
                            "kind": "AnarchySubject",
                            "spec": {
                                "vars": {
                                    "current_state": "provision-failed",
                                    "job_vars": {
                                        "guid": "lab01",
                                        "cloud_provider": "openshift_cnv",
                                    },
                                },
                            },
                            "status": {
                                "towerJobs": {
                                    "provision": {
                                        "towerHost": "west.ctrl",
                                        "deployerJob": "9999",
                                        "jobStatus": "failed",
                                    },
                                },
                            },
                        },
                    },
                ],
            },
        }
        with patch("src.tools.babylon.k8s_get_resource", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_rc
            result = await _fetch_resource_claims("east", "user-ns", ["multi-rc"])
        assert len(result) == 1
        rc = result[0]
        assert len(rc["resources"]) == 2
        sandbox_comp = rc["resources"][0]
        assert sandbox_comp["name"] == "sandbox"
        assert sandbox_comp["anarchy_subject"] == "as-sandbox"
        lab_comp = rc["resources"][1]
        assert lab_comp["name"] == "lab"
        assert lab_comp["current_state"] == "provision-failed"
        assert lab_comp["tower_jobs"]["provision"]["controller"] == "west.ctrl"
        assert lab_comp["tower_jobs"]["provision"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_empty_rc_names(self):
        with patch("src.tools.babylon.k8s_get_resource", new_callable=AsyncMock) as mock_get:
            result = await _fetch_resource_claims("east", "user-ns", [])
        assert result == []
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# _strip_secrets
# ---------------------------------------------------------------------------


class TestStripSecrets:
    def test_strips_exact_match_keys(self):
        obj = {
            "ocp4_pull_secret": "my-pull-secret-value",
            "aws_access_key_id": "AKIA_FAKE_TEST_KEY_",
            "safe_key": "visible",
        }
        result = _strip_secrets(obj)
        assert result["ocp4_pull_secret"] == "[REDACTED]"
        assert result["aws_access_key_id"] == "[REDACTED]"
        assert result["safe_key"] == "visible"

    def test_strips_pattern_match_keys(self):
        obj = {
            "my_custom_password": "secret123",
            "some_access_key_value": "AKIA...",
            "api_key_for_service": "key-123",
            "client_secret_id": "cs-456",
            "normal_field": "ok",
        }
        result = _strip_secrets(obj)
        assert result["my_custom_password"] == "[REDACTED]"
        assert result["some_access_key_value"] == "[REDACTED]"
        assert result["api_key_for_service"] == "[REDACTED]"
        assert result["client_secret_id"] == "[REDACTED]"
        assert result["normal_field"] == "ok"

    def test_handles_nested_dicts(self):
        obj = {
            "level1": {
                "level2": {
                    "ocp4_token": "token-val",
                    "env_type": "ocp4-workshop",
                },
            },
        }
        result = _strip_secrets(obj)
        assert result["level1"]["level2"]["ocp4_token"] == "[REDACTED]"
        assert result["level1"]["level2"]["env_type"] == "ocp4-workshop"

    def test_handles_lists(self):
        obj = [
            {"aws_secret_access_key": "secret", "name": "item1"},
            {"bastion_ssh_password": "pass", "name": "item2"},
        ]
        result = _strip_secrets(obj)
        assert result[0]["aws_secret_access_key"] == "[REDACTED]"
        assert result[0]["name"] == "item1"
        assert result[1]["bastion_ssh_password"] == "[REDACTED]"
        assert result[1]["name"] == "item2"

    def test_handles_nested_lists_in_dicts(self):
        obj = {
            "instances": [
                {"name": "bastion", "ssh_pass_override": "hidden"},
                {"name": "worker", "flavor": "m5.xlarge"},
            ],
        }
        result = _strip_secrets(obj)
        assert result["instances"][0]["ssh_pass_override"] == "[REDACTED]"
        assert result["instances"][1]["flavor"] == "m5.xlarge"

    def test_passes_through_non_dict_non_list(self):
        assert _strip_secrets("hello") == "hello"
        assert _strip_secrets(42) == 42
        assert _strip_secrets(None) is None
        assert _strip_secrets(True) is True

    def test_empty_dict(self):
        assert _strip_secrets({}) == {}

    def test_empty_list(self):
        assert _strip_secrets([]) == []

    def test_pattern_case_insensitive(self):
        obj = {
            "MY_PASSWORD_FIELD": "secret",
            "Api_Key_Upper": "key",
        }
        result = _strip_secrets(obj)
        assert result["MY_PASSWORD_FIELD"] == "[REDACTED]"
        assert result["Api_Key_Upper"] == "[REDACTED]"

    def test_activationkey_pattern(self):
        obj = {
            "satellite_activationkey": "ak-123",
            "set_repositories_satellite_activationkey": "ak-456",
            "some_activationkey_field": "ak-789",
        }
        result = _strip_secrets(obj)
        assert result["satellite_activationkey"] == "[REDACTED]"
        assert result["set_repositories_satellite_activationkey"] == "[REDACTED]"
        assert result["some_activationkey_field"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# _normalize_ci_name
# ---------------------------------------------------------------------------


class TestNormalizeCiName:
    def test_replaces_slash_with_dot(self):
        assert _normalize_ci_name("openshift_cnv/ocp-virt-lab") == "openshift-cnv.ocp-virt-lab"

    def test_replaces_underscore_with_dash(self):
        assert _normalize_ci_name("open_environment_aws") == "open-environment-aws"

    def test_lowercases(self):
        assert _normalize_ci_name("OCP4/Workshop") == "ocp4.workshop"

    def test_combined_transformations(self):
        assert _normalize_ci_name("My_Env/Lab_Name") == "my-env.lab-name"

    def test_already_normalized(self):
        assert _normalize_ci_name("ocp4-workshop.prod") == "ocp4-workshop.prod"

    def test_empty_string(self):
        assert _normalize_ci_name("") == ""

    def test_multiple_slashes(self):
        assert _normalize_ci_name("a/b/c") == "a.b.c"

    def test_multiple_underscores(self):
        assert _normalize_ci_name("a_b_c") == "a-b-c"


# ---------------------------------------------------------------------------
# _extract_instance_info
# ---------------------------------------------------------------------------


class TestExtractInstanceInfo:
    def test_pattern_a_instances_list_with_flavor_ec2_dict(self):
        """flavor_ec2 must be a dict with an 'ec2' key for extraction."""
        definition = {
            "instances": [
                {
                    "name": "bastion",
                    "count": 1,
                    "image": "ami-12345",
                    "flavor_ec2": {"ec2": "t3.medium"},
                },
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["purpose"] == "bastion"
        assert result[0]["instance_type"] == "t3.medium"
        assert result[0]["count"] == 1
        assert result[0]["cloud"] == "aws"

    def test_pattern_a_string_flavor_ec2_is_ignored(self):
        """A plain string for flavor_ec2 is not a dict, so nothing is extracted."""
        definition = {
            "instances": [
                {"name": "node", "flavor_ec2": "t3.medium"},
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 0

    def test_pattern_a_flavor_dict(self):
        definition = {
            "instances": [
                {
                    "name": "worker",
                    "count": 3,
                    "flavor": {"ec2": "m5.xlarge", "azure": "Standard_D4s_v3"},
                },
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["instance_type"] == "m5.xlarge"
        assert result[0]["cloud"] == "aws"

    def test_pattern_a_cnv_instance_with_cores_and_memory(self):
        definition = {
            "instances": [
                {
                    "name": "vm",
                    "count": 2,
                    "cores": 4,
                    "memory": "16Gi",
                },
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["cores"] == 4
        assert result[0]["memory"] == "16Gi"
        assert result[0]["cloud"] == "cnv"
        assert result[0]["count"] == 2

    def test_pattern_b_role_variables(self):
        definition = {
            "bastion_instance_type": "t3.small",
            "bastion_instance_count": 1,
            "worker_instance_type": "m5.2xlarge",
            "num_workers": 3,
        }
        result = _extract_instance_info(definition)
        assert len(result) == 2
        bastion = next(r for r in result if r["purpose"] == "bastion")
        worker = next(r for r in result if r["purpose"] == "worker")
        assert bastion["instance_type"] == "t3.small"
        assert bastion["count"] == 1
        assert worker["instance_type"] == "m5.2xlarge"
        assert worker["count"] == 3

    def test_pattern_b_skips_jinja_templates(self):
        definition = {
            "bastion_instance_type": "{{ bastion_type }}",
            "worker_instance_type": "m5.xlarge",
        }
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["purpose"] == "worker"

    def test_pattern_e_rosa_cluster(self):
        definition = {
            "rosa_deploy": True,
            "rosa_compute_machine_type": "m5.2xlarge",
            "rosa_compute_replicas": 3,
        }
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["purpose"] == "rosa_worker"
        assert result[0]["instance_type"] == "m5.2xlarge"
        assert result[0]["count"] == 3
        assert result[0]["cloud"] == "aws"

    def test_pattern_e_rosa_defaults(self):
        definition = {"rosa_deploy": True}
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["instance_type"] == "m5.xlarge"
        assert result[0]["count"] == 2

    def test_pattern_e_rosa_not_deployed(self):
        definition = {"rosa_deploy": False}
        result = _extract_instance_info(definition)
        assert len(result) == 0

    def test_pattern_f_machineset_groups(self):
        definition = {
            "ocp4_workload_machinesets_machineset_groups": [
                {
                    "name": "gpu",
                    "instance_type": "g4dn.xlarge",
                    "total_replicas": 2,
                },
                {
                    "name": "infra",
                    "instance_type": "m5.4xlarge",
                    "replicas": 3,
                },
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 2
        gpu = next(r for r in result if r["purpose"] == "machineset_gpu")
        infra = next(r for r in result if r["purpose"] == "machineset_infra")
        assert gpu["instance_type"] == "g4dn.xlarge"
        assert gpu["count"] == 2
        assert infra["instance_type"] == "m5.4xlarge"
        assert infra["count"] == 3

    def test_pattern_f_machineset_group_name_fallback(self):
        definition = {
            "ocp4_workload_machinesets_machineset_groups": [
                {
                    "group_name": "workers",
                    "instance_type": "m5.xlarge",
                },
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["purpose"] == "machineset_workers"
        assert result[0]["count"] == 1

    def test_pattern_f_skips_non_dict_entries(self):
        definition = {
            "ocp4_workload_machinesets_machineset_groups": [
                "not a dict",
                {"name": "valid", "instance_type": "m5.xlarge"},
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 1

    def test_pattern_f_skips_entries_without_instance_type(self):
        definition = {
            "ocp4_workload_machinesets_machineset_groups": [
                {"name": "empty"},
            ],
        }
        result = _extract_instance_info(definition)
        assert len(result) == 0

    def test_empty_definition(self):
        assert _extract_instance_info({}) == []

    def test_instances_not_a_list(self):
        definition = {"instances": "not-a-list"}
        result = _extract_instance_info(definition)
        assert result == []

    def test_combined_patterns(self):
        """Instances list (flavor dict) + role variables + ROSA all contribute."""
        definition = {
            "instances": [
                {"name": "bastion", "count": 1, "flavor": {"ec2": "t3.micro"}},
            ],
            "master_instance_type": "m5.xlarge",
            "num_masters": 3,
            "rosa_deploy": True,
            "rosa_compute_machine_type": "m5.2xlarge",
            "rosa_compute_replicas": 2,
        }
        result = _extract_instance_info(definition)
        purposes = [r["purpose"] for r in result]
        assert "bastion" in purposes
        assert "master" in purposes
        assert "rosa_worker" in purposes

    def test_pattern_b_default_count(self):
        definition = {"bastion_instance_type": "t3.small"}
        result = _extract_instance_info(definition)
        assert len(result) == 1
        assert result[0]["count"] == 1

    def test_pattern_a_default_count(self):
        definition = {
            "instances": [
                {"name": "node", "flavor": {"ec2": "m5.large"}},
            ],
        }
        result = _extract_instance_info(definition)
        assert result[0]["count"] == 1


# ---------------------------------------------------------------------------
# _filter_job_vars
# ---------------------------------------------------------------------------


class TestFilterJobVars:
    def test_filters_to_relevant_keys(self):
        jv = {
            "guid": "abc12",
            "cloud_provider": "ec2",
            "aws_region": "us-east-1",
            "env_type": "ocp4-workshop",
            "some_random_key": "ignored",
            "display_name": "also ignored",
        }
        result = _filter_job_vars(jv)
        assert "guid" in result
        assert "cloud_provider" in result
        assert "aws_region" in result
        assert "env_type" in result
        assert "some_random_key" not in result
        assert "display_name" not in result

    def test_excludes_secrets_exact_match(self):
        jv = {
            "guid": "abc12",
            "ocp4_pull_secret": "secret-value",
            "aws_access_key_id": "AKIA_FAKE_TEST_KEY_",
        }
        result = _filter_job_vars(jv)
        assert "guid" in result
        assert "ocp4_pull_secret" not in result
        assert "aws_access_key_id" not in result

    def test_excludes_secrets_pattern_match(self):
        jv = {
            "guid": "abc12",
            "my_password_field": "secret",
            "some_token_value": "tok-123",
        }
        result = _filter_job_vars(jv)
        assert "guid" in result
        assert "my_password_field" not in result
        assert "some_token_value" not in result

    def test_includes_instance_related_keys(self):
        jv = {
            "bastion_instance_type": "t3.small",
            "worker_instance_type": "m5.xlarge",
            "num_workers": 3,
            "master_instance_type": "m5.2xlarge",
            "cluster_size": "large",
        }
        result = _filter_job_vars(jv)
        assert "bastion_instance_type" in result
        assert "worker_instance_type" in result
        assert "num_workers" in result
        assert "master_instance_type" in result
        assert "cluster_size" in result

    def test_includes_sandbox_and_platform_keys(self):
        jv = {
            "sandbox_name": "sandbox123",
            "platform": "openshift",
            "open_environment": True,
        }
        result = _filter_job_vars(jv)
        assert "sandbox_name" in result
        assert "platform" in result
        assert "open_environment" in result

    def test_includes_rosa_keys(self):
        jv = {
            "rosa_compute_machine_type": "m5.2xlarge",
            "rosa_compute_replicas": 3,
        }
        result = _filter_job_vars(jv)
        assert "rosa_compute_machine_type" in result
        assert "rosa_compute_replicas" in result

    def test_empty_input(self):
        assert _filter_job_vars({}) == {}

    def test_keyword_matching_is_case_insensitive(self):
        jv = {
            "AWS_REGION": "us-west-2",
            "GUID": "XYZ99",
        }
        result = _filter_job_vars(jv)
        assert "AWS_REGION" in result
        assert "GUID" in result

    def test_preserves_values(self):
        jv = {
            "guid": "abc12",
            "num_workers": 3,
            "open_environment": True,
            "region": "us-east-1",
        }
        result = _filter_job_vars(jv)
        assert result["guid"] == "abc12"
        assert result["num_workers"] == 3
        assert result["open_environment"] is True
        assert result["region"] == "us-east-1"

    def test_uuid_keyword_match(self):
        jv = {"asset_uuid": "550e8400-e29b-41d4-a716-446655440000"}
        result = _filter_job_vars(jv)
        assert "asset_uuid" in result

    def test_size_keyword_match(self):
        jv = {"node_size": "large", "infra_count": 2}
        result = _filter_job_vars(jv)
        assert "node_size" in result
        assert "infra_count" in result
