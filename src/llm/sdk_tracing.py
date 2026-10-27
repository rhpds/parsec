"""Configure MLflow's existing Claude SDK tracing integration.

Environment variables configure an installed CLI integration; they do not
install instrumentation. Successful MLflow initialization enables the built-in
ClaudeSDKClient autolog hook for full SDK orchestration.
"""

from __future__ import annotations

import logging
from typing import Any

from src.llm.config_section import section

logger = logging.getLogger(__name__)

DEFAULT_EXPERIMENT = "parsec-agent-metrics"


def enable_sdk_tracing(config: Any) -> None:
    """Hook the existing SDK client; keep legacy manual spans unchanged."""
    from src.llm import RUNTIME_SDK, get_runtime

    sdk_cfg = section(section(config, "agent"), "sdk")
    if get_runtime(config) == RUNTIME_SDK and sdk_cfg.get("orchestrator", False):
        import mlflow.anthropic

        mlflow.anthropic.autolog()


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
