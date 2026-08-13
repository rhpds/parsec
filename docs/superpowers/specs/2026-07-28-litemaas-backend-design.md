# LiteMaaS / LiteLLM Backend Support

**Date:** 2026-07-28
**Status:** Draft
**Context:** [Slack thread] Eoghan O'Connor requesting LiteMaaS integration for Parsec; GPTEINFRA-17047 (longer-term LibreChat migration)

## Problem

Parsec's Anthropic client is hardcoded to three backends (api, vertex, bedrock) with no support for custom endpoints. The RHDP team operates a LiteLLM proxy at `maas-rhdp.apps.maas.redhatworkshops.io` that provides access to open-source models (Qwen3, Granite, Llama, DeepSeek) and proxied Claude models through a unified API. There is no way to point Parsec at this proxy today.

Additionally, all components (orchestrator, sub-agents, learnings, AAP2 fix analysis) share a single backend and model configuration. There is no way to run the orchestrator on a cheaper model while keeping sub-agents on Claude.

## Solution

Add a `litellm` backend type and per-component override configuration, implemented through a centralized client factory that replaces the 4 existing duplicated factory functions.

## LiteMaaS Instance Details

- **Endpoint:** `https://maas-rhdp.apps.maas.redhatworkshops.io`
- **Namespace:** `maas-rhdp` on `api.maas.redhatworkshops.io:6443`
- **Auth:** API key via `x-api-key` header (Anthropic SDK passes this as `api_key`)
- **Protocol:** LiteLLM Anthropic-compatible `/v1/messages` endpoint — confirmed working with Anthropic Python SDK including tool use
- **Models:** 31 available including Claude (opus-4-6, sonnet-4-6, haiku-4-5), Qwen3 (14b, 235b), Granite, Llama, DeepSeek, Gemini 2.5 Pro

## Config Structure

```yaml
anthropic:
  backend: "vertex"                    # default backend for all components
  model: "claude-sonnet-4-6"           # default model for all components
  max_tokens: 4096
  max_tool_rounds: 10

  # LiteLLM proxy settings (used when any component has backend: "litellm")
  litellm_base_url: ""                 # e.g. "https://maas-rhdp.apps.maas.redhatworkshops.io"
  litellm_api_key: ""                  # API key for the LiteLLM proxy

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

**Resolution order:** component override > top-level default.

**Backward compatibility:** The existing `orchestrator_model` config key is preserved. Precedence for the orchestrator's model: `overrides.orchestrator.model` > `orchestrator_model` > top-level `model`. The `orchestrator_model` key is only consulted when there is no override.

**Env var overrides:** Dynaconf nesting with `PARSEC_` prefix:
- `PARSEC_ANTHROPIC__LITELLM_BASE_URL=https://...`
- `PARSEC_ANTHROPIC__OVERRIDES__ORCHESTRATOR__BACKEND=litellm`
- `PARSEC_ANTHROPIC__OVERRIDES__ORCHESTRATOR__MODEL=qwen3-14b`

## Architecture

### New Module: `src/agent/client_factory.py`

Centralized client factory replacing 4 duplicated factory functions:

```
_build_client()           in orchestrator.py    -> build_client(cfg, "orchestrator")
_build_client()           in agents.py (import) -> build_client(cfg, agent_type)
_create_anthropic_client() in aap2_fix.py       -> build_client(cfg, "aap2_fix")
_analyze_direct/vertex/bedrock() in learnings.py -> build_async_client(cfg, "learnings")
```

**Public API:**

```python
def build_client(
    cfg, component: str = "default"
) -> anthropic.Anthropic | AnthropicVertex | AnthropicBedrock:
    """Build sync Anthropic client for a component."""

def build_async_client(
    cfg, component: str = "default"
) -> anthropic.AsyncAnthropic | AsyncAnthropicVertex | AsyncAnthropicBedrock:
    """Build async Anthropic client for a component."""

def resolve_model(cfg, component: str = "default") -> str:
    """Resolve the effective model name for a component."""

def resolve_max_tokens(cfg, component: str = "default") -> int:
    """Resolve the effective max_tokens for a component."""
```

**Internal config resolution:**

```python
def _resolve_config(cfg, component: str) -> dict:
    """Merge top-level defaults with component overrides.

    Returns dict with keys: backend, model, max_tokens,
    litellm_base_url, litellm_api_key, api_key,
    vertex_project_id, vertex_region, vertex_credentials_path,
    bedrock_region.
    """
```

1. Read all top-level `cfg.anthropic.*` keys into a dict
2. If `cfg.anthropic.overrides.<component>` exists, merge on top
3. Backward compat: if component is `orchestrator` and `orchestrator_model` is set but no override model, use `orchestrator_model`

**Backend construction:**

| Backend | Sync Client | Async Client |
|---------|-------------|--------------|
| `api` | `anthropic.Anthropic(api_key=...)` | `anthropic.AsyncAnthropic(api_key=...)` |
| `vertex` | `AnthropicVertex(project_id=..., region=..., credentials=...)` | `AsyncAnthropicVertex(...)` |
| `bedrock` | `AnthropicBedrock(aws_region=...)` | `AsyncAnthropicBedrock(...)` |
| `litellm` | `anthropic.Anthropic(base_url=..., api_key=...)` | `anthropic.AsyncAnthropic(base_url=..., api_key=...)` |

The `litellm` backend uses the standard `Anthropic` client — LiteLLM's `/v1/messages` endpoint accepts Anthropic-format requests and translates to whatever backend model is configured. No OpenAI SDK needed.

### Thinking-Token Handling

Open-source models (Qwen3, DeepSeek) emit `<think>...</think>` blocks containing chain-of-thought reasoning. These must be stripped before displaying to users.

A utility function in client_factory.py:

```python
def strip_thinking_tokens(text: str) -> str:
    """Strip <think>...</think> blocks from model responses."""
    return re.sub(r'<think>[\s\S]*?</think>', '', text).strip()
```

Applied in two locations:
- `orchestrator.py` — when extracting text blocks from responses (`_parse_response_blocks`)
- `agents.py` — same response parsing path

Applied unconditionally (Claude never emits these tags, so it's a no-op for Claude responses).

### Caller Changes

**`src/agent/orchestrator.py`:**
- Remove `_build_client()` function (~55 lines)
- Import `build_client`, `resolve_model`, `resolve_max_tokens` from `client_factory`
- Replace `model = cfg.anthropic.get("orchestrator_model", "") or cfg.anthropic.get("model", ...)` with `model = resolve_model(cfg, "orchestrator")`
- Replace `client = _build_client(cfg)` with `client = build_client(cfg, "orchestrator")`
- Apply `strip_thinking_tokens()` to text content blocks

**`src/agent/agents.py`:**
- Replace `from src.agent.orchestrator import _build_client` with import from `client_factory`
- `run_sub_agent()`: `build_client(cfg, agent_type)`, `resolve_model(cfg, agent_type)`
- `run_sub_agent_streaming()`: same changes
- Apply `strip_thinking_tokens()` to text content blocks

**`src/tools/aap2_fix.py`:**
- Remove `_create_anthropic_client()` (~19 lines) and `_create_vertex_client()` (~16 lines)
- Import from `client_factory`
- Replace with `build_client(cfg, "aap2_fix")`, `resolve_model(cfg, "aap2_fix")`

**`src/agent/learnings.py`:**
- Remove `_analyze_direct()`, `_analyze_vertex()`, `_analyze_bedrock()` (~70 lines total)
- Replace with single function using `build_async_client(cfg, "learnings")`, `resolve_model(cfg, "learnings")`

**`config/config.yaml`:**
- Add `litellm_base_url` and `litellm_api_key` (commented out)
- Add `overrides` block with commented examples

### Startup Logging

The factory logs which backend and model each component resolves to:

```
LiteLLM backend for orchestrator (model=qwen3-14b, url=https://maas-rhdp.apps...)
Vertex AI backend for cost (model=claude-sonnet-4-6, project=..., region=us-east5)
```

This makes it easy to verify the config resolution is working as expected.

## Not In Scope

- **API key provisioning** — Eoghan/MaaS team generates a Parsec-specific key; we consume it from config
- **Model quality validation** — no gating on which models operators choose; quality is their responsibility
- **OpenAI SDK** — not needed; LiteLLM's Anthropic endpoint handles format translation
- **Streaming** — LiteLLM's Anthropic endpoint supports SSE streaming; no special handling
- **UI changes** — purely backend configuration
- **Per-request model switching** — model is fixed per component at config time, not per-query

## Testing

- Unit tests for `_resolve_config()` — verify override merging, backward compat with `orchestrator_model`, default fallback
- Unit tests for `build_client()` — verify correct client type returned per backend
- Unit tests for `strip_thinking_tokens()` — verify stripping of `<think>` blocks, no-op on clean text
- Integration test: mock LiteLLM endpoint, verify full request/response cycle through the factory

## Risk

- **Tool-use quality** — open-source models have weaker tool-use reasoning than Claude. The orchestrator (simple routing) is the safest candidate; sub-agents doing multi-round tool use with complex tools may degrade significantly. This is an operator decision, not something we gate in code.
- **Thinking tokens** — the `<think>` strip regex handles the known pattern. If a model uses a different convention, it would leak through. The strip is simple enough to extend.
- **Proxy availability** — LiteMaaS is another failure point. If the proxy is down and the orchestrator is configured to use it, the entire query fails. No fallback mechanism is included (would add complexity for a rare case).
