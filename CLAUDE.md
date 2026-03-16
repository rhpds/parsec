# Parsec

Natural language cloud cost investigation tool. Investigators type questions in a chat UI, and Claude queries the provision DB, AWS Cost Explorer, Azure billing CSVs, GCP BigQuery, CloudTrail Lake, individual AWS member accounts, Babylon clusters, and GitHub repos to answer them.

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
    aws_pricing.py           # EC2 pricing lookup (static cache, no AWS creds)
    aws_capacity_manager.py  # ODCR metrics from EC2 Capacity Manager
    cloudtrail.py            # CloudTrail Lake queries (org-wide API events)
    aws_account.py           # Cross-account member account inspection (read-only)
    marketplace_agreements.py # DynamoDB marketplace agreement inventory queries
    azure_costs.py           # Azure billing queries (SQLite cache + live CSV fallback)
    gcp_costs.py             # GCP BigQuery billing queries
    babylon.py               # Babylon cluster catalog/deployment queries
    aap2.py                  # AAP2 controller job queries (REST API)
    agnosticd.py             # AgnosticD GitHub source code lookup
    github_files.py          # GitHub file/directory fetching via remote MCP server
  agent/
    learnings.py             # Post-conversation AI analysis and learning
  connections/
    postgres.py              # asyncpg pool
    aws.py                   # boto3 session
    azure.py                 # Azure blob client
    gcp.py                   # BigQuery client
    babylon.py               # Babylon cluster K8s API clients (httpx-based)
    aap2.py                  # AAP2 controller REST API clients (httpx-based)
    github_mcp.py            # GitHub remote MCP server client (streamable HTTP)
  routes/
    query.py                 # GET /api/auth/check, POST /api/query (SSE), GET /api/reports/{filename}
    alert.py                 # POST /api/alert/investigate (alert investigation API for cloud-slack-alerts)
    conversations.py         # Conversation persistence (save/load/list/delete)
    learnings.py             # Admin API for agent learnings (view/clear)
    health.py                # GET /api/health, /api/health/ready
static/                      # Chat UI (plain HTML/CSS/JS, no build step)
config/
  config.yaml                # Base config (no secrets)
data/
  ec2_pricing.json           # Static EC2 pricing cache (checked into git)
scripts/
  refresh_pricing.py         # Refreshes ec2_pricing.json from public AWS API
  refresh_azure_billing.py   # Refreshes azure_billing.db from Azure Blob Storage
  local-server.sh            # Local dev server management (start/stop/restart/status)
playbooks/
  deploy.yaml                # Ansible deployment playbook (replaces deploy.sh)
  requirements.yaml          # Ansible collection requirements
  templates/
    manifests.yaml.j2         # All OpenShift manifests (Jinja2 template)
  tasks/
    mgmt-rbac.yml             # Bootstrap management SA RBAC
    secrets.yml               # Create/update secrets
    oauth.yml                 # OAuth setup (idempotent)
    apply-manifests.yml       # Render and apply manifests
    wait-for-builds.yml       # Wait for build completion
  vars/
    common.yml                # Shared variables (committed)
    dev.yml.example           # Dev vars template
    prod.yml.example          # Prod vars template
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
  - The app queries `user.openshift.io/v1/groups` using the service account token (same pattern as Babylon's catalog API). Results are cached for 60 seconds. This requires the `parsec-oauth` ClusterRole (defined in `openshift/base/auth/oauth-rbac.yaml`) which grants `get`/`list` on `user.openshift.io/groups` and `get` on `user.openshift.io/users`. Without these RBAC permissions, group-based authorization will silently fail (no groups resolved).
  - `auth.allowed_groups` in `config.yaml` lists allowed OpenShift groups (comma-separated). Default: `rhpds-admins,parsec-local-users`.
  - `parsec-local-users` is a cluster-scoped OpenShift group for non-SSO test accounts. It must be created manually on a fresh cluster:
    ```bash
    oc adm groups new parsec-local-users
    oc adm groups add-users parsec-local-users <user>
    ```
    Note: SSO users have long OpenShift usernames (e.g. `demo-platform-ops+rhdp-test-user1@redhat.com`). Use `oc get users` to find the exact name.
  - `auth.allowed_users` provides an optional email whitelist fallback. Users matching either groups or email list are allowed.
  - The `--openshift-group` flag on `ose-oauth-proxy` does NOT reliably enforce group membership, and the proxy does NOT forward `X-Forwarded-Groups`. Do not use either — query groups from the API instead.
  - The UI calls `GET /api/auth/check` on page load. Unauthorized users see an access-denied page instead of the chat interface. In local dev without a proxy, the check falls through gracefully and the chat UI loads normally.
- **Local dev**: Set `auth.allowed_groups` and/or `auth.allowed_users` in `config.local.yaml` (both empty = all users allowed).

## Cost-Monitor Integration

Cost-monitor (`rhpds/cost-monitor`) is a multi-cloud cost monitoring dashboard that collects, stores, and visualizes AWS/Azure/GCP billing data. Parsec integrates with it as a data source for cost investigation.

### How They Connect

```
User ──▶ Parsec chat UI ──▶ Claude agent ──▶ cost-monitor data service API
                                                (http://cost-data-service.cost-monitor.svc:8000)
User ──▶ Cost-monitor dashboard (Dash/Plotly) ──▶ "Parsec AI Explorer" link ──▶ Parsec
```

- **Parsec → cost-monitor**: The `query_cost_monitor` tool (`src/tools/cost_monitor.py`) calls the cost-data-service REST API for aggregated cost data. This is a server-to-server call within the OpenShift cluster — no auth on the internal service. Configured via `cost_monitor.api_url` in `config.yaml`.
- **Cost-monitor → Parsec**: The cost-monitor dashboard has a "Parsec AI Explorer" button that links back to the parsec chat UI for natural language investigation.
- **Shared cluster**: Both apps deploy to the same OpenShift cluster. Dev namespaces: `parsec-dev` and `cost-monitor-dev`. Prod namespaces: `parsec` and `cost-monitor`.
- **Shared auth pattern**: Both apps use the same group-based authorization — OAuth proxy for SSO, app-level group checks via the Kubernetes API, same `rhpds-admins` group. Each has its own local-users group (`parsec-local-users`, `cost-monitor-local-users`).
- **Shared repo org**: Both repos live under `github.com/rhpds/`.

### Available Cost-Monitor Endpoints

| `endpoint` param | API path | Purpose |
|---|---|---|
| `summary` | `/api/v1/costs/summary` | Aggregated costs by provider and date range |
| `breakdown` | `/api/v1/costs/aws/breakdown` | AWS costs grouped by account or instance type |
| `drilldown` | `/api/v1/costs/aws/drilldown` | Drill into specific AWS account or instance type |
| `providers` | `/api/v1/providers` | List available cloud providers |

### Local Dev

For local development, port-forward the cost-data-service and set `cost_monitor.api_url` to `http://localhost:8001`:

```bash
oc port-forward svc/cost-data-service 8001:8000 -n cost-monitor-dev
```

## Alert Investigation API

Parsec exposes a `POST /api/alert/investigate` endpoint that cloud-slack-alerts (`rhpds/cloud-slack-alerts`) calls before posting alerts to Slack. Parsec runs a full Claude investigation (provision DB, Cost Explorer, CloudTrail, account inspection) and returns a structured verdict: should this alert fire, and if so, at what severity with an AI-generated summary.

### How It Works

```
EventBridge ──▶ Lambda (cloud-slack-alerts) ──▶ POST /api/alert/investigate ──▶ Parsec
                     │                                                             │
                     │◀──────── {should_alert, severity, summary} ◀────────────────┘
                     │
                     ├── should_alert=false → suppress (skip Slack), log to CloudWatch
                     ├── should_alert=true  → append AI summary to Slack message, post
                     └── error/timeout/None → post original alert without summary (safe fallback)
```

- **Auth**: `X-API-Key` header checked against `alert_api_key` config (not OAuth — bypasses the proxy via `-skip-auth-regex=^/api/(health|alert/)`).
- **Request**: `AlertRequest` with `alert_type`, `account_id`, `account_name`, `user_arn`, `event_time`, `region`, `alert_text`, `event_details`.
- **Response**: `AlertResponse` with `should_alert` (bool), `severity` (critical/high/medium/low/benign), `summary` (1-3 sentences), `investigation_log` (full text), `duration_seconds`.
- **Agent loop**: Reuses the same Claude model, tools, and orchestrator as the chat UI. Adds a `submit_alert_verdict` tool that Claude calls to submit its verdict. Excludes `render_chart` and `generate_report` (not useful in background mode). Appends `ALERT_INVESTIGATION_PROMPT` to the system prompt with per-alert-type investigation strategies.
- **Safe fallback**: If Claude never calls the verdict tool, or if the endpoint errors, defaults to `should_alert=true`.

### Configuration

- **`alert_api_key`** in `config.yaml` (empty = endpoint returns 503). Set via `PARSEC_ALERT_API_KEY` env var or `parsec-secrets` secret key `alert-api-key`.
- Generate a key: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- The same key must be set in both Parsec (`alert-api-key` in `parsec-secrets`) and cloud-slack-alerts (`PARSEC_API_KEY` Lambda env var).

### Local Testing

```bash
# Set a test key in config.local.yaml:
#   alert_api_key: "test-key"  # pragma: allowlist secret
# Start Parsec, then:

curl -X POST http://localhost:8000/api/alert/investigate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: test-key" \
  -d '{"alert_type":"iam_access_key","account_id":"123456789012","alert_text":"IAM access key created by test-user in sandbox4440"}'
```

### Slack Message Format (with AI summary)

When Parsec returns `should_alert=true`, the Lambda appends an AI investigation block:

```
:moneybag: *AWS Marketplace Purchase*
*Account:* `sandbox4440` (`123456789012`)
...

:warning: *AI Investigation* (high):
External user test-user@gmail.com purchased CentOS Stream on sandbox4440.
The $450 estimated cost is significant.

:mag: Investigate in Parsec
```

## EC2 Pricing Cache

The `query_aws_pricing` tool reads from a static cache (`data/ec2_pricing.json`) instead of calling the AWS Pricing API at runtime. No AWS credentials are needed for pricing lookups.

### How It Works

- **`scripts/refresh_pricing.py`** downloads per-region pricing from the public AWS Pricing Bulk API (no credentials needed). It uses ETag-based conditional requests — a quick 18KB check against `region_index.json` skips everything if pricing hasn't changed.
- **`data/ec2_pricing.json`** is the generated cache (~1.4MB, 1,075 instance types across 13 regions). Checked into git so it's baked into the Docker image.
- **OpenShift CronJob** (`parsec-pricing-refresh`) runs weekly (Monday 3am UTC) to refresh the cache on a shared CephFS PVC. The Deployment mounts the same PVC at `/app/data/`. An init container seeds the PVC from the image-baked file on first deploy.
- **Auto-reload**: The tool checks file mtime on each call and reloads when the CronJob writes a newer file.

### Refreshing Locally

```bash
python3 scripts/refresh_pricing.py              # All 13 regions, ETag-cached
python3 scripts/refresh_pricing.py --force      # Force re-download
python3 scripts/refresh_pricing.py --regions us-east-1,us-west-2  # Subset
```

### Triggering Manually in OpenShift

```bash
oc create job pricing-manual --from=cronjob/parsec-pricing-refresh -n parsec-dev
```

## Azure Billing Cache

The `query_azure_costs` tool reads from a local SQLite cache (`data/azure_billing.db`) instead of streaming billing CSVs from Azure Blob Storage on every query. This turns minutes-long blob scans into sub-second SQL queries.

### How It Works

- **`scripts/refresh_azure_billing.py`** downloads billing CSVs from Azure Blob Storage and ingests them into SQLite. It uses incremental processing — only new or changed blobs (by ETag) are re-downloaded. Streaming CSV parsing and batched inserts (10K rows per batch) keep memory low.
- **`data/azure_billing.db`** is the SQLite cache (~1.3GB, WAL mode for concurrent reads during writes). Not checked into git — populated by the CronJob.
- **OpenShift CronJob** (`parsec-azure-billing-refresh`) runs daily (04:00 UTC) to refresh the cache on the shared `parsec-pricing-cache` PVC (3Gi RWX CephFS). The Deployment mounts the same PVC at `/app/data/`. An init container creates the schema on first deploy.
- **Fallback**: If the cache DB is missing or empty, `query_azure_costs` falls back to live blob streaming automatically.
- **Response metadata**: Results include `"source": "cache"` or `"source": "live"` and `"cache_last_refresh"` timestamp.

### Refreshing Locally

```bash
# Requires PARSEC_AZURE__* env vars (storage_account, container, client_id, client_secret, tenant_id)
python3 scripts/refresh_azure_billing.py           # Incremental refresh
python3 scripts/refresh_azure_billing.py --force    # Reprocess all blobs
python3 scripts/refresh_azure_billing.py --init-only  # Create schema only
```

### Triggering Manually in OpenShift

```bash
oc create job azure-billing-manual --from=cronjob/parsec-azure-billing-refresh -n parsec-dev
```

## Babylon Cluster Integration

The `query_babylon_catalog` tool queries Babylon clusters for catalog item definitions, active deployments, and provisioning state. This enables comparing expected vs actual cloud resources during cost investigation.

### How It Works

- Uses httpx-based K8s API clients (no `kubernetes` Python library dependency)
- Parses kubeconfig files for server URL, bearer token, and TLS settings
- Queries CatalogItem, AgnosticVComponent, ResourceClaim, and AnarchySubject CRDs
- Automatically strips secrets (AWS keys, passwords, tokens) from all results
- Multiple Babylon clusters supported (east, west, partner0, etc.)
- Cluster resolution from sandbox DynamoDB `comment` field via configurable pattern matching

### Babylon CRDs Used

| CRD | API Group | Namespace | Purpose |
|---|---|---|---|
| CatalogItem | `babylon.gpte.redhat.com/v1` | `babylon-catalog-{prod,event,dev}` | Catalog entries with display names |
| AgnosticVComponent | `gpte.redhat.com/v1` | `babylon-config` | Full variable definitions (expected instances) |
| ResourceClaim | `poolboy.gpte.redhat.com/v1` | Per-user namespaces | Active deployments with resolved instance types |
| AnarchySubject | `anarchy.gpte.redhat.com/v1` | `babylon-anarchy-*` | Provision lifecycle and state |

### Configuration

```yaml
# config.local.yaml
babylon:
  clusters:
    east:
      kubeconfig: "~/secrets/babylon-prod.kubeconfig"
    west:
      kubeconfig: "~/secrets/babylon-west.kubeconfig"
  default_cluster: "east"
  comment_cluster_map:
    partner0: "partner0"
```

For OpenShift deployment, store kubeconfigs as secrets and mount them into the pod.

### Instance Definition Extraction

The tool extracts expected instances from AgnosticVComponent `spec.definition` using several patterns:
- **instances list**: `{name, count, flavor: {ec2: "m5.xlarge"}}` dicts
- **Role variables**: `bastion_instance_type`, `master_instance_type`, etc. with `*_instance_count`
- **ROSA clusters**: `rosa_deploy: true` with `rosa_compute_machine_type`
- **MachineSet groups**: `ocp4_workload_machinesets_machineset_groups` with `instance_type` and `total_replicas`

Note: Some catalog items define instance types only in AgnosticD defaults (not in the CRD). For these, the `expected_instances` list may be empty, but the ResourceClaim `job_vars` will have the resolved values.

## AAP2 Job Investigation

The `query_aap2` tool queries AAP2 (Ansible Automation Platform) controller REST APIs for job metadata, execution events, and job search. Used for investigating provisioning failures, slow jobs, and retry patterns.

### How It Works

- Uses the AAP2 REST API (`/api/v2/`) with HTTP Basic Auth (`monitor` user per controller)
- Four controllers configured: `east` (aap2-prod-us-east-2), `west` (aap2-prod-us-west-2), `event0` (event controller on ocpv-infra01), `partner0` (partner Babylon controller)
- Controller resolution: accepts short names (`east`) or full hostnames from AnarchySubject `status.towerJobs.<action>.towerHost`
- httpx-based async client with pagination support

### Available Actions

| Action | API Endpoint | Purpose |
|---|---|---|
| `get_job` | `GET /api/v2/jobs/{id}/` | Job metadata, status, duration, extra_vars, git context |
| `get_job_events` | `GET /api/v2/jobs/{id}/job_events/` | Execution events with `failed_only`/`changed_only` filters |
| `find_jobs` | `GET /api/v2/jobs/` | Search by status, time range, template name (one or all controllers) |

### Investigation Flow

1. Get the provision GUID from the user's question or provision DB
2. Use `query_babylon_catalog` with `list_anarchy_subjects` + guid to find the AnarchySubject
3. Read `status.towerJobs` to get `towerHost` (controller hostname) and `deployerJob` (job ID)
4. Call `query_aap2(action="get_job", controller=towerHost, job_id=deployerJob)`
5. If failed, call `query_aap2(action="get_job_events", controller=towerHost, job_id=deployerJob, failed_only=true)`

### Configuration

```yaml
# config.local.yaml
aap2:
  clusters:
    east:
      url: "https://aap2-prod-us-east-2.aap.infra.demo.redhat.com"
      username: "monitor"
      password: "<password>"
    west:
      url: "https://aap2-prod-us-west-2.aap.infra.demo.redhat.com"
      username: "monitor"
      password: "<password>"
    event0:
      url: "https://event0.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
      username: "monitor"
      password: "<password>"
    partner0:
      url: "https://aap2-partner0-prod-us-east-2.aap.infra.partner.demo.redhat.com"
      username: "monitor"
      password: "<password>"
```

For OpenShift deployment, store credentials in a secret and inject via env vars (`PARSEC_AAP2__CLUSTERS__EAST__PASSWORD`, etc.).

### Secret Stripping

Extra vars from AAP2 jobs often contain AWS keys, passwords, and tokens. The tool strips these using the same `_SECRET_PATTERNS` and `_SECRET_KEYS` as the Babylon tool before returning results.

## AgnosticD Source Lookup

The `query_agnosticd_source` tool fetches Ansible role/config source code from the agnosticd GitHub repositories via the GitHub Contents API. Used to trace AAP2 job failures back to their exact source code.

### Repos

- **agnosticd-v2** (current): `agnosticd/agnosticd-v2`, default ref: `main`
- **agnosticd** (legacy): `redhat-cop/agnosticd`, default ref: `development`

Auto-detected from `scm_url` parameter. Falls back to the other repo on 404 when no `scm_url` specified.

### Actions

- `get_role` — Fetch role task files from `ansible/roles/{role}/tasks/`
- `get_config` — Fetch env_type defaults from `ansible/configs/{env_type}/`
- `get_file` — Arbitrary file or directory listing

### Investigation Flow

1. Get `git_url`/`git_branch` from AAP2 job metadata, or `scm_url`/`scm_ref` from AgnosticVComponent
2. Get failed role/task from AAP2 job events
3. Call `query_agnosticd_source(action="get_role", role="bookbag", task_file="remove_workload", scm_url=git_url)`

## GitHub MCP Integration

The `fetch_github_file` tool fetches files and directory listings from GitHub repositories via GitHub's hosted remote MCP server (`https://api.githubcopilot.com/mcp/`).

### How It Works

- Connects directly to GitHub's remote MCP server — no sidecar or local server needed
- Uses the MCP streamable HTTP transport (`mcp.client.streamable_http.streamablehttp_client`)
- Authenticates via `Authorization: Bearer <PAT>` header using the configured `github.token`
- Each tool call opens a fresh HTTP connection (no persistent session to manage)

### Use Cases

- **AAP2 job failure investigation**: Fetching agnosticv config files (common.yaml, prod.yaml) and agnosticd env_type configs (default_vars.yml) to trace provisioning failures
- **Catalog item inspection**: Reading catalog item definitions from agnosticv repos
- **Code tracing**: Fetching specific Ansible roles or tasks from agnosticd when debugging failures

### Configuration

```yaml
# config.yaml (default)
github:
  mcp_url: "https://api.githubcopilot.com/mcp/"
  token: ""  # set in config.local.yaml or PARSEC_GITHUB__TOKEN env var
```

The PAT needs `repo` scope for access to rhpds private repos. For OpenShift deployment, the PAT is stored in `parsec-secrets` (key: `github-token`) — shared with the agnosticd source tool.

## AAP2 Job Failure Investigation

Parsec can investigate AAP2 (Ansible Automation Platform 2) job failures when users paste job details and logs into the chat. Claude traces the failure through the agnosticv/agnosticd config hierarchy using `fetch_github_file`.

### Investigation Flow

1. User pastes AAP2 job details (job template name, project, revision) and job log
2. Claude parses the job template name to derive the agnosticv path
3. `fetch_github_file` fetches config files from agnosticv repos (rhpds/agnosticv, etc.)
4. Claude resolves components if present (virtual CI or chained CI patterns)
5. Claude extracts env_type and scm_ref, then fetches agnosticd config
6. Claude analyzes the job log for error patterns
7. Claude cross-references with Parsec's existing tools (provision DB, Babylon, AWS)

### Job Template Name Parsing

Format: `RHPDS {account}.{catalog-item}.{stage}-{guid}-{action} {uuid}`

Example: `RHPDS sandboxes-gpte.ans-bu-wksp-rhel-90.prod-zhkrm-provision 54ca9081...`
- Account: `sandboxes-gpte`
- Catalog item path: `ANS_BU_WKSP_RHEL_90` (dashes → underscores, uppercase)
- Stage: `prod`
- AgnosticV path: `sandboxes-gpte/ANS_BU_WKSP_RHEL_90/prod.yaml`

### AgnosticV Repos (searched in order)

| Repo | Owner | Description |
|------|-------|-------------|
| `agnosticv` | `rhpds` | Primary catalog (most items) |
| `partner-agnosticv` | `rhpds` | Partner subset (configs usually identical to agnosticv) |
| `zt-ansiblebu-agnosticv` | `rhpds` | Zero-touch Ansible BU catalog |
| `zt-rhelbu-agnosticv` | `rhpds` | Zero-touch RHEL BU catalog |

### AgnosticD Repos

| Project URL | Version | Owner | Repo |
|-------------|---------|-------|------|
| `https://github.com/redhat-cop/agnosticd.git` | v1 | `redhat-cop` | `agnosticd` |
| `https://github.com/rhpds/agnosticd-v2.git` | v2 | `rhpds` | `agnosticd-v2` |

## Conversation History

Conversations are persisted as JSON files in `data/conversations/` on the shared PVC. Each file includes an `owner` field (authenticated user's email) for per-user filtering.

### API

- `POST /api/conversations` — Save/update a conversation
- `GET /api/conversations` — List conversations for the current user
- `GET /api/conversations/{id}` — Load a specific conversation
- `DELETE /api/conversations/{id}` — Delete a conversation

### UI

- Left sidebar shows conversation history (open by default)
- `«` button collapses sidebar; `History »` tab reopens it
- Click a conversation to resume; `×` to delete
- Auto-saves after each agent response

## Self-Learning

After each conversation save, a background task analyzes the conversation using Claude and extracts 1-3 actionable learnings (wasteful tool calls, better sequences, resolution patterns).

### How It Works

- Background `asyncio.create_task` after conversation save — never blocks the UI
- Sends a compact conversation summary to Claude with an analysis prompt
- Extracts learnings as a JSON array of strings
- Merges with existing learnings (60% word overlap = duplicate → increment count)
- Saves to `data/agent_learnings.md`, capped at 50 entries
- System prompt hot-reloads learnings file (mtime check) — next conversation benefits immediately

### Admin UI

- Learnings panel at bottom of sidebar (visible to `learnings.admin_users` only)
- **View/Copy** button opens modal with full learnings content
- **Clear** button deletes the learnings file after copying
- Workflow: review learnings → copy useful ones → paste into `config/agent_instructions.md` → commit → clear

### Configuration

```yaml
learnings:
  admin_users: "prutledg@redhat.com"
  allow_anonymous_admin: false  # set true in config.local.yaml for local dev
```

## CloudTrail Lake

The `query_cloudtrail` tool queries CloudTrail Lake — an org-wide event data store that aggregates CloudTrail logs from all AWS accounts. Used for investigating marketplace subscriptions, IAM activity, service quota increases, and other API events.

### How It Works

- Queries a CloudTrail Lake event data store in us-east-1 (configured per environment)
- The agent writes SQL with `FROM cloudtrail_events` — the tool substitutes the real event data store ID automatically
- Uses `start_query()` → poll `get_query_results()` every 2s → paginate with `NextToken`
- Parses CloudTrail Lake's `[{col: val}, ...]` row format into flat dicts
- Auto-parses Java-style `{key=value}` strings in `requestParameters` and `responseElements` into dicts
- Only SELECT queries allowed (validated before submission)
- Configured via `cloudtrail.event_data_store_id` in local config (not checked into git).
  Set in `config.local.yaml` for local dev, or in `playbooks/vars/<env>.yml` for OpenShift (the Ansible playbook patches the ConfigMap).
  Find the ID with: `aws cloudtrail list-event-data-stores --region us-east-1`

## AWS Account Inspection

The `query_aws_account` tool inspects individual AWS member accounts using cross-account STS AssumeRole with an inline session policy that restricts access to read-only operations.

### How It Works

- Assumes `OrganizationAccountAccessRole` in the target account via STS
- Inline session policy limits to: `ec2:Describe*`, `iam:List*/Get*`, `cloudtrail:LookupEvents`, `aws-marketplace:DescribeAgreement/GetAgreementTerms/SearchAgreements`, `marketplace-entitlement:GetEntitlements`
- Write operations (`CreateSecurityGroup`, `CreateUser`, etc.) return AccessDenied — verified in testing
- Credentials cached per account to avoid redundant STS calls
- When AssumeRole fails, checks `organizations:DescribeAccount` for suspended/closed status

### Available Actions

- `describe_instances` — EC2 instances with optional state/ID filters
- `lookup_events` — recent CloudTrail events (account-local, last few hours)
- `list_users` — IAM users + access keys per user
- `describe_marketplace` — marketplace agreements via `describe_agreement` + `get_agreement_terms`. Pass `filters: {agreement_ids: ["agmt-..."]}` with IDs from CloudTrail Lake. `SearchAgreements` discovery doesn't work on member accounts.

### Marketplace Investigation Pattern

The marketplace-agreement API uses `aws-marketplace:` as its IAM action prefix (not `marketplace-agreement:`). The session policy must include explicit actions like `aws-marketplace:DescribeAgreement` — wildcard `marketplace-agreement:*` does not match. `SearchAgreements` returns `ValidationException` on member accounts; the correct flow is CloudTrail Lake discovery → direct `describe_agreement` with known agreement IDs.

## AWS IAM Policy

All AWS tools use the `cost-monitor` IAM user in the payer account, with the
managed policy `CostMonitorPolicy`. When adding new AWS tools that need additional
IAM permissions, update this policy.

**Policy version limit:** AWS managed policies can have at most 5 versions. Before
creating a new version (`aws iam create-policy-version`), list existing versions and
delete the oldest non-default one:
```bash
aws iam list-policy-versions --policy-arn <POLICY_ARN>
aws iam delete-policy-version --policy-arn <POLICY_ARN> --version-id <OLDEST_NON_DEFAULT>
```

**Current policy statements:**

| Sid | Actions | Resource |
|---|---|---|
| CostExplorerReadOnly | `ce:GetCostAndUsage`, `ce:GetDimensionValues`, `ce:GetReservation*`, `ce:ListCostCategoryDefinitions`, `ce:GetCostCategories` | `*` |
| OrganizationsReadOnly | `organizations:DescribeAccount`, `organizations:DescribeOrganization`, `organizations:ListAccounts`, `organizations:ListRoots`, `organizations:ListOrganizationalUnitsForParent`, `organizations:ListChildren` | `*` |
| CapacityManagerReadOnly | `ec2:GetCapacityManager*` | `*` |
| CloudTrailLakeReadOnly | `cloudtrail:StartQuery`, `cloudtrail:GetQueryResults` | Event data store ARN |
| AssumeReadOnlyAccess | `sts:AssumeRole` | `arn:aws:iam::*:role/OrganizationAccountAccessRole` |
| DynamoDBReadOnly | `dynamodb:Scan`, `dynamodb:Query`, `dynamodb:GetItem` | `marketplace-agreement-inventory` table + indexes, `accounts` table |

**AssumeReadOnlyAccess** allows assuming `OrganizationAccountAccessRole` in any member account. This role exists by default in accounts created through AWS Organizations. **Read-only enforcement is handled by the inline session policy in `src/tools/aws_account.py`**, not by the IAM policy — the session policy restricts to: `ec2:Describe*`, `iam:List*/Get*`, `cloudtrail:LookupEvents`, `aws-marketplace:DescribeAgreement/GetAgreementTerms/SearchAgreements`, `marketplace-entitlement:GetEntitlements`.

## Abuse Indicators

- **AWS GPU**: g4dn.*, g5.*, g6.*, p3.*, p4.*, p5.*
- **AWS large/metal**: *.metal, *.96xlarge, *.48xlarge, *.24xlarge
- **AWS Lightsail**: Large Windows instances, especially ap-south-1
- **AWS instance names**: "Web-Created-VM" — strong indicator of compromised account (attacker-created via console)
- **Azure GPU**: NC, ND, NV series (meterSubCategory)
- **Suspicious**: External users with 50+ provisions in 90 days
- **Disposable emails**: Multiple accounts from temporary email domains

## OpenShift Deployment

Deployment is managed by an Ansible playbook (`playbooks/deploy.yaml`). All tasks are
idempotent — running the playbook twice produces the same result. No kustomize dependency.

```bash
# Prerequisites
pip install kubernetes
ansible-galaxy collection install kubernetes.core

# Create vars file from template
cp playbooks/vars/dev.yml.example playbooks/vars/dev.yml
# Edit with your secrets

# Bootstrap management SA RBAC (one-time, needs cluster-admin)
ansible-playbook playbooks/deploy.yaml -e env=dev \
  -e kubeconfig=~/secrets/cluster-admin.kubeconfig \
  --tags mgmt-rbac

# Full deploy (uses SA kubeconfig)
ansible-playbook playbooks/deploy.yaml -e env=dev

# Just update secrets
ansible-playbook playbooks/deploy.yaml -e env=dev --tags secrets

# Just apply manifests
ansible-playbook playbooks/deploy.yaml -e env=dev --tags apply

# Production
ansible-playbook playbooks/deploy.yaml -e env=prod
```

Required secrets (created by playbook): `parsec-secrets`, `vertex-credentials`,
`parsec-cloud-credentials`, `gcp-billing-credentials`, `babylon-kubeconfigs`,
`oauth-proxy-secret`.  <!-- pragma: allowlist secret -->

Management SA: `parsec-admin` in `parsec-dev` namespace with `parsec-mgmt` ClusterRole
(namespaces, OAuthClients, ClusterRoles) and `admin` RoleBinding per target namespace.

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
  that the GitHub webhook secrets match the BuildConfig trigger secrets.

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
- **ClusterRoleBindings**: Each environment has its own CRB (`parsec-oauth-dev`, `parsec-oauth-prod`) created by the Ansible playbook with the environment name suffix.
- **EC2 pricing cache**: Uses a static JSON file from the public AWS Pricing Bulk API instead of calling `pricing:GetProducts` at runtime. No AWS credentials needed. Refreshed weekly by an OpenShift CronJob on a shared RWX CephFS PVC (`ocs-storagecluster-cephfs`). The per-region bulk files are ~250-400MB each so the CronJob needs 3Gi memory limit for JSON parsing.
- **Azure billing cache**: Uses a SQLite database (`data/azure_billing.db`, ~1.3GB) on the same shared PVC (3Gi). Refreshed daily by the `parsec-azure-billing-refresh` CronJob. Incremental processing via ETag comparison — only changed/new blobs are re-ingested. WAL mode enables concurrent reads during writes. Falls back to live blob streaming if the cache is missing. For initial deployment, copy the DB from dev to prod via `oc rsync` to avoid a full re-ingest.
- **CloudTrail Lake**: The event data store ID is environment-specific — set via local config overrides (`config.local.yaml`) or Ansible vars (`playbooks/vars/<env>.yml`), not hardcoded. The tool substitutes `cloudtrail_events` in FROM clauses with the real ID so the agent never sees it. CloudTrail Lake charges per byte scanned — always include `eventTime >` filters. `responseElements` often comes as Java-style `{key=value}` instead of JSON; the tool auto-parses these.
- **Alert investigation API**: The `/api/alert/investigate` endpoint uses API key auth (`X-API-Key` header), not OAuth. The OAuth proxy's `-skip-auth-regex` bypasses SSO for `/api/alert/` paths. The agent loop is non-streaming (synchronous JSON response, not SSE) and excludes `render_chart`/`generate_report` tools. Claude calls `submit_alert_verdict` to submit its verdict; if it never calls the tool, the endpoint defaults to `should_alert=true`. Investigation typically takes 30-90 seconds depending on how many tools Claude calls.
- **Cross-account access**: Uses STS AssumeRole into `OrganizationAccountAccessRole` with an inline session policy for read-only enforcement. The session policy (not the IAM role) is the security boundary — even though `OrganizationAccountAccessRole` has admin, the inline policy limits to `ec2:Describe*`, `iam:List*/Get*`, etc. Credentials are cached per account ID at module level. The `marketplace-agreement` SDK client calls IAM actions under the `aws-marketplace:` prefix, not `marketplace-agreement:` — the session policy must use `aws-marketplace:DescribeAgreement` explicitly.
- **GitHub MCP**: Connects to GitHub's hosted remote MCP server at `https://api.githubcopilot.com/mcp/` via streamable HTTP transport (`mcp.client.streamable_http.streamablehttp_client`). No sidecar container needed — replaced the previous supergateway + `@modelcontextprotocol/server-github` sidecar approach. Auth via `Authorization: Bearer <PAT>` header. Each tool call opens a fresh HTTP connection (no persistent session to manage). The PAT needs `repo` scope for access to rhpds private repos.
- **AAP2 investigation**: Users paste job details and logs into the chat. The investigation workflow (job template parsing, agnosticv config resolution, component tracing, agnosticd version detection) is encoded in `config/agent_instructions.md`. No direct AAP2 Controller API connection — paste-based input only for now.

See `docs/TODO.md` for the full project backlog.
