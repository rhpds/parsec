# Parsec Development Notes

Session findings, architectural decisions, and context for future work.

## Codebase Overview (as of 2026-02-12)

### Architecture

- **Backend**: FastAPI app with Claude tool-use orchestration loop (max 10 rounds)
- **Frontend**: Plain HTML/CSS/JS with SSE streaming, Chart.js, marked.js (no build step)
- **8 tools**: query_provisions_db, query_aws_costs, query_azure_costs, query_gcp_costs, query_aws_pricing, query_cost_monitor, render_chart, generate_report
- **3 Claude backends**: Direct API, Vertex AI, AWS Bedrock (prod uses Vertex)
- **Config**: Dynaconf with `PARSEC_` prefix, layered YAML files

### Key Files by Size/Importance

| File | Purpose |
|------|---------|
| `src/agent/orchestrator.py` | Core agent loop, tool dispatch, history trimming |
| `src/agent/tool_definitions.py` | JSON schemas for all 8 tools |
| `config/agent_instructions.md` | System prompt — highest-impact file for behavior |
| `static/app.js` | Frontend SSE parser, chart/report rendering |
| `deploy.sh` | OpenShift deployment automation |

### Connection Architecture

| Backend | Type | Init | Error Recovery |
|---------|------|------|----------------|
| PostgreSQL | asyncpg pool (2-10) | Lazy — retries on first query | Pool re-init |
| AWS Cost Explorer | boto3 singleton | Startup | Returns error dict |
| Azure Blob | ContainerClient singleton | Startup (optional) | Returns None |
| GCP BigQuery | Client singleton | Startup (optional) | Returns None |
| cost-monitor | httpx per-request | None | Friendly error message |

## Findings from Initial Review

### Pre-existing CI Issues (Fixed in PR #1)

1. **5 mypy type errors** across `orchestrator.py`, `azure.py`, `cost_monitor.py`
   - `_build_client` return type didn't include Vertex/Bedrock variants
   - Azure credential union type missing
   - httpx params type too narrow
2. **Dockerfile venv not activating** — S2I base image profiles override PATH
   - Fix: `VIRTUAL_ENV` env var + `ENTRYPOINT []`
   - Was hidden because mypy failed first, skipping docker-build

### Agent Instructions Gaps (Fixed in PR #1)

- No investigation playbooks (added 4)
- cost-monitor breakdown/drilldown misleadingly described as cross-provider (AWS-only)
- No GCP abuse indicators (added A2/G2/N1 GPU types)
- Missing tool response format documentation
- Hardcoded date example instead of relative dates
- No guidance on parallel vs sequential tool calls
- No handling for truncated/empty/error results

### Items Still Needing Validation

- `provisions.last_state` values documented as: started, provisioned, retiring, retired, error
- `provisions.provision_result` values documented as: success, failed
- These need `SELECT DISTINCT` verification against production DB

## Git Workflow

- **GitHub org**: rhpds
- **Push access**: Use `rhjcd` profile (`gh auth switch --user rhjcd`)
- **Branches**: `main` (dev) → `production` (stable)
- **CI**: GitHub Actions — quality-gates (black/ruff/mypy/bandit) → docker-build → ci-status
- **Pre-commit**: detect-secrets, gitleaks, black, ruff, mypy, vulture, prettier, typos

## Open Work

- **PR #1** (improve-agent-instructions): Agent instructions + mypy + Dockerfile — CI green, pending review
- **PR #2** (add-docs-todo): docs/TODO.md + CONTRIBUTING.md move — CI green, depends on PR #1
- See `docs/TODO.md` for full backlog (21 items across 4 priority tiers)
