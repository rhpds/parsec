"""Tests for src.llm.sdk_tracing and its wiring into AgentSdkClient.from_config."""

from __future__ import annotations

from src.llm import AgentSdkClient
from src.llm.sdk_tracing import (
    SESSION_START_HOOK_HANDLER,
    STOP_HOOK_HANDLER,
    build_hooks_settings,
    build_tracing_env,
)

# ------------------------------------------------------------- build_tracing_env


def test_tracing_env_empty_when_no_tracking_url() -> None:
    assert build_tracing_env({}) == {}
    assert build_tracing_env({"mlflow": {"tracking_url": ""}}) == {}
    assert build_tracing_env({"mlflow": {"experiment_name": "x"}}) == {}


def test_tracing_env_enabled_when_url_set() -> None:
    env = build_tracing_env(
        {"mlflow": {"tracking_url": "https://mlflow.example/", "experiment_name": "exp1"}}
    )
    assert env["MLFLOW_CLAUDE_TRACING_ENABLED"] == "true"
    assert env["MLFLOW_TRACKING_URI"] == "https://mlflow.example/"
    assert env["MLFLOW_EXPERIMENT_NAME"] == "exp1"
    # no creds unless configured
    assert "MLFLOW_TRACKING_USERNAME" not in env


def test_tracing_env_defaults_experiment() -> None:
    env = build_tracing_env({"mlflow": {"tracking_url": "https://m"}})
    assert env["MLFLOW_EXPERIMENT_NAME"] == "parsec-agent-metrics"


def test_tracing_env_includes_basic_auth() -> None:
    env = build_tracing_env(
        {
            "mlflow": {
                "tracking_url": "https://m",
                "tracking_username": "u",
                "tracking_password": "p",
            }
        }
    )
    assert env["MLFLOW_TRACKING_USERNAME"] == "u"
    assert env["MLFLOW_TRACKING_PASSWORD"] == "p"


def test_tracing_env_strips_whitespace_url() -> None:
    assert build_tracing_env({"mlflow": {"tracking_url": "   "}}) == {}


# ------------------------------------------------------------ build_hooks_settings


def test_hooks_settings_shape() -> None:
    settings = build_hooks_settings()
    hooks = settings["hooks"]
    assert "Stop" in hooks and "SessionStart" in hooks
    assert STOP_HOOK_HANDLER in hooks["Stop"][0]["hooks"][0]["command"]
    assert SESSION_START_HOOK_HANDLER in hooks["SessionStart"][0]["hooks"][0]["command"]


# ----------------------------------------------------- wiring into from_config


def test_from_config_merges_tracing_env() -> None:
    client = AgentSdkClient.from_config(
        {
            "anthropic": {"model": "claude-sonnet-4-5"},
            "mlflow": {"tracking_url": "https://m", "experiment_name": "exp2"},
        }
    )
    assert client._cfg.extra_env["MLFLOW_CLAUDE_TRACING_ENABLED"] == "true"
    assert client._cfg.extra_env["MLFLOW_EXPERIMENT_NAME"] == "exp2"


def test_explicit_sdk_env_overrides_tracing() -> None:
    client = AgentSdkClient.from_config(
        {
            "anthropic": {"model": "claude-sonnet-4-5"},
            "mlflow": {"tracking_url": "https://m"},
            "agent": {"sdk": {"env": {"MLFLOW_EXPERIMENT_NAME": "override"}}},
        }
    )
    assert client._cfg.extra_env["MLFLOW_EXPERIMENT_NAME"] == "override"
    # tracing flag still present (explicit env only overrode the one key)
    assert client._cfg.extra_env["MLFLOW_CLAUDE_TRACING_ENABLED"] == "true"


def test_no_tracing_env_when_mlflow_disabled() -> None:
    client = AgentSdkClient.from_config({"anthropic": {"model": "m"}})
    assert "MLFLOW_CLAUDE_TRACING_ENABLED" not in client._cfg.extra_env
