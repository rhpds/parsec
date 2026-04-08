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
    """Readiness probe — checks Reporting MCP connectivity."""
    if not reporting_mcp.get_mcp_url():
        return {"status": "ready", "db": "reporting_mcp_not_configured"}

    try:
        result = await reporting_mcp.call_tool("list_tables", {"schema": "public"})
        if "error" in result:
            return {"status": "not_ready", "db": result["error"]}
        return {"status": "ready", "db": "via_reporting_mcp"}
    except Exception as e:
        logger.exception("Readiness check failed")
        return {"status": "not_ready", "db": str(e)}
