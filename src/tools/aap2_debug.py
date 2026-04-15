"""AAP2 debug orchestrator — fetches job metadata, traces failures, recommends fixes."""

import contextlib
import json
import logging
import re
from urllib.parse import urlparse

import httpx

from src.connections.aap2 import api_get, api_get_text, resolve_controller

logger = logging.getLogger(__name__)


def parse_job_url(url: str) -> tuple[str, int]:
    """Parse AAP2 job URL and extract controller base URL and job ID.

    Supported formats:
    - https://controller/#/jobs/playbook/12345
    - https://controller/api/v2/jobs/12345/
    - https://controller/#/jobs/command/12345?tab=output
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid AAP2 job URL: {e}") from e

    controller_url = f"{parsed.scheme}://{parsed.netloc}"

    # Try hash fragment format: /#/jobs/playbook/12345
    if parsed.fragment:
        hash_match = re.search(
            r"/jobs/(?:playbook|command|inventory|project)/(\d+)",
            parsed.fragment,
        )
        if hash_match:
            return controller_url, int(hash_match.group(1))

    # Try API format: /api/v2/jobs/12345/
    path_match = re.search(r"/api/v2/jobs/(\d+)", parsed.path)
    if path_match:
        return controller_url, int(path_match.group(1))

    raise ValueError(f"Could not extract job ID from URL: {url}")


def find_controller_for_url(url: str) -> str:
    """Match a controller URL against parsec's configured controllers.

    Extracts the hostname and delegates to resolve_controller().
    Returns the cluster name.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    return resolve_controller(hostname)


async def fetch_job_metadata(cluster_name: str, job_id: int) -> dict:
    """Fetch job metadata from AAP2 controller."""
    data = await api_get(cluster_name, f"/api/v2/jobs/{job_id}/")

    extra_vars: dict = {}
    raw_ev = data.get("extra_vars")
    if isinstance(raw_ev, str) and raw_ev:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            extra_vars = json.loads(raw_ev)
    elif isinstance(raw_ev, dict):
        extra_vars = raw_ev

    action = (
        extra_vars.get("ACTION", "unknown")
        if isinstance(extra_vars.get("ACTION"), str)
        else "unknown"
    )

    ee_val = data.get("execution_environment")
    return {
        "id": job_id,
        "status": data.get("status", "pending"),
        "action": action,
        "executionEnvironment": ee_val if isinstance(ee_val, int) else None,
        "instanceGroup": (
            str(data["instance_group"]) if isinstance(data.get("instance_group"), int) else None
        ),
        "executionNode": data.get("execution_node") or None,
        "jobExplanation": data.get("job_explanation", "") or "",
        "resultTraceback": data.get("result_traceback", "") or "",
        "launchType": data.get("launch_type", "") or "",
        "jobTemplate": (
            data["job_template"] if isinstance(data.get("job_template"), int) else None
        ),
        "jobTemplateName": ((data.get("summary_fields") or {}).get("job_template", {}).get("name")),
        "projectId": (data["project"] if isinstance(data.get("project"), int) else None),
        "started": data.get("started") or None,
        "finished": data.get("finished") or None,
        "elapsed": data.get("elapsed") if isinstance(data.get("elapsed"), int | float) else 0,
        "extraVars": extra_vars,
    }


async def fetch_job_stdout(cluster_name: str, job_id: int) -> str:
    """Fetch job stdout as plain text."""
    try:
        return await api_get_text(
            cluster_name,
            f"/api/v2/jobs/{job_id}/stdout/",
            {"format": "txt"},
        )
    except Exception as e:
        logger.warning("Failed to fetch stdout for job %d: %s", job_id, e)
        return ""


async def fetch_project_info(cluster_name: str, project_id: int) -> dict:
    """Fetch project SCM details."""
    data = await api_get(cluster_name, f"/api/v2/projects/{project_id}/")
    return {
        "scmUrl": data.get("scm_url", "") or "",
        "scmBranch": data.get("scm_branch", "") or "",
        "scmRevision": data.get("scm_revision", "") or "",
    }


async def fetch_correlation(
    cluster_name: str, job_id: int, job_template: int | None = None
) -> dict:
    """Fetch correlation data — recent failures grouped by error, EE, instance group."""
    params: dict = {
        "status__in": "error,failed",
        "order_by": "-finished",
        "page_size": "50",
    }
    if job_template:
        params["job_template"] = str(job_template)
    data = await api_get(cluster_name, "/api/v2/jobs/", params)

    failures = [
        job
        for job in data.get("results", [])
        if isinstance(job.get("id"), int) and job["id"] != job_id
    ]

    by_error: dict[str, list[int]] = {}
    by_ee: dict[int, list[int]] = {}
    by_ig: dict[str, list[int]] = {}

    for job in failures:
        jid = job.get("id")
        if not isinstance(jid, int):
            continue

        explanation = (job.get("job_explanation") or "")[:100]
        by_error.setdefault(explanation, []).append(jid)

        ee = job.get("execution_environment")
        if isinstance(ee, int):
            by_ee.setdefault(ee, []).append(jid)

        ig = job.get("instance_group")
        if isinstance(ig, int):
            by_ig.setdefault(str(ig), []).append(jid)

    return {
        "totalFailures": len(failures),
        "byError": [{"error": e, "count": len(ids), "jobIds": ids} for e, ids in by_error.items()],
        "byEE": [
            {"image": str(ee_id), "count": len(ids), "jobIds": ids} for ee_id, ids in by_ee.items()
        ],
        "byInstanceGroup": [
            {"group": g, "count": len(ids), "jobIds": ids} for g, ids in by_ig.items()
        ],
    }


# Known EE image-name to source-directory mappings
_EE_NAME_MAP = {
    "ee-multicloud": "ee-multicloud-public",
    "ee-multicloud-public": "ee-multicloud-public",
    "ee-multicloud-private": "ee-multicloud-private",
    "ee-ansible-workshop": "ee-ansible-workshop",
}

_EE_SOURCE_FILES = [
    "Containerfile",
    "entrypoint.sh",
    "requirements.txt",
    "requirements.yml",
]


async def fetch_ee_info(cluster_name: str, ee_id: int) -> dict:
    """Fetch EE metadata and source definition files from GitHub."""
    data = await api_get(cluster_name, f"/api/v2/execution_environments/{ee_id}/")
    image = data.get("image", "") or ""

    result: dict = {
        "id": ee_id,
        "image": image,
        "sourceRepo": None,
        "sourceDir": None,
        "sourceFiles": [],
    }

    if not image:
        return result

    # Extract EE directory name from image URL
    name_tag = image.split("/")[-1]
    name = name_tag.split(":")[0]
    ee_dir = _EE_NAME_MAP.get(name, name)

    source_files = []
    for filename in _EE_SOURCE_FILES:
        path = f"tools/execution_environments/{ee_dir}/{filename}"
        url = f"https://raw.githubusercontent.com/agnosticd/agnosticd-v2/main/{path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    source_files.append({"name": filename, "content": resp.text[:5000]})
        except Exception:
            pass

    if source_files:
        result["sourceRepo"] = "agnosticd/agnosticd-v2"
        result["sourceDir"] = f"tools/execution_environments/{ee_dir}"
        result["sourceFiles"] = source_files

    return result
