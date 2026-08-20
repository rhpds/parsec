"""AAP2 debug API endpoints."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.tools.aap2_debug import (
    fetch_correlation,
    fetch_ee_info,
    fetch_job_metadata,
    fetch_job_stdout,
    fetch_project_info,
    find_controller_for_url,
    parse_job_url,
)
from src.tools.aap2_fix import match_pattern, recommend_fix
from src.tools.aap2_stdout import extract_failing_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])


class DiagnoseRequest(BaseModel):
    url: str


class CorrelationRequest(BaseModel):
    url: str
    job_id: int
    job_template: int | None = None


class EERequest(BaseModel):
    url: str
    job_id: int
    ee_id: int


async def _fetch_project_info_safe(cluster_name: str, project_id: int | None) -> dict | None:
    """Fetch project info, returning None on failure."""
    if not project_id:
        return None
    try:
        return await fetch_project_info(cluster_name, project_id)
    except Exception as e:
        logger.warning("Failed to fetch project info: %s", e)
        return None


async def _diagnose_failed_job(
    cluster_name: str, job_id: int, metadata: dict, result: dict
) -> None:
    """Phase 2-3: extract failing task from stdout and recommend a fix."""
    stdout = await fetch_job_stdout(cluster_name, job_id)
    if not stdout:
        return
    failing_task = extract_failing_task(stdout)
    if not failing_task:
        return
    result["failingTask"] = failing_task
    fix = await recommend_fix(
        failing_task,
        extra_vars=metadata["extraVars"],
        job_template_name=metadata.get("jobTemplateName"),
    )
    if fix:
        result["fix"] = fix


async def _diagnose_error_job(cluster_name: str, metadata: dict, result: dict) -> None:
    """Phase 5: pattern-match job_explanation and inspect EE for status=error."""
    if metadata["jobExplanation"]:
        fix = match_pattern(metadata["jobExplanation"])
        if fix:
            result["fix"] = fix
    if not metadata["executionEnvironment"]:
        return
    try:
        result["eeInfo"] = await fetch_ee_info(cluster_name, metadata["executionEnvironment"])
    except Exception as e:
        logger.warning("EE inspection failed: %s", e)


@router.post(
    "/diagnose",
    responses={
        400: {"description": "Bad Request"},
        401: {"description": "Unauthorized"},
        404: {"description": "Not Found"},
        500: {"description": "Internal Server Error"},
    },
)
async def diagnose(body: DiagnoseRequest):
    """Diagnose an AAP2 job failure (Phases 1-3 + fix).

    Auto-triggers Phase 5 for status=error.
    """
    try:
        controller_url, job_id = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)

        logger.info("Diagnosing job %d on controller %s", job_id, cluster_name)

        metadata = await fetch_job_metadata(cluster_name, job_id)
        result: dict = {
            "metadata": metadata,
            "failingTask": None,
            "projectInfo": await _fetch_project_info_safe(cluster_name, metadata.get("projectId")),
            "fix": None,
            "eeInfo": None,
        }

        if metadata["status"] == "failed":
            await _diagnose_failed_job(cluster_name, job_id, metadata, result)

        if metadata["status"] == "error":
            await _diagnose_error_job(cluster_name, metadata, result)

        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.exception("Diagnosis failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/correlation",
    responses={400: {"description": "Bad Request"}, 500: {"description": "Internal Server Error"}},
)
async def correlation(body: CorrelationRequest):
    """Fetch correlation data for a job (Phase 4)."""
    try:
        controller_url, _ = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)
        return await fetch_correlation(cluster_name, body.job_id, body.job_template)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Correlation fetch failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/ee",
    responses={400: {"description": "Bad Request"}, 500: {"description": "Internal Server Error"}},
)
async def ee_info(body: EERequest):
    """Fetch execution environment info (Phase 5)."""
    try:
        controller_url, _ = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)
        return await fetch_ee_info(cluster_name, body.ee_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("EE fetch failed")
        raise HTTPException(status_code=500, detail=str(e)) from e
