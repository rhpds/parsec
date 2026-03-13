"""Conversation persistence — save/load/list/delete chat sessions as JSON files."""

import asyncio
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

router = APIRouter(prefix="/api", tags=["conversations"])

CONVERSATIONS_DIR = os.path.join("data", "conversations")
os.makedirs(CONVERSATIONS_DIR, exist_ok=True)

_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class SaveConversationRequest(BaseModel):
    id: str | None = None
    title: str | None = None
    messages: list


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


def _auto_title(messages: list) -> str:
    """Generate a title from the first user message."""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") for block in content if isinstance(block, dict)
                )
            content = content.strip()
            if content:
                if len(content) <= 80:
                    return content
                truncated = content[:80]
                last_space = truncated.rfind(" ")
                if last_space > 40:
                    truncated = truncated[:last_space]
                return truncated + "..."
    return "New conversation"


def _count_user_messages(messages: list) -> int:
    """Count messages for display (user + assistant, skip tool_result)."""
    return sum(
        1 for m in messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    )


@router.post("/conversations")
async def save_conversation(
    body: SaveConversationRequest,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Save or update a conversation."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    owner = user or "anonymous"

    now = datetime.now(UTC).isoformat()
    conv_id = body.id or str(uuid.uuid4())

    if not _UUID_RE.match(conv_id):
        raise HTTPException(status_code=422, detail="Invalid conversation ID format")

    # If updating, verify ownership
    fpath = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
    if os.path.isfile(fpath):
        with open(fpath) as f:
            existing = json.load(f)
        if existing.get("owner") != owner:
            raise HTTPException(status_code=403, detail="Not your conversation")
        created_at = existing.get("created_at", now)
    else:
        created_at = now

    title = body.title or _auto_title(body.messages)

    conv_data = {
        "id": conv_id,
        "owner": owner,
        "title": title,
        "created_at": created_at,
        "updated_at": now,
        "messages": body.messages,
    }

    with open(fpath, "w") as f:
        json.dump(conv_data, f)

    # Kick off background learning analysis (fire-and-forget)
    asyncio.create_task(_background_learn(body.messages))

    return {"id": conv_id, "title": title}


async def _background_learn(messages: list) -> None:
    """Run learning analysis in the background, never blocking the response."""
    try:
        from src.agent.learnings import analyze_and_learn

        await analyze_and_learn(messages)
    except Exception:
        logger.exception("Background learning analysis failed (non-fatal)")


@router.get("/conversations")
async def list_conversations(
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """List conversations for the current user, newest first."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    owner = user or "anonymous"

    conversations: list[dict] = []
    try:
        for fname in os.listdir(CONVERSATIONS_DIR):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(CONVERSATIONS_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                if data.get("owner") != owner:
                    continue
                conversations.append(
                    {
                        "id": data["id"],
                        "title": data.get("title", "Untitled"),
                        "created_at": data.get("created_at", ""),
                        "updated_at": data.get("updated_at", data.get("created_at", "")),
                        "message_count": _count_user_messages(data.get("messages", [])),
                    }
                )
            except Exception:  # nosec B112 — skip corrupt/unreadable JSON files
                continue
    except FileNotFoundError:
        pass

    conversations.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return {"conversations": conversations}


@router.get("/conversations/{conv_id}")
async def get_conversation(
    conv_id: str,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Load a specific conversation."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    owner = user or "anonymous"

    if not _UUID_RE.match(conv_id):
        raise HTTPException(status_code=422, detail="Invalid conversation ID format")

    fpath = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Conversation not found")

    with open(fpath) as f:
        data = json.load(f)

    if data.get("owner") != owner:
        raise HTTPException(status_code=403, detail="Not your conversation")

    return data


@router.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: str,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Delete a conversation."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    owner = user or "anonymous"

    if not _UUID_RE.match(conv_id):
        raise HTTPException(status_code=422, detail="Invalid conversation ID format")

    fpath = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Conversation not found")

    with open(fpath) as f:
        data = json.load(f)

    if data.get("owner") != owner:
        raise HTTPException(status_code=403, detail="Not your conversation")

    os.remove(fpath)
    return {"deleted": True}
