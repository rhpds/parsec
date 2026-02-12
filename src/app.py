"""FastAPI application — lifespan, static files, CORS."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import get_config
from src.connections.aws import init_aws
from src.connections.azure import init_azure
from src.connections.gcp import init_gcp
from src.connections.postgres import close_pool, init_pool
from src.routes.health import router as health_router
from src.routes.query import router as query_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    cfg = get_config()
    logger.info("Parsec starting up (model=%s)", cfg.anthropic.get("model", "unknown"))

    # Initialize connections — non-fatal so the app starts even if a backend is unreachable
    for name, init_fn in [
        ("Provision DB", init_pool),
        ("AWS", init_aws),
        ("Azure", init_azure),
        ("GCP", init_gcp),
    ]:
        try:
            result = init_fn()
            if hasattr(result, "__await__"):
                await result
            logger.info("%s initialized", name)
        except Exception:
            logger.exception("%s initialization failed — will retry on first query", name)

    logger.info("Startup complete")
    yield

    # Shutdown
    await close_pool()
    logger.info("Parsec shut down")


app = FastAPI(
    title="Parsec",
    description="Natural language cloud cost investigation tool",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — permissive for local dev, OAuth proxy handles auth in prod
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(health_router)
app.include_router(query_router)

# Serve static frontend files
app.mount("/", StaticFiles(directory="static", html=True), name="static")
