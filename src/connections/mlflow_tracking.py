"""MLflow tracking server connection.

Provides a thin wrapper around the MLflow client SDK. The module is
conditional — when tracking_url is empty, all operations are no-ops.

Pattern follows src/connections/splunk.py: global singleton, init at
startup, getter for the client.
"""

from __future__ import annotations

import logging

import mlflow
from mlflow.tracking import MlflowClient

from src.config import get_config

logger = logging.getLogger(__name__)

_client: MlflowClient | None = None
_experiment_name: str | None = None


def init_mlflow() -> None:
    global _client, _experiment_name

    cfg = get_config()
    mlflow_cfg = cfg.get("mlflow", {})
    tracking_url = mlflow_cfg.get("tracking_url", "")
    _experiment_name = mlflow_cfg.get("experiment_name", "parsec-agent-metrics")

    if not tracking_url:
        logger.info("MLflow tracking disabled (no tracking_url configured)")
        return

    mlflow.set_tracking_uri(tracking_url)
    _client = mlflow.MlflowClient()

    try:
        _client.get_experiment_by_name(_experiment_name)
        logger.info(
            "MLflow tracking enabled: %s (experiment: %s)",
            tracking_url,
            _experiment_name,
        )
    except Exception:
        logger.warning(
            "MLflow server at %s not reachable — metrics will be logged when available",
            tracking_url,
        )


def get_mlflow_client() -> MlflowClient | None:
    return _client


def get_experiment_name() -> str:
    return _experiment_name or "parsec-agent-metrics"
