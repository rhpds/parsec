# Parsec

Natural language cloud cost investigation tool. Investigators type questions in a chat UI, and Claude queries the provision DB, AWS Cost Explorer, Azure billing CSVs, GCP BigQuery, CloudTrail Lake, individual AWS member accounts, Babylon clusters, and GitHub repos to answer them.

For detailed subsystem documentation, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Project Structure

```
src/
  app.py                    # FastAPI app, lifespan, static mount
  config.py                 # Dynaconf settings
  agent/
    orchestrator.py          # Claude tool-use loop (orchestrator + sub-agent dispatch)
    agents.py                # Sub-agent runner (cost, aap2, babylon, security, ocpv)
    tool_definitions.py      # Tool schemas for Claude API (per-agent tool groups)
    system_prompt.py         # Per-agent prompt loading with shared context
    streaming.py             # SSE helpers
    learnings.py             # Post-conversation AI analysis and learning
    log_trimmer.py           # Token-aware history trimming
  tools/
    provision_db.py          # Raw SQL against provision DB (read-only)
    aws_costs.py             # AWS Cost Explorer queries
    aws_pricing.py           # EC2 pricing lookup (static cache, no AWS creds)
    aws_capacity_manager.py  # ODCR metrics from EC2 Capacity Manager
    aws_account.py           # Cross-account member account inspection (read-only)
    aws_accounts.py          # AWS Organizations account listing
    cloudtrail.py            # CloudTrail Lake queries (org-wide API events)
    marketplace_agreements.py # DynamoDB marketplace agreement inventory queries
    azure_costs.py           # Azure billing queries (SQLite cache + live CSV fallback)
    gcp_costs.py             # GCP BigQuery billing queries
    cost_monitor.py          # Cost-monitor data service API client
    babylon.py               # Babylon cluster catalog/deployment queries
    aap2.py                  # AAP2 controller job queries (REST API)
    ocpv.py                  # OCPV cluster inspection (VMs, nodes, pods, PVCs)
    splunk.py                # Splunk log queries (Babylon pods, AAP2 logs)
    github_files.py          # GitHub file/directory fetching via remote MCP server
  connections/
    postgres.py              # asyncpg pool
    aws.py                   # boto3 session
    azure.py                 # Azure blob client
    gcp.py                   # BigQuery client
    babylon.py               # Babylon cluster K8s API clients (httpx-based)
    aap2.py                  # AAP2 controller REST API clients (httpx-based)
    ocpv.py                  # OCPV cluster K8s API clients (httpx-based)
    splunk.py                # Splunk REST API client
    github_mcp.py            # GitHub remote MCP server client (streamable HTTP)
  routes/
    query.py                 # GET /api/auth/check, POST /api/query (SSE), GET /api/reports/{filename}
    alert.py                 # POST /api/alert/investigate (alert investigation API)
    conversations.py         # Conversation persistence (save/load/list/delete)
    share.py                 # Conversation sharing (public read-only links)
    learnings.py             # Admin API for agent learnings (view/clear)
    health.py                # GET /api/health, /api/health/ready
static/                      # Chat UI (plain HTML/CSS/JS, no build step)
config/
  config.yaml                # Base config (no secrets)
  prompts/                   # Per-agent system prompts
    orchestrator.md          # Orchestrator routing instructions
    cost_agent.md            # Cost investigation agent
    aap2_agent.md            # AAP2 job investigation agent
    babylon_agent.md         # Babylon catalog/deployment agent
    security_agent.md        # Security investigation agent
    ocpv_agent.md            # OCPV cluster inspection agent
    shared_context.md        # Shared context prepended to all agent prompts
data/
  ec2_pricing.json           # Static EC2 pricing cache (checked into git)
scripts/
  refresh_pricing.py         # Refreshes ec2_pricing.json from public AWS API
  refresh_azure_billing.py   # Refreshes azure_billing.db from Azure Blob Storage
  local-server.sh            # Local dev server management (start/stop/restart/status)
playbooks/
  deploy.yaml                # Ansible deployment playbook
  templates/
    manifests.yaml.j2         # All OpenShift manifests (Jinja2 template)
  tasks/                     # mgmt-rbac, secrets, oauth, apply-manifests, wait-for-builds
  vars/                      # common.yml (committed), dev.yml/prod.yml (gitignored)
```

## Running Locally

```bash
cp config/config.local.yaml.template config/config.local.yaml
# Fill in DB creds, ANTHROPIC_API_KEY, cloud credentials, allowed_users

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn src.app:app --host 0.0.0.0 --port 8000
```

**Important:** Always activate the venv first (`source .venv/bin/activate`).
If you skip activation, run directly with `.venv/bin/python -m uvicorn src.app:app --host 0.0.0.0 --port 8000`.

## Configuration

Dynaconf with `PARSEC_` env var prefix. Nesting uses `__`:

```bash
PARSEC_ANTHROPIC__API_KEY=sk-ant-...
PARSEC_PROVISION_DB__HOST=db.example.com
PARSEC_AUTH__ALLOWED_USERS=user1@redhat.com,user2@redhat.com
```

Files loaded in order: `config/config.yaml` → `config/config.local.yaml` (gitignored).

## Key Database Tables

- `provisions` — sandbox/account provisioning records
- `users` — user emails and profiles
- `catalog_items` — catalog item definitions (`name LIKE 'zt-%'` for zero-touch)
- `provision_request` — links provisions to root catalog items
- `catalog_resource` — subcomponent relationships

### Important Patterns

Effective catalog item name:
```sql
COALESCE(ci_root.name, ci_component.name)
```

External users:
```sql
WHERE u.email NOT LIKE '%@redhat.com'
  AND u.email NOT LIKE '%@opentlc.com'
  AND u.email NOT LIKE '%@demo.redhat.com'
```

## Auth

- **OpenShift**: OAuth proxy handles SSO. Authorization enforced at app level by querying OpenShift groups via K8s API (cached 60s). Requires `parsec-oauth` ClusterRole (defined in `playbooks/templates/manifests.yaml.j2`).
- `auth.allowed_groups` in `config.yaml` — default: `rhpds-admins,parsec-local-users`
- `auth.allowed_users` — optional email whitelist fallback
- The `--openshift-group` flag on `ose-oauth-proxy` does NOT reliably enforce group membership. Do not use — query groups from the API instead.
- **Local dev**: Set `auth.allowed_groups` and/or `auth.allowed_users` in `config.local.yaml` (both empty = all users allowed).

## Abuse Indicators

- **AWS GPU**: g4dn.*, g5.*, g6.*, p3.*, p4.*, p5.*
- **AWS large/metal**: *.metal, *.96xlarge, *.48xlarge, *.24xlarge
- **AWS Lightsail**: Large Windows instances, especially ap-south-1
- **AWS instance names**: "Web-Created-VM" — strong indicator of compromised account
- **Azure GPU**: NC, ND, NV series (meterSubCategory)
- **Suspicious**: External users with 50+ provisions in 90 days
- **Disposable emails**: Multiple accounts from temporary email domains

## Development Notes

### Branching

- `main` — development branch, deploys to `parsec-dev` namespace
- `production` — stable branch, deploys to `parsec` namespace
- Always create feature branches off `main` for changes
- PRs target `main` and must pass CI (quality-gates + docker-build)
- **Pushes to `main` and `production` auto-trigger OpenShift builds** via GitHub webhooks. Do NOT manually trigger builds with `oc start-build` or rollouts with `oc rollout restart`.

### CI Pipeline

Quality gates: black (formatting), ruff (linting), mypy (type checking), bandit (security).
Docker build: multi-stage UBI 9 image with verify step.

**Pre-commit hooks** run locally on `git commit`. If `black` reformats a file, the commit
is rejected and the reformatted file is left staged. Re-run `git add` and `git commit`
with the same message — do NOT amend the previous commit (the failed commit never happened).

### Key Technical Decisions

- **Dockerfile**: Do NOT use `ENTRYPOINT []` — CRI-O on OpenShift requires the S2I base image's `container-entrypoint`. Do NOT hardcode PATH — use `$PATH` to inherit base image paths.
- **Claude backend**: Supports direct API, Vertex AI, and AWS Bedrock. Production uses Vertex AI (`claude-sonnet-4@20250514`).
- **Sub-agent architecture**: Orchestrator classifies queries and dispatches to domain sub-agents (cost, aap2, babylon, security, ocpv). Fast-path classifier skips LLM call for obvious single-domain queries. Per-agent prompts in `config/prompts/`.
- **Lazy DB init**: The provision DB pool retries on first query if startup initialization failed. Health readiness probe does NOT trigger DB init.
- **GitHub auth**: Push access to `rhpds/parsec` requires a GitHub account with write permissions. Use `gh auth status` to check the current profile.
- **AWS IAM**: All AWS tools use the `cost-monitor` IAM user with `CostMonitorPolicy`. Cross-account access uses STS AssumeRole with inline session policy for read-only enforcement. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full policy details.

See `docs/TODO.md` for the full project backlog.
