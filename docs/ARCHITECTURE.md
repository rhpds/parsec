# Parsec Architecture

Detailed subsystem documentation for Parsec. For project overview, structure, and development workflow, see [CLAUDE.md](../CLAUDE.md).

## Cost-Monitor Integration

Cost-monitor (`rhpds/cost-monitor`) is a multi-cloud cost monitoring dashboard. Parsec integrates with it as a data source.

```
User --> Parsec chat UI --> Claude agent --> cost-monitor data service API
                                              (http://cost-data-service.cost-monitor.svc:8000)
User --> Cost-monitor dashboard (Dash/Plotly) --> "Parsec AI Explorer" link --> Parsec
```

- Server-to-server call within OpenShift cluster (no auth). Configured via `cost_monitor.api_url`.
- Both apps deploy to the same cluster. Dev: `parsec-dev`/`cost-monitor-dev`. Prod: `parsec`/`cost-monitor`.
- `breakdown`/`drilldown` endpoints are AWS-only. For Azure/GCP, use `query_azure_costs`/`query_gcp_costs`.

### Available Endpoints

| `endpoint` param | API path | Purpose |
|---|---|---|
| `summary` | `/api/v1/costs/summary` | Aggregated costs by provider and date range |
| `breakdown` | `/api/v1/costs/aws/breakdown` | AWS costs grouped by account or instance type |
| `drilldown` | `/api/v1/costs/aws/drilldown` | Drill into specific AWS account or instance type |
| `providers` | `/api/v1/providers` | List available cloud providers |

Local dev: `oc port-forward svc/cost-data-service 8001:8000 -n cost-monitor-dev`

## Alert Investigation API

`POST /api/alert/investigate` — called by cloud-slack-alerts (`rhpds/cloud-slack-alerts`) before posting alerts to Slack.

```
EventBridge --> Lambda (cloud-slack-alerts) --> POST /api/alert/investigate --> Parsec
                     |                                                           |
                     |<-------- {should_alert, severity, summary} <--------------+
                     |
                     +-- should_alert=false -> suppress (skip Slack)
                     +-- should_alert=true  -> append AI summary to Slack message
                     +-- error/timeout/None -> post original alert (safe fallback)
```

- **Auth**: `X-API-Key` header (not OAuth). Bypasses proxy via `-skip-auth-regex=^/api/(health|alert/)`.
- **Request**: `AlertRequest` with `alert_type`, `account_id`, `account_name`, `user_arn`, `event_time`, `region`, `alert_text`, `event_details`.
- **Response**: `AlertResponse` with `should_alert`, `severity`, `summary`, `investigation_log`, `duration_seconds`.
- **Agent loop**: Non-streaming. Reuses orchestrator but excludes `render_chart`/`generate_report`. Claude calls `submit_alert_verdict` to submit verdict; defaults to `should_alert=true` if never called.
- **Config**: `alert_api_key` in config (empty = 503). Same key must be set in both Parsec and cloud-slack-alerts Lambda.

## EC2 Pricing Cache

`query_aws_pricing` reads from `data/ec2_pricing.json` (static, no AWS creds needed).

- **Refresh**: `scripts/refresh_pricing.py` — ETag-based conditional downloads from public AWS Pricing Bulk API
- **OpenShift**: CronJob `parsec-pricing-refresh` runs weekly (Monday 3am UTC) on shared CephFS PVC. Auto-reloads on mtime change.
- **Manual**: `python3 scripts/refresh_pricing.py [--force] [--regions us-east-1,us-west-2]`
- **OpenShift manual**: `oc create job pricing-manual --from=cronjob/parsec-pricing-refresh -n parsec-dev`

## Azure Billing Cache

`query_azure_costs` reads from `data/azure_billing.db` (SQLite, ~1.3GB, WAL mode).

- **Refresh**: `scripts/refresh_azure_billing.py` — incremental via ETag, streaming CSV, batched inserts
- **OpenShift**: CronJob `parsec-azure-billing-refresh` runs daily (04:00 UTC) on shared PVC
- **Fallback**: Falls back to live blob streaming if cache missing
- **Response metadata**: `"source": "cache"|"live"` and `"cache_last_refresh"` timestamp
- **GPU detection**: Auto-detects NC/ND/NV series VMs, reports separate `gpu_cost` per subscription
- **Initial deploy**: Copy DB from dev to prod via `oc rsync` to avoid full re-ingest
- **Manual**: `python3 scripts/refresh_azure_billing.py [--force] [--init-only]`

## Babylon Cluster Integration

`query_babylon_catalog` queries Babylon clusters for catalog items, deployments, and provisioning state.

- httpx-based K8s API clients (no `kubernetes` Python library)
- Strips secrets from all results automatically
- Multiple clusters supported (east, west, partner0, etc.)
- Cluster resolution from sandbox DynamoDB `comment` field

### CRDs Used

| CRD | API Group | Namespace | Purpose |
|---|---|---|---|
| CatalogItem | `babylon.gpte.redhat.com/v1` | `babylon-catalog-{prod,event,dev}` | Catalog entries |
| AgnosticVComponent | `gpte.redhat.com/v1` | `babylon-config` | Variable definitions (expected instances) |
| ResourceClaim | `poolboy.gpte.redhat.com/v1` | Per-user namespaces | Active deployments |
| AnarchySubject | `anarchy.gpte.redhat.com/v1` | `babylon-anarchy-*` | Provision lifecycle |

### Instance Extraction Patterns

- **instances list**: `{name, count, flavor: {ec2: "m5.xlarge"}}` dicts
- **Role variables**: `bastion_instance_type`, `master_instance_type`, etc.
- **ROSA clusters**: `rosa_deploy: true` with `rosa_compute_machine_type`
- **MachineSet groups**: `ocp4_workload_machinesets_machineset_groups`

Note: Some items define instance types only in AgnosticD defaults. ResourceClaim `job_vars` always has resolved values.

### Configuration

```yaml
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

## AAP2 Job Investigation

`query_aap2` queries AAP2 controller REST APIs for job metadata, events, and search.

- HTTP Basic Auth (`monitor` user per controller)
- Four controllers: `east`, `west`, `event0`, `partner0`
- Controller resolution: short names or full hostnames from AnarchySubject `status.towerJobs.<action>.towerHost`
- Secret stripping on extra vars (same patterns as Babylon tool)

### Actions

| Action | API Endpoint | Purpose |
|---|---|---|
| `get_job` | `GET /api/v2/jobs/{id}/` | Job metadata, status, duration, extra_vars |
| `get_job_events` | `GET /api/v2/jobs/{id}/job_events/` | Execution events (failed_only/changed_only filters) |
| `find_jobs` | `GET /api/v2/jobs/` | Search by status, time range, template name |

### Investigation Flow

1. Get GUID from user question or provision DB
2. `query_babylon_catalog` → `list_anarchy_subjects` + guid → find AnarchySubject
3. Read `status.towerJobs` → `towerHost` + `deployerJob`
4. `query_aap2(action="get_job", controller=towerHost, job_id=deployerJob)`
5. If failed: `query_aap2(action="get_job_events", ..., failed_only=true)`

### Configuration

```yaml
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

## GitHub MCP & Source Code Tracing

`fetch_github_file` fetches files from GitHub via the hosted remote MCP server (`https://api.githubcopilot.com/mcp/`). No sidecar needed. Auth via Bearer PAT. PAT needs `repo` scope.

```yaml
github:
  mcp_url: "https://api.githubcopilot.com/mcp/"
  token: ""  # set in config.local.yaml or PARSEC_GITHUB__TOKEN
```

### AgnosticV Repos (searched in order)

| Repo | Owner | Description |
|------|-------|-------------|
| `agnosticv` | `rhpds` | Primary catalog |
| `partner-agnosticv` | `rhpds` | Partner subset |
| `zt-ansiblebu-agnosticv` | `rhpds` | Zero-touch Ansible BU |
| `zt-rhelbu-agnosticv` | `rhpds` | Zero-touch RHEL BU |

Repo name mapping: `rhpds-agnosticv` -> `agnosticv` (owner: `rhpds`). All others keep same name.

### AgnosticD Repos

| Repo | Owner | Default Ref | Notes |
|------|-------|-------------|-------|
| `agnosticd-v2` | `agnosticd` | `main` | Current |
| `agnosticd` | `redhat-cop` | `development` | Legacy |

Determine which from `git_url` in AAP2 job metadata or `scm_url` from AgnosticVComponent.

### Job Template Name Parsing

Format: `RHPDS {account}.{catalog-item}.{stage}-{guid}-{action} {uuid}`

Example: `RHPDS sandboxes-gpte.ans-bu-wksp-rhel-90.prod-zhkrm-provision 54ca9081...`

**Directory names vary** (UPPERCASE_UNDERSCORES vs lowercase-dashes). Always list the account directory first. See `config/prompts/aap2_agent.md` for full discovery sequence.

## CloudTrail Lake

`query_cloudtrail` queries an org-wide CloudTrail Lake event data store.

- Agent writes SQL with `FROM cloudtrail_events` — tool substitutes real event data store ID
- Auto-parses Java-style `{key=value}` strings in `requestParameters`/`responseElements`
- Only SELECT queries allowed
- **Cost**: charges per byte scanned — always include `eventTime >` filters
- Config: `cloudtrail.event_data_store_id` in local config or Ansible vars

## AWS Account Inspection

`query_aws_account` inspects member accounts via cross-account STS AssumeRole.

- Assumes `OrganizationAccountAccessRole` with inline session policy (read-only enforcement)
- Session policy limits to: `ec2:Describe*`, `iam:List*/Get*`, `cloudtrail:LookupEvents`, `aws-marketplace:DescribeAgreement/GetAgreementTerms/SearchAgreements`, `marketplace-entitlement:GetEntitlements`
- Credentials cached per account. Checks `organizations:DescribeAccount` on AssumeRole failure.

### Actions

- `describe_instances` — EC2 instances (optional state/ID filters)
- `lookup_events` — recent CloudTrail events (last few hours)
- `list_users` — IAM users + access keys
- `describe_marketplace` — agreements via `describe_agreement` + `get_agreement_terms` (pass `agreement_ids` from CloudTrail Lake; `SearchAgreements` doesn't work on member accounts)

### Marketplace IAM Gotcha

The `marketplace-agreement` SDK uses `aws-marketplace:` as IAM action prefix (not `marketplace-agreement:`). Session policy must use `aws-marketplace:DescribeAgreement` explicitly.

## AWS IAM Policy

All AWS tools use the `cost-monitor` IAM user with `CostMonitorPolicy`.

**Policy version limit:** Max 5 versions. Delete oldest non-default before creating new:
```bash
aws iam list-policy-versions --policy-arn <ARN>
aws iam delete-policy-version --policy-arn <ARN> --version-id <OLDEST_NON_DEFAULT>
```

| Sid | Actions | Resource |
|---|---|---|
| CostExplorerReadOnly | `ce:GetCostAndUsage`, `ce:GetDimensionValues`, `ce:GetReservation*`, `ce:ListCostCategoryDefinitions`, `ce:GetCostCategories` | `*` |
| OrganizationsReadOnly | `organizations:Describe*`, `organizations:List*` | `*` |
| CapacityManagerReadOnly | `ec2:GetCapacityManager*` | `*` |
| CloudTrailLakeReadOnly | `cloudtrail:StartQuery`, `cloudtrail:GetQueryResults` | Event data store ARN |
| AssumeReadOnlyAccess | `sts:AssumeRole` | `arn:aws:iam::*:role/OrganizationAccountAccessRole` |
| DynamoDBReadOnly | `dynamodb:Scan`, `dynamodb:Query`, `dynamodb:GetItem` | `marketplace-agreement-inventory` + `accounts` tables |

## Conversation History & Self-Learning

### Conversations

Persisted as JSON in `data/conversations/` on shared PVC. Owner field for per-user filtering.

API: `POST/GET/DELETE /api/conversations[/{id}]`

### Self-Learning

After each save, background task extracts 1-3 learnings via Claude analysis.

- Merges duplicates (60% word overlap threshold), capped at 50 entries
- Saves to `data/agent_learnings.md`, hot-reloaded by system prompt (mtime check)
- **Admin workflow**: review learnings -> copy to `config/prompts/` -> clear
- Admin panel visible to `learnings.admin_users` only

```yaml
learnings:
  admin_users: "prutledg@redhat.com"
  allow_anonymous_admin: false  # true for local dev
```

## OpenShift Deployment

Ansible playbook (`playbooks/deploy.yaml`), fully idempotent.

```bash
# Prerequisites
pip install kubernetes && ansible-galaxy collection install kubernetes.core

# Bootstrap (one-time, needs cluster-admin)
ansible-playbook playbooks/deploy.yaml -e env=dev \
  -e kubeconfig=~/secrets/cluster-admin.kubeconfig --tags mgmt-rbac

# Full deploy
ansible-playbook playbooks/deploy.yaml -e env=dev

# Selective: --tags secrets | apply
# Production: -e env=prod
```

Required secrets: `parsec-secrets`, `vertex-credentials`, `parsec-cloud-credentials`, `gcp-billing-credentials`, `babylon-kubeconfigs`, `oauth-proxy-secret`.  <!-- pragma: allowlist secret -->

Management SA: `parsec-admin` in `parsec-dev` with `parsec-mgmt` ClusterRole.
Each environment has its own CRB (`parsec-oauth-dev`, `parsec-oauth-prod`).

## Report Generation

Users ask for reports in chat. Claude uses `generate_report` to produce `.md`/`.adoc` files, saved to `/app/reports/` with download link in UI.
