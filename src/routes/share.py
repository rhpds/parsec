"""Share endpoint — POST /api/share + GET /api/share/{id}."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from src.routes.query import _check_user_allowed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["share"])

SHARES_DIR = os.path.join("data", "shares")


def ensure_shares_dir() -> None:
    """Create the shares directory if it doesn't exist."""
    os.makedirs(SHARES_DIR, exist_ok=True)


_SHARE_TTL_DAYS = 90
_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


def _read_json(fpath: str) -> dict:
    with open(fpath) as f:
        return json.load(f)


def _write_json(fpath: str, data: dict) -> None:
    with open(fpath, "w") as f:
        json.dump(data, f)


class ShareRequest(BaseModel):
    messages: list
    title: str | None = None


class ShareResponse(BaseModel):
    id: str
    url: str


def _extract_first_user_text(messages: list) -> str:
    """Extract text from the first user message in a conversation."""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        content = content.strip()
        if content:
            return content
    return ""


def _truncate_title(text: str, max_len: int) -> str:
    """Truncate text at a word boundary for use as a title."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated + "..."


def _auto_title(messages: list) -> str:
    """Generate a title from the first user message."""
    text = _extract_first_user_text(messages)
    if not text:
        return "Shared investigation"
    return _truncate_title(text, 100)


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
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
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
    await asyncio.to_thread(_write_json, fpath, share_data)

    logger.info("Share created: %s by %s (%s)", share_id, user, title[:60])

    # Lazy cleanup in background — don't block the response
    asyncio.get_event_loop().run_in_executor(None, _cleanup_old_shares)

    # Build URL from request
    base_url = str(request.base_url).rstrip("/")
    url = f"{base_url}/?share={share_id}"

    return ShareResponse(id=share_id, url=url)


@router.get(
    "/share/{share_id}",
    responses={404: {"description": "Not Found"}, 422: {"description": "Unprocessable Entity"}},
)
async def get_share(
    share_id: str,
    request: Request,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Retrieve a shared conversation snapshot."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)

    if not _UUID_RE.match(share_id):
        raise HTTPException(status_code=422, detail="Invalid share ID format")

    fpath = os.path.join(SHARES_DIR, f"{share_id}.json")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Shared session not found")

    return await asyncio.to_thread(_read_json, fpath)
