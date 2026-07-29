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

    _apply_component_overrides(defaults, cfg, component)
    return defaults


def _apply_component_overrides(defaults: dict, cfg, component: str) -> None:
    """Apply per-component overrides from anthropic.overrides.<component>."""
    overrides = cfg.anthropic.get("overrides", {})
    if not isinstance(overrides, dict) or component not in overrides:
        return
    component_overrides = overrides[component]
    if not isinstance(component_overrides, dict):
        return
    for key in ("backend", "model", "max_tokens"):
        if key in component_overrides:
            defaults[key] = component_overrides[key]


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


def _build_litellm(resolved: dict, component: str, sync: bool):
    base_url = resolved["litellm_base_url"]
    api_key = resolved["litellm_api_key"]
    if not base_url:
        raise ValueError("anthropic.litellm_base_url required when backend is 'litellm'")
    logger.info(
        "LiteLLM backend for %s (model=%s, url=%s)",
        component,
        resolved["model"],
        base_url,
    )
    if sync:
        return anthropic.Anthropic(base_url=base_url, api_key=api_key)
    return anthropic.AsyncAnthropic(base_url=base_url, api_key=api_key)


def _build_vertex(resolved: dict, component: str, sync: bool):
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
            component,
            project_id,
            region,
            creds_path,
        )
    else:
        logger.info(
            "Vertex AI backend for %s (project=%s, region=%s, ADC)",
            component,
            project_id,
            region,
        )
    if sync:
        return anthropic.AnthropicVertex(**kwargs)
    return anthropic.AsyncAnthropicVertex(**kwargs)


def _build_bedrock(resolved: dict, component: str, sync: bool):
    region = resolved["bedrock_region"]
    logger.info("Bedrock backend for %s (region=%s)", component, region)
    if sync:
        return anthropic.AnthropicBedrock(aws_region=region)
    return anthropic.AsyncAnthropicBedrock(aws_region=region)


def _build_api(resolved: dict, component: str, sync: bool):
    api_key = resolved["api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")
    logger.info("Direct Anthropic API backend for %s", component)
    if sync:
        return anthropic.Anthropic(api_key=api_key)
    return anthropic.AsyncAnthropic(api_key=api_key)


_BACKEND_BUILDERS = {
    "litellm": _build_litellm,
    "vertex": _build_vertex,
    "bedrock": _build_bedrock,
}


def _build_from_resolved(resolved: dict, component: str, *, sync: bool):
    """Construct the correct client from resolved config."""
    builder = _BACKEND_BUILDERS.get(resolved["backend"], _build_api)
    return builder(resolved, component, sync)


def strip_thinking_tokens(text: str) -> str:
    """Strip <think>...</think> blocks emitted by open-source models."""
    if not text or "<think>" not in text:
        return text
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
