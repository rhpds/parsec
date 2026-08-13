# LiteMaaS / LiteLLM Backend Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add LiteLLM as a 4th backend type with per-component model/backend overrides, replacing 4 duplicated client factories with one centralized module.

**Architecture:** New `src/agent/client_factory.py` provides `build_client(cfg, component)`, `build_async_client(cfg, component)`, `resolve_model(cfg, component)`, and `resolve_max_tokens(cfg, component)`. Config resolution merges top-level `anthropic.*` defaults with per-component `anthropic.overrides.<component>.*` overrides. The `litellm` backend creates a standard `anthropic.Anthropic(base_url=..., api_key=...)` client pointing at a LiteLLM proxy's `/v1/messages` endpoint.

**Tech Stack:** Python, `anthropic` SDK (existing), Dynaconf config, pytest

**Spec:** `docs/superpowers/specs/2026-07-28-litemaas-backend-design.md`

## Global Constraints

- Python 3.11+, all code passes `black`, `ruff`, `mypy`, `bandit`
- No new pip dependencies — the `litellm` backend reuses the existing `anthropic` SDK
- The `orchestrator_model` config key must continue to work for backward compatibility
- Always activate venv first: `source .venv/bin/activate`
- After any code change, restart local server: `scripts/local-server.sh restart`

---

### Task 1: Client Factory Module — Config Resolution and Client Builders

**Files:**
- Create: `src/agent/client_factory.py`
- Create: `tests/test_client_factory.py`

**Interfaces:**
- Consumes: Dynaconf `cfg` object (same shape as `get_config()` returns)
- Produces:
  - `build_client(cfg, component: str = "default") -> anthropic.Anthropic | AnthropicVertex | AnthropicBedrock` — sync client
  - `build_async_client(cfg, component: str = "default") -> AsyncAnthropic | AsyncAnthropicVertex | AsyncAnthropicBedrock` — async client
  - `resolve_model(cfg, component: str = "default") -> str` — effective model name
  - `resolve_max_tokens(cfg, component: str = "default") -> int` — effective max_tokens
  - `strip_thinking_tokens(text: str) -> str` — strips `<think>...</think>` blocks

- [ ] **Step 1: Write failing tests for config resolution**

```python
# tests/test_client_factory.py
"""Tests for the centralized client factory."""

import re
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

        cfg = _make_cfg({
            "model": "claude-sonnet-4-6",
            "overrides": {"aap2": {"model": "claude-opus-4-6"}},
        })
        assert resolve_model(cfg, "aap2") == "claude-opus-4-6"

    def test_orchestrator_model_backward_compat(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg({
            "model": "claude-sonnet-4-6",
            "orchestrator_model": "claude-haiku-4-5",
        })
        assert resolve_model(cfg, "orchestrator") == "claude-haiku-4-5"

    def test_override_beats_orchestrator_model(self):
        from src.agent.client_factory import resolve_model

        cfg = _make_cfg({
            "model": "claude-sonnet-4-6",
            "orchestrator_model": "claude-haiku-4-5",
            "overrides": {"orchestrator": {"model": "qwen3-14b"}},
        })
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

        cfg = _make_cfg({
            "max_tokens": 4096,
            "overrides": {"orchestrator": {"max_tokens": 2048}},
        })
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
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-env-key"}):  # pragma: allowlist secret
            build_client(cfg, "cost")
        mock_cls.assert_called_once_with(api_key="test-env-key")  # pragma: allowlist secret

    def test_api_backend_no_key_raises(self):
        from src.agent.client_factory import build_client

        cfg = _make_cfg({"backend": "api"})
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                build_client(cfg, "cost")

    @patch("anthropic.Anthropic")
    def test_litellm_backend(self, mock_cls):
        from src.agent.client_factory import build_client

        cfg = _make_cfg({
            "backend": "api",
            "litellm_base_url": "https://maas.example.com",
            "litellm_api_key": "test-lite-key",  # pragma: allowlist secret
            "overrides": {"orchestrator": {"backend": "litellm"}},
        })
        build_client(cfg, "orchestrator")
        mock_cls.assert_called_once_with(
            base_url="https://maas.example.com",
            api_key="test-lite-key",  # pragma: allowlist secret
        )

    def test_litellm_backend_no_url_raises(self):
        from src.agent.client_factory import build_client

        cfg = _make_cfg({
            "backend": "litellm",
        })
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

        cfg = _make_cfg({
            "backend": "vertex",
            "vertex_project_id": "my-proj",
            "litellm_base_url": "https://maas.example.com",
            "litellm_api_key": "test-lite-key",  # pragma: allowlist secret
            "overrides": {"orchestrator": {"backend": "litellm"}},
        })
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

        cfg = _make_cfg({
            "backend": "litellm",
            "litellm_base_url": "https://maas.example.com",
            "litellm_api_key": "test-lite-key",  # pragma: allowlist secret
        })
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate
python3 -m pytest tests/test_client_factory.py -v
```

Expected: All tests fail with `ModuleNotFoundError: No module named 'src.agent.client_factory'`

- [ ] **Step 3: Write the client_factory module**

```python
# src/agent/client_factory.py
"""Centralized Anthropic client factory with per-component overrides.

Replaces duplicated client construction in orchestrator.py, agents.py,
aap2_fix.py, and learnings.py. Supports 4 backends: api, vertex,
bedrock, litellm.
"""

from __future__ import annotations

import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_MAX_TOKENS = 4096


def _resolve_config(cfg, component: str) -> dict:
    """Merge top-level anthropic defaults with per-component overrides.

    Resolution order: overrides.<component>.<key> > top-level <key> > hardcoded default.
    Special case: orchestrator_model is a legacy shorthand for overrides.orchestrator.model.
    """
    defaults = {
        "backend": cfg.anthropic.get("backend", "api"),
        "model": cfg.anthropic.get("model", _DEFAULT_MODEL),
        "max_tokens": cfg.anthropic.get("max_tokens", _DEFAULT_MAX_TOKENS),
        "api_key": cfg.anthropic.get("api_key", ""),
        "litellm_base_url": cfg.anthropic.get("litellm_base_url", ""),
        "litellm_api_key": cfg.anthropic.get("litellm_api_key", ""),
        "vertex_project_id": cfg.anthropic.get("vertex_project_id", ""),
        "vertex_region": cfg.anthropic.get("vertex_region", "us-east5"),
        "vertex_credentials_path": cfg.anthropic.get("vertex_credentials_path", ""),
        "bedrock_region": cfg.anthropic.get("bedrock_region", ""),
    }

    # Vertex project_id fallback to gcp.project_id
    if not defaults["vertex_project_id"]:
        defaults["vertex_project_id"] = cfg.gcp.get("project_id", "")

    # Bedrock region fallback to aws.region
    if not defaults["bedrock_region"]:
        defaults["bedrock_region"] = cfg.aws.get("region", "us-east-1")

    # Backward compat: orchestrator_model as legacy shorthand
    if component == "orchestrator":
        legacy_model = cfg.anthropic.get("orchestrator_model", "")
        if legacy_model:
            defaults["model"] = legacy_model

    # Apply per-component overrides
    overrides = cfg.anthropic.get("overrides", {})
    if isinstance(overrides, dict) and component in overrides:
        component_overrides = overrides[component]
        if isinstance(component_overrides, dict):
            for key in ("backend", "model", "max_tokens"):
                if key in component_overrides:
                    defaults[key] = component_overrides[key]

    return defaults


def resolve_model(cfg, component: str = "default") -> str:
    """Resolve the effective model name for a component."""
    return _resolve_config(cfg, component)["model"]


def resolve_max_tokens(cfg, component: str = "default") -> int:
    """Resolve the effective max_tokens for a component."""
    return int(_resolve_config(cfg, component)["max_tokens"])


def build_client(cfg, component: str = "default"):
    """Build a sync Anthropic client for the given component.

    Returns anthropic.Anthropic, AnthropicVertex, or AnthropicBedrock
    depending on the resolved backend.
    """
    resolved = _resolve_config(cfg, component)
    return _build_from_resolved(resolved, component, sync=True)


def build_async_client(cfg, component: str = "default"):
    """Build an async Anthropic client for the given component.

    Returns AsyncAnthropic, AsyncAnthropicVertex, or AsyncAnthropicBedrock
    depending on the resolved backend.
    """
    resolved = _resolve_config(cfg, component)
    return _build_from_resolved(resolved, component, sync=False)


def _build_from_resolved(resolved: dict, component: str, *, sync: bool):
    """Construct the correct client from resolved config."""
    backend = resolved["backend"]

    if backend == "litellm":
        base_url = resolved["litellm_base_url"]
        api_key = resolved["litellm_api_key"]
        if not base_url:
            raise ValueError(
                "anthropic.litellm_base_url required when backend is 'litellm'"
            )
        logger.info(
            "LiteLLM backend for %s (model=%s, url=%s)",
            component,
            resolved["model"],
            base_url,
        )
        if sync:
            return anthropic.Anthropic(base_url=base_url, api_key=api_key)
        return anthropic.AsyncAnthropic(base_url=base_url, api_key=api_key)

    if backend == "vertex":
        project_id = resolved["vertex_project_id"]
        region = resolved["vertex_region"]
        if not project_id:
            raise ValueError(
                "anthropic.vertex_project_id or gcp.project_id required for Vertex backend"
            )
        kwargs: dict = {"project_id": project_id, "region": region}
        creds_path = resolved["vertex_credentials_path"]
        if creds_path and os.path.isfile(creds_path):
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            kwargs["credentials"] = credentials
            logger.info(
                "Vertex AI backend for %s (project=%s, region=%s, sa=%s)",
                component, project_id, region, creds_path,
            )
        else:
            logger.info(
                "Vertex AI backend for %s (project=%s, region=%s, ADC)",
                component, project_id, region,
            )
        if sync:
            return anthropic.AnthropicVertex(**kwargs)
        return anthropic.AsyncAnthropicVertex(**kwargs)

    if backend == "bedrock":
        region = resolved["bedrock_region"]
        logger.info("Bedrock backend for %s (region=%s)", component, region)
        if sync:
            return anthropic.AnthropicBedrock(aws_region=region)
        return anthropic.AsyncAnthropicBedrock(aws_region=region)

    # Default: direct API
    api_key = resolved["api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")
    logger.info("Direct Anthropic API backend for %s", component)
    if sync:
        return anthropic.Anthropic(api_key=api_key)
    return anthropic.AsyncAnthropic(api_key=api_key)


def strip_thinking_tokens(text: str) -> str:
    """Strip <think>...</think> blocks emitted by open-source models."""
    if not text or "<think>" not in text:
        return text
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
source .venv/bin/activate
python3 -m pytest tests/test_client_factory.py -v
```

Expected: All tests PASS

- [ ] **Step 5: Run quality gates**

```bash
source .venv/bin/activate
black src/agent/client_factory.py tests/test_client_factory.py
ruff check src/agent/client_factory.py tests/test_client_factory.py
mypy src/agent/client_factory.py
```

- [ ] **Step 6: Commit**

```bash
git add src/agent/client_factory.py tests/test_client_factory.py
git commit -m "feat: add centralized client factory with LiteLLM backend and per-component overrides"
```

---

### Task 2: Migrate Orchestrator and Agents to Client Factory

**Files:**
- Modify: `src/agent/orchestrator.py` — remove `_build_client()` (lines 459-513), update 3 call sites (lines 1210-1217, 1513-1518, 1279), update `_parse_response_blocks()` (lines 1031-1042)
- Modify: `src/agent/agents.py` — update imports (lines 543, 787), update 2 call sites (lines 569-573, 813-818), update `_parse_response_blocks()` (lines 379-390)

**Interfaces:**
- Consumes: `build_client`, `resolve_model`, `resolve_max_tokens`, `strip_thinking_tokens` from `src.agent.client_factory` (Task 1)
- Produces: Same public APIs as before — `investigate_streaming()`, `investigate_alert()`, `run_sub_agent()`, `run_sub_agent_streaming()` — now using the centralized factory

- [ ] **Step 1: Write a test that verifies orchestrator uses the factory**

```python
# tests/test_orchestrator_client_factory.py
"""Verify orchestrator uses centralized client factory."""

from unittest.mock import MagicMock, patch


def test_orchestrator_imports_from_client_factory():
    """Verify _build_client is no longer defined in orchestrator."""
    import src.agent.orchestrator as orch

    assert not hasattr(orch, "_build_client"), (
        "_build_client should be removed from orchestrator.py"
    )


def test_parse_response_blocks_strips_thinking_tokens():
    """Verify _parse_response_blocks strips <think> tags from text."""
    from src.agent.orchestrator import _parse_response_blocks

    block = MagicMock()
    block.type = "text"
    block.text = "<think>\nreasoning here\n</think>\nThe answer is 42."

    text_parts, tool_blocks = _parse_response_blocks([block])
    assert text_parts == ["The answer is 42."]
    assert tool_blocks == []


def test_parse_response_blocks_preserves_clean_text():
    """Verify _parse_response_blocks doesn't alter text without <think> tags."""
    from src.agent.orchestrator import _parse_response_blocks

    block = MagicMock()
    block.type = "text"
    block.text = "Normal response text."

    text_parts, _ = _parse_response_blocks([block])
    assert text_parts == ["Normal response text."]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate
python3 -m pytest tests/test_orchestrator_client_factory.py -v
```

Expected: `test_orchestrator_imports_from_client_factory` fails (function still exists), `test_parse_response_blocks_strips_thinking_tokens` fails (no stripping yet)

- [ ] **Step 3: Modify orchestrator.py**

In `src/agent/orchestrator.py`:

**3a.** Add import at the top (near existing imports):

```python
from src.agent.client_factory import (
    build_client,
    resolve_max_tokens,
    resolve_model,
    strip_thinking_tokens,
)
```

**3b.** Delete the `_build_client()` function (lines 459-513 — the entire function from `def _build_client` through the `return anthropic.Anthropic(api_key=api_key)` line).

**3c.** Update the orchestrator loop model/client resolution (around line 1210):

Replace:
```python
        model = cfg.anthropic.get("orchestrator_model", "") or cfg.anthropic.get(
            "model", "claude-sonnet-4-20250514"
        )
        max_tokens = cfg.anthropic.get("max_tokens", 4096)
```

With:
```python
        model = resolve_model(cfg, "orchestrator")
        max_tokens = resolve_max_tokens(cfg, "orchestrator")
```

**3d.** Update the orchestrator client creation (around line 1217):

Replace:
```python
            client = _build_client(cfg)
```

With:
```python
            client = build_client(cfg, "orchestrator")
```

**3e.** Update the alert investigation function (around line 1513):

Replace:
```python
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
    max_tokens = cfg.anthropic.get("max_tokens", 4096)
```

With:
```python
    model = resolve_model(cfg, "security")
    max_tokens = resolve_max_tokens(cfg, "security")
```

And replace:
```python
        client = _build_client(cfg)
```

With:
```python
        client = build_client(cfg, "security")
```

**3f.** Update `_parse_response_blocks()` (around line 1031) to strip thinking tokens:

Replace:
```python
        if block.type == "text":
            text_parts.append(block.text)
```

With:
```python
        if block.type == "text":
            cleaned = strip_thinking_tokens(block.text)
            if cleaned:
                text_parts.append(cleaned)
```

- [ ] **Step 4: Modify agents.py**

In `src/agent/agents.py`:

**4a.** Update the import in `run_sub_agent()` (around line 543):

Replace:
```python
    from src.agent.orchestrator import _build_client, _cap_tool_result, _trim_history
```

With:
```python
    from src.agent.client_factory import build_client, resolve_max_tokens, resolve_model
    from src.agent.orchestrator import _cap_tool_result, _trim_history
```

**4b.** Update model/client resolution in `run_sub_agent()` (around line 569):

Replace:
```python
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
    max_tokens = cfg.anthropic.get("max_tokens", 4096)

    if client is None:
        client = _build_client(cfg)
```

With:
```python
    model = resolve_model(cfg, agent_type)
    max_tokens = resolve_max_tokens(cfg, agent_type)

    if client is None:
        client = build_client(cfg, agent_type)
```

**4c.** Update the import in `run_sub_agent_streaming()` (around line 787):

Replace:
```python
        _build_client,
```

With (remove `_build_client` from the orchestrator import, add client_factory import before it):
```python
    from src.agent.client_factory import build_client, resolve_max_tokens, resolve_model
```

And remove `_build_client` from the `from src.agent.orchestrator import` line.

**4d.** Update model/client resolution in `run_sub_agent_streaming()` (around line 813):

Replace:
```python
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
    max_tokens = cfg.anthropic.get("max_tokens", 4096)
```

With:
```python
    model = resolve_model(cfg, agent_type)
    max_tokens = resolve_max_tokens(cfg, agent_type)
```

And replace:
```python
            client = _build_client(cfg)
```

With:
```python
            client = build_client(cfg, agent_type)
```

**4e.** Update `_parse_response_blocks()` (around line 379) to strip thinking tokens:

Add import at the top of the function or module level:
```python
from src.agent.client_factory import strip_thinking_tokens
```

Replace:
```python
        if block.type == "text":
            text_parts.append(block.text)
```

With:
```python
        if block.type == "text":
            cleaned = strip_thinking_tokens(block.text)
            if cleaned:
                text_parts.append(cleaned)
```

- [ ] **Step 5: Run tests**

```bash
source .venv/bin/activate
python3 -m pytest tests/test_orchestrator_client_factory.py tests/test_client_factory.py -v
```

Expected: All PASS

- [ ] **Step 6: Run existing test suite to verify no regressions**

```bash
source .venv/bin/activate
python3 -m pytest tests/ -x --timeout=60 -q
```

Expected: No new failures

- [ ] **Step 7: Run quality gates**

```bash
source .venv/bin/activate
black src/agent/orchestrator.py src/agent/agents.py
ruff check src/agent/orchestrator.py src/agent/agents.py
mypy src/agent/orchestrator.py src/agent/agents.py
```

- [ ] **Step 8: Commit**

```bash
git add src/agent/orchestrator.py src/agent/agents.py tests/test_orchestrator_client_factory.py
git commit -m "refactor: migrate orchestrator and agents to centralized client factory"
```

---

### Task 3: Migrate AAP2 Fix and Learnings to Client Factory

**Files:**
- Modify: `src/tools/aap2_fix.py` — remove `_create_vertex_client()` (lines 249-264), `_create_anthropic_client()` (lines 267-285), update call site (line 388)
- Modify: `src/agent/learnings.py` — remove `_analyze_direct()` (lines 282-299), `_analyze_vertex()` (lines 302-333), `_analyze_bedrock()` (lines 336-351), replace backend dispatch (lines 270-276)

**Interfaces:**
- Consumes: `build_client`, `build_async_client`, `resolve_model`, `strip_thinking_tokens` from `src.agent.client_factory` (Task 1)
- Produces: Same public APIs — `analyze_fix()` in aap2_fix.py, `extract_learnings()` in learnings.py

- [ ] **Step 1: Write failing tests**

```python
# tests/test_aap2_fix_client_factory.py
"""Verify aap2_fix uses centralized client factory."""


def test_aap2_fix_no_local_factory():
    """Verify local factory functions are removed from aap2_fix."""
    import src.tools.aap2_fix as mod

    assert not hasattr(mod, "_create_anthropic_client"), (
        "_create_anthropic_client should be removed"
    )
    assert not hasattr(mod, "_create_vertex_client"), (
        "_create_vertex_client should be removed"
    )
```

```python
# tests/test_learnings_client_factory.py
"""Verify learnings uses centralized client factory."""


def test_learnings_no_local_analyze_functions():
    """Verify backend-specific analyze functions are removed."""
    import src.agent.learnings as mod

    assert not hasattr(mod, "_analyze_direct"), "_analyze_direct should be removed"
    assert not hasattr(mod, "_analyze_vertex"), "_analyze_vertex should be removed"
    assert not hasattr(mod, "_analyze_bedrock"), "_analyze_bedrock should be removed"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate
python3 -m pytest tests/test_aap2_fix_client_factory.py tests/test_learnings_client_factory.py -v
```

Expected: All fail (functions still exist)

- [ ] **Step 3: Modify aap2_fix.py**

In `src/tools/aap2_fix.py`:

**3a.** Add import near the top:

```python
from src.agent.client_factory import build_client, resolve_model
```

**3b.** Delete `_create_vertex_client()` (lines 249-264) and `_create_anthropic_client()` (lines 267-285) — both entire functions.

**3c.** Update the call site in the fix analysis function (around line 388):

Replace:
```python
        client = _create_anthropic_client(cfg)
        if client is None:
            logger.info("AI not available: no API key configured")
            return None
```

With:
```python
        try:
            client = build_client(cfg, "aap2_fix")
        except ValueError:
            logger.info("AI not available: no API key configured")
            return None
```

**3d.** Update the model resolution (find where `model` is set earlier in the same function):

Replace:
```python
    model = cfg.anthropic.get("model", "claude-sonnet-4-6")
```

With:
```python
    model = resolve_model(cfg, "aap2_fix")
```

- [ ] **Step 4: Modify learnings.py**

In `src/agent/learnings.py`:

**4a.** Add import near the top:

```python
from src.agent.client_factory import build_async_client, resolve_model, strip_thinking_tokens
```

**4b.** Delete `_analyze_direct()` (lines 282-299), `_analyze_vertex()` (lines 302-333), and `_analyze_bedrock()` (lines 336-351) — all three functions.

**4c.** Replace the backend dispatch block (around lines 270-276):

Replace:
```python
    try:
        if backend == "bedrock":
            return await _analyze_bedrock(cfg, model, analysis_prompt)
        elif backend == "vertex":
            return await _analyze_vertex(cfg, model, analysis_prompt)
        else:
            return await _analyze_direct(cfg, model, analysis_prompt)
    except Exception:
        logger.exception("AI analysis call failed")
        return []
```

With:
```python
    try:
        client = build_async_client(cfg, "learnings")
        model = resolve_model(cfg, "learnings")
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        block = resp.content[0]
        if not isinstance(block, TextBlock):
            return []
        return _parse_analysis_response(strip_thinking_tokens(block.text))
    except ValueError:
        logger.info("AI not available for learnings extraction")
        return []
    except Exception:
        logger.exception("AI analysis call failed")
        return []
```

**4d.** Remove the now-unused `backend` and `model` variables set earlier (around lines 241-242):

Remove:
```python
    backend = cfg.anthropic.get("backend", "direct")
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
```

(These are now resolved inside the try block via `resolve_model`.)

- [ ] **Step 5: Run tests**

```bash
source .venv/bin/activate
python3 -m pytest tests/test_aap2_fix_client_factory.py tests/test_learnings_client_factory.py tests/test_client_factory.py -v
```

Expected: All PASS

- [ ] **Step 6: Run full test suite**

```bash
source .venv/bin/activate
python3 -m pytest tests/ -x --timeout=60 -q
```

Expected: No new failures

- [ ] **Step 7: Run quality gates**

```bash
source .venv/bin/activate
black src/tools/aap2_fix.py src/agent/learnings.py
ruff check src/tools/aap2_fix.py src/agent/learnings.py
mypy src/tools/aap2_fix.py src/agent/learnings.py
```

- [ ] **Step 8: Commit**

```bash
git add src/tools/aap2_fix.py src/agent/learnings.py tests/test_aap2_fix_client_factory.py tests/test_learnings_client_factory.py
git commit -m "refactor: migrate aap2_fix and learnings to centralized client factory"
```

---

### Task 4: Config File Update and Local Smoke Test

**Files:**
- Modify: `config/config.yaml` — add litellm and overrides config keys

**Interfaces:**
- Consumes: Client factory from Task 1 (reads these config keys)
- Produces: Updated config file with documented litellm and overrides options

- [ ] **Step 1: Update config/config.yaml**

Add the following under the `anthropic:` section, after the existing `bedrock_region` comment (around line 21):

```yaml
  # LiteLLM proxy settings (used when any component has backend: "litellm")
  # litellm_base_url: ""  # e.g. "https://maas-rhdp.apps.maas.redhatworkshops.io"
  # litellm_api_key: ""
  # Per-component overrides. Keys: orchestrator, cost, aap2, babylon,
  # security, ocpv, icinga, learnings, aap2_fix.
  # Each can override: backend, model, max_tokens.
  # Unspecified keys inherit from top-level defaults.
  # overrides:
  #   orchestrator:
  #     backend: "litellm"
  #     model: "qwen3-14b"
  #   aap2:
  #     model: "claude-opus-4-6"
```

- [ ] **Step 2: Run quality gates on yaml**

```bash
source .venv/bin/activate
python3 -c "import yaml; yaml.safe_load(open('config/config.yaml'))" && echo "YAML valid"
```

- [ ] **Step 3: Run full test suite one final time**

```bash
source .venv/bin/activate
python3 -m pytest tests/ -x --timeout=60 -q
```

Expected: All pass, no regressions

- [ ] **Step 4: Smoke test with local server (if possible)**

Set up `config/config.local.yaml` with a litellm override for the orchestrator and verify startup logs show the correct backend resolution:

```yaml
anthropic:
  litellm_base_url: "https://maas-rhdp.apps.maas.redhatworkshops.io"
  litellm_api_key: "<PARSEC_LITELLM_KEY>"
  overrides:
    orchestrator:
      backend: "litellm"
      model: "qwen3-14b"
```

```bash
scripts/local-server.sh restart
# Check logs for: "LiteLLM backend for orchestrator (model=qwen3-14b, url=https://maas-rhdp...)"
# Check logs for: "Vertex AI backend for cost (model=claude-sonnet-4-6, ...)" (or whichever default)
```

- [ ] **Step 5: Commit**

```bash
git add config/config.yaml
git commit -m "feat: add litellm and per-component override config options"
```
