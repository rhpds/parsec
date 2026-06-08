"""MLflow tracing for the Claude Agent SDK subprocess.

The Agent SDK runs the Claude Code CLI in a child process. Unlike the legacy
path — which logs metrics from app code via ``src.connections.mlflow_tracking``
— the SDK path is best traced the way RHDP already traces Claude Code itself:
**by environment + hooks**, so the subprocess exports its own
`claude_code.*` spans (token usage, per-turn latency, tool calls) to the same
MLflow server.

This module derives that tracing configuration from Parsec's existing
``mlflow.*`` config so the SDK runtime exports traces automatically when MLflow
is enabled — no extra wiring at the call site. When ``mlflow.tracking_url`` is
empty (the default), :func:`build_tracing_env` returns ``{}`` and nothing is
exported, exactly like the legacy collector's no-op behavior.

The env vars are consumed by MLflow's Claude Code integration
(``MLFLOW_CLAUDE_TRACING_ENABLED`` + ``MLFLOW_TRACKING_URI`` /
``MLFLOW_EXPERIMENT_NAME``). :func:`build_hooks_settings` returns the matching
``settings.json`` ``hooks`` block (Stop + SessionStart) for deployments that
prefer the hook-based wiring; it is verified against the installed MLflow
version in-cluster.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# MLflow's Claude Code hook handlers (settings.json -> hooks).
STOP_HOOK_HANDLER = "mlflow.claude_code.hooks.stop_hook_handler"
SESSION_START_HOOK_HANDLER = "mlflow.claude_code.hooks.session_start_hook_handler"

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
    mlflow_cfg = _section(config, "mlflow")
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


def build_hooks_settings() -> dict[str, Any]:
    """Return the ``settings.json`` ``hooks`` block for MLflow Claude Code tracing.

    For deployments that wire tracing via Claude Code hooks rather than env
    alone: a ``Stop`` hook flushes the trace and a ``SessionStart`` hook
    captures ``CLAUDE_SESSION_ID``. The handler module paths are stable across
    MLflow versions; the invocation is verified in-cluster.
    """
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": f"python -m {SESSION_START_HOOK_HANDLER}"}
                    ]
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": f"python -m {STOP_HOOK_HANDLER}"}]}],
        }
    }


def _section(config: Any, key: str) -> dict[str, Any]:
    """Return config sub-section ``key`` as a plain dict (``{}`` if missing).

    Accepts Dynaconf objects (``.get`` + ``.to_dict``) and plain dicts (tests),
    mirroring ``src.llm.agent_sdk_client._get_section``.
    """
    if config is None:
        return {}
    raw = config.get(key, {}) if hasattr(config, "get") else getattr(config, key, {})
    if raw is None:
        return {}
    if hasattr(raw, "to_dict"):
        return raw.to_dict()
    return dict(raw)
