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

from src.llm.agent_sdk_client import backend_cli_env, build_subprocess_env


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


# ----------------------------------------------------------- backend auth
#
# The subprocess needs credentials of its own. It has none of Parsec's: the
# PARSEC_ namespace is denied above, and the CLI would not read it anyway. Until
# backend_cli_env existed nothing closed that gap, and parsec-dev — LiteLLM
# backend, no hand-wired ANTHROPIC_* on the pod — answered its first
# SDK-routed question with "the agent runtime failed: Not logged in · Please
# run /login".


def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a pod that carries no CLI credentials of its own."""
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLOUD_ML_REGION",
    ):
        monkeypatch.delenv(key, raising=False)


def test_litellm_backend_authenticates_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parsec-dev regression, at the level the bug actually lived."""
    _clean(monkeypatch)
    config = {
        "anthropic": {
            "backend": "litellm",
            "litellm_base_url": "https://maas-rhdp.example.io",
            "litellm_api_key": "sk-gateway",  # pragma: allowlist secret
        }
    }

    env = build_subprocess_env(None, backend_cli_env(config))

    assert env["ANTHROPIC_BASE_URL"] == "https://maas-rhdp.example.io"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-gateway"  # pragma: allowlist secret


def test_dynaconf_uppercases_env_keys_and_the_backend_still_resolves() -> None:
    """PARSEC_ANTHROPIC__* arrives uppercased, as it does on every deployment."""
    config = {
        "ANTHROPIC": {
            "BACKEND": "litellm",
            "LITELLM_BASE_URL": "https://maas-rhdp.example.io",
            "LITELLM_API_KEY": "sk-gateway",  # pragma: allowlist secret
        }
    }

    assert backend_cli_env(config)["ANTHROPIC_BASE_URL"] == "https://maas-rhdp.example.io"


def test_vertex_backend_authenticates_the_cli() -> None:
    config = {
        "anthropic": {
            "backend": "vertex",
            "vertex_project_id": "itpc-gcp-product-all-claude",
            "vertex_region": "us-east5",
            "vertex_credentials_path": "/app/gcp/service-account.json",
        }
    }

    env = backend_cli_env(config)

    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "itpc-gcp-product-all-claude"
    assert env["CLOUD_ML_REGION"] == "us-east5"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/app/gcp/service-account.json"


def test_vertex_project_falls_back_to_gcp_section() -> None:
    config = {"anthropic": {"backend": "vertex"}, "gcp": {"project_id": "fallback-project"}}

    assert backend_cli_env(config)["ANTHROPIC_VERTEX_PROJECT_ID"] == "fallback-project"


def test_bedrock_backend_sets_region_but_not_credentials() -> None:
    """AWS keys stay behind the allowlist; agent.sdk.env is the explicit grant."""
    config = {"anthropic": {"backend": "bedrock"}, "aws": {"region": "us-west-2"}}

    env = backend_cli_env(config)

    assert env == {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-west-2"}


def test_direct_api_backend_uses_the_configured_key() -> None:
    config = {
        "anthropic": {"backend": "api", "api_key": "sk-ant-direct"}
    }  # pragma: allowlist secret

    assert backend_cli_env(config) == {
        "ANTHROPIC_API_KEY": "sk-ant-direct"
    }  # pragma: allowlist secret


def test_incomplete_backend_leaves_hand_wired_env_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never replace working auth with nothing — the pre-fix deployments relied
    on operators setting these by hand, and an unconfigured key must not break
    them."""
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "hand-wired")
    config = {"anthropic": {"backend": "litellm", "litellm_base_url": ""}}

    assert backend_cli_env(config) == {}
    env = build_subprocess_env(None, backend_cli_env(config))
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "hand-wired"


def test_resolved_backend_evicts_a_conflicting_ambient_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config is the declaration of intent.

    A pod that still carries CLAUDE_CODE_USE_VERTEX from an earlier deployment
    must not send the CLI to Vertex while the app itself talks to the gateway —
    that bills two accounts and lets two different models answer one question.
    """
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "stale-project")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stale")  # pragma: allowlist secret
    config = {
        "anthropic": {
            "backend": "litellm",
            "litellm_base_url": "https://maas-rhdp.example.io",
            "litellm_api_key": "sk-gateway",  # pragma: allowlist secret
        }
    }

    env = build_subprocess_env(None, backend_cli_env(config))

    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://maas-rhdp.example.io"


def test_agent_sdk_env_still_wins_over_the_derived_backend() -> None:
    """Precedence: inherited < derived-from-config < operator's agent.sdk.env."""
    config = {
        "anthropic": {
            "backend": "litellm",
            "litellm_base_url": "https://derived.example.io",
            "litellm_api_key": "sk-derived",  # pragma: allowlist secret
        }
    }

    env = build_subprocess_env(
        {"ANTHROPIC_BASE_URL": "https://operator-override.example.io"},
        backend_cli_env(config),
    )

    assert env["ANTHROPIC_BASE_URL"] == "https://operator-override.example.io"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-derived"  # pragma: allowlist secret


def test_backend_credentials_do_not_reopen_the_parsec_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disclosure boundary survives the fix."""
    monkeypatch.setenv(
        "PARSEC_ANTHROPIC__LITELLM_API_KEY", "sk-gateway"
    )  # pragma: allowlist secret
    monkeypatch.setenv("PARSEC_AWS__SECRET_ACCESS_KEY", "shh")
    config = {
        "anthropic": {
            "backend": "litellm",
            "litellm_base_url": "https://maas-rhdp.example.io",
            "litellm_api_key": "sk-gateway",  # pragma: allowlist secret
        }
    }

    env = build_subprocess_env(None, backend_cli_env(config))

    assert sorted(k for k in env if k.startswith("PARSEC_")) == []
