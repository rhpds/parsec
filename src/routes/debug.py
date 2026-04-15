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


@router.post("/diagnose")
async def diagnose(body: DiagnoseRequest):
    """Diagnose an AAP2 job failure (Phases 1-3 + fix).

    Auto-triggers Phase 5 for status=error.
    """
    try:
        controller_url, job_id = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)

        logger.info("Diagnosing job %d on controller %s", job_id, cluster_name)

        # Phase 1: Fetch job metadata
        metadata = await fetch_job_metadata(cluster_name, job_id)

        result: dict = {
            "metadata": metadata,
            "failingTask": None,
            "projectInfo": None,
            "fix": None,
            "eeInfo": None,
        }

        # Fetch project info for SCM ref
        if metadata.get("projectId"):
            try:
                result["projectInfo"] = await fetch_project_info(
                    cluster_name, metadata["projectId"]
                )
            except Exception as e:
                logger.warning("Failed to fetch project info: %s", e)

        # Phase 2: If failed, fetch stdout and extract failing task
        if metadata["status"] == "failed":
            stdout = await fetch_job_stdout(cluster_name, job_id)
            if stdout:
                failing_task = extract_failing_task(stdout)
                if failing_task:
                    result["failingTask"] = failing_task

                    # Phase 3: Recommend fix
                    fix = await recommend_fix(
                        failing_task,
                        extra_vars=metadata["extraVars"],
                        job_template_name=metadata.get("jobTemplateName"),
                    )
                    if fix:
                        result["fix"] = fix

        # Auto Phase 5 for status=error
        if metadata["status"] == "error":
            # Try pattern match on job_explanation
            if metadata["jobExplanation"]:
                fix = match_pattern(metadata["jobExplanation"])
                if fix:
                    result["fix"] = fix

            # Auto-fetch EE info
            if metadata["executionEnvironment"]:
                try:
                    result["eeInfo"] = await fetch_ee_info(
                        cluster_name, metadata["executionEnvironment"]
                    )
                except Exception as e:
                    logger.warning("EE inspection failed: %s", e)

        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Diagnosis failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/correlation")
async def correlation(body: CorrelationRequest):
    """Fetch correlation data for a job (Phase 4)."""
    try:
        controller_url, _ = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)
        return await fetch_correlation(cluster_name, body.job_id, body.job_template)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("Correlation fetch failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/ee")
async def ee_info(body: EERequest):
    """Fetch execution environment info (Phase 5)."""
    try:
        controller_url, _ = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)
        return await fetch_ee_info(cluster_name, body.ee_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("EE fetch failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
