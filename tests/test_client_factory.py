"""Tests for the centralized client factory."""

from unittest.mock import MagicMock, patch

import pytest


def _make_cfg(anthropic_dict: dict, gcp_dict: dict | None = None, aws_dict: dict | None = None):
    """Build a mock Dynaconf config with nested .get() support."""
    cfg = MagicMock()

    # anthropic section
    anthropic_section = MagicMock()

    def _anthropic_get(key, default=""):
        parts = key.split(".")
        current = anthropic_dict
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    anthropic_section.get = _anthropic_get
    cfg.anthropic = anthropic_section

    # gcp/aws sections
    gcp_section = MagicMock()
    gcp_section.get = lambda k, d="": (gcp_dict or {}).get(k, d)
    cfg.gcp = gcp_section

    aws_section = MagicMock()
    aws_section.get = lambda k, d="": (aws_dict or {}).get(k, d)
    cfg.aws = aws_section

    return cfg


class TestResolveModel:
    """Test model resolution with overrides and backward compatibility."""

    def test_top_level_default(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg({"model": "claude-sonnet-4-6"})
        assert resolve_model(cfg, "cost") == "claude-sonnet-4-6"

    def test_component_override(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg(
            {
                "model": "claude-sonnet-4-6",
                "overrides": {"aap2": {"model": "claude-opus-4-6"}},
            }
        )
        assert resolve_model(cfg, "aap2") == "claude-opus-4-6"

    def test_orchestrator_model_backward_compat(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg(
            {
                "model": "claude-sonnet-4-6",
                "orchestrator_model": "claude-haiku-4-5",
            }
        )
        assert resolve_model(cfg, "orchestrator") == "claude-haiku-4-5"

    def test_override_beats_orchestrator_model(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg(
            {
                "model": "claude-sonnet-4-6",
                "orchestrator_model": "claude-haiku-4-5",
                "overrides": {"orchestrator": {"model": "qwen3-14b"}},
            }
        )
        assert resolve_model(cfg, "orchestrator") == "qwen3-14b"

    def test_missing_model_uses_hardcoded_default(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg({})
        assert resolve_model(cfg, "default") == "claude-sonnet-4-6"

    def test_unrecognized_component_uses_top_level(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg({"model": "claude-sonnet-4-6"})
        assert resolve_model(cfg, "unknown_agent") == "claude-sonnet-4-6"


class TestResolveMaxTokens:
    """Test max_tokens resolution."""

    def test_top_level_default(self):
        from src.agent.client_factory import resolve_max_tokens

        cfg = _make_cfg({"max_tokens": 8192})
        assert resolve_max_tokens(cfg, "cost") == 8192

    def test_component_override(self):
        from src.agent.client_factory import resolve_max_tokens

        cfg = _make_cfg(
            {
                "max_tokens": 4096,
                "overrides": {"orchestrator": {"max_tokens": 2048}},
            }
        )
        assert resolve_max_tokens(cfg, "orchestrator") == 2048

    def test_missing_uses_hardcoded_default(self):
        from src.agent.client_factory import resolve_max_tokens

        cfg = _make_cfg({})
        assert resolve_max_tokens(cfg, "default") == 4096


class TestBuildClient:
    """Test client construction per backend type."""

    @patch("anthropic.Anthropic")
    def test_api_backend(self, mock_cls):
        from src.agent.client_factory import build_client

        cfg = _make_cfg({"backend": "api", "api_key": "test-key"})  # pragma: allowlist secret
        build_client(cfg, "cost")
        mock_cls.assert_called_once_with(api_key="test-key")  # pragma: allowlist secret

    @patch("anthropic.Anthropic")
    def test_api_backend_from_env(self, mock_cls):
        from src.agent.client_factory import build_client

        cfg = _make_cfg({"backend": "api"})
        with patch.dict(
            "os.environ", {"ANTHROPIC_API_KEY": "test-env-key"}
        ):  # pragma: allowlist secret
            build_client(cfg, "cost")
        mock_cls.assert_called_once_with(api_key="test-env-key")  # pragma: allowlist secret

    def test_api_backend_no_key_raises(self):
        from src.agent.client_factory import build_client

        cfg = _make_cfg({"backend": "api"})
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(ValueError, match="ANTHROPIC_API_KEY"),
        ):
            build_client(cfg, "cost")

    @patch("anthropic.Anthropic")
    def test_litellm_backend(self, mock_cls):
        from src.agent.client_factory import build_client

        cfg = _make_cfg(
            {
                "backend": "api",
                "litellm_base_url": "https://maas.example.com",
                "litellm_api_key": "test-lite-key",  # pragma: allowlist secret
                "overrides": {"orchestrator": {"backend": "litellm"}},
            }
        )
        build_client(cfg, "orchestrator")
        mock_cls.assert_called_once_with(
            base_url="https://maas.example.com",
            api_key="test-lite-key",  # pragma: allowlist secret
        )

    def test_litellm_backend_no_url_raises(self):
        from src.agent.client_factory import build_client

        cfg = _make_cfg(
            {
                "backend": "litellm",
            }
        )
        with pytest.raises(ValueError, match="litellm_base_url"):
            build_client(cfg, "default")

    @patch("anthropic.AnthropicVertex")
    def test_vertex_backend(self, mock_cls):
        from src.agent.client_factory import build_client

        cfg = _make_cfg(
            {"backend": "vertex", "vertex_project_id": "my-proj", "vertex_region": "us-east5"},
        )
        build_client(cfg, "cost")
        mock_cls.assert_called_once_with(project_id="my-proj", region="us-east5")

    @patch("anthropic.AnthropicBedrock")
    def test_bedrock_backend(self, mock_cls):
        from src.agent.client_factory import build_client

        cfg = _make_cfg({"backend": "bedrock", "bedrock_region": "us-west-2"})
        build_client(cfg, "cost")
        mock_cls.assert_called_once_with(aws_region="us-west-2")

    @patch("anthropic.Anthropic")
    def test_component_override_backend(self, mock_cls):
        """A component can use litellm while the default is vertex."""
        from src.agent.client_factory import build_client

        cfg = _make_cfg(
            {
                "backend": "vertex",
                "vertex_project_id": "my-proj",
                "litellm_base_url": "https://maas.example.com",
                "litellm_api_key": "test-lite-key",  # pragma: allowlist secret
                "overrides": {"orchestrator": {"backend": "litellm"}},
            }
        )
        build_client(cfg, "orchestrator")
        mock_cls.assert_called_once_with(
            base_url="https://maas.example.com",
            api_key="test-lite-key",  # pragma: allowlist secret
        )


class TestBuildAsyncClient:
    """Test async client construction."""

    @patch("anthropic.AsyncAnthropic")
    def test_api_backend(self, mock_cls):
        from src.agent.client_factory import build_async_client

        cfg = _make_cfg({"backend": "api", "api_key": "test-key"})  # pragma: allowlist secret
        build_async_client(cfg, "learnings")
        mock_cls.assert_called_once_with(api_key="test-key")  # pragma: allowlist secret

    @patch("anthropic.AsyncAnthropic")
    def test_litellm_backend(self, mock_cls):
        from src.agent.client_factory import build_async_client

        cfg = _make_cfg(
            {
                "backend": "litellm",
                "litellm_base_url": "https://maas.example.com",
                "litellm_api_key": "test-lite-key",  # pragma: allowlist secret
            }
        )
        build_async_client(cfg, "learnings")
        mock_cls.assert_called_once_with(
            base_url="https://maas.example.com",
            api_key="test-lite-key",  # pragma: allowlist secret
        )


class TestStripThinkingTokens:
    """Test thinking-token stripping for open-source models."""

    def test_strips_think_block(self):
        from src.agent.client_factory import strip_thinking_tokens

        text = "<think>\nLet me reason about this.\n</think>\nHello!"
        assert strip_thinking_tokens(text) == "Hello!"

    def test_strips_multiline_think_block(self):
        from src.agent.client_factory import strip_thinking_tokens

        text = "<think>\nStep 1: do X\nStep 2: do Y\n</think>\n\nThe answer is 42."
        assert strip_thinking_tokens(text) == "The answer is 42."

    def test_noop_on_clean_text(self):
        from src.agent.client_factory import strip_thinking_tokens

        text = "No thinking tokens here."
        assert strip_thinking_tokens(text) == "No thinking tokens here."

    def test_empty_string(self):
        from src.agent.client_factory import strip_thinking_tokens

        assert strip_thinking_tokens("") == ""

    def test_only_think_block_returns_empty(self):
        from src.agent.client_factory import strip_thinking_tokens

        text = "<think>reasoning only</think>"
        assert strip_thinking_tokens(text) == ""

    def test_multiple_think_blocks(self):
        from src.agent.client_factory import strip_thinking_tokens

        text = "<think>first</think>Hello <think>second</think>world"
        assert strip_thinking_tokens(text) == "Hello world"
