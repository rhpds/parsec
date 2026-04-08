"""Health check endpoints."""

import logging

from fastapi import APIRouter

import src.connections.reporting_mcp as reporting_mcp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness():
    """Readiness probe — checks MCP init succeeded, doesn't trigger init."""
    if not reporting_mcp.get_mcp_url():
        return {"status": "ready", "db": "reporting_mcp_not_configured"}

    if reporting_mcp.get_server_instructions() or reporting_mcp.get_mcp_tools():
        return {"status": "ready", "db": "via_reporting_mcp"}

    return {"status": "not_ready", "db": "reporting_mcp_not_initialized"}
