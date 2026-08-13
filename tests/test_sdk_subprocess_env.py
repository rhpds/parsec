"""The ``claude`` CLI subprocess must not inherit Parsec's credential environment.

The SDK runs the CLI as a child process and the model can read that process's
environment through its built-in tools, so ``ClaudeAgentOptions(env=...)`` is a
disclosure boundary. Parsec's pod carries every cloud credential the app needs
(``envFrom`` on ``parsec-cloud-credentials`` / ``parsec-aap2-credentials``), and
customer-controlled text reaches the model verbatim via Splunk pod logs and AAP2
job stdout — so the boundary has to be an allowlist.
"""

from __future__ import annotations

import pytest

from src.llm.agent_sdk_client import build_subprocess_env


def test_parsec_namespace_is_never_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every app secret lands under the Dynaconf ``PARSEC_`` prefix."""
    monkeypatch.setenv("PARSEC_AWS__SECRET_ACCESS_KEY", "shh")
    monkeypatch.setenv("PARSEC_AZURE__CLIENT_SECRET", "shh")
    monkeypatch.setenv("PARSEC_AZURE_COSMOS__KEY", "shh")
    monkeypatch.setenv("PARSEC_GITHUB__TOKEN", "shh")
    monkeypatch.setenv("PARSEC_AAP2__CONTROLLERS__0__PASSWORD", "shh")
    monkeypatch.setenv("PARSEC_ALERT_API_KEY", "shh")
    monkeypatch.setenv("PARSEC_REPORTING_MCP__TOKEN", "shh")

    env = build_subprocess_env()

    leaked = sorted(k for k in env if k.startswith("PARSEC_"))
    assert leaked == [], f"credential env leaked to the CLI subprocess: {leaked}"


def test_unknown_variables_are_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allowlist, not denylist — anything unrecognised stays in the app process."""
    monkeypatch.setenv("SOME_INTERNAL_TOKEN", "shh")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shh")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "shh")

    env = build_subprocess_env()

    assert "SOME_INTERNAL_TOKEN" not in env
    assert "DATABASE_URL" not in env
    # Bedrock deployments grant these explicitly via agent.sdk.env instead.
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_SESSION_TOKEN" not in env


def test_runtime_essentials_are_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI still needs its own runtime, Vertex wiring, TLS and proxy config."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/claude")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("CLAUDE_CODE_ACCEPT_TOS", "true")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "itpc-gcp-octo-eng-claude")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/app/gcp/service-account.json")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/pki/tls/certs/ca-bundle.crt")

    env = build_subprocess_env()

    for key in (
        "PATH",
        "HOME",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_ACCEPT_TOS",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_REGION",
        "HTTPS_PROXY",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert key in env, f"{key} must reach the CLI subprocess"


def test_extra_env_is_merged_and_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator-supplied env (tracing + agent.sdk.env) is an explicit grant."""
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "0")

    env = build_subprocess_env(
        {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "MLFLOW_TRACKING_URI": "http://mlflow-tracking.mlflow.svc:5000",
            "AWS_SECRET_ACCESS_KEY": "explicitly-granted-for-bedrock",
        }
    )

    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["MLFLOW_TRACKING_URI"] == "http://mlflow-tracking.mlflow.svc:5000"
    # Not filtered: an operator writing it into agent.sdk.env meant it.
    assert env["AWS_SECRET_ACCESS_KEY"] == "explicitly-granted-for-bedrock"
