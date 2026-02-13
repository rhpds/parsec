# Parsec

Natural language cloud cost investigation tool. Investigators type questions in a chat UI, and Claude queries the provision DB, AWS Cost Explorer, Azure billing CSVs, and GCP BigQuery to answer them.

## Project Structure

```
src/
  app.py                    # FastAPI app, lifespan, static mount
  config.py                 # Dynaconf settings
  agent/
    orchestrator.py          # Claude tool-use loop
    tool_definitions.py      # Tool schemas for Claude API
    system_prompt.py         # DB schema, abuse indicators, instructions
    streaming.py             # SSE helpers
  tools/
    provision_db.py          # Raw SQL against provision DB (read-only)
    aws_costs.py             # AWS Cost Explorer queries
    azure_costs.py           # Azure billing CSV queries
    gcp_costs.py             # GCP BigQuery billing queries
  connections/
    postgres.py              # asyncpg pool
    aws.py                   # boto3 session
    azure.py                 # Azure blob client
    gcp.py                   # BigQuery client
  routes/
    query.py                 # POST /api/query (SSE), GET /api/reports/{filename}
    health.py                # GET /api/health, /api/health/ready
static/                      # Chat UI (plain HTML/CSS/JS, no build step)
config/
  config.yaml                # Base config (no secrets)
  config.local.yaml.template # Local dev secrets template
```

## Running Locally

```bash
cp config/config.local.yaml.template config/config.local.yaml
# Fill in DB creds, ANTHROPIC_API_KEY, cloud credentials, allowed_users

pip install -r requirements.txt
uvicorn src.app:app --host 0.0.0.0 --port 8000
```

Or with Docker:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker-compose up --build
```

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

- **OpenShift**: OAuth proxy sidecar handles SSO. User lockdown via `parsec-allowed-users` ConfigMap.
- **Local dev**: Set `auth.allowed_users` in `config.local.yaml` (empty = all users allowed).
- The OAuth proxy passes `X-Forwarded-Email` / `X-Forwarded-User` headers to the app.

## Abuse Indicators

- **AWS GPU**: g4dn.*, g5.*, g6.*, p3.*, p4.*, p5.*
- **AWS large/metal**: *.metal, *.96xlarge, *.48xlarge, *.24xlarge
- **AWS Lightsail**: Large Windows instances, especially ap-south-1
- **Azure GPU**: NC, ND, NV series (meterSubCategory)
- **Suspicious**: External users with 50+ provisions in 90 days
- **Disposable emails**: Multiple accounts from temporary email domains

## OpenShift Deployment

```bash
# Dev
oc apply -k openshift/overlays/dev/

# Prod
oc apply -k openshift/overlays/prod/
```

Required secrets: `parsec-secrets` (API key, DB creds), `oauth-proxy-secret` (client-id, client-secret, session_secret).  <!-- pragma: allowlist secret -->

## Report Generation

Users can ask for reports in the chat ("generate a report of findings"). Claude uses the `generate_report` tool to produce `.md` or `.adoc` files, saved to `/app/reports/` with a download link in the UI.

## Development Notes

### Branching

- `main` — development branch, deploys to `parsec-dev` namespace
- `production` — stable branch, deploys to `parsec` namespace
- Always create feature branches off `main` for changes
- PRs target `main` and must pass CI (quality-gates + docker-build)

### CI Pipeline

Quality gates: black (formatting), ruff (linting), mypy (type checking), bandit (security).
Docker build: multi-stage UBI 9 image with verify step.
Both must pass before merge.

### Key Technical Decisions

- **Dockerfile**: Uses `VIRTUAL_ENV` env var and `ENTRYPOINT []` to override the ubi9/python-311 S2I base image's bash profiles that prepend `/opt/app-root/bin` to PATH. Without this, `python` resolves to the base image's interpreter instead of the venv.
- **Claude backend**: Supports direct API, Vertex AI, and AWS Bedrock. Production uses Vertex AI (`claude-sonnet-4@20250514`).
- **Cost-monitor `breakdown`/`drilldown` endpoints are AWS-only** — for Azure/GCP breakdowns, use `query_azure_costs` or `query_gcp_costs` directly.
- **Azure `gpu_cost` auto-detection**: The `query_azure_costs` tool automatically detects NC/ND/NV series VMs and reports a separate `gpu_cost` field per subscription.
- **Lazy DB init**: The provision DB pool retries on first query if startup initialization failed. Health readiness probe does NOT trigger DB init.
- **GitHub auth**: Use the `rhjcd` profile for push access to `rhpds/parsec` (`gh auth switch --user rhjcd`).

See `docs/TODO.md` for the full project backlog.
