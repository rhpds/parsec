"""Health check endpoints."""

from fastapi import APIRouter

import src.connections.postgres as pg

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness():
    """Readiness probe — checks DB if pool exists, doesn't trigger init."""
    if pg._pool is None:
        return {"status": "ready", "db": "not_connected_yet"}
    try:
        async with pg._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready", "db": "connected"}
    except Exception as e:
        return {"status": "not_ready", "db": str(e)}
