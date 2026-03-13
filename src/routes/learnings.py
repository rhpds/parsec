"""Learnings admin API — view/delete agent learnings (admin users only)."""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from src.agent.learnings import clear_learnings, get_learnings, is_admin_user
from src.routes.query import _check_user_allowed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["learnings"])


def _require_admin(user: str | None) -> None:
    """Raise 403 if the user is not an admin."""
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/learnings")
async def get_learnings_api(
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Get the current learnings content (admin only)."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    _require_admin(user)

    content = get_learnings()
    return {"content": content, "has_learnings": bool(content.strip())}


@router.delete("/learnings")
async def delete_learnings_api(
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Clear the learnings file (admin only)."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    _require_admin(user)

    clear_learnings()
    logger.info("Learnings cleared by %s", user)
    return {"deleted": True}


@router.get("/learnings/check")
async def check_learnings_admin(
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Check if the current user is an admin (used by the UI to show/hide the panel)."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)

    return {"is_admin": is_admin_user(user)}
