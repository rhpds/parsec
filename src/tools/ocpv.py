"""OCPV cluster inspection tool — PVCs, PVs, VMs, pods, nodes, storage classes."""

import contextlib
import logging
import re
from typing import Any

from src.connections.ocpv import (
    get_configured_clusters,
    k8s_get,
    k8s_get_text,
    k8s_list_cluster,
    k8s_list_namespaced,
    resolve_cluster_from_comment,
)

logger = logging.getLogger(__name__)

# Reuse secret stripping from babylon tool
_SECRET_PATTERNS = re.compile(
    r"(password|secret|token|key|credential|aws_access|aws_secret"
    r"|api_key|apikey|auth|bearer|certificate-authority-data"
    r"|client-certificate-data|client-key-data)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def _strip_secrets_from_line(line: str) -> str | None:
    """Return None if line contains a secret pattern, else return the line."""
    if _SECRET_PATTERNS.search(line):
        return None
    return line


async def query_ocpv_cluster(
    action: str,
    cluster: str = "",
    namespace: str = "",
    name: str = "",
    search: str = "",
    sandbox_comment: str = "",
    max_results: int = 50,
) -> dict[str, Any]:
    """Query an OCPV cluster for infrastructure state.

    Args:
        action: Action to perform (find_namespace, list_pvcs, list_pvs,
                list_storage_classes, list_vms, get_node_resources,
                get_pod_logs, list_pods).
        cluster: OCPV cluster short name (e.g., 'ocpv08'). If omitted,
                 resolved from sandbox_comment or searched.
        namespace: Kubernetes namespace (required for namespace-scoped actions).
        name: Filter by resource name substring.
        search: Grep filter for pod logs.
        sandbox_comment: Sandbox DynamoDB comment field for cluster resolution.
        max_results: Max results to return (default 50).
    """
    try:
        # Resolve cluster
        target_cluster = cluster.lower() if cluster else ""
        if not target_cluster and sandbox_comment:
            target_cluster = resolve_cluster_from_comment(sandbox_comment)

        # Actions that don't require a pre-resolved cluster
        if action == "find_namespace":
            return await _find_namespace(namespace, target_cluster)

        # For other actions, resolve cluster from namespace if needed
        if not target_cluster and namespace:
            found = await _find_namespace(namespace, "")
            if "cluster" in found and "error" not in found:
                target_cluster = found["cluster"]
            else:
                return found  # Return the error

        if not target_cluster:
            configured = get_configured_clusters()
            return {
                "error": "No cluster specified and could not resolve one. "
                f"Available clusters: {configured}",
            }

        if action == "list_pvcs":
            return await _list_pvcs(target_cluster, namespace, name, max_results)
        elif action == "list_pvs":
            return await _list_pvs(target_cluster, name, max_results)
        elif action == "list_storage_classes":
            return await _list_storage_classes(target_cluster)
        elif action == "list_vms":
            return await _list_vms(target_cluster, namespace, name, max_results)
        elif action == "get_node_resources":
            return await _get_node_resources(target_cluster, name)
        elif action == "get_pod_logs":
            return await _get_pod_logs(target_cluster, namespace, name, search, max_results)
        elif action == "list_pods":
            return await _list_pods(target_cluster, namespace, name, max_results)
        else:
            return {
                "error": f"Unknown action: {action}. Use: find_namespace, "
                "list_pvcs, list_pvs, list_storage_classes, list_vms, "
                "get_node_resources, get_pod_logs, list_pods"
            }
    except Exception as e:
        logger.exception("OCPV query failed: action=%s cluster=%s", action, target_cluster)
        return {"error": str(e), "cluster": target_cluster}


async def _find_namespace(namespace: str, cluster: str) -> dict[str, Any]:
    """Search OCPV clusters for a namespace."""
    if not namespace:
        return {"error": "namespace is required for find_namespace"}

    clusters_to_search = [cluster] if cluster else get_configured_clusters()

    for c in clusters_to_search:
        try:
            result = await k8s_get(c, f"/api/v1/namespaces/{namespace}")
            status = result.get("status", {}).get("phase", "Unknown")
            return {
                "cluster": c,
                "namespace": namespace,
                "status": status,
            }
        except Exception as e:
            error_str = str(e)
            if "404" in error_str or "NotFound" in error_str:
                continue
            logger.debug("Error checking namespace on %s: %s", c, e)
            continue

    return {
        "error": f"Namespace '{namespace}' not found on any OCPV cluster",
        "clusters_searched": clusters_to_search,
    }


async def _list_pvcs(cluster: str, namespace: str, name: str, max_results: int) -> dict[str, Any]:
    """List PVCs in a namespace with status, storageClass, volumeMode."""
    if not namespace:
        return {"error": "namespace is required for list_pvcs"}

    result = await k8s_list_namespaced(cluster, "", "v1", "persistentvolumeclaims", namespace)
    items = result.get("items", [])

    if name:
        name_lower = name.lower()
        items = [i for i in items if name_lower in i["metadata"]["name"].lower()]

    pvcs = []
    pending_count = 0
    for item in items[:max_results]:
        status = item.get("status", {}).get("phase", "Unknown")
        if status == "Pending":
            pending_count += 1

        spec = item.get("spec", {})
        pvc_info: dict[str, Any] = {
            "name": item["metadata"]["name"],
            "status": status,
            "storage_class": spec.get("storageClassName", ""),
            "size": spec.get("resources", {}).get("requests", {}).get("storage", "?"),
            "volume_mode": spec.get("volumeMode", "Filesystem"),
            "access_modes": spec.get("accessModes", []),
        }

        # Include events for Pending PVCs
        if status == "Pending":
            try:
                events = await k8s_get(
                    cluster,
                    f"/api/v1/namespaces/{namespace}/events"
                    f"?fieldSelector=involvedObject.name={item['metadata']['name']}"
                    f",involvedObject.kind=PersistentVolumeClaim",
                )
                event_messages = []
                for ev in events.get("items", [])[-3:]:
                    reason = ev.get("reason", "")
                    message = ev.get("message", "")
                    event_messages.append(f"{reason}: {message}")
                if event_messages:
                    pvc_info["events"] = event_messages
            except Exception:
                pass

        pvcs.append(pvc_info)

    return {
        "cluster": cluster,
        "namespace": namespace,
        "pvcs": pvcs,
        "count": len(pvcs),
        "pending_count": pending_count,
    }


async def _list_pvs(cluster: str, name: str, max_results: int) -> dict[str, Any]:
    """List PVs grouped by node and storage class."""
    result = await k8s_list_cluster(cluster, "", "v1", "persistentvolumes")
    items = result.get("items", [])

    if name:
        name_lower = name.lower()
        items = [i for i in items if name_lower in i["metadata"]["name"].lower()]

    # Group by node + storage class
    summary: dict[str, dict[str, Any]] = {}
    for item in items:
        sc = item.get("spec", {}).get("storageClassName", "unknown")
        status = item.get("status", {}).get("phase", "Unknown")

        # Extract node from nodeAffinity
        node = "unassigned"
        affinity = (
            item.get("spec", {})
            .get("nodeAffinity", {})
            .get("required", {})
            .get("nodeSelectorTerms", [])
        )
        for term in affinity:
            for expr in term.get("matchExpressions", []):
                if expr.get("values"):
                    node = expr["values"][0]

        key = f"{node}|{sc}"
        if key not in summary:
            summary[key] = {
                "node": node,
                "storage_class": sc,
                "bound": 0,
                "bound_capacity_gi": 0,
                "available": 0,
                "released": 0,
            }

        cap_str = item.get("spec", {}).get("capacity", {}).get("storage", "0")
        gi = 0
        if "Gi" in cap_str:
            with contextlib.suppress(ValueError):
                gi = int(cap_str.replace("Gi", ""))

        if status == "Bound":
            summary[key]["bound"] += 1
            summary[key]["bound_capacity_gi"] += gi
        elif status == "Available":
            summary[key]["available"] += 1
        elif status == "Released":
            summary[key]["released"] += 1

    rows = sorted(summary.values(), key=lambda r: -r["bound_capacity_gi"])

    return {
        "cluster": cluster,
        "summary": rows[:max_results],
        "total_pvs": len(items),
        "total_bound_gi": sum(r["bound_capacity_gi"] for r in rows),
    }


async def _list_storage_classes(cluster: str) -> dict[str, Any]:
    """List storage classes on the cluster."""
    result = await k8s_list_cluster(cluster, "storage.k8s.io", "v1", "storageclasses")
    items = result.get("items", [])

    scs = []
    for item in items:
        scs.append(
            {
                "name": item["metadata"]["name"],
                "provisioner": item.get("provisioner", ""),
                "reclaim_policy": item.get("reclaimPolicy", ""),
                "binding_mode": item.get("volumeBindingMode", ""),
                "allow_volume_expansion": item.get("allowVolumeExpansion", False),
                "default": "storageclass.kubernetes.io/is-default-class"
                in item.get("metadata", {}).get("annotations", {}),
            }
        )

    return {"cluster": cluster, "storage_classes": scs, "count": len(scs)}


async def _list_vms(cluster: str, namespace: str, name: str, max_results: int) -> dict[str, Any]:
    """List VirtualMachines and VirtualMachineInstances in a namespace."""
    if not namespace:
        return {"error": "namespace is required for list_vms"}

    # Get VMs
    vm_result = await k8s_list_namespaced(
        cluster, "kubevirt.io", "v1", "virtualmachines", namespace
    )
    vm_items = vm_result.get("items", [])

    # Get VMIs
    vmi_result = await k8s_list_namespaced(
        cluster, "kubevirt.io", "v1", "virtualmachineinstances", namespace
    )
    vmi_items = vmi_result.get("items", [])

    # Index VMIs by name
    vmi_by_name = {}
    for vmi in vmi_items:
        vmi_name = vmi["metadata"]["name"]
        vmi_by_name[vmi_name] = vmi

    if name:
        name_lower = name.lower()
        vm_items = [i for i in vm_items if name_lower in i["metadata"]["name"].lower()]

    vms = []
    for vm in vm_items[:max_results]:
        vm_name = vm["metadata"]["name"]
        vm_status = vm.get("status", {})
        print_status = vm_status.get("printableStatus", "Unknown")
        ready = vm_status.get("ready", False)

        # Get VMI info if running
        vmi = vmi_by_name.get(vm_name, {})
        vmi_status = vmi.get("status", {}) if vmi else {}
        node = vmi_status.get("nodeName", "")
        phase = vmi_status.get("phase", "")
        interfaces = vmi_status.get("interfaces", [])
        ip = interfaces[0].get("ipAddress", "") if interfaces else ""

        vm_info: dict[str, Any] = {
            "name": vm_name,
            "status": print_status,
            "ready": ready,
            "phase": phase,
            "node": node or None,
            "ip": ip or None,
        }

        # Add scheduling conditions for non-running VMs
        if not ready:
            conditions = vmi_status.get("conditions", [])
            for cond in conditions:
                if cond.get("status") == "False" and cond.get("message"):
                    vm_info["condition"] = {
                        "type": cond.get("type", ""),
                        "reason": cond.get("reason", ""),
                        "message": cond["message"][:200],
                    }
                    break

        vms.append(vm_info)

    return {
        "cluster": cluster,
        "namespace": namespace,
        "vms": vms,
        "count": len(vms),
    }


async def _get_node_resources(cluster: str, name: str) -> dict[str, Any]:
    """Get node CPU, memory, and ephemeral storage capacity."""
    result = await k8s_list_cluster(cluster, "", "v1", "nodes")
    items = result.get("items", [])

    if name:
        name_lower = name.lower()
        items = [i for i in items if name_lower in i["metadata"]["name"].lower()]

    nodes = []
    for node in items:
        node_name = node["metadata"]["name"]
        capacity = node.get("status", {}).get("capacity", {})

        # Parse capacity values
        cpu = int(capacity.get("cpu", "0"))
        mem_ki = capacity.get("memory", "0")
        if isinstance(mem_ki, str) and mem_ki.endswith("Ki"):
            mem_gi = int(mem_ki.replace("Ki", "")) // (1024 * 1024)
        else:
            mem_gi = 0

        eph = capacity.get("ephemeral-storage", "0")
        if isinstance(eph, str) and eph.endswith("Ki"):
            eph_gi = int(eph.replace("Ki", "")) // (1024 * 1024)
        else:
            eph_gi = 0

        # Node status
        conditions = node.get("status", {}).get("conditions", [])
        ready = "Unknown"
        for cond in conditions:
            if cond.get("type") == "Ready":
                ready = "Ready" if cond.get("status") == "True" else "NotReady"

        nodes.append(
            {
                "name": node_name,
                "cpu": cpu,
                "memory_gi": mem_gi,
                "ephemeral_storage_gi": eph_gi,
                "status": ready,
            }
        )

    return {
        "cluster": cluster,
        "nodes": sorted(nodes, key=lambda n: n["name"]),
        "count": len(nodes),
    }


async def _get_pod_logs(
    cluster: str,
    namespace: str,
    name: str,
    search: str,
    max_results: int,
) -> dict[str, Any]:
    """Get pod logs from a namespace with optional grep filtering."""
    if not namespace:
        return {"error": "namespace is required for get_pod_logs"}

    # List pods
    try:
        pod_list = await k8s_get(cluster, f"/api/v1/namespaces/{namespace}/pods?limit=100")
    except Exception as e:
        error_msg = str(e)
        if "Forbidden" in error_msg or "403" in error_msg:
            return {
                "error": f"No permission to list pods in {namespace} on {cluster}.",
                "cluster": cluster,
                "namespace": namespace,
            }
        return {"error": f"Failed to list pods in {namespace}: {e}", "cluster": cluster}

    pods = pod_list.get("items", [])
    if not pods:
        return {
            "cluster": cluster,
            "namespace": namespace,
            "results": [],
            "message": f"No pods found in {namespace}",
        }

    # Filter pods by name
    if name:
        name_lower = name.lower()
        pods = [p for p in pods if name_lower in p["metadata"]["name"].lower()]

    max_pods = 5
    results = []

    for pod in pods[:max_pods]:
        pod_name = pod["metadata"]["name"]
        pod_phase = pod.get("status", {}).get("phase", "Unknown")
        containers = [c["name"] for c in pod.get("spec", {}).get("containers", [])]

        tail_lines = min(max_results, 500)
        params: dict[str, str | int] = {"tailLines": tail_lines}

        log_text = ""
        try:
            log_text = await k8s_get_text(
                cluster,
                f"/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
                params=params,
            )
        except Exception as e:
            log_text = f"[Error fetching logs: {e}]"

        lines = log_text.splitlines()
        if search:
            search_lower = search.lower()
            lines = [ln for ln in lines if search_lower in ln.lower()]

        # Strip secrets
        filtered = []
        for ln in lines[:max_results]:
            cleaned = _strip_secrets_from_line(ln)
            if cleaned is not None:
                filtered.append(cleaned)

        results.append(
            {
                "pod": pod_name,
                "phase": pod_phase,
                "containers": containers,
                "log_lines": len(filtered),
                "logs": "\n".join(filtered) if filtered else "(no matching lines)",
                "grep": search or "(none)",
            }
        )

    return {
        "cluster": cluster,
        "namespace": namespace,
        "total_pods": len(pods),
        "pods_shown": len(results),
        "results": results,
    }


async def _list_pods(cluster: str, namespace: str, name: str, max_results: int) -> dict[str, Any]:
    """List pods in a namespace with status."""
    if not namespace:
        return {"error": "namespace is required for list_pods"}

    result = await k8s_list_namespaced(cluster, "", "v1", "pods", namespace)
    items = result.get("items", [])

    if name:
        name_lower = name.lower()
        items = [i for i in items if name_lower in i["metadata"]["name"].lower()]

    pods = []
    for item in items[:max_results]:
        status = item.get("status", {})
        container_statuses = status.get("containerStatuses", [])
        restarts = sum(cs.get("restartCount", 0) for cs in container_statuses)

        # Calculate age from creation timestamp
        created = item["metadata"].get("creationTimestamp", "")

        pods.append(
            {
                "name": item["metadata"]["name"],
                "phase": status.get("phase", "Unknown"),
                "node": item.get("spec", {}).get("nodeName", ""),
                "restarts": restarts,
                "created": created,
            }
        )

    return {
        "cluster": cluster,
        "namespace": namespace,
        "pods": pods,
        "count": len(pods),
    }
