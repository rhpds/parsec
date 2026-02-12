"""Query endpoint — POST /api/query with SSE streaming."""

import logging
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from src.agent.orchestrator import REPORTS_DIR, run_agent
from src.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["query"])


class QueryRequest(BaseModel):
    question: str
    conversation_history: list | None = None


def _log_identity_debug(request: Request) -> None:
    """Log all identity-related headers from the OAuth proxy / Keycloak.

    TODO: Remove this once the allowed_users list is finalized.
    """
    identity_headers = {}
    for header_name, header_value in request.headers.items():
        lower = header_name.lower()
        if (
            lower.startswith("x-forwarded-")
            or lower.startswith("x-auth-")
            or lower.startswith("x-remote-")
        ):
            identity_headers[header_name] = header_value

    if identity_headers:
        logger.info("=== SSO DEBUG: Identity headers ===")
        for name, value in sorted(identity_headers.items()):
            # Don't log full access tokens, just note their presence
            if "token" in name.lower() or "authorization" in name.lower():
                logger.info("  %s: [present, %d chars]", name, len(value))
            else:
                logger.info("  %s: %s", name, value)
        logger.info("=== END SSO DEBUG ===")
    else:
        logger.info("=== SSO DEBUG: No identity headers found in request ===")


def _check_user_allowed(request: Request, user: str | None) -> None:
    """Check if the user is in the allowed list. Raises HTTPException if not."""
    _log_identity_debug(request)

    cfg = get_config()
    allowed_str = cfg.auth.get("allowed_users", "")
    if not allowed_str:
        return  # No restriction configured
    allowed = {u.strip().lower() for u in allowed_str.split(",") if u.strip()}
    if not allowed:
        return
    if not user:
        logger.warning("Access denied: no user identity in request headers")
        raise HTTPException(
            status_code=403, detail="Authentication required — no user identity found in request"
        )
    if user.lower() not in allowed:
        logger.warning("Access denied for user '%s' — not in allowed_users list", user)
        logger.warning("  Allowed users: %s", ", ".join(sorted(allowed)))
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: user '{user}' is not in the allowed users list. "
            f"Add this address to the allowed_users config to grant access.",
        )


@router.post("/query")
async def query(
    body: QueryRequest,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
    x_forwarded_groups: str | None = Header(None),
    x_forwarded_preferred_username: str | None = Header(None),
):
    """Stream an agent response as SSE events.

    The OAuth proxy sets X-Forwarded-User, X-Forwarded-Email,
    X-Forwarded-Groups, and X-Forwarded-Preferred-Username headers.
    """
    user = x_forwarded_email or x_forwarded_user
    _check_user_allowed(request, user)

    logger.info(
        "Query from user=%s preferred_username=%s groups=%s: %s",
        user,
        x_forwarded_preferred_username,
        x_forwarded_groups,
        body.question[:200],
    )

    async def event_stream():
        async for event in run_agent(body.question, body.conversation_history):
            yield event

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/reports/{filename}")
async def download_report(
    filename: str,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Download a generated report file."""
    user = x_forwarded_email or x_forwarded_user
    _check_user_allowed(request, user)

    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = os.path.join(REPORTS_DIR, safe_name)

    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Report not found")

    media_type = "text/asciidoc" if safe_name.endswith(".adoc") else "text/markdown"
    return FileResponse(filepath, filename=safe_name, media_type=media_type)
