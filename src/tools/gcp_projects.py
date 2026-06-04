"""Tool: query_gcp_projects — list RHDP-created GCP projects under the open-envs folder."""

import asyncio
import logging

from src.config import get_config

logger = logging.getLogger(__name__)

DEFAULT_FOLDER_ID = "178296823511"
MAX_RESULTS_CAP = 500
DEFAULT_MAX_RESULTS = 100


def _get_folder_id() -> str:
    cfg = get_config()
    return cfg.gcp.get("open_envs_folder_id", "") or DEFAULT_FOLDER_ID


def _get_projects_client():
    from google.cloud import resourcemanager_v3

    return resourcemanager_v3.ProjectsClient()


def _list_projects(
    state_filter: str | None,
    max_results: int,
) -> dict:
    """List all projects under the rhpds-open-envs folder."""
    folder_id = _get_folder_id()
    client = _get_projects_client()

    results = client.search_projects(query=f"parent.id:{folder_id}")

    projects = []
    for p in results:
        state = p.state.name
        if state_filter and state.upper() != state_filter.upper():
            continue
        projects.append(
            {
                "project_id": p.project_id,
                "name": p.display_name,
                "state": state,
                "create_time": str(p.create_time) if p.create_time else None,
                "labels": dict(p.labels) if p.labels else {},
            }
        )
        if len(projects) >= max_results:
            break

    active = sum(1 for p in projects if p["state"] == "ACTIVE")
    return {
        "folder_id": folder_id,
        "projects": projects,
        "count": len(projects),
        "active": active,
        "delete_requested": len(projects) - active,
        "truncated": len(projects) >= max_results,
    }


def _get_project(project_id: str) -> dict:
    """Get details for a specific GCP project."""
    client = _get_projects_client()

    try:
        p = client.get_project(name=f"projects/{project_id}")
    except Exception as e:
        return {"error": f"Project '{project_id}' not found: {e}"}

    return {
        "project_id": p.project_id,
        "name": p.display_name,
        "state": p.state.name,
        "create_time": str(p.create_time) if p.create_time else None,
        "labels": dict(p.labels) if p.labels else {},
        "parent": p.parent if p.parent else None,
    }


async def query_gcp_projects(
    action: str,
    project_id: str | None = None,
    state_filter: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """Query GCP projects under the RHDP open-envs folder."""
    max_results = min(max_results, MAX_RESULTS_CAP)

    try:
        if action == "list_projects":
            return await asyncio.to_thread(_list_projects, state_filter, max_results)

        elif action == "get_project":
            if not project_id:
                return {"error": "project_id is required (e.g. 'cluster-4d99p')"}
            return await asyncio.to_thread(_get_project, project_id)

        else:
            return {"error": f"Unknown action '{action}'. Valid: list_projects, get_project"}

    except Exception as e:
        logger.exception("GCP projects query failed")
        return {"error": f"GCP projects query failed: {e}"}
