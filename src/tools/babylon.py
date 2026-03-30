"""Tool: query_babylon_catalog — query Babylon clusters for catalog and deployment data."""

import asyncio
import logging
import re
from typing import Any

from src.connections.babylon import (
    get_configured_clusters,
    k8s_get,
    k8s_get_resource,
    k8s_get_text,
    k8s_list,
    k8s_list_cluster_wide,
    resolve_cluster_from_comment,
)

logger = logging.getLogger(__name__)

# CRD coordinates
CATALOG_ITEM_GROUP = "babylon.gpte.redhat.com"
CATALOG_ITEM_VERSION = "v1"
CATALOG_ITEM_PLURAL = "catalogitems"
CATALOG_NAMESPACES = ["babylon-catalog-prod", "babylon-catalog-event", "babylon-catalog-dev"]

AVC_GROUP = "gpte.redhat.com"
AVC_VERSION = "v1"
AVC_PLURAL = "agnosticvcomponents"
AVC_NAMESPACE = "babylon-config"

RESOURCE_CLAIM_GROUP = "poolboy.gpte.redhat.com"
RESOURCE_CLAIM_VERSION = "v1"
RESOURCE_CLAIM_PLURAL = "resourceclaims"

ANARCHY_SUBJECT_GROUP = "anarchy.gpte.redhat.com"
ANARCHY_SUBJECT_VERSION = "v1"
ANARCHY_SUBJECT_PLURAL = "anarchysubjects"

ANARCHY_ACTION_GROUP = "anarchy.gpte.redhat.com"
ANARCHY_ACTION_VERSION = "v1"
ANARCHY_ACTION_PLURAL = "anarchyactions"

RESOURCE_POOL_GROUP = "poolboy.gpte.redhat.com"
RESOURCE_POOL_VERSION = "v1"
RESOURCE_POOL_PLURAL = "resourcepools"
RESOURCE_POOL_NAMESPACE = "poolboy"

WORKSHOP_GROUP = "babylon.gpte.redhat.com"
WORKSHOP_VERSION = "v1"
WORKSHOP_PLURAL = "workshops"

MULTI_WORKSHOP_GROUP = "babylon.gpte.redhat.com"
MULTI_WORKSHOP_VERSION = "v1"
MULTI_WORKSHOP_PLURAL = "multiworkshops"

# Fields that contain secrets and must be stripped from results
_SECRET_PATTERNS = re.compile(
    r"(access_key|secret_key|password|token|pull_secret|hmac_key|eab_key|"
    r"ssh_pass|secret_access|api_key|client_secret|activationkey)",
    re.IGNORECASE,
)

# Keys known to contain secrets (exact match)
_SECRET_KEYS = {
    "ocp4_pull_secret",
    "ocp4_token",
    "openshift_cluster_admin_token",
    "aws_access_key_id",
    "aws_secret_access_key",
    "bastion_ansible_ssh_pass",
    "openshift_kubeadmin_password",
    "aws_web_console_password",
    "bastion_ssh_password",
    "sandbox_openshift_api_key",
    "openshift_api_key",
    "openshift_api_ca_cert",
    "agnosticd_save_output_dir_s3_access_key_id",
    "agnosticd_save_output_dir_s3_secret_access_key",
    "ocp4_workload_cert_manager_ec2_access_key_id",
    "ocp4_workload_cert_manager_ec2_secret_access_key",
    "certbot_cert_manager_zerossl_eab_key_id",
    "certbot_cert_manager_zerossl_hmac_key",
    "ocp4_workload_cert_manager_zerossl_eab_key_id",
    "ocp4_workload_cert_manager_zerossl_hmac_key",
    "satellite_activationkey",
    "set_repositories_satellite_activationkey",
}


def _strip_secrets(obj: Any) -> Any:
    """Recursively strip secret values from a dict/list."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in _SECRET_KEYS or _SECRET_PATTERNS.search(k):
                result[k] = "[REDACTED]"
            else:
                result[k] = _strip_secrets(v)
        return result
    elif isinstance(obj, list):
        return [_strip_secrets(item) for item in obj]
    return obj


def _normalize_ci_name(name: str) -> str:
    """Normalize a catalog item name to Babylon CRD format.

    Sheet/DB format uses slashes and underscores: openshift_cnv/ocp-virt-lab
    K8s CRD names use dots and dashes: openshift-cnv.ocp-virt-lab
    """
    return name.replace("/", ".").replace("_", "-").lower()


def _parse_catalog_item(item: dict) -> dict:
    """Parse a CatalogItem CRD into a summary dict."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    labels = metadata.get("labels", {})

    name = metadata.get("name", "")
    # ci_name is name without the stage suffix (e.g., "foo.bar.prod" → "foo.bar")
    parts = name.rsplit(".", 1)
    stage = labels.get("babylon.gpte.redhat.com/stage", "")
    if len(parts) == 2 and parts[1] in ("prod", "dev", "event", "test"):
        ci_name = parts[0]
        stage = stage or parts[1]
    else:
        ci_name = name

    return {
        "ci_name": ci_name,
        "display_name": spec.get("displayName", ""),
        "namespace": metadata.get("namespace", ""),
        "stage": stage,
        "multiuser": spec.get("multiuser", False),
        "asset_uuid": labels.get("gpte.redhat.com/asset-uuid", ""),
        "keywords": spec.get("keywords", []),
    }


def _extract_instances_from_list(definition: dict) -> list[dict]:
    """Extract instances from the `instances` list (Pattern A) and CNV (Pattern C)."""
    instances: list[dict] = []
    raw_instances = definition.get("instances", [])
    if not isinstance(raw_instances, list):
        return instances

    for inst in raw_instances:
        if not isinstance(inst, dict):
            continue
        entry: dict[str, Any] = {
            "purpose": inst.get("name", "unknown"),
            "count": inst.get("count", 1),
            "image": inst.get("image", ""),
        }
        for cloud in ("ec2", "aws", "azure", "gcp"):
            flavor = inst.get(f"flavor_{cloud}") or inst.get("flavor", {})
            if isinstance(flavor, dict) and cloud in flavor:
                entry["instance_type"] = flavor[cloud]
                entry["cloud"] = cloud if cloud != "ec2" else "aws"
                break
            elif isinstance(flavor, dict):
                for fk, fv in flavor.items():
                    if fk in ("ec2", "aws", "azure", "gcp"):
                        entry["instance_type"] = fv
                        entry["cloud"] = fk if fk != "ec2" else "aws"
                        break
        if "cores" in inst or "memory" in inst:
            entry["cores"] = inst.get("cores")
            entry["memory"] = inst.get("memory")
            entry["cloud"] = "cnv"
        if entry.get("instance_type") or entry.get("cores"):
            instances.append(entry)

    return instances


def _extract_instances_from_roles(definition: dict) -> list[dict]:
    """Extract instances from role variables (Pattern B)."""
    instances = []
    roles = ["bastion", "master", "worker", "node", "control_plane", "infra"]
    for role in roles:
        for type_key in [f"{role}_instance_type", f"{role}_instance_flavor"]:
            itype = definition.get(type_key)
            if itype and isinstance(itype, str) and "{{" not in itype:
                count = _resolve_role_count(definition, role)
                instances.append({"purpose": role, "instance_type": itype, "count": count})
    return instances


def _resolve_role_count(definition: dict, role: str) -> int:
    """Resolve instance count for a role from various naming conventions."""
    count_keys = [f"{role}_instance_count", f"num_{role}s", f"num_{role}", f"{role}_count"]
    for ck in count_keys:
        cv = definition.get(ck)
        if cv is not None:
            try:
                return int(cv)
            except (ValueError, TypeError):
                pass
            break
    return 1


def _extract_instance_info(definition: dict) -> list[dict]:
    """Extract expected instance types and counts from an AgnosticVComponent definition."""
    instances = _extract_instances_from_list(definition)
    instances.extend(_extract_instances_from_roles(definition))

    # Pattern E: ROSA cluster
    if definition.get("rosa_deploy"):
        rosa_type = definition.get("rosa_compute_machine_type", "m5.xlarge")
        rosa_count = definition.get("rosa_compute_replicas", 2)
        instances.append(
            {
                "purpose": "rosa_worker",
                "instance_type": rosa_type,
                "count": rosa_count,
                "cloud": "aws",
            }
        )

    # Pattern F: MachineSet groups
    ms_groups = definition.get("ocp4_workload_machinesets_machineset_groups", [])
    if isinstance(ms_groups, list):
        for group in ms_groups:
            if not isinstance(group, dict):
                continue
            itype = group.get("instance_type", "")
            replicas = group.get("total_replicas", group.get("replicas", 1))
            gname = group.get("name", group.get("group_name", "machineset"))
            if itype:
                instances.append(
                    {"purpose": f"machineset_{gname}", "instance_type": itype, "count": replicas}
                )

    return instances


# Keywords for filtering job_vars to deployment-relevant fields
_JOB_VAR_KEYWORDS = [
    "aws_region",
    "bastion",
    "cloud_provider",
    "cluster_size",
    "count",
    "env_type",
    "guid",
    "infra",
    "instance",
    "master",
    "node",
    "num_",
    "open_environment",
    "platform",
    "region",
    "rosa",
    "sandbox",
    "size",
    "uuid",
    "worker",
]


def _filter_job_vars(jv: dict) -> dict:
    """Filter job_vars to deployment-relevant fields, excluding secrets."""
    result = {}
    for k, v in jv.items():
        if k in _SECRET_KEYS or _SECRET_PATTERNS.search(k):
            continue
        if any(x in k.lower() for x in _JOB_VAR_KEYWORDS):
            result[k] = v
    return result


def _extract_deployment_info(resource_claim: dict) -> dict:
    """Extract key deployment info from a ResourceClaim, stripping secrets."""
    metadata = resource_claim.get("metadata", {})
    spec = resource_claim.get("spec", {})
    status = resource_claim.get("status", {})
    labels = metadata.get("labels", {})

    result: dict[str, Any] = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "catalog_item": labels.get("babylon.gpte.redhat.com/catalogItemName", ""),
        "catalog_namespace": labels.get("babylon.gpte.redhat.com/catalogItemNamespace", ""),
        "asset_uuid": labels.get("gpte.redhat.com/asset-uuid", ""),
        "lifespan": spec.get("lifespan", {}),
        "healthy": status.get("healthy"),
        "ready": status.get("ready"),
        "state": "unknown",
    }

    # Extract summary (has provision_data with sandbox info)
    summary = status.get("summary", {})
    if summary:
        result["state"] = summary.get("state", "unknown")
        result["agnosticv"] = summary.get("agnosticv", {})
        result["runtime_default"] = summary.get("runtime_default", "")
        result["runtime_maximum"] = summary.get("runtime_maximum", "")

        # Extract sanitized provision_data
        pdata = summary.get("provision_data", {})
        if pdata:
            cloud = pdata.get("cloud_provider", "")
            result["provision_data"] = {
                "cloud_provider": cloud,
                "guid": pdata.get("guid", ""),
            }
            # AWS sandbox fields — present for both AWS-native and CNV items
            # that provision spoke clusters or other resources on AWS
            aws_account = pdata.get("aws_sandbox_account_id", "")
            if aws_account:
                result["provision_data"]["aws_region"] = pdata.get(
                    "aws_default_region", pdata.get("aws_region", "")
                )
                result["provision_data"]["sandbox_account_id"] = aws_account
                domain = pdata.get("aws_route53_domain", "")
                if domain:
                    parts = domain.strip(".").split(".")
                    if parts:
                        result["provision_data"]["sandbox_name"] = parts[0]
            # CNV-specific fields
            if cloud == "openshift_cnv":
                result["provision_data"]["cnv_cluster"] = pdata.get(
                    "sandbox_openshift_cluster",
                    pdata.get("openshift_cluster_ingress_domain", ""),
                )

    # Extract job_vars from embedded AnarchySubject state
    resources = status.get("resources", [])
    if resources:
        state = resources[0].get("state", {})
        if state.get("kind") == "AnarchySubject":
            as_vars = state.get("spec", {}).get("vars", {})
            result["current_state"] = as_vars.get("current_state", "")
            result["desired_state"] = as_vars.get("desired_state", "")

            jv = as_vars.get("job_vars", {})
            # Extract instance-related vars
            instance_vars = _filter_job_vars(jv)
            result["instance_vars"] = instance_vars

    return result


def _extract_resource_components(resource_claim: dict) -> list[dict]:
    """Extract info from ALL resources in a ResourceClaim (not just [0]).

    Each resource is an AnarchySubject component. Multi-component catalog items
    (e.g., azure sandbox + zt-lab-developer-cnv) have multiple entries.
    Returns a list of component dicts with state, GUID, and tower job refs.
    """
    status = resource_claim.get("status", {})
    resources = status.get("resources", [])
    components: list[dict] = []

    for res in resources:
        if not isinstance(res, dict):
            continue

        comp: dict[str, Any] = {
            "name": res.get("name", ""),
            "healthy": res.get("healthy"),
            "ready": res.get("ready"),
        }

        # Extract AnarchySubject reference
        ref = res.get("reference", {})
        if ref:
            comp["anarchy_subject"] = ref.get("name", "")
            comp["anarchy_namespace"] = ref.get("namespace", "")

        # Extract state from embedded AnarchySubject
        state = res.get("state", {})
        if state.get("kind") == "AnarchySubject":
            as_vars = state.get("spec", {}).get("vars", {})
            comp["current_state"] = as_vars.get("current_state", "")
            comp["desired_state"] = as_vars.get("desired_state", "")

            jv = as_vars.get("job_vars", {})
            comp["guid"] = jv.get("guid", "")
            comp["cloud_provider"] = jv.get("cloud_provider", "")

            # Tower job references from AnarchySubject status
            tower_jobs = state.get("status", {}).get("towerJobs", {})
            if tower_jobs and isinstance(tower_jobs, dict):
                parsed_jobs: dict[str, Any] = {}
                for action_name, job_info in tower_jobs.items():
                    if not isinstance(job_info, dict):
                        continue
                    entry: dict[str, Any] = {}
                    if job_info.get("towerHost"):
                        entry["controller"] = job_info["towerHost"]
                    if job_info.get("deployerJob"):
                        entry["job_id"] = job_info["deployerJob"]
                    if job_info.get("jobStatus"):
                        entry["status"] = job_info["jobStatus"]
                    if job_info.get("completeTimestamp"):
                        entry["completed"] = job_info["completeTimestamp"]
                    if entry:
                        parsed_jobs[action_name] = entry
                if parsed_jobs:
                    comp["tower_jobs"] = parsed_jobs

        components.append(comp)

    return components


def _extract_anarchy_subject_info(subject: dict) -> dict:
    """Extract key info from an AnarchySubject, stripping secrets."""
    metadata = subject.get("metadata", {})
    spec = subject.get("spec", {})
    status = subject.get("status", {})

    as_vars = spec.get("vars", {})
    jv = as_vars.get("job_vars", {})

    annotations = metadata.get("annotations", {})

    result: dict[str, Any] = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "governor": spec.get("governor", ""),
        "current_state": as_vars.get("current_state", ""),
        "desired_state": as_vars.get("desired_state", ""),
        "healthy": as_vars.get("healthy"),
    }

    # Extract ResourceClaim reference from poolboy annotations
    claim_name = annotations.get("poolboy.gpte.redhat.com/resource-claim-name", "")
    claim_ns = annotations.get("poolboy.gpte.redhat.com/resource-claim-namespace", "")
    if claim_name:
        result["resource_claim"] = {"name": claim_name, "namespace": claim_ns}
    requester = annotations.get("poolboy.gpte.redhat.com/resource-requester-email", "")
    if requester:
        result["requester_email"] = requester

    instance_vars = _filter_job_vars(jv)
    result["instance_vars"] = instance_vars

    # Extract towerJobs from status — contains controller hostname and job IDs
    # for each lifecycle action (provision, destroy, stop, start)
    tower_jobs = status.get("towerJobs", {})
    if tower_jobs and isinstance(tower_jobs, dict):
        parsed_jobs: dict[str, Any] = {}
        for action_name, job_info in tower_jobs.items():
            if not isinstance(job_info, dict):
                continue
            entry: dict[str, Any] = {}
            if job_info.get("towerHost"):
                entry["towerHost"] = job_info["towerHost"]
            if job_info.get("deployerJob"):
                entry["deployerJob"] = job_info["deployerJob"]
            if job_info.get("completeJob"):
                entry["completeJob"] = job_info["completeJob"]
            if entry:
                parsed_jobs[action_name] = entry
        if parsed_jobs:
            result["tower_jobs"] = parsed_jobs

    return result


def _extract_resource_pool_info(pool: dict) -> dict:
    """Extract key info from a ResourcePool."""
    metadata = pool.get("metadata", {})
    spec = pool.get("spec", {})
    status = pool.get("status", {})

    result: dict[str, Any] = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "min_available": spec.get("minAvailable", 0),
        "max_unready": spec.get("maxUnready", 0),
        "lifespan": spec.get("lifespan", {}),
        "resource_handle_count": status.get("resourceHandleCount", 0),
    }

    # Extract provider name from resources list
    resources = spec.get("resources", [])
    if resources and isinstance(resources, list):
        r0 = resources[0]
        if isinstance(r0, dict):
            provider = r0.get("provider", {})
            if isinstance(provider, dict):
                result["provider_name"] = provider.get("name", "")
            result["resource_name"] = r0.get("name", "")

    return result


def _extract_workshop_info(workshop: dict) -> dict:
    """Extract key info from a Workshop."""
    metadata = workshop.get("metadata", {})
    spec = workshop.get("spec", {})
    status = workshop.get("status", {})
    labels = metadata.get("labels", {})

    provision_count = status.get("provisionCount", {})
    if not isinstance(provision_count, dict):
        provision_count = {}

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "catalog_item": labels.get("babylon.gpte.redhat.com/catalogItemName", ""),
        "workshop_id": labels.get("babylon.gpte.redhat.com/workshop-id", ""),
        "display_name": spec.get("displayName", ""),
        "description": spec.get("description", ""),
        "open_registration": spec.get("openRegistration", False),
        "lifespan": spec.get("lifespan", {}),
        "provision_count": {
            "ordered": provision_count.get("ordered", 0),
            "active": provision_count.get("active", 0),
            "failed": provision_count.get("failed", 0),
            "retries": provision_count.get("retries", 0),
        },
        "user_count": status.get("userCount", 0),
        "user_assignments_count": (
            len(status.get("userAssignments", {}))
            if isinstance(status.get("userAssignments"), dict)
            else 0
        ),
        "resource_claims_count": (
            len(status.get("resourceClaims", {}))
            if isinstance(status.get("resourceClaims"), dict)
            else 0
        ),
    }


def _extract_anarchy_action_info(action: dict) -> dict:
    """Extract key info from an AnarchyAction."""
    metadata = action.get("metadata", {})
    spec = action.get("spec", {})
    status = action.get("status", {})

    subject_ref = spec.get("subjectRef", {})

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "action": spec.get("action", ""),
        "after": spec.get("after", ""),
        "subject_name": subject_ref.get("name", ""),
        "subject_namespace": subject_ref.get("namespace", ""),
        "governor": spec.get("governorRef", {}).get("name", ""),
        "state": status.get("state", ""),
        "finished": status.get("finishedTimestamp", ""),
    }


def _extract_multi_workshop_info(mw: dict) -> dict:
    """Extract key info from a MultiWorkshop."""
    metadata = mw.get("metadata", {})
    spec = mw.get("spec", {})

    assets = spec.get("assets", [])
    parsed_assets = []
    for a in assets:
        if isinstance(a, dict):
            parsed_assets.append(
                {
                    "name": a.get("name", ""),
                    "display_name": a.get("displayName", ""),
                    "key": a.get("key", ""),
                    "type": a.get("type", ""),
                    "namespace": a.get("namespace", ""),
                    "workshop_id": a.get("workshopId", ""),
                }
            )

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "display_name": spec.get("displayName", spec.get("name", "")),
        "number_seats": spec.get("numberSeats", 0),
        "start_date": spec.get("startDate", ""),
        "end_date": spec.get("endDate", ""),
        "purpose": spec.get("purpose", ""),
        "assets": parsed_assets,
        "asset_count": len(parsed_assets),
    }


async def query_babylon_catalog(
    action: str,
    cluster: str = "",
    name: str = "",
    search: str = "",
    namespace: str = "",
    sandbox_comment: str = "",
    env_type: str = "",
    account_id: str = "",
    guid: str = "",
    max_results: int = 50,
) -> dict:
    """Query a Babylon cluster for catalog and deployment data.

    Args:
        action: Action to perform (search_catalog, get_component, list_deployments,
                get_deployment, list_anarchy_subjects, list_resource_pools,
                list_workshops, list_anarchy_actions).
        cluster: Babylon cluster name. If empty, resolved from sandbox_comment
                 or uses default.
        name: Resource name for get actions.
        search: Search term for search/list actions (case-insensitive contains).
        namespace: Namespace for scoped queries. For deployments, specify the
                   user namespace (e.g., "clusterplatform-prod").
        sandbox_comment: Sandbox DynamoDB comment field — used to resolve
                         which Babylon cluster to query.
        env_type: Filter by env_type (for search/list actions).
        account_id: Filter deployments by sandbox account ID.
        guid: Filter deployments or AnarchySubjects by GUID.
        max_results: Maximum results to return. Default: 50.

    Returns:
        Dict with action-specific results.
    """
    configured = get_configured_clusters()
    if not configured:
        return {"error": "No Babylon clusters configured. Set babylon.clusters in config."}

    # Resolve which cluster to query
    target_cluster = cluster
    if not target_cluster and sandbox_comment:
        target_cluster = resolve_cluster_from_comment(sandbox_comment)

    # For GUID-based searches, search all clusters until found
    if not target_cluster and guid and action in ("list_anarchy_subjects", "list_anarchy_actions"):
        return await _search_all_clusters_for_guid(
            action, configured, namespace, search, guid, max_results
        )

    # For get_multiworkshop, search all clusters if no cluster specified
    if not target_cluster and action == "get_multiworkshop" and name and namespace:
        return await _search_all_clusters_for_multiworkshop(configured, name, namespace)

    if not target_cluster:
        return {
            "error": "Could not determine which Babylon cluster to query. "
            "Either specify 'cluster' directly, or pass 'sandbox_comment' "
            "(from query_aws_account_db) to auto-resolve. "
            f"Configured clusters: {configured}"
        }

    try:
        if action == "search_catalog":
            return await _search_catalog(target_cluster, search, env_type, max_results)
        elif action == "get_component":
            return await _get_component(target_cluster, name)
        elif action == "list_deployments":
            return await _list_deployments(
                target_cluster, namespace, search, account_id, guid, max_results
            )
        elif action == "get_deployment":
            return await _get_deployment(target_cluster, name, namespace)
        elif action == "list_anarchy_subjects":
            return await _list_anarchy_subjects(
                target_cluster, namespace, search, guid, max_results
            )
        elif action == "list_resource_pools":
            return await _list_resource_pools(target_cluster, search, max_results)
        elif action == "list_workshops":
            return await _list_workshops(target_cluster, namespace, search, max_results)
        elif action == "list_anarchy_actions":
            return await _list_anarchy_actions(target_cluster, namespace, search, guid, max_results)
        elif action == "list_multiworkshops":
            return await _list_multiworkshops(target_cluster, namespace, search, max_results)
        elif action == "get_multiworkshop":
            return await _get_multiworkshop(target_cluster, name, namespace)
        elif action == "get_babylon_pod_logs":
            return await _get_babylon_pod_logs(
                target_cluster, namespace, name, search, guid, max_results
            )
        else:
            return {
                "error": f"Unknown action: {action}. Use: search_catalog, "
                "get_component, list_deployments, get_deployment, "
                "list_anarchy_subjects, list_resource_pools, list_workshops, "
                "list_multiworkshops, get_multiworkshop, list_anarchy_actions, "
                "get_babylon_pod_logs"
            }
    except Exception as e:
        logger.exception("Babylon query failed: action=%s cluster=%s", action, target_cluster)
        return {"error": f"Babylon query failed: {e}", "cluster": target_cluster}


async def _search_all_clusters_for_guid(
    action: str,
    clusters: list[str],
    namespace: str,
    search: str,
    guid: str,
    max_results: int,
) -> dict:
    """Search all configured clusters for a GUID, stopping when found."""
    errors: list[str] = []

    for cluster_name in clusters:
        try:
            if action == "list_anarchy_subjects":
                result = await _list_anarchy_subjects(
                    cluster_name, namespace, search, guid, max_results
                )
            else:
                result = await _list_anarchy_actions(
                    cluster_name, namespace, search, guid, max_results
                )

            items_key = "subjects" if action == "list_anarchy_subjects" else "actions"
            if result.get(items_key):
                return result
            if result.get("errors"):
                errors.extend(f"{cluster_name}: {e}" for e in result["errors"])
        except Exception as e:
            errors.append(f"{cluster_name}: {e}")

    return {
        "clusters_searched": clusters,
        "subjects" if action == "list_anarchy_subjects" else "actions": [],
        "count": 0,
        "truncated": False,
        "errors": errors if errors else None,
    }


async def _search_catalog(cluster: str, search: str, env_type: str, max_results: int) -> dict:
    """Search CatalogItems across babylon-catalog-* namespaces."""
    all_items: list[dict] = []

    # Fetch from all catalog namespaces in parallel
    tasks = []
    for ns in CATALOG_NAMESPACES:
        tasks.append(
            k8s_list(cluster, CATALOG_ITEM_GROUP, CATALOG_ITEM_VERSION, CATALOG_ITEM_PLURAL, ns)
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            logger.warning("Failed to list CatalogItems from %s: %s", CATALOG_NAMESPACES[i], result)
            continue
        for item in result.get("items", []):
            parsed = _parse_catalog_item(item)
            all_items.append(parsed)

    # Apply filters
    search_lower = search.lower() if search else ""
    env_type_lower = env_type.lower() if env_type else ""
    filtered = []

    for item in all_items:
        if search_lower:
            searchable = f"{item['ci_name']} {item['display_name']} {' '.join(item.get('keywords', []))}".lower()
            if search_lower not in searchable:
                continue
        if env_type_lower and env_type_lower not in item["ci_name"].lower():
            continue
        filtered.append(item)
        if len(filtered) >= max_results:
            break

    return {
        "cluster": cluster,
        "items": filtered,
        "count": len(filtered),
        "total_scanned": len(all_items),
        "truncated": len(filtered) >= max_results,
    }


async def _get_component(cluster: str, name: str) -> dict:
    """Get an AgnosticVComponent definition with extracted instance info."""
    if not name:
        return {"error": "name is required for get_component"}

    normalized = _normalize_ci_name(name)
    # Try with and without .prod suffix
    candidates = [normalized]
    if "." not in normalized or normalized.count(".") < 2:
        candidates.append(f"{normalized}.prod")
        candidates.append(f"{normalized}.event")
        candidates.append(f"{normalized}.dev")

    last_error = None
    for candidate in candidates:
        try:
            result = await k8s_get_resource(
                cluster, AVC_GROUP, AVC_VERSION, AVC_PLURAL, AVC_NAMESPACE, candidate
            )
            spec = result.get("spec", {})
            defn = spec.get("definition", {})
            sanitized = _strip_secrets(defn)

            # AgnosticV repo reference (for fetch_github_file)
            agnosticv_repo = spec.get("agnosticvRepo", "")
            agnosticv_path = spec.get("path", "")

            # Extract instance info
            expected_instances = _extract_instance_info(sanitized)

            # Extract deployer info from __meta__
            meta = sanitized.get("__meta__", {})
            deployer = meta.get("deployer", {}) or {}
            scm_url = deployer.get("scm_url", "") or sanitized.get("scm_url", "") or ""
            scm_ref = (
                deployer.get("scm_ref", "")
                or deployer.get("scm_branch", "")
                or sanitized.get("scm_ref", "")
                or ""
            )

            # Get key fields
            component_result: dict[str, Any] = {
                "cluster": cluster,
                "name": candidate,
                "cloud_provider": sanitized.get("cloud_provider", ""),
                "env_type": sanitized.get("env_type", ""),
                "platform": sanitized.get("platform", ""),
                "expected_instances": expected_instances,
            }

            # Add deployer/source info if available
            if scm_url:
                component_result["scm_url"] = scm_url
            if scm_ref:
                component_result["scm_ref"] = scm_ref

            # Add agnosticv repo reference for config lookup
            if agnosticv_repo:
                component_result["agnosticv_repo"] = agnosticv_repo
            if agnosticv_path:
                component_result["agnosticv_path"] = agnosticv_path

            # Extract component composition from __meta__.components
            components = meta.get("components", [])
            if components and isinstance(components, list):
                component_result["sub_components"] = [
                    {
                        "name": c.get("name", ""),
                        "item": c.get("item", ""),
                    }
                    for c in components
                    if isinstance(c, dict)
                ]

            component_result["definition"] = sanitized
            return component_result
        except Exception as e:
            last_error = e
            continue

    return {
        "error": f"AgnosticVComponent not found: tried {candidates}. Error: {last_error}",
        "cluster": cluster,
    }


async def _list_deployments(
    cluster: str,
    namespace: str,
    search: str,
    account_id: str,
    guid: str,
    max_results: int,
) -> dict:
    """List active ResourceClaims (deployments)."""
    if not namespace:
        return {
            "error": "namespace is required for list_deployments. "
            "Use the user's provision namespace (visible in ResourceClaim metadata). "
            "Common namespaces: 'clusterplatform-prod', or search AnarchySubjects by GUID first."
        }

    try:
        result = await k8s_list(
            cluster, RESOURCE_CLAIM_GROUP, RESOURCE_CLAIM_VERSION, RESOURCE_CLAIM_PLURAL, namespace
        )
    except Exception as e:
        return {"error": f"Failed to list ResourceClaims in {namespace}: {e}", "cluster": cluster}

    items = []
    search_lower = search.lower() if search else ""
    account_id_str = account_id or ""
    guid_str = guid or ""

    for item in result.get("items", []):
        info = _extract_deployment_info(item)

        # Apply filters
        if (
            search_lower
            and search_lower not in info["name"].lower()
            and search_lower not in info.get("catalog_item", "").lower()
        ):
            continue
        if account_id_str:
            pdata = info.get("provision_data", {})
            if pdata.get("sandbox_account_id", "") != account_id_str:
                continue
        if guid_str:
            pdata = info.get("provision_data", {})
            if pdata.get("guid", "") != guid_str:
                iv = info.get("instance_vars", {})
                if iv.get("guid", "") != guid_str:
                    continue

        items.append(info)
        if len(items) >= max_results:
            break

    return {
        "cluster": cluster,
        "namespace": namespace,
        "deployments": items,
        "count": len(items),
        "truncated": len(items) >= max_results,
    }


async def _get_deployment(cluster: str, name: str, namespace: str) -> dict:
    """Get a specific ResourceClaim with full details."""
    if not name or not namespace:
        return {"error": "Both name and namespace are required for get_deployment"}

    try:
        result = await k8s_get_resource(
            cluster,
            RESOURCE_CLAIM_GROUP,
            RESOURCE_CLAIM_VERSION,
            RESOURCE_CLAIM_PLURAL,
            namespace,
            name,
        )
    except Exception as e:
        return {"error": f"ResourceClaim not found: {name} in {namespace}: {e}", "cluster": cluster}

    info = _extract_deployment_info(result)
    return {"cluster": cluster, "deployment": info}


async def _list_anarchy_subjects(
    cluster: str,
    namespace: str,
    search: str,
    guid: str,
    max_results: int,
) -> dict:
    """List AnarchySubjects (active provisions).

    When searching by GUID or search term, uses cluster-wide listing for
    efficiency (works across all babylon-anarchy-* namespaces automatically).
    When a specific namespace is given, queries only that namespace.
    """
    all_subjects: list[dict] = []
    errors: list[str] = []
    namespaces_searched: list[str] | str = "all (cluster-wide)"

    if namespace:
        # Scoped to a specific namespace
        namespaces_searched = [namespace]
        try:
            result = await k8s_list(
                cluster,
                ANARCHY_SUBJECT_GROUP,
                ANARCHY_SUBJECT_VERSION,
                ANARCHY_SUBJECT_PLURAL,
                namespace,
            )
            all_subjects.extend(result.get("items", []))
        except Exception as e:
            errors.append(f"{namespace}: {e}")
    else:
        # Cluster-wide listing — covers all anarchy namespaces automatically.
        try:
            result = await k8s_list_cluster_wide(
                cluster,
                ANARCHY_SUBJECT_GROUP,
                ANARCHY_SUBJECT_VERSION,
                ANARCHY_SUBJECT_PLURAL,
            )
            all_subjects.extend(result.get("items", []))
        except Exception as e:
            errors.append(f"cluster-wide: {e}")

    # Apply filters
    search_lower = search.lower() if search else ""
    guid_str = guid or ""
    filtered = []

    for item in all_subjects:
        info = _extract_anarchy_subject_info(item)

        if (
            search_lower
            and search_lower not in info["name"].lower()
            and search_lower not in info.get("governor", "").lower()
        ):
            continue
        if guid_str:
            iv = info.get("instance_vars", {})
            if iv.get("guid", "") != guid_str and guid_str not in info["name"]:
                continue

        # When searching by GUID, include all states (including destroy-failed).
        # Otherwise, skip fully completed subjects to reduce noise.
        if not guid_str:
            state = info.get("current_state", "")
            if state in ("destroyed",):
                continue

        filtered.append(info)
        if len(filtered) >= max_results:
            break

    return {
        "cluster": cluster,
        "subjects": filtered,
        "count": len(filtered),
        "truncated": len(filtered) >= max_results,
        "namespaces_searched": namespaces_searched,
        "errors": errors if errors else None,
    }


async def _list_resource_pools(
    cluster: str,
    search: str,
    max_results: int,
) -> dict:
    """List ResourcePools from the poolboy namespace."""
    try:
        result = await k8s_list(
            cluster,
            RESOURCE_POOL_GROUP,
            RESOURCE_POOL_VERSION,
            RESOURCE_POOL_PLURAL,
            RESOURCE_POOL_NAMESPACE,
        )
    except Exception as e:
        return {"error": f"Failed to list ResourcePools: {e}", "cluster": cluster}

    search_lower = search.lower() if search else ""
    pools = []

    for item in result.get("items", []):
        info = _extract_resource_pool_info(item)
        if search_lower and search_lower not in info["name"].lower():
            continue
        pools.append(info)
        if len(pools) >= max_results:
            break

    return {
        "cluster": cluster,
        "pools": pools,
        "count": len(pools),
        "truncated": len(pools) >= max_results,
    }


async def _list_workshops(
    cluster: str,
    namespace: str,
    search: str,
    max_results: int,
) -> dict:
    """List Workshops. If namespace is given, scoped to that namespace."""
    if not namespace:
        return {
            "error": "namespace is required for list_workshops. "
            "Workshop namespaces are user-scoped (e.g. 'user-jdoe-redhat-com'). "
            "Use search with list_anarchy_subjects to find the namespace first."
        }

    try:
        result = await k8s_list(
            cluster, WORKSHOP_GROUP, WORKSHOP_VERSION, WORKSHOP_PLURAL, namespace
        )
    except Exception as e:
        return {"error": f"Failed to list Workshops in {namespace}: {e}", "cluster": cluster}

    search_lower = search.lower() if search else ""
    workshops = []

    for item in result.get("items", []):
        info = _extract_workshop_info(item)
        if (
            search_lower
            and search_lower not in info["name"].lower()
            and search_lower not in info.get("catalog_item", "").lower()
            and search_lower not in info.get("display_name", "").lower()
        ):
            continue
        workshops.append(info)
        if len(workshops) >= max_results:
            break

    return {
        "cluster": cluster,
        "namespace": namespace,
        "workshops": workshops,
        "count": len(workshops),
        "truncated": len(workshops) >= max_results,
    }


async def _list_anarchy_actions(
    cluster: str,
    namespace: str,
    search: str,
    guid: str,
    max_results: int,
) -> dict:
    """List AnarchyActions (provision/start/stop/destroy lifecycle events)."""
    all_actions: list[dict] = []
    errors: list[str] = []
    namespaces_searched: list[str] | str = "all (cluster-wide)"

    if namespace:
        namespaces_searched = [namespace]
        try:
            result = await k8s_list(
                cluster,
                ANARCHY_ACTION_GROUP,
                ANARCHY_ACTION_VERSION,
                ANARCHY_ACTION_PLURAL,
                namespace,
            )
            all_actions.extend(result.get("items", []))
        except Exception as e:
            errors.append(f"{namespace}: {e}")
    else:
        try:
            result = await k8s_list_cluster_wide(
                cluster,
                ANARCHY_ACTION_GROUP,
                ANARCHY_ACTION_VERSION,
                ANARCHY_ACTION_PLURAL,
                limit=500,
            )
            all_actions.extend(result.get("items", []))
        except Exception as e:
            errors.append(f"cluster-wide: {e}")

    search_lower = search.lower() if search else ""
    guid_str = guid or ""
    filtered = []

    for item in all_actions:
        info = _extract_anarchy_action_info(item)

        if (
            search_lower
            and search_lower not in info["name"].lower()
            and search_lower not in info.get("subject_name", "").lower()
            and search_lower not in info.get("action", "").lower()
        ):
            continue
        if guid_str and guid_str not in info.get("subject_name", ""):
            continue

        filtered.append(info)
        if len(filtered) >= max_results:
            break

    return {
        "cluster": cluster,
        "actions": filtered,
        "count": len(filtered),
        "truncated": len(filtered) >= max_results,
        "namespaces_searched": namespaces_searched,
        "errors": errors if errors else None,
    }


async def _list_multiworkshops(
    cluster: str,
    namespace: str,
    search: str,
    max_results: int,
) -> dict:
    """List MultiWorkshops in a namespace."""
    if not namespace:
        return {
            "error": "namespace is required for list_multiworkshops. "
            "MultiWorkshop namespaces are user-scoped (e.g. 'user-jdoe-redhat-com')."
        }

    try:
        result = await k8s_list(
            cluster,
            MULTI_WORKSHOP_GROUP,
            MULTI_WORKSHOP_VERSION,
            MULTI_WORKSHOP_PLURAL,
            namespace,
        )
    except Exception as e:
        return {
            "error": f"Failed to list MultiWorkshops in {namespace}: {e}",
            "cluster": cluster,
        }

    search_lower = search.lower() if search else ""
    multiworkshops = []

    for item in result.get("items", []):
        info = _extract_multi_workshop_info(item)
        if (
            search_lower
            and search_lower not in info["name"].lower()
            and search_lower not in info.get("display_name", "").lower()
        ):
            continue
        multiworkshops.append(info)
        if len(multiworkshops) >= max_results:
            break

    return {
        "cluster": cluster,
        "namespace": namespace,
        "multiworkshops": multiworkshops,
        "count": len(multiworkshops),
        "truncated": len(multiworkshops) >= max_results,
    }


async def _get_multiworkshop(
    cluster: str,
    name: str,
    namespace: str,
) -> dict:
    """Get a MultiWorkshop and traverse its full hierarchy.

    Fetches the MultiWorkshop, then each child Workshop (via label selector),
    then each Workshop's ResourceClaims, extracting all AnarchySubject
    components and their tower job references.
    """
    if not name:
        return {"error": "name is required for get_multiworkshop."}
    if not namespace:
        return {
            "error": "namespace is required for get_multiworkshop. "
            "MultiWorkshop namespaces are user-scoped (e.g. 'user-jdoe-redhat-com')."
        }

    # 1. Fetch the MultiWorkshop
    try:
        mw = await k8s_get_resource(
            cluster,
            MULTI_WORKSHOP_GROUP,
            MULTI_WORKSHOP_VERSION,
            MULTI_WORKSHOP_PLURAL,
            namespace,
            name,
        )
    except Exception as e:
        return {
            "error": f"Failed to get MultiWorkshop {name} in {namespace}: {e}",
            "cluster": cluster,
        }

    mw_meta = mw.get("metadata", {})
    mw_spec = mw.get("spec", {})
    mw_annotations = mw_meta.get("annotations", {})

    result: dict[str, Any] = {
        "cluster": cluster,
        "name": mw_meta.get("name", ""),
        "namespace": mw_meta.get("namespace", ""),
        "display_name": mw_spec.get("displayName", mw_spec.get("name", "")),
        "requester": mw_annotations.get("demo.redhat.com/requester", ""),
        "seats": mw_spec.get("numberSeats", 0),
        "start_date": mw_spec.get("startDate", ""),
        "end_date": mw_spec.get("endDate", ""),
        "purpose": mw_spec.get("purpose", ""),
    }

    # 2. List child Workshops via label selector
    try:
        ws_result = await k8s_list(
            cluster,
            WORKSHOP_GROUP,
            WORKSHOP_VERSION,
            WORKSHOP_PLURAL,
            namespace,
            label_selector=f"babylon.gpte.redhat.com/multiworkshop={name}",
        )
    except Exception as e:
        result["error"] = f"Failed to list child Workshops: {e}"
        result["workshops"] = []
        return result

    workshops_data: list[dict] = []
    total_failed = 0
    total_active = 0

    for ws in ws_result.get("items", []):
        ws_meta = ws.get("metadata", {})
        ws_spec = ws.get("spec", {})
        ws_status = ws.get("status", {})
        ws_labels = ws_meta.get("labels", {})

        provision_count = ws_status.get("provisionCount", {})
        if not isinstance(provision_count, dict):
            provision_count = {}

        failed = provision_count.get("failed", 0)
        active = provision_count.get("active", 0)
        total_failed += failed
        total_active += active

        ws_info: dict[str, Any] = {
            "name": ws_meta.get("name", ""),
            "display_name": ws_spec.get("displayName", ""),
            "catalog_item": ws_labels.get("babylon.gpte.redhat.com/catalogItemName", ""),
            "workshop_id": ws_labels.get("babylon.gpte.redhat.com/workshop-id", ""),
            "provision_count": {
                "ordered": provision_count.get("ordered", 0),
                "active": active,
                "failed": failed,
                "retries": provision_count.get("retries", 0),
            },
        }

        # 3. Fetch each ResourceClaim referenced by this Workshop
        rc_refs = ws_status.get("resourceClaims", {})
        if not isinstance(rc_refs, dict):
            rc_refs = {}

        resource_claims: list[dict] = []
        for rc_name in rc_refs:
            try:
                rc = await k8s_get_resource(
                    cluster,
                    RESOURCE_CLAIM_GROUP,
                    RESOURCE_CLAIM_VERSION,
                    RESOURCE_CLAIM_PLURAL,
                    namespace,
                    rc_name,
                )
            except Exception as e:
                resource_claims.append({"name": rc_name, "error": str(e)})
                continue

            rc_status = rc.get("status", {})
            rc_info: dict[str, Any] = {
                "name": rc_name,
                "state": rc_status.get("summary", {}).get("state", "unknown"),
                "healthy": rc_status.get("healthy"),
                "ready": rc_status.get("ready"),
            }

            # 4. Extract ALL resource components (AnarchySubjects)
            rc_info["resources"] = _extract_resource_components(rc)
            resource_claims.append(rc_info)

        ws_info["resource_claims"] = resource_claims
        workshops_data.append(ws_info)

    result["summary"] = {
        "total_workshops": len(workshops_data),
        "active": total_active,
        "failed": total_failed,
    }
    result["workshops"] = workshops_data

    return result


async def _search_all_clusters_for_multiworkshop(
    clusters: list[str],
    name: str,
    namespace: str,
) -> dict:
    """Search all configured clusters for a MultiWorkshop by name."""
    errors: list[str] = []

    for cluster_name in clusters:
        try:
            result = await _get_multiworkshop(cluster_name, name, namespace)
            if "error" not in result or "workshops" in result:
                return result
        except Exception as e:
            errors.append(f"{cluster_name}: {e}")

    return {
        "error": f"MultiWorkshop '{name}' not found on any cluster. "
        f"Searched: {clusters}. Errors: {errors}",
    }


async def _get_babylon_pod_logs(
    cluster: str,
    namespace: str,
    name: str,
    search: str,
    guid: str,
    max_results: int,
) -> dict:
    """Get pod logs from a Babylon cluster.

    Lists pods in a namespace, optionally filtered by name/label/guid,
    then fetches logs from matching pods. Returns structured results
    with pod metadata and log lines.

    Args:
        cluster: Babylon cluster name.
        namespace: Kubernetes namespace (required, e.g. "poolboy").
        name: Pod name substring filter (optional).
        search: Text to grep for in logs (optional).
        guid: GUID to grep for in logs (optional).
        max_results: Max log lines per pod.
    """
    if not namespace:
        return {"error": "namespace is required for get_babylon_pod_logs"}

    # List pods in the namespace
    try:
        pod_list = await k8s_get(cluster, f"/api/v1/namespaces/{namespace}/pods?limit=100")
    except Exception as e:
        error_msg = str(e)
        if "Forbidden" in error_msg or "403" in error_msg:
            return {
                "error": f"No permission to list pods in {namespace} on {cluster}. "
                "The rhdp-readonly SA needs pods/log access (pending RBAC update).",
                "cluster": cluster,
                "namespace": namespace,
            }
        return {"error": f"Failed to list pods in {namespace}: {e}", "cluster": cluster}

    pods = pod_list.get("items", [])
    if not pods:
        return {
            "cluster": cluster,
            "namespace": namespace,
            "pods": [],
            "message": f"No pods found in {namespace}",
        }

    # Filter pods by name if specified
    if name:
        name_lower = name.lower()
        pods = [p for p in pods if name_lower in p["metadata"]["name"].lower()]

    # Cap pods to avoid fetching too many logs
    max_pods = 5
    pod_results = []

    for pod in pods[:max_pods]:
        pod_name = pod["metadata"]["name"]
        pod_phase = pod.get("status", {}).get("phase", "Unknown")
        containers = [c["name"] for c in pod.get("spec", {}).get("containers", [])]

        # Fetch logs (tail limited)
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

        # Filter log lines by search term or GUID if specified
        lines = log_text.splitlines()
        grep_term = search or guid or ""
        if grep_term:
            grep_lower = grep_term.lower()
            lines = [ln for ln in lines if grep_lower in ln.lower()]

        # Strip secrets from log lines
        filtered_lines = []
        for ln in lines[:max_results]:
            if _SECRET_PATTERNS.search(ln):
                continue
            filtered_lines.append(ln)

        pod_results.append(
            {
                "pod": pod_name,
                "phase": pod_phase,
                "containers": containers,
                "log_lines": len(filtered_lines),
                "logs": "\n".join(filtered_lines) if filtered_lines else "(no matching lines)",
                "grep": grep_term or "(none)",
            }
        )

    return {
        "cluster": cluster,
        "namespace": namespace,
        "total_pods": len(pods),
        "pods_shown": len(pod_results),
        "results": pod_results,
    }
