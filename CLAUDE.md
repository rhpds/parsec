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
    aws_capacity_manager.py  # ODCR metrics from EC2 Capacity Manager
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

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn src.app:app --host 0.0.0.0 --port 8000
```

**Important:** Always activate the venv first (`source .venv/bin/activate`).
The system Python does not have project dependencies installed. If you skip
activation, run directly with `.venv/bin/python -m uvicorn src.app:app --host 0.0.0.0 --port 8000`.

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

- **OpenShift**: OAuth proxy handles SSO authentication only (passes `X-Forwarded-Email` / `X-Forwarded-User`). Authorization is enforced at the app level by querying OpenShift groups via the Kubernetes API.
  - The app queries `user.openshift.io/v1/groups` using the service account token (same pattern as Babylon's catalog API). Results are cached for 60 seconds.
  - `auth.allowed_groups` in `config.yaml` lists allowed OpenShift groups (comma-separated). Default: `rhpds-admins,parsec-local-users`.
  - `parsec-local-users` is a cluster-scoped OpenShift group for non-SSO test accounts. It must be created manually on a fresh cluster:
    ```bash
    oc adm groups new parsec-local-users
    oc adm groups add-users parsec-local-users <user>
    ```
    Note: SSO users have long OpenShift usernames (e.g. `demo-platform-ops+rhdp-test-user1@redhat.com`). Use `oc get users` to find the exact name.
  - `auth.allowed_users` provides an optional email whitelist fallback. Users matching either groups or email list are allowed.
  - The `--openshift-group` flag on `ose-oauth-proxy` does NOT reliably enforce group membership, and the proxy does NOT forward `X-Forwarded-Groups`. Do not use either — query groups from the API instead.
- **Local dev**: Set `auth.allowed_groups` and/or `auth.allowed_users` in `config.local.yaml` (both empty = all users allowed).

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
- **Pushes to `main` and `production` auto-trigger OpenShift builds** via GitHub webhooks.
  Do NOT manually trigger builds with `oc start-build`. If webhooks stop working, check
  that the GitHub webhook secrets match the BuildConfig trigger secrets (they get out of
  sync when `deploy.sh` regenerates them).

### CI Pipeline

Quality gates: black (formatting), ruff (linting), mypy (type checking), bandit (security).
Docker build: multi-stage UBI 9 image with verify step.
Both must pass before merge.

**Pre-commit hooks** run locally on `git commit`. If `black` reformats a file, the commit
is rejected and the reformatted file is left staged. Re-run `git add` and `git commit`
with the same message — do NOT amend the previous commit (the failed commit never happened).

**Do NOT manually trigger builds** with `oc start-build` or rollouts with `oc rollout restart`.
Pushes to `main` and `production` auto-trigger builds via GitHub webhooks. When the build
completes, the new image is pushed to the ImageStream, and the deployment's `image.openshift.io/triggers`
annotation auto-triggers a rollout. Monitor with `oc get builds -n <namespace>`.

### Key Technical Decisions

- **Dockerfile**: Prepends the venv to PATH with `$PATH` inheritance. Do NOT use `ENTRYPOINT []` — CRI-O on OpenShift requires the S2I base image's `container-entrypoint` (which does `exec "$@"`) to properly exec the CMD. Without it, executables get interpreted as shell scripts causing `null: command not found`. Do NOT hardcode PATH either — use `$PATH` to inherit the base image paths.
- **Claude backend**: Supports direct API, Vertex AI, and AWS Bedrock. Production uses Vertex AI (`claude-sonnet-4@20250514`).
- **Cost-monitor `breakdown`/`drilldown` endpoints are AWS-only** — for Azure/GCP breakdowns, use `query_azure_costs` or `query_gcp_costs` directly.
- **Azure `gpu_cost` auto-detection**: The `query_azure_costs` tool automatically detects NC/ND/NV series VMs and reports a separate `gpu_cost` field per subscription.
- **Lazy DB init**: The provision DB pool retries on first query if startup initialization failed. Health readiness probe does NOT trigger DB init.
- **AWS Capacity Manager (ODCRs)**: Uses `get_capacity_manager_metric_data` API from the payer account in us-east-1 with Organizations access. The `get_capacity_manager_metric_dimensions` API does not reliably return results, so inventory uses metric data grouped by `reservation-id` instead. RHDP ODCRs are transient (1-2 hours during provisioning) — the tool filters out ODCRs with < 24 hours of activity. Historical analysis (Nov 2025 – Feb 2026, 87k+ ODCRs) confirmed zero persistent waste. The Capacity Manager GUI's low utilization % reflects the brief startup window, not real waste.
- **GitHub auth**: Push access to `rhpds/parsec` requires a GitHub account with write permissions. Use `gh auth status` to check the current profile.
- **ClusterRoleBindings**: Each environment has its own CRB (`parsec-oauth-dev`, `parsec-oauth-prod`) defined in the overlay, not the base. This avoids conflicts when applying overlays independently.

See `docs/TODO.md` for the full project backlog.
