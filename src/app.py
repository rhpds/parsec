"""FastAPI application — lifespan, static files, CORS."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import get_config
from src.connections.aap2 import init_aap2
from src.connections.aws import init_aws
from src.connections.azure import init_azure
from src.connections.babylon import init_babylon
from src.connections.gcp import init_gcp
from src.connections.github_mcp import init_github_mcp
from src.connections.icinga_mcp import init_icinga_mcp
from src.connections.ocpv import init_ocpv
from src.connections.postgres import close_pool, init_pool
from src.connections.splunk import init_splunk
from src.routes.alert import router as alert_router
from src.routes.conversations import ensure_conversations_dir
from src.routes.conversations import router as conversations_router
from src.routes.health import router as health_router
from src.routes.learnings import router as learnings_router
from src.routes.query import router as query_router
from src.routes.share import ensure_shares_dir
from src.routes.share import router as share_router

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
    for name, init_coro in [("Provision DB", init_pool)]:
        try:
            await init_coro()
            logger.info("%s initialized", name)
        except Exception:
            logger.exception("%s initialization failed — will retry on first query", name)

    for name, init_fn in [
        ("AWS", init_aws),
        ("Azure", init_azure),
        ("GCP", init_gcp),
        ("Babylon", init_babylon),
        ("OCPV", init_ocpv),
        ("AAP2", init_aap2),
        ("GitHub MCP", init_github_mcp),
        ("Icinga MCP", init_icinga_mcp),
        ("Splunk", init_splunk),
    ]:
        try:
            init_fn()
            logger.info("%s initialized", name)
        except Exception:
            logger.exception("%s initialization failed — will retry on first query", name)

    # Ensure data directories exist
    ensure_conversations_dir()
    ensure_shares_dir()

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
app.include_router(alert_router)
app.include_router(query_router)
app.include_router(share_router)
app.include_router(conversations_router)
app.include_router(learnings_router)

# Serve static frontend files
app.mount("/", StaticFiles(directory="static", html=True), name="static")
