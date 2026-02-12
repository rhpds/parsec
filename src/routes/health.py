"""Health check endpoints."""

from fastapi import APIRouter

from src.connections.postgres import get_pool

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness():
    """Readiness probe — checks DB connectivity."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready", "db": "connected"}
    except Exception as e:
        return {"status": "not_ready", "db": str(e)}
