# Parsec TODO

Tracked improvements, tech debt, and feature ideas.

## High Priority

- [ ] **Add tests** — pytest is in dev deps but no test files exist. Priority areas:
  - SQL validation (`provision_db.validate_sql`) — test injection blocking, allowed patterns
  - Tool response parsing (mock Claude API, verify tool dispatch)
  - Health endpoint responses
  - Auth/allowed-users enforcement in query route
- [ ] **Verify `last_state` / `provision_result` values against production DB** — the agent instructions document common values (started, provisioned, retiring, retired, error) but these need validation with `SELECT DISTINCT` queries against prod
- [ ] **Update README data sources table** — lists only 4 tools but there are now 8 (missing `query_aws_pricing`, `query_cost_monitor`, `render_chart`, `generate_report`)
- [ ] **Make bandit security scan a gate** — currently `bandit -r src/ ... || true` always passes; consider failing CI on high-severity findings

## Medium Priority

- [ ] **Extend cost-monitor for Azure/GCP breakdowns** — breakdown and drilldown endpoints are AWS-only (`/api/v1/costs/aws/...`); Azure/GCP users must fall back to raw tools
- [ ] **Add conversation persistence** — history is only stored client-side in JS variable; refreshing the page loses all context. Consider server-side session storage or local storage
- [ ] **Add rate limiting on `/api/query`** — no throttling exists; a user could flood the endpoint with expensive Claude + Cost Explorer calls
- [ ] **Streaming response for tool execution** — currently tool results are sent after full execution; long-running CE queries show no progress
- [ ] **Add GCP account/project lookup from provisions** — AWS and Azure have clear provision-to-account mappings but GCP provisions don't link to project IDs, so `query_gcp_costs` can't be filtered per-user

## Low Priority

- [ ] **Frontend error handling for network failures** — if the SSE stream drops mid-response, the UI shows no error; add reconnection or failure state
- [ ] **Add OpenAPI documentation** — FastAPI auto-generates `/docs` but it's not exposed or documented; could help API consumers
- [ ] **Clean up noqa/type-ignore comments** — `azure_costs.py` has a `# noqa: typos:ignore` directive that ruff warns about (not valid noqa syntax); `orchestrator.py` uses `# type: ignore[arg-type]` for TOOLS — consider typing TOOLS as `list[ToolParam]` properly
- [ ] **Add `from __future__ import annotations`** consistently — only `orchestrator.py` has it; add to other files for consistent deferred annotation evaluation
- [ ] **Dead code audit** — vulture pre-commit hook is configured but skipped in CI; run it manually and clean up any findings
- [ ] **Report cleanup** — `/app/reports/` grows unbounded; add a periodic cleanup or max-age policy
- [ ] **Multi-tab/user support** — conversation history is global in the browser; opening two tabs shares state unexpectedly

## Ideas / Future

- [ ] **Saved investigations** — let users save and share investigation sessions (questions + findings)
- [ ] **Alerting integration** — auto-flag suspicious patterns (GPU abuse, high spend) and notify via Slack/email
- [ ] **Cost attribution dashboard** — pre-built views for common questions (top users by cost, GPU usage trends)
- [ ] **Support for more cloud cost APIs** — AWS Savings Plans, Reserved Instance coverage, Azure Advisor recommendations
- [ ] **Prompt caching** — enable Anthropic prompt caching for the system prompt + tool definitions to reduce latency and cost
