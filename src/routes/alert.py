"""Alert investigation endpoint — POST /api/alert/investigate."""

import logging
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from src.agent.orchestrator import run_alert_investigation
from src.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alert", tags=["alert"])


class AlertRequest(BaseModel):
    alert_type: str
    account_id: str
    alert_text: str
    account_name: str = ""
    user_arn: str = ""
    event_time: str = ""
    region: str = ""
    event_details: dict | None = None


class AlertResponse(BaseModel):
    should_alert: bool
    severity: str
    summary: str
    investigation_log: str
    duration_seconds: float


@router.post("/investigate", response_model=AlertResponse)
async def investigate_alert(
    body: AlertRequest,
    x_api_key: str | None = Header(None),
):
    """Investigate an alert and return a structured verdict.

    Authenticated via X-API-Key header (not OAuth — called by Lambda).
    """
    cfg = get_config()
    configured_key = cfg.get("alert_api_key", "")

    if not configured_key:
        raise HTTPException(
            status_code=503,
            detail="Alert investigation endpoint is not configured (alert_api_key is empty)",
        )

    if not x_api_key or x_api_key != configured_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    logger.info(
        "Alert investigation request: type=%s account=%s",
        body.alert_type,
        body.account_id,
    )

    start = time.monotonic()
    try:
        result = await run_alert_investigation(
            alert_type=body.alert_type,
            account_id=body.account_id,
            alert_text=body.alert_text,
            account_name=body.account_name,
            user_arn=body.user_arn,
            event_time=body.event_time,
            region=body.region,
            event_details=body.event_details,
        )
    except Exception:
        logger.exception("Alert investigation failed")
        elapsed = round(time.monotonic() - start, 1)
        result = {
            "should_alert": True,
            "severity": "medium",
            "summary": "Investigation encountered an unexpected error — alerting as a precaution.",
            "investigation_log": "",
            "duration_seconds": elapsed,
        }

    return AlertResponse(**result)
