# LiteMaaS Setup Guide

LiteMaaS is our internal LiteLLM proxy that speaks the **Anthropic Messages API protocol**. Any code using the standard `anthropic` Python SDK can switch to LiteMaaS with zero code changes — just override `base_url` and `api_key`.

## Quick Start (Local / Claude Code)

```python
import anthropic

client = anthropic.Anthropic(
    base_url="https://<your-litemaas-endpoint>",
    api_key="<your-litemaas-key>",
)

response = client.messages.create(
    model="claude-sonnet-4-6",  # any model available on LiteMaaS
    max_tokens=4096,
    messages=[{"role": "user", "content": "Hello"}],
)
```

The API key is a **LiteMaaS-issued key** (not an Anthropic key). Generate one via `POST /key/generate` on the LiteMaaS admin API using the master key.

## Parsec Configuration

### Config file

Set in `config/config.local.yaml` (gitignored):

```yaml
anthropic:
  backend: "litellm"
  model: "claude-sonnet-4-6"
  litellm_base_url: "https://<your-litemaas-endpoint>"
  litellm_api_key: "<your-litemaas-key>"
```

### Environment variables

Dynaconf uses the `PARSEC_` prefix with `__` for nesting:

```bash
PARSEC_ANTHROPIC__BACKEND=litellm
PARSEC_ANTHROPIC__MODEL=claude-sonnet-4-6
PARSEC_ANTHROPIC__LITELLM_BASE_URL=https://<your-litemaas-endpoint>
PARSEC_ANTHROPIC__LITELLM_API_KEY=<your-litemaas-key>
```

## Per-Component Model Overrides

Different components can use different models through the same proxy:

```yaml
anthropic:
  backend: "litellm"
  model: "claude-sonnet-4-6"         # default for all components
  litellm_base_url: "https://<your-litemaas-endpoint>"
  litellm_api_key: "<your-litemaas-key>"
  overrides:
    orchestrator:
      model: "qwen3-14b"             # cheaper model for routing
    aap2:
      model: "claude-opus-4-6"       # stronger model for complex debugging
```

Each component key (`orchestrator`, `cost`, `aap2`, `babylon`, `security`, `ocpv`, `icinga`, `learnings`, `aap2_fix`) can override `backend`, `model`, and `max_tokens`. The `litellm_base_url` and `litellm_api_key` are always shared.

## How It Works

The client factory (`src/agent/client_factory.py`) creates a standard Anthropic SDK client pointed at the LiteMaaS proxy:

```python
def _build_litellm(resolved, component, sync):
    base_url = resolved["litellm_base_url"]
    api_key = resolved["litellm_api_key"]
    if sync:
        return anthropic.Anthropic(base_url=base_url, api_key=api_key)
    return anthropic.AsyncAnthropic(base_url=base_url, api_key=api_key)
```

All callers use the same pattern:

```python
from src.agent.client_factory import build_client, resolve_model

client = build_client(cfg, "cost")       # sync client for cost agent
model = resolve_model(cfg, "cost")       # resolved model name
```

## OpenShift Deployment

For deployed environments, the API key is stored in a Kubernetes Secret and injected as an env var:

```yaml
# In parsec-secrets:
litellm-api-key: "<your-litemaas-key>"

# In the Deployment spec:
- name: PARSEC_ANTHROPIC__LITELLM_API_KEY
  valueFrom:
    secretKeyRef:
      name: parsec-secrets
      key: litellm-api-key
```

The base URL and backend are set as plain env vars in the ConfigMap. Per-component overrides are expanded from the `litellm_overrides` Ansible variable:

```yaml
# In playbooks/vars/dev.yml or prod.yml:
anthropic_backend: "litellm"
anthropic_model: "claude-sonnet-4-6"
litellm_base_url: "https://<your-litemaas-endpoint>"
litellm_api_key: "<your-litemaas-key>"
```

## Available Models

LiteMaaS supports multiple model families through a single endpoint:

- **Claude** — sonnet, opus, haiku
- **Qwen3** — 14b and other sizes
- **Granite**, **Llama**, **DeepSeek**, **Gemini**

Open-source models (Qwen, Granite) emit `<think>...</think>` blocks during reasoning. The `strip_thinking_tokens()` utility in `client_factory.py` strips these before presenting results to users.

## Key Files

| File | Purpose |
|------|---------|
| `src/agent/client_factory.py` | Client construction, config resolution, backend dispatch |
| `src/config.py` | Dynaconf loader (`config.yaml` → `config.local.yaml`) |
| `config/config.yaml` | Base config with all LiteLLM keys (commented out) |
| `playbooks/tasks/secrets.yml` | Creates `parsec-secrets` K8s Secret |
| `playbooks/templates/manifests.yaml.j2` | Deployment env vars and secret refs |
