"""MLflow tracing for the Claude Agent SDK subprocess.

The Agent SDK runs the Claude Code CLI in a child process. Unlike the legacy
path — which logs metrics from app code via ``src.connections.mlflow_tracking``
— the SDK path is best traced the way RHDP already traces Claude Code itself:
**by environment**, so the subprocess exports its own `claude_code.*` spans
(token usage, per-turn latency, tool calls) to the same MLflow server.

This module derives that tracing configuration from Parsec's existing
``mlflow.*`` config so the SDK runtime exports traces automatically when MLflow
is enabled — no extra wiring at the call site. When ``mlflow.tracking_url`` is
empty (the default), :func:`build_tracing_env` returns ``{}`` and nothing is
exported, exactly like the legacy collector's no-op behavior.

The env vars are consumed by MLflow's Claude Code integration
(``MLFLOW_CLAUDE_TRACING_ENABLED`` + ``MLFLOW_TRACKING_URI`` /
``MLFLOW_EXPERIMENT_NAME``).
"""

from __future__ import annotations

import logging
from typing import Any

from src.llm.config_section import section

logger = logging.getLogger(__name__)

DEFAULT_EXPERIMENT = "parsec-agent-metrics"


def build_tracing_env(config: Any) -> dict[str, str]:
    """Derive the MLflow tracing env for the SDK subprocess from ``mlflow.*`` config.

    Returns an empty dict when ``mlflow.tracking_url`` is unset, so enabling the
    SDK runtime without MLflow configured exports nothing (matching the legacy
    collector's no-op semantics).

    Mirrors the reads in ``src.connections.mlflow_tracking.init_mlflow`` so the
    SDK subprocess and the app-side collector point at the same server /
    experiment / credentials.
    """
    mlflow_cfg = section(config, "mlflow")
    tracking_url = str(mlflow_cfg.get("tracking_url", "") or "").strip()
    if not tracking_url:
        return {}

    env: dict[str, str] = {
        "MLFLOW_CLAUDE_TRACING_ENABLED": "true",
        "MLFLOW_TRACKING_URI": tracking_url,
        "MLFLOW_EXPERIMENT_NAME": str(mlflow_cfg.get("experiment_name", "") or DEFAULT_EXPERIMENT),
    }
    # Basic-auth credentials, if the server requires them (same keys as the
    # collector; env-provided creds aren't re-derived here — the subprocess
    # inherits os.environ separately).
    username = mlflow_cfg.get("tracking_username")
    password = mlflow_cfg.get("tracking_password")
    if username:
        env["MLFLOW_TRACKING_USERNAME"] = str(username)
    if password:
        env["MLFLOW_TRACKING_PASSWORD"] = str(password)

    logger.debug(
        "SDK MLflow tracing enabled: uri=%s experiment=%s",
        tracking_url,
        env["MLFLOW_EXPERIMENT_NAME"],
    )
    return env
