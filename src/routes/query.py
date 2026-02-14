"""Query endpoint — POST /api/query with SSE streaming."""

import logging
import os
import ssl
import time

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from src.agent.orchestrator import REPORTS_DIR, run_agent
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
        with open(_SA_TOKEN_PATH) as f:
            token = f.read().strip()

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


async def _check_user_allowed(request: Request, user: str | None) -> None:
    """Check if the user is allowed via group membership or email whitelist.

    Groups are resolved by querying the OpenShift API (like Babylon),
    not from proxy headers. Access is granted if the user belongs to any
    allowed group OR is in the allowed_users email list.
    """
    _log_identity_debug(request)

    cfg = get_config()

    # Check group membership first (queried from OpenShift API)
    allowed_groups_str = cfg.auth.get("allowed_groups", "")
    if allowed_groups_str and user:
        allowed_groups = {g.strip().lower() for g in allowed_groups_str.split(",") if g.strip()}
        if allowed_groups:
            user_groups = await _get_user_groups(user)
            if user_groups & allowed_groups:
                return  # User is in an allowed group

        # Groups are configured but user is not in any — check email fallback
        allowed_str = cfg.auth.get("allowed_users", "")
        if allowed_str:
            allowed = {u.strip().lower() for u in allowed_str.split(",") if u.strip()}
            if allowed and user.lower() in allowed:
                return  # User is in the email whitelist

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

    if not user and allowed_groups_str:
        logger.warning("Access denied: no user identity in request headers")
        raise HTTPException(
            status_code=403,
            detail="Authentication required — no user identity found in request",
        )

    # No group restriction — fall back to email-only check
    allowed_str = cfg.auth.get("allowed_users", "")
    if not allowed_str:
        return  # No restriction configured
    allowed = {u.strip().lower() for u in allowed_str.split(",") if u.strip()}
    if not allowed:
        return
    if not user:
        logger.warning("Access denied: no user identity in request headers")
        raise HTTPException(
            status_code=403,
            detail="Authentication required — no user identity found in request",
        )
    if user.lower() not in allowed:
        logger.warning("Access denied for user '%s' — not in allowed_users list", user)
        logger.warning("  Allowed users: %s", ", ".join(sorted(allowed)))
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: user '{user}' is not in the allowed users list. "
            f"Add this address to the allowed_users config to grant access.",
        )


@router.get("/auth/check")
async def auth_check(
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
):
    """Check if the current user is authorized to use Parsec."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    return {"authorized": True, "user": user}


@router.post("/query")
async def query(
    body: QueryRequest,
    request: Request,
    x_forwarded_user: str | None = Header(None),
    x_forwarded_email: str | None = Header(None),
    x_forwarded_preferred_username: str | None = Header(None),
):
    """Stream an agent response as SSE events.

    The OAuth proxy sets X-Forwarded-User and X-Forwarded-Email headers.
    Group membership is resolved by querying the OpenShift API directly.
    """
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)

    logger.info(
        "Query from user=%s preferred_username=%s: %s",
        user,
        x_forwarded_preferred_username,
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
    await _check_user_allowed(request, user)

    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = os.path.join(REPORTS_DIR, safe_name)

    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Report not found")

    media_type = "text/asciidoc" if safe_name.endswith(".adoc") else "text/markdown"
    return FileResponse(filepath, filename=safe_name, media_type=media_type)
