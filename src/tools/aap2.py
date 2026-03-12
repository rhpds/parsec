"""Tool: query_aap2 — query AAP2 controllers for job data."""

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from src.connections.aap2 import (
    api_get,
    api_paginate,
    get_configured_controllers,
    resolve_controller,
)
from src.tools.babylon import _SECRET_KEYS, _SECRET_PATTERNS

logger = logging.getLogger(__name__)


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


def _extract_variable(extra_vars: dict, key: str, default: str = "unknown") -> Any:
    """Extract a variable from extra_vars, checking common nested locations."""
    if not extra_vars or not isinstance(extra_vars, dict):
        return default

    if key in extra_vars:
        return extra_vars[key]

    jm = extra_vars.get("job_metadata", {})
    if isinstance(jm, dict) and key in jm:
        return jm[key]

    return default


def _extract_guid(extra_vars: dict, job_name: str) -> str:
    """Extract GUID from extra_vars or parse from job name."""
    guid = _extract_variable(extra_vars, "guid", "")
    if guid:
        return guid

    # Parse from job name pattern:
    # "RHPDS agd-v2.sovereign-cloud.prod-gm5ld-2-provision-..."
    match = re.search(r"-([0-9a-z]{5})-\d+-(?:provision|destroy|stop|start)", job_name)
    if match:
        return match.group(1)

    return "unknown"


def _parse_job_metadata(data: dict, cluster_name: str) -> dict:
    """Parse AAP2 job API response into a clean metadata dict."""
    extra_vars_raw = data.get("extra_vars", "{}")
    try:
        extra_vars = (
            json.loads(extra_vars_raw) if isinstance(extra_vars_raw, str) else extra_vars_raw
        )
    except (json.JSONDecodeError, TypeError):
        extra_vars = {}

    job_name = data.get("name", "")
    summary = data.get("summary_fields", {})

    elapsed = data.get("elapsed")
    duration = int(float(elapsed)) if elapsed else None

    result: dict[str, Any] = {
        "job_id": data.get("id"),
        "job_name": job_name,
        "status": data.get("status", ""),
        "failed": data.get("failed", False),
        "started": data.get("started", ""),
        "finished": data.get("finished", ""),
        "duration_seconds": duration,
        "launch_type": data.get("launch_type", ""),
        "job_explanation": data.get("job_explanation", ""),
        "template_name": summary.get("job_template", {}).get("name", ""),
        "env_type": _extract_variable(extra_vars, "env_type", "unknown"),
        "guid": _extract_guid(extra_vars, job_name),
        "action": _extract_variable(extra_vars, "ACTION", "unknown"),
        "cloud_provider": _extract_variable(extra_vars, "cloud_provider", "unknown"),
        "display_name": _extract_variable(extra_vars, "display_name", ""),
        "controller": cluster_name,
    }

    # Git context from __meta__.deployer in extra_vars
    deployer = (
        extra_vars.get("__meta__", {}).get("deployer", {}) if isinstance(extra_vars, dict) else {}
    )
    if deployer:
        result["git_url"] = deployer.get("scm_url", "")
        result["git_branch"] = deployer.get("scm_ref", deployer.get("scm_branch", ""))
        result["git_revision"] = deployer.get("scm_revision", "")

    return result


def _parse_event(event: dict) -> dict:
    """Parse an AAP2 job event into a clean dict."""
    event_data = event.get("event_data", {})
    if isinstance(event_data, str):
        try:
            event_data = json.loads(event_data)
        except (json.JSONDecodeError, TypeError):
            event_data = {}

    # Extract error message from event_data.res
    error_msg = ""
    res = event_data.get("res", {}) if isinstance(event_data, dict) else {}
    if isinstance(res, dict):
        error_msg = res.get("msg", "")
        if not error_msg:
            error_msg = res.get("module_stderr", "")

    # Truncate stdout to keep response manageable
    stdout = event.get("stdout", "") or ""
    if len(stdout) > 500:
        stdout = stdout[:500] + "... [truncated]"

    return {
        "event": event.get("event", ""),
        "task": event.get("task", ""),
        "play": event.get("play", ""),
        "role": event.get("role", ""),
        "host": event.get("host_name", ""),
        "failed": event.get("failed", False),
        "changed": event.get("changed", False),
        "stdout": stdout,
        "error_msg": str(error_msg)[:500] if error_msg else "",
        "counter": event.get("counter", 0),
    }


def _parse_job_summary(data: dict, cluster_name: str) -> dict:
    """Parse a job list entry into a summary dict."""
    elapsed = data.get("elapsed")
    duration = int(float(elapsed)) if elapsed else None
    summary = data.get("summary_fields", {})

    return {
        "job_id": data.get("id"),
        "job_name": data.get("name", ""),
        "status": data.get("status", ""),
        "failed": data.get("failed", False),
        "started": data.get("started", ""),
        "finished": data.get("finished", ""),
        "duration_seconds": duration,
        "template_name": summary.get("job_template", {}).get("name", ""),
        "controller": cluster_name,
    }


async def _get_job(cluster_name: str, job_id: int) -> dict:
    """Fetch job metadata from an AAP2 controller."""
    data = await api_get(cluster_name, f"/api/v2/jobs/{job_id}/")
    return _parse_job_metadata(data, cluster_name)


async def _get_job_events(
    cluster_name: str,
    job_id: int,
    failed_only: bool = False,
    changed_only: bool = False,
    max_results: int = 50,
) -> dict:
    """Fetch job events from an AAP2 controller."""
    params: dict[str, Any] = {"order_by": "counter"}
    if failed_only:
        params["failed"] = "true"
    if changed_only:
        params["changed"] = "true"

    raw_events = await api_paginate(
        cluster_name,
        f"/api/v2/jobs/{job_id}/job_events/",
        params=params,
        max_results=max_results,
    )

    # Filter to task-level events (unless they failed — always include those)
    events = []
    for e in raw_events:
        event_type = e.get("event", "")
        if event_type in (
            "runner_on_ok",
            "runner_on_failed",
            "runner_on_unreachable",
            "runner_on_skipped",
            "runner_retry",
        ) or e.get("failed"):
            events.append(_parse_event(e))

    return {
        "job_id": job_id,
        "controller": cluster_name,
        "event_count": len(events),
        "filters": {
            "failed_only": failed_only,
            "changed_only": changed_only,
        },
        "events": events,
    }


async def _find_jobs_on_controller(
    cluster_name: str,
    status: str = "",
    created_after: str = "",
    created_before: str = "",
    template_name: str = "",
    max_results: int = 50,
) -> list[dict]:
    """Search for jobs on a single AAP2 controller."""
    params: dict[str, Any] = {"order_by": "-finished"}
    if status:
        params["status"] = status
    if created_after:
        params["created__gt"] = created_after
    if created_before:
        params["created__lt"] = created_before
    if template_name:
        params["name__icontains"] = template_name

    raw_jobs = await api_paginate(
        cluster_name, "/api/v2/jobs/", params=params, max_results=max_results
    )
    return [_parse_job_summary(j, cluster_name) for j in raw_jobs]


async def _find_jobs(
    controller: str = "",
    status: str = "",
    created_after: str = "",
    created_before: str = "",
    template_name: str = "",
    max_results: int = 50,
) -> dict:
    """Search for jobs across one or all controllers."""
    jobs: list[dict] = []
    if controller:
        cluster_name = resolve_controller(controller)
        jobs = await _find_jobs_on_controller(
            cluster_name,
            status,
            created_after,
            created_before,
            template_name,
            max_results,
        )
    else:
        # Query all controllers in parallel
        controllers = get_configured_controllers()
        if not controllers:
            return {"error": "No AAP2 controllers configured"}

        per_controller = max(max_results // len(controllers), 10)
        tasks = [
            _find_jobs_on_controller(
                name,
                status,
                created_after,
                created_before,
                template_name,
                per_controller,
            )
            for name in controllers
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = []
        for name, result in zip(controllers, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{name}: {result}")
                logger.warning("AAP2 find_jobs failed on %s: %s", name, result)
            else:
                jobs.extend(result)

        # Sort merged results by finished time (descending)
        jobs.sort(key=lambda j: j.get("finished", "") or "", reverse=True)
        jobs = jobs[:max_results]

        if errors and not jobs:
            return {"error": f"All controllers failed: {'; '.join(errors)}"}

    return {
        "total": len(jobs),
        "jobs": jobs,
    }


async def query_aap2(
    action: str,
    controller: str = "",
    job_id: int | None = None,
    failed_only: bool = False,
    changed_only: bool = False,
    status: str = "",
    created_after: str = "",
    created_before: str = "",
    template_name: str = "",
    max_results: int = 50,
) -> dict:
    """Main entry point for the query_aap2 tool."""
    max_results = min(max_results, 200)

    try:
        if action == "get_job":
            if not job_id:
                return {"error": "job_id is required for get_job"}
            if not controller:
                return {"error": "controller is required for get_job"}
            cluster_name = resolve_controller(controller)
            return await _get_job(cluster_name, job_id)

        elif action == "get_job_events":
            if not job_id:
                return {"error": "job_id is required for get_job_events"}
            if not controller:
                return {"error": "controller is required for get_job_events"}
            cluster_name = resolve_controller(controller)
            return await _get_job_events(
                cluster_name,
                job_id,
                failed_only,
                changed_only,
                max_results,
            )

        elif action == "find_jobs":
            return await _find_jobs(
                controller,
                status,
                created_after,
                created_before,
                template_name,
                max_results,
            )

        else:
            return {
                "error": f"Unknown action: '{action}'. "
                "Use get_job, get_job_events, or find_jobs."
            }

    except (ValueError, LookupError, PermissionError) as e:
        return {"error": str(e)}
    except httpx.ConnectError as e:
        return {"error": f"Cannot reach AAP2 controller: {e}"}
    except httpx.HTTPStatusError as e:
        return {"error": f"AAP2 API error: {e.response.status_code} " f"{e.response.reason_phrase}"}
    except Exception:
        logger.exception("Unexpected error in query_aap2")
        return {"error": "Internal error querying AAP2 — check logs"}
