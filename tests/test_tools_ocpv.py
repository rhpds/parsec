"""Tests for src/tools/ocpv.py — dispatch, pure helpers, and async handlers."""

from unittest.mock import AsyncMock, patch

import pytest

from src.tools.ocpv import (
    _dispatch_ocpv_action,
    _extract_vm_condition,
    _find_namespace,
    _get_node_ready_status,
    _list_pvcs,
    _parse_capacity,
    _parse_cpu_nanocores,
    _parse_memory_ki,
    _parse_vm_info,
    _parse_volume_entry,
    _strip_secrets_from_line,
)

# ---------------------------------------------------------------------------
# _strip_secrets_from_line
# ---------------------------------------------------------------------------


class TestStripSecretsFromLine:
    def test_plain_line_returned(self):
        assert _strip_secrets_from_line("INFO starting server on port 8080") == (
            "INFO starting server on port 8080"
        )

    def test_empty_line_returned(self):
        assert _strip_secrets_from_line("") == ""

    def test_password_filtered(self):
        assert _strip_secrets_from_line("password=hunter2") is None

    def test_password_colon_filtered(self):
        assert _strip_secrets_from_line("password: mysecret") is None

    def test_api_key_filtered(self):
        assert _strip_secrets_from_line("api_key=abc123xyz") is None

    def test_token_filtered(self):
        assert _strip_secrets_from_line("token: fake-jwt-for-testing") is None

    def test_bearer_filtered(self):
        assert _strip_secrets_from_line("bearer: sk-ant-123") is None

    def test_aws_secret_filtered(self):
        assert _strip_secrets_from_line("aws_secret_access_key = AKIA1234") is None

    def test_certificate_authority_data_filtered(self):
        assert _strip_secrets_from_line("certificate-authority-data: LS0tLS1...") is None

    def test_client_key_data_filtered(self):
        assert _strip_secrets_from_line("client-key-data: LS0tLS1...") is None

    def test_case_insensitive(self):
        assert _strip_secrets_from_line("PASSWORD=hunter2") is None
        assert _strip_secrets_from_line("Token: abc") is None

    def test_keyword_in_non_secret_context_passes(self):
        # "password" without an assignment-like pattern
        assert _strip_secrets_from_line("enter your password below") == (
            "enter your password below"
        )


# ---------------------------------------------------------------------------
# _parse_capacity
# ---------------------------------------------------------------------------


class TestParseCapacity:
    def test_standard_node_capacity(self):
        cap = {"cpu": "64", "memory": "528130688Ki", "ephemeral-storage": "1046380032Ki"}
        cpu, mem, eph = _parse_capacity(cap)
        assert cpu == 64
        assert mem == 528130688 // (1024 * 1024)
        assert eph == 1046380032 // (1024 * 1024)

    def test_small_node(self):
        cap = {"cpu": "4", "memory": "16777216Ki", "ephemeral-storage": "104857600Ki"}
        cpu, mem, eph = _parse_capacity(cap)
        assert cpu == 4
        assert mem == 16777216 // (1024 * 1024)  # 16 GiB
        assert eph == 104857600 // (1024 * 1024)  # 100 GiB

    def test_missing_fields_default_zero(self):
        cpu, mem, eph = _parse_capacity({})
        assert cpu == 0
        assert mem == 0
        assert eph == 0

    def test_memory_without_ki_suffix_returns_zero(self):
        cap = {"cpu": "8", "memory": "32Gi", "ephemeral-storage": "100Gi"}
        cpu, mem, eph = _parse_capacity(cap)
        assert cpu == 8
        assert mem == 0  # only Ki suffix is handled
        assert eph == 0

    def test_integer_memory_value_returns_zero(self):
        cap = {"cpu": "2", "memory": 12345678}
        cpu, mem, eph = _parse_capacity(cap)
        assert cpu == 2
        assert mem == 0  # not a string with Ki suffix
        assert eph == 0


# ---------------------------------------------------------------------------
# _get_node_ready_status
# ---------------------------------------------------------------------------


class TestGetNodeReadyStatus:
    def test_ready_true(self):
        conditions = [
            {"type": "MemoryPressure", "status": "False"},
            {"type": "Ready", "status": "True"},
        ]
        assert _get_node_ready_status(conditions) == "Ready"

    def test_ready_false(self):
        conditions = [
            {"type": "Ready", "status": "False"},
        ]
        assert _get_node_ready_status(conditions) == "NotReady"

    def test_ready_unknown_status(self):
        conditions = [
            {"type": "Ready", "status": "Unknown"},
        ]
        assert _get_node_ready_status(conditions) == "NotReady"

    def test_no_ready_condition(self):
        conditions = [
            {"type": "MemoryPressure", "status": "False"},
            {"type": "DiskPressure", "status": "False"},
        ]
        assert _get_node_ready_status(conditions) == "Unknown"

    def test_empty_conditions(self):
        assert _get_node_ready_status([]) == "Unknown"


# ---------------------------------------------------------------------------
# _parse_cpu_nanocores
# ---------------------------------------------------------------------------


class TestParseCpuNanocores:
    def test_nanocores(self):
        result = _parse_cpu_nanocores("18556768841n")
        assert abs(result - 18.556768841) < 0.0001

    def test_millicores(self):
        assert _parse_cpu_nanocores("2500m") == 2.5

    def test_whole_cores(self):
        assert _parse_cpu_nanocores("4") == 4.0

    def test_zero_nanocores(self):
        assert _parse_cpu_nanocores("0n") == 0.0

    def test_zero_plain(self):
        assert _parse_cpu_nanocores("0") == 0.0

    def test_small_millicore(self):
        assert _parse_cpu_nanocores("1m") == 0.001

    def test_one_nanocore(self):
        result = _parse_cpu_nanocores("1n")
        assert result == pytest.approx(1e-9)


# ---------------------------------------------------------------------------
# _parse_memory_ki
# ---------------------------------------------------------------------------


class TestParseMemoryKi:
    def test_ki_suffix(self):
        # 88164824 Ki / (1024*1024) = 84 GiB
        assert _parse_memory_ki("88164824Ki") == 88164824 // (1024 * 1024)

    def test_mi_suffix(self):
        # 4096 Mi / 1024 = 4 GiB
        assert _parse_memory_ki("4096Mi") == 4

    def test_gi_suffix(self):
        assert _parse_memory_ki("16Gi") == 16

    def test_unknown_suffix_returns_zero(self):
        assert _parse_memory_ki("1234") == 0

    def test_bytes_string_returns_zero(self):
        assert _parse_memory_ki("1073741824") == 0

    def test_zero_ki(self):
        assert _parse_memory_ki("0Ki") == 0


# ---------------------------------------------------------------------------
# _parse_volume_entry
# ---------------------------------------------------------------------------


class TestParseVolumeEntry:
    def test_data_volume(self):
        v = {"name": "rootdisk", "dataVolume": {"name": "my-vm-rootdisk"}}
        result = _parse_volume_entry(v)
        assert result == {
            "name": "rootdisk",
            "type": "dataVolume",
            "source": "my-vm-rootdisk",
        }

    def test_pvc_volume(self):
        v = {"name": "data", "persistentVolumeClaim": {"claimName": "my-pvc"}}
        result = _parse_volume_entry(v)
        assert result == {"name": "data", "type": "pvc", "source": "my-pvc"}

    def test_cloud_init(self):
        v = {"name": "cloudinit", "cloudInitNoCloud": {"userData": "#cloud-config\n"}}
        result = _parse_volume_entry(v)
        assert result == {"name": "cloudinit", "type": "cloudInit"}

    def test_container_disk(self):
        v = {
            "name": "cdrom",
            "containerDisk": {"image": "quay.io/kubevirt/fedora-cloud:latest"},
        }
        result = _parse_volume_entry(v)
        assert result == {
            "name": "cdrom",
            "type": "containerDisk",
            "source": "quay.io/kubevirt/fedora-cloud:latest",
        }

    def test_unknown_volume_type(self):
        v = {"name": "config", "configMap": {"name": "my-config"}}
        result = _parse_volume_entry(v)
        assert result == {"name": "config", "type": "other"}

    def test_missing_name(self):
        v = {"dataVolume": {"name": "dv"}}
        result = _parse_volume_entry(v)
        assert result["name"] == ""
        assert result["type"] == "dataVolume"

    def test_empty_dict(self):
        result = _parse_volume_entry({})
        assert result == {"name": "", "type": "other"}


# ---------------------------------------------------------------------------
# _extract_vm_condition
# ---------------------------------------------------------------------------


class TestExtractVmCondition:
    def test_failing_condition_extracted(self):
        vmi_status = {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {
                    "type": "LiveMigratable",
                    "status": "False",
                    "reason": "DisksNotLiveMigratable",
                    "message": "cannot migrate with disk xyz",
                },
            ]
        }
        result = _extract_vm_condition(vmi_status)
        assert result is not None
        assert result["type"] == "LiveMigratable"
        assert result["reason"] == "DisksNotLiveMigratable"
        assert "cannot migrate" in result["message"]

    def test_all_true_returns_none(self):
        vmi_status = {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "LiveMigratable", "status": "True"},
            ]
        }
        assert _extract_vm_condition(vmi_status) is None

    def test_no_conditions_returns_none(self):
        assert _extract_vm_condition({}) is None
        assert _extract_vm_condition({"conditions": []}) is None

    def test_false_without_message_skipped(self):
        vmi_status = {
            "conditions": [
                {"type": "Synchronized", "status": "False", "reason": "SyncFailed"},
            ]
        }
        assert _extract_vm_condition(vmi_status) is None

    def test_long_message_truncated(self):
        long_msg = "x" * 500
        vmi_status = {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "PodFailed",
                    "message": long_msg,
                },
            ]
        }
        result = _extract_vm_condition(vmi_status)
        assert result is not None
        assert len(result["message"]) == 200


# ---------------------------------------------------------------------------
# _parse_vm_info
# ---------------------------------------------------------------------------


class TestParseVmInfo:
    def _make_vm(
        self,
        name="test-vm",
        printable_status="Running",
        ready=True,
        cores=4,
        sockets=1,
        threads=2,
        guest_mem="8Gi",
    ):
        return {
            "metadata": {"name": name},
            "status": {"printableStatus": printable_status, "ready": ready},
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "cpu": {
                                "cores": cores,
                                "sockets": sockets,
                                "threads": threads,
                            },
                            "memory": {"guest": guest_mem},
                            "devices": {
                                "disks": [
                                    {"name": "rootdisk", "disk": {"bus": "virtio"}},
                                    {"name": "cdrom", "cdrom": {"bus": "sata"}},
                                ],
                            },
                        },
                        "volumes": [
                            {"name": "rootdisk", "dataVolume": {"name": "test-vm-root"}},
                            {
                                "name": "cloudinit",
                                "cloudInitNoCloud": {"userData": "..."},
                            },
                        ],
                    },
                },
            },
        }

    def _make_vmi(self, name="test-vm", node="worker-1", phase="Running", ip="10.0.0.5"):
        return {
            "metadata": {"name": name},
            "status": {
                "nodeName": node,
                "phase": phase,
                "interfaces": [{"ipAddress": ip}],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }

    def test_running_vm_with_vmi(self):
        vm = self._make_vm()
        vmi = self._make_vmi()
        result = _parse_vm_info(vm, {"test-vm": vmi})

        assert result["name"] == "test-vm"
        assert result["status"] == "Running"
        assert result["ready"] is True
        assert result["phase"] == "Running"
        assert result["node"] == "worker-1"
        assert result["ip"] == "10.0.0.5"
        assert result["vcpus"] == 4 * 1 * 2  # cores * sockets * threads
        assert result["memory"] == "8Gi"
        assert len(result["disks"]) == 2
        assert result["disks"][0]["name"] == "rootdisk"
        assert result["disks"][0]["bus"] == "virtio"
        assert result["disks"][1]["name"] == "cdrom"
        assert result["disks"][1]["bus"] == "sata"
        assert len(result["volumes"]) == 2
        assert "condition" not in result  # ready=True, no condition added

    def test_stopped_vm_without_vmi(self):
        vm = self._make_vm(printable_status="Stopped", ready=False)
        result = _parse_vm_info(vm, {})

        assert result["status"] == "Stopped"
        assert result["ready"] is False
        assert result["phase"] == ""
        assert result["node"] is None
        assert result["ip"] is None
        # No condition since VMI is empty (no conditions to extract)
        assert "condition" not in result

    def test_not_ready_vm_gets_condition(self):
        vm = self._make_vm(printable_status="Provisioning", ready=False)
        vmi_data = self._make_vmi(node="", phase="Scheduling", ip="")
        vmi_data["status"]["conditions"] = [
            {
                "type": "Ready",
                "status": "False",
                "reason": "PodNotExists",
                "message": "virt-launcher pod not found",
            },
        ]
        result = _parse_vm_info(vm, {"test-vm": vmi_data})

        assert result["ready"] is False
        assert "condition" in result
        assert result["condition"]["reason"] == "PodNotExists"

    def test_vcpu_multiplication(self):
        vm = self._make_vm(cores=2, sockets=2, threads=2)
        result = _parse_vm_info(vm, {})
        assert result["vcpus"] == 8

    def test_default_cpu_values(self):
        vm = {
            "metadata": {"name": "minimal-vm"},
            "status": {"printableStatus": "Running", "ready": True},
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "cpu": {},  # no cores/sockets/threads
                            "memory": {},
                            "devices": {"disks": []},
                        },
                        "volumes": [],
                    },
                },
            },
        }
        result = _parse_vm_info(vm, {})
        assert result["vcpus"] == 1  # 1*1*1 defaults

    def test_vm_with_no_interfaces(self):
        vm = self._make_vm()
        vmi = self._make_vmi()
        vmi["status"]["interfaces"] = []
        result = _parse_vm_info(vm, {"test-vm": vmi})
        assert result["ip"] is None


# ---------------------------------------------------------------------------
# _dispatch_ocpv_action
# ---------------------------------------------------------------------------


class TestDispatchOcpvAction:
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await _dispatch_ocpv_action("bogus_action", "ocpv08", "", "", "", 50)
        assert "error" in result
        assert "Unknown action: bogus_action" in result["error"]

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._list_pvcs", new_callable=AsyncMock)
    async def test_list_pvcs_dispatched(self, mock_fn):
        mock_fn.return_value = {"pvcs": []}
        await _dispatch_ocpv_action("list_pvcs", "ocpv08", "ns", "pvc-name", "", 10)
        mock_fn.assert_awaited_once_with("ocpv08", "ns", "pvc-name", 10)

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._list_pvs", new_callable=AsyncMock)
    async def test_list_pvs_dispatched(self, mock_fn):
        mock_fn.return_value = {"summary": []}
        await _dispatch_ocpv_action("list_pvs", "ocpv08", "", "pv-name", "", 10)
        mock_fn.assert_awaited_once_with("ocpv08", "pv-name", 10)

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._list_storage_classes", new_callable=AsyncMock)
    async def test_list_storage_classes_dispatched(self, mock_fn):
        mock_fn.return_value = {"storage_classes": []}
        await _dispatch_ocpv_action("list_storage_classes", "ocpv08", "", "", "", 50)
        mock_fn.assert_awaited_once_with("ocpv08")

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._list_vms", new_callable=AsyncMock)
    async def test_list_vms_dispatched(self, mock_fn):
        mock_fn.return_value = {"vms": []}
        await _dispatch_ocpv_action("list_vms", "ocpv08", "ns", "vm-name", "", 20)
        mock_fn.assert_awaited_once_with("ocpv08", "ns", "vm-name", 20)

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._get_node_resources", new_callable=AsyncMock)
    async def test_get_node_resources_dispatched(self, mock_fn):
        mock_fn.return_value = {"nodes": []}
        await _dispatch_ocpv_action("get_node_resources", "ocpv08", "", "worker", "", 50)
        mock_fn.assert_awaited_once_with("ocpv08", "worker")

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._get_ocpv_pod_logs", new_callable=AsyncMock)
    async def test_get_ocpv_pod_logs_dispatched(self, mock_fn):
        mock_fn.return_value = {"results": []}
        await _dispatch_ocpv_action("get_ocpv_pod_logs", "ocpv08", "ns", "pod-name", "error", 100)
        mock_fn.assert_awaited_once_with("ocpv08", "ns", "pod-name", "error", 100)

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._list_pods", new_callable=AsyncMock)
    async def test_list_pods_dispatched(self, mock_fn):
        mock_fn.return_value = {"pods": []}
        await _dispatch_ocpv_action("list_pods", "ocpv08", "ns", "pod", "", 50)
        mock_fn.assert_awaited_once_with("ocpv08", "ns", "pod", 50)

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._nodes_top", new_callable=AsyncMock)
    async def test_nodes_top_dispatched(self, mock_fn):
        mock_fn.return_value = {"nodes": []}
        await _dispatch_ocpv_action("nodes_top", "ocpv08", "", "worker-1", "", 50)
        mock_fn.assert_awaited_once_with("ocpv08", "worker-1")

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._pods_top", new_callable=AsyncMock)
    async def test_pods_top_dispatched(self, mock_fn):
        mock_fn.return_value = {"pods": []}
        await _dispatch_ocpv_action("pods_top", "ocpv08", "ns", "pod", "", 25)
        mock_fn.assert_awaited_once_with("ocpv08", "ns", "pod", 25)

    @pytest.mark.asyncio
    @patch("src.tools.ocpv._list_machines", new_callable=AsyncMock)
    async def test_list_machines_dispatched(self, mock_fn):
        mock_fn.return_value = {"machines": []}
        await _dispatch_ocpv_action("list_machines", "ocpv08", "", "worker", "", 50)
        mock_fn.assert_awaited_once_with("ocpv08", "worker", 50)


# ---------------------------------------------------------------------------
# _find_namespace  (async)
# ---------------------------------------------------------------------------


class TestFindNamespace:
    @pytest.mark.asyncio
    async def test_empty_namespace_returns_error(self):
        result = await _find_namespace("", "ocpv08")
        assert result["error"] == "namespace is required for find_namespace"

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    async def test_found_on_specified_cluster(self, mock_k8s_get):
        mock_k8s_get.return_value = {
            "status": {"phase": "Active"},
        }
        result = await _find_namespace("my-ns", "ocpv08")
        assert result["cluster"] == "ocpv08"
        assert result["namespace"] == "my-ns"
        assert result["status"] == "Active"
        mock_k8s_get.assert_awaited_once_with("ocpv08", "/api/v1/namespaces/my-ns")

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.get_configured_clusters", return_value=["ocpv05", "ocpv06"])
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    async def test_searches_all_clusters_when_none_specified(self, mock_k8s_get, mock_clusters):
        mock_k8s_get.side_effect = [
            Exception("404 Not Found"),  # ocpv05 miss
            {"status": {"phase": "Active"}},  # ocpv06 hit
        ]
        result = await _find_namespace("guid-abc", "")
        assert result["cluster"] == "ocpv06"
        assert result["namespace"] == "guid-abc"

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.get_configured_clusters", return_value=["ocpv05", "ocpv06"])
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    async def test_not_found_on_any_cluster(self, mock_k8s_get, mock_clusters):
        mock_k8s_get.side_effect = [
            Exception("404 Not Found"),
            Exception("NotFound"),
        ]
        result = await _find_namespace("missing-ns", "")
        assert "error" in result
        assert "not found on any OCPV cluster" in result["error"]
        assert result["clusters_searched"] == ["ocpv05", "ocpv06"]

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    async def test_non_404_error_continues(self, mock_k8s_get):
        """Non-404 errors (e.g., timeout) should be skipped, not crash."""
        mock_k8s_get.side_effect = Exception("connection timeout")
        result = await _find_namespace("some-ns", "ocpv08")
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    async def test_missing_status_defaults_to_unknown(self, mock_k8s_get):
        mock_k8s_get.return_value = {}  # no status field
        result = await _find_namespace("ns-no-status", "ocpv08")
        assert result["status"] == "Unknown"


# ---------------------------------------------------------------------------
# _list_pvcs  (async)
# ---------------------------------------------------------------------------


class TestListPvcs:
    @pytest.mark.asyncio
    async def test_namespace_required(self):
        result = await _list_pvcs("ocpv08", "", "", 50)
        assert result["error"] == "namespace is required for list_pvcs"

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_basic_pvc_listing(self, mock_list):
        mock_list.return_value = {
            "items": [
                {
                    "metadata": {"name": "data-pvc"},
                    "status": {"phase": "Bound"},
                    "spec": {
                        "storageClassName": "ocs-storagecluster-ceph-rbd",
                        "resources": {"requests": {"storage": "100Gi"}},
                        "volumeMode": "Filesystem",
                        "accessModes": ["ReadWriteOnce"],
                    },
                },
            ],
        }
        result = await _list_pvcs("ocpv08", "my-ns", "", 50)

        assert result["cluster"] == "ocpv08"
        assert result["namespace"] == "my-ns"
        assert result["count"] == 1
        assert result["pending_count"] == 0

        pvc = result["pvcs"][0]
        assert pvc["name"] == "data-pvc"
        assert pvc["status"] == "Bound"
        assert pvc["storage_class"] == "ocs-storagecluster-ceph-rbd"
        assert pvc["size"] == "100Gi"
        assert pvc["volume_mode"] == "Filesystem"
        assert pvc["access_modes"] == ["ReadWriteOnce"]

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_name_filter(self, mock_list):
        mock_list.return_value = {
            "items": [
                {"metadata": {"name": "data-pvc"}, "status": {"phase": "Bound"}, "spec": {}},
                {"metadata": {"name": "logs-pvc"}, "status": {"phase": "Bound"}, "spec": {}},
                {"metadata": {"name": "DATA-BACKUP"}, "status": {"phase": "Bound"}, "spec": {}},
            ],
        }
        result = await _list_pvcs("ocpv08", "ns", "data", 50)
        # "data" matches "data-pvc" and "DATA-BACKUP" (case-insensitive)
        assert result["count"] == 2
        names = [p["name"] for p in result["pvcs"]]
        assert "data-pvc" in names
        assert "DATA-BACKUP" in names

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_pending_pvc_gets_events(self, mock_list, mock_get):
        mock_list.return_value = {
            "items": [
                {
                    "metadata": {"name": "stuck-pvc"},
                    "status": {"phase": "Pending"},
                    "spec": {
                        "storageClassName": "gp3-csi",
                        "resources": {"requests": {"storage": "50Gi"}},
                    },
                },
            ],
        }
        mock_get.return_value = {
            "items": [
                {
                    "reason": "ProvisioningFailed",
                    "message": "no capacity in zone us-east-1a",
                },
            ],
        }
        result = await _list_pvcs("ocpv08", "ns", "", 50)

        assert result["pending_count"] == 1
        pvc = result["pvcs"][0]
        assert pvc["status"] == "Pending"
        assert "events" in pvc
        assert "ProvisioningFailed" in pvc["events"][0]

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_pending_pvc_event_fetch_failure_is_silent(self, mock_list, mock_get):
        mock_list.return_value = {
            "items": [
                {
                    "metadata": {"name": "stuck-pvc"},
                    "status": {"phase": "Pending"},
                    "spec": {},
                },
            ],
        }
        mock_get.side_effect = Exception("forbidden")
        result = await _list_pvcs("ocpv08", "ns", "", 50)

        # Should succeed without events (exception is swallowed)
        assert result["count"] == 1
        assert "events" not in result["pvcs"][0]

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_max_results_limits_output(self, mock_list):
        items = [
            {"metadata": {"name": f"pvc-{i}"}, "status": {"phase": "Bound"}, "spec": {}}
            for i in range(10)
        ]
        mock_list.return_value = {"items": items}
        result = await _list_pvcs("ocpv08", "ns", "", 3)
        assert result["count"] == 3

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_empty_pvc_list(self, mock_list):
        mock_list.return_value = {"items": []}
        result = await _list_pvcs("ocpv08", "ns", "", 50)
        assert result["count"] == 0
        assert result["pvcs"] == []
        assert result["pending_count"] == 0

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_missing_spec_fields_default_gracefully(self, mock_list):
        mock_list.return_value = {
            "items": [
                {
                    "metadata": {"name": "bare-pvc"},
                    "status": {},
                    "spec": {},
                },
            ],
        }
        result = await _list_pvcs("ocpv08", "ns", "", 50)
        pvc = result["pvcs"][0]
        assert pvc["status"] == "Unknown"
        assert pvc["storage_class"] == ""
        assert pvc["size"] == "?"
        assert pvc["volume_mode"] == "Filesystem"
        assert pvc["access_modes"] == []

    @pytest.mark.asyncio
    @patch("src.tools.ocpv.k8s_get", new_callable=AsyncMock)
    @patch("src.tools.ocpv.k8s_list_namespaced", new_callable=AsyncMock)
    async def test_pending_pvc_events_limited_to_last_three(self, mock_list, mock_get):
        mock_list.return_value = {
            "items": [
                {
                    "metadata": {"name": "stuck-pvc"},
                    "status": {"phase": "Pending"},
                    "spec": {},
                },
            ],
        }
        mock_get.return_value = {
            "items": [{"reason": f"Event{i}", "message": f"msg {i}"} for i in range(5)],
        }
        result = await _list_pvcs("ocpv08", "ns", "", 50)
        pvc = result["pvcs"][0]
        # Only last 3 events should be included
        assert len(pvc["events"]) == 3
        assert "Event2" in pvc["events"][0]
        assert "Event4" in pvc["events"][2]
