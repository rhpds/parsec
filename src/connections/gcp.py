"""GCP BigQuery client for billing queries."""

import logging
import os

from src.config import get_config

logger = logging.getLogger(__name__)

_bq_client = None


def init_gcp() -> None:
    """Initialize the BigQuery client."""
    global _bq_client
    cfg = get_config()
    gcp_cfg = cfg.gcp

    project_id = gcp_cfg.get("project_id", "")
    if not project_id:
        logger.warning("GCP project_id not configured — GCP tools disabled")
        return

    creds_path = gcp_cfg.get("credentials_path", "")
    if creds_path:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", creds_path)

    from google.cloud import bigquery

    _bq_client = bigquery.Client(project=project_id)
    logger.info("GCP BigQuery client initialized (project=%s)", project_id)


def get_bq_client():
    """Get the BigQuery client (None if not configured)."""
    return _bq_client
