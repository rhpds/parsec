"""Query endpoint — POST /api/query with SSE streaming."""

import asyncio
import logging
import os
import pathlib
import ssl
import time
from typing import Annotated, Any, NoReturn

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from src.agent.orchestrator import REPORTS_DIR, run_agent
from src.agent.streaming import with_keepalive
from src.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["query"])

# --- OpenShift group resolution (like Babylon catalog API) ---
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_K8S_API = "https://kubernetes.default.svc"

_groups_cache: list[dict] = []
_groups_cache_time: float = 0
_GROUPS_CACHE_TTL = 60  # seconds


async def _fetch_openshift_groups() -> list[dict]:
    """Fetch all OpenShift groups from the API, cached for 60s."""
    global _groups_cache, _groups_cache_time
    if _groups_cache and time.time() - _groups_cache_time < _GROUPS_CACHE_TTL:
        return _groups_cache

    if not os.path.exists(_SA_TOKEN_PATH):
        logger.debug("Not running in OpenShift — skipping group lookup")
        return []

    try:
        token = await asyncio.to_thread(pathlib.Path(_SA_TOKEN_PATH).read_text)
        token = token.strip()

        ssl_ctx = ssl.create_default_context(cafile=_SA_CA_PATH)
        async with httpx.AsyncClient(verify=ssl_ctx) as client:
            resp = await client.get(
                f"{_K8S_API}/apis/user.openshift.io/v1/groups",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            _groups_cache = data.get("items", [])
            _groups_cache_time = time.time()
            logger.debug("Fetched %d OpenShift groups", len(_groups_cache))
            return _groups_cache
    except Exception:
        logger.warning("Failed to fetch OpenShift groups", exc_info=True)
        return _groups_cache  # return stale cache on error


async def _get_user_groups(user: str) -> set[str]:
    """Get the OpenShift groups a user belongs to."""
    groups = await _fetch_openshift_groups()
    return {g["metadata"]["name"].lower() for g in groups if user in g.get("users", [])}


class QueryRequest(BaseModel):
    question: str
    conversation_history: list | None = None
    conversation_id: str | None = None
    session_id: str | None = None


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


def _parse_csv_set(value: str) -> set[str]:
    """Parse a comma-separated string into a lowercase set, skipping blanks."""
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _is_user_in_email_list(user: str, allowed_str: str) -> bool:
    """Return True if user's email is in the comma-separated allowed list."""
    if not allowed_str:
        return False
    allowed = _parse_csv_set(allowed_str)
    return bool(allowed and user.lower() in allowed)


def _raise_no_identity() -> NoReturn:
    """Raise 403 for missing user identity."""
    logger.warning("Access denied: no user identity in request headers")
    raise HTTPException(
        status_code=403,
        detail="Authentication required — no user identity found in request",
    )


async def _check_group_access(cfg: Any, user: str) -> bool:
    """Check if user is in an allowed OpenShift group. Returns True if allowed."""
    allowed_groups_str = cfg.auth.get("allowed_groups", "")
    if not allowed_groups_str:
        return False  # No group restriction configured
    allowed_groups = _parse_csv_set(allowed_groups_str)
    if not allowed_groups:
        return False
    user_groups = await _get_user_groups(user)
    if user_groups & allowed_groups:
        return True

    # Check email fallback before denying
    if _is_user_in_email_list(user, cfg.auth.get("allowed_users", "")):
        return True

    # Neither group nor email matched
    user_groups_str = ", ".join(sorted(user_groups)) if user_groups else "(none)"
    logger.warning("Access denied for user '%s' — not in allowed groups or users", user)
    logger.warning("  Allowed groups: %s", ", ".join(sorted(allowed_groups)))
    logger.warning("  User groups: %s", user_groups_str)
    raise HTTPException(
        status_code=403,
        detail=f"Access denied: user '{user}' is not in an allowed group. "
        f"Contact an administrator to request access.",
    )


async def _check_user_allowed(request: Request, user: str | None) -> None:
    """Check if the user is allowed via group membership or email whitelist.

    Groups are resolved by querying the OpenShift API (like Babylon),
    not from proxy headers. Access is granted if the user belongs to any
    allowed group OR is in the allowed_users email list.
    """
    _log_identity_debug(request)

    cfg = get_config()
    allowed_groups_str = cfg.auth.get("allowed_groups", "")

    # Group-based auth path
    if allowed_groups_str:
        if not user:
            _raise_no_identity()
        await _check_group_access(cfg, user)
        return

    # No group restriction — fall back to email-only check
    allowed_str = cfg.auth.get("allowed_users", "")
    if not allowed_str:
        return
    allowed = _parse_csv_set(allowed_str)
    if not allowed:
        return
    if not user:
        _raise_no_identity()
    if user.lower() not in allowed:
        logger.warning("Access denied for user '%s' — not in allowed_users list", user)
        logger.warning("  Allowed users: %s", ", ".join(sorted(allowed)))
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: user '{user}' is not in the allowed users list. "
            f"Add this address to the allowed_users config to grant access.",
        )


@router.get(
    "/auth/check",
    responses={403: {"description": "Forbidden"}},
)
async def auth_check(
    request: Request,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Check if the current user is authorized to use Parsec."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    return {"authorized": True, "user": user}


@router.post(
    "/query",
    responses={403: {"description": "Forbidden"}},
)
async def query(
    body: QueryRequest,
    request: Request,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
    x_forwarded_preferred_username: Annotated[str | None, Header()] = None,
):
    """Stream an agent response as SSE events.

    The OAuth proxy sets X-Forwarded-User and X-Forwarded-Email headers.
    Group membership is resolved by querying the OpenShift API directly.
    """
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)

    logger.info(
        "Query from user=%s preferred_username=%s session_id=%s conversation_id=%s: %s",
        user,
        x_forwarded_preferred_username,
        body.session_id,
        body.conversation_id,
        body.question[:200],
    )

    async def event_stream():
        async for event in run_agent(
            body.question,
            body.conversation_history,
            conversation_id=body.conversation_id,
            session_id=body.session_id,
        ):
            yield event

    return StreamingResponse(
        # Keepalive comments during quiet stretches. Without them the OpenShift
        # router drops the connection after 30s of silence — which is exactly
        # what a long investigation looks like on the wire — and the browser
        # reports "network error" mid-answer.
        with_keepalive(event_stream()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/reports/{filename}",
    responses={
        400: {"description": "Bad Request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
    },
)
async def download_report(
    filename: str,
    request: Request,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Download a generated report file."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)

    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = os.path.realpath(os.path.join(REPORTS_DIR, safe_name))
    if not filepath.startswith(os.path.realpath(REPORTS_DIR)):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Report not found")

    media_type = "text/asciidoc" if safe_name.endswith(".adoc") else "text/markdown"
    return FileResponse(filepath, filename=safe_name, media_type=media_type)
