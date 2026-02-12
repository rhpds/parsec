"""Asyncpg connection pool for the provision DB."""

import logging

import asyncpg

from src.config import get_config

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the asyncpg connection pool."""
    global _pool
    cfg = get_config()
    db = cfg.provision_db

    dsn = f"postgresql://{db.user}:{db.password}" f"@{db.host}:{db.get('port', 5432)}/{db.database}"

    _pool = await asyncpg.create_pool(
        dsn,
        min_size=cfg.provision_db.get("min_pool_size", 2),
        max_size=cfg.provision_db.get("max_pool_size", 10),
        command_timeout=cfg.provision_db.get("statement_timeout_ms", 30000) / 1000,
        timeout=10,
    )
    logger.info("Provision DB pool initialized")
    return _pool


async def close_pool() -> None:
    """Close the asyncpg connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Provision DB pool closed")


async def get_pool() -> asyncpg.Pool:
    """Get the current connection pool, initializing if needed."""
    global _pool
    if _pool is None:
        logger.info("DB pool not initialized — attempting lazy connect")
        await init_pool()
    return _pool
