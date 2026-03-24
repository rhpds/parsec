"""Tool: query_splunk — search Splunk for Babylon/AAP2 logs."""

import logging
from typing import Any

from src.connections.splunk import get_splunk_client

logger = logging.getLogger(__name__)

# Splunk index names for RHDP logs.
# Data lives on Splunk Cloud (rhcorporate.splunkcloud.com) but API access goes
# through the on-prem REST endpoint (splunk-api.corp.redhat.com:8089) using
# federated search. The "federated:" prefix routes queries to the Cloud instance.
OCP_APP_INDEX = "federated:rh_pds-001_ocp_app"
OCP_INFRA_INDEX = "federated:rh_pds-001_ocp_infra"
AAP_INDEX = "federated:rh_pds-001_aap"


def _sanitize_query_value(value: str) -> str:
    """Sanitize a value for use in Splunk queries to prevent injection."""
    return value.replace('"', "").replace("'", "").replace("\\", "")


_DANGEROUS_COMMANDS = [
    "| delete",
    "| outputlookup",
    "| sendalert",
    "| collect",
    "| outputcsv",
    "| sendemail",
]


def _build_ocp_query(
    namespace_filter: str,
    cluster_name: str,
    errors_only: bool,
    search_terms: str,
) -> str:
    """Build an OCP app log query with common filters.

    Federated search returns raw JSON in _raw without auto-extracting fields.
    We use raw text matching for pre-spath filters and add | spath to extract
    fields for the result set.
    """
    query = f"index={OCP_APP_INDEX} {namespace_filter}"
    if cluster_name:
        cluster_name = _sanitize_query_value(cluster_name)
        query += f' "{cluster_name}"'
    if errors_only:
        query += ' ("error" OR "fatal" OR "warning")'
    if search_terms:
        search_terms = _sanitize_query_value(search_terms)
        query += f' "{search_terms}"'
    return query + " | spath | sort -_time"


def _build_query(  # noqa: C901
    action: str,
    guid: str,
    namespace: str,
    cluster_name: str,
    controller: str,
    search_terms: str,
    errors_only: bool,
    raw_query: str,
) -> tuple[str, str] | dict[str, Any]:
    """Build SPL query for the given action. Returns (query, index) or error dict."""
    if action == "search_by_guid":
        if not guid:
            return {"error": "guid is required for search_by_guid action"}
        guid = _sanitize_query_value(guid)
        # GUID appears in namespace_name in _raw JSON — use raw text match
        ns_filter = f'"{guid}"'
        return _build_ocp_query(ns_filter, cluster_name, errors_only, search_terms), OCP_APP_INDEX

    if action == "search_namespace":
        if not namespace:
            return {"error": "namespace is required for search_namespace action"}
        namespace = _sanitize_query_value(namespace)
        ns_filter = f'"{namespace}"'
        return _build_ocp_query(ns_filter, cluster_name, errors_only, search_terms), OCP_APP_INDEX

    if action == "search_aap2_logs":
        if not controller:
            return {"error": "controller is required for search_aap2_logs action"}
        controller = _sanitize_query_value(controller)
        # Federated search: use raw text match, then spath for field extraction
        query = f'index={AAP_INDEX} "{controller}"'
        if guid:
            guid = _sanitize_query_value(guid)
            query += f' "{guid}"'
        if errors_only:
            query += ' ("ERROR" OR "CRITICAL" OR "WARNING" OR "failed")'
        if search_terms:
            search_terms = _sanitize_query_value(search_terms)
            query += f' "{search_terms}"'
        return query + " | spath | sort -_time", AAP_INDEX

    if action == "search_raw":
        if not raw_query:
            return {"error": "raw_query is required for search_raw action"}
        stripped = raw_query.strip()
        if not stripped.startswith("search ") and not stripped.startswith("|"):
            return {"error": "raw_query must start with 'search' or '|'"}
        lower = stripped.lower()
        for cmd in _DANGEROUS_COMMANDS:
            if cmd in lower:
                return {"error": f"Dangerous command '{cmd}' not allowed in raw queries"}
        return stripped, "custom"

    return {
        "error": f"Unknown action: {action}",
        "valid_actions": ["search_by_guid", "search_namespace", "search_aap2_logs", "search_raw"],
    }


async def query_splunk(
    action: str,
    guid: str = "",
    namespace: str = "",
    cluster_name: str = "",
    controller: str = "",
    search_terms: str = "",
    earliest: str = "-24h",
    latest: str = "now",
    errors_only: bool = False,
    raw_query: str = "",
    max_results: int = 200,
) -> dict[str, Any]:
    """Query Splunk for Babylon/AAP2 logs."""
    try:
        client = get_splunk_client()
    except RuntimeError:
        return {
            "error": "Splunk not configured. Set splunk.host and splunk.token in config.",
            "hint": "See config.yaml for Splunk configuration options.",
        }

    max_results = min(max_results, 500)

    query_result = _build_query(
        action, guid, namespace, cluster_name, controller, search_terms, errors_only, raw_query
    )
    if isinstance(query_result, dict):
        return query_result
    query, target_index = query_result

    logger.info(
        "Splunk query [%s]: %s (earliest=%s, latest=%s)",
        action,
        query[:200],
        earliest,
        latest,
    )

    result = await client.run_search(
        query=query,
        earliest=earliest,
        latest=latest,
        max_results=max_results,
    )

    if "error" in result:
        return result

    results = result.get("results", [])
    if action in ("search_by_guid", "search_namespace"):
        results = _slim_ocp_results(results)
    elif action == "search_aap2_logs":
        results = _slim_aap2_results(results)

    return {
        "results": results,
        "result_count": len(results),
        "total_count": result.get("total_count", 0),
        "truncated": result.get("truncated", False),
        "query": query,
        "index": target_index,
    }


def _slim_ocp_results(results: list[dict]) -> list[dict]:
    """Extract key fields from OCP app log results for readability."""
    slimmed = []
    for r in results:
        slimmed.append(
            {
                "time": r.get("_time", ""),
                "namespace": r.get("kubernetes.namespace_name", ""),
                "pod": r.get("kubernetes.pod_name", ""),
                "container": r.get("kubernetes.container_name", ""),
                "image": r.get("kubernetes.container_image", ""),
                "level": r.get("level", ""),
                "message": r.get("message", ""),
                "cluster": r.get("openshift.labels.cluster_name", ""),
            }
        )
    return slimmed


def _slim_aap2_results(results: list[dict]) -> list[dict]:
    """Extract key fields from AAP2 log results for readability."""
    slimmed = []
    for r in results:
        entry: dict[str, Any] = {
            "time": r.get("_time", ""),
            "controller": r.get("cluster_host_id", ""),
            "level": r.get("level", ""),
            "logger": r.get("logger_name", ""),
            "message": r.get("message", ""),
        }
        task = r.get("event_data.task", "")
        if task:
            entry["task"] = task
            entry["role"] = r.get("event_data.role", "")
            entry["task_action"] = r.get("event_data.task_action", "")
            entry["playbook"] = r.get("event_data.playbook", "")
        stdout = r.get("stdout", "")
        if stdout:
            entry["stdout"] = stdout[:2000]
        slimmed.append(entry)
    return slimmed
