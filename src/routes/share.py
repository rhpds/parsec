"""Share endpoint — POST /api/share + GET /api/share/{id}."""

import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from src.routes.query import _check_user_allowed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["share"])

SHARES_DIR = os.path.join("data", "shares")
os.makedirs(SHARES_DIR, exist_ok=True)

_SHARE_TTL_DAYS = 90
_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class ShareRequest(BaseModel):
    messages: list
    title: str | None = None


class ShareResponse(BaseModel):
    id: str
    url: str


def _auto_title(messages: list) -> str:
    """Generate a title from the first user message."""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # Extract text from content blocks
                content = " ".join(
                    block.get("text", "") for block in content if isinstance(block, dict)
                )
            content = content.strip()
            if content:
                if len(content) <= 100:
                    return content
                # Truncate at word boundary
                truncated = content[:100]
                last_space = truncated.rfind(" ")
                if last_space > 50:
                    truncated = truncated[:last_space]
                return truncated + "..."
    return "Shared investigation"


def _cleanup_old_shares() -> None:
    """Delete shares older than 90 days. Best-effort, errors ignored."""
    try:
        now = datetime.now(UTC)
        for fname in os.listdir(SHARES_DIR):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(SHARES_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                created = datetime.fromisoformat(data["created_at"])
                if (now - created).days > _SHARE_TTL_DAYS:
                    os.remove(fpath)
                    logger.info("Cleaned up expired share: %s", fname)
            except Exception:
                pass
    except Exception:
        pass


@router.post("/share")
async def create_share(
    body: ShareRequest,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Create a read-only snapshot of a conversation."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)

    share_id = str(uuid.uuid4())
    title = body.title or _auto_title(body.messages)

    share_data = {
        "id": share_id,
        "shared_by": user or "anonymous",
        "title": title,
        "created_at": datetime.now(UTC).isoformat(),
        "messages": body.messages,
    }

    fpath = os.path.join(SHARES_DIR, f"{share_id}.json")
    with open(fpath, "w") as f:
        json.dump(share_data, f)

    logger.info("Share created: %s by %s (%s)", share_id, user, title[:60])

    # Lazy cleanup of old shares
    _cleanup_old_shares()

    # Build URL from request
    base_url = str(request.base_url).rstrip("/")
    url = f"{base_url}/?share={share_id}"

    return ShareResponse(id=share_id, url=url)


@router.get("/share/{share_id}")
async def get_share(
    share_id: str,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Retrieve a shared conversation snapshot."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)

    if not _UUID_RE.match(share_id):
        raise HTTPException(status_code=422, detail="Invalid share ID format")

    fpath = os.path.join(SHARES_DIR, f"{share_id}.json")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Shared session not found")

    with open(fpath) as f:
        return json.load(f)
