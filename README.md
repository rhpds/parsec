# Parsec

Natural language cloud cost and provisioning investigation tool for the RHDP platform. Type questions like "what services does user@redhat.com have?" or "what instances should clusterplatform.ocp4-aws.prod be running?" and get answers drawn from real data sources.

## How It Works

1. Investigator types a question in the chat UI
2. The question is sent to Claude with tool definitions for multiple data sources
3. Claude decides which tools to call and in what order
4. The backend executes tool calls against real data, returns results to Claude
5. Claude synthesizes findings into a natural language answer
6. The answer streams back to the UI as server-sent events

## Data Sources

| Tool | Source | Use Case |
|------|--------|----------|
| `query_provisions_db` | Reporting MCP (read-only) | User lookups, provision history, catalog items, account mappings |
| `query_aws_costs` | AWS Cost Explorer | AWS spending by service, instance type, or account |
| `query_azure_costs` | Azure billing CSVs | Azure spending by subscription, GPU VM detection |
| `query_gcp_costs` | GCP BigQuery | GCP spending by service or project |
| `query_aws_pricing` | Static cache | EC2 instance pricing lookup |
| `query_cost_monitor` | Cost-monitor API | Aggregated cross-provider cost summaries |
| `query_aws_capacity_manager` | EC2 Capacity Manager | ODCR utilization and waste |
| `query_cloudtrail` | CloudTrail Lake | Org-wide AWS API event investigation |
| `query_aws_account` | Cross-account STS | Individual account inspection (instances, IAM, marketplace) |
| `query_marketplace_agreements` | DynamoDB | Marketplace subscription inventory |
| `query_aws_account_db` | DynamoDB | Sandbox account pool metadata |
| `query_babylon_catalog` | Babylon K8s clusters | Catalog definitions, active deployments, workshops, resource pools |
| `fetch_github_file` | GitHub remote MCP server | Fetch agnosticv/agnosticd config files for AAP2 job failure investigation |

Claude can chain multiple tools — for example, querying the provision DB for account IDs, checking Babylon for expected instance types, then comparing against actual AWS instances.

## Reports

Ask for a report in the chat and Claude will generate a formatted Markdown or AsciiDoc document with executive summary, findings, and cost breakdowns. A download link appears in the UI.

## Setup

### Prerequisites

- Python 3.11+
- Access to the Reporting MCP server (provision DB passthrough)
- AWS credentials (cost-monitor IAM user)
- Azure client credentials for billing blob access
- GCP service account for BigQuery billing export
- Vertex AI credentials (or Anthropic API key)
- Babylon cluster kubeconfigs (`rhdp-readonly` SA)
- GitHub PAT with `repo` scope (for agnosticv/agnosticd private repos)

### Local Development

```bash
# Configure
cp config/config.yaml config/config.local.yaml
# Edit config.local.yaml with your credentials

# Run
pip install -r requirements.txt
scripts/local-server.sh start    # start in background
scripts/local-server.sh status   # check status
scripts/local-server.sh restart  # restart
scripts/local-server.sh stop     # stop

# Or run directly
uvicorn src.app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000

### OpenShift Deployment

Deployment is managed by an Ansible playbook. All tasks are idempotent.

```bash
# Prerequisites
pip install kubernetes
ansible-galaxy collection install kubernetes.core

# Create vars file
cp playbooks/vars/dev.yml.example playbooks/vars/dev.yml
# Edit with your secrets

# Bootstrap management SA (one-time, needs cluster-admin)
ansible-playbook playbooks/deploy.yaml -e env=dev \
  -e kubeconfig=~/secrets/cluster-admin.kubeconfig \
  --tags mgmt-rbac

# Full deploy
ansible-playbook playbooks/deploy.yaml -e env=dev

# Production
ansible-playbook playbooks/deploy.yaml -e env=prod
```

### Access Control

Set allowed users (comma-separated emails) in:
- **Local**: `auth.allowed_users` in `config/config.local.yaml`
- **OpenShift**: `parsec-allowed-users` ConfigMap (managed by playbook)
- **Env var**: `PARSEC_AUTH__ALLOWED_USERS`

Empty value = group-based auth only (OpenShift groups).

## AAP2 Job Failure Investigation

Paste AAP2 job details and logs into the chat, and Parsec will trace the failure through the agnosticv/agnosticd config hierarchy:

1. Parses the job template name to find the agnosticv config path
2. Fetches config files from GitHub repos (agnosticv, partner-agnosticv, etc.)
3. Resolves component chains (virtual CI and chained CI patterns)
4. Fetches the agnosticd env_type config at the exact git revision
5. Analyzes the job log for error patterns
6. Cross-references with provision DB and Babylon for full context

### GitHub MCP Integration

The `fetch_github_file` tool connects to [GitHub's hosted remote MCP server](https://github.com/github/github-mcp-server) at `https://api.githubcopilot.com/mcp/` via streamable HTTP transport. No sidecar needed — just configure a GitHub PAT with `repo` scope in `github.token`.

## Babylon Integration

Parsec queries Babylon clusters to understand what catalog items should deploy and what's currently running. This enables comparing expected vs actual resources during cost investigation.

- **6 clusters**: east, west, partner0, partner1, integration, babydev
- **Read-only**: Uses `rhdp-readonly` SA with `babylon-readonly` ClusterRole
- **Auto-resolution**: Sandbox DynamoDB `comment` field identifies which Babylon cluster manages each sandbox
- **CRDs queried**: CatalogItems, AgnosticVComponents, ResourceClaims, AnarchySubjects, AnarchyActions, ResourcePools, Workshops, MultiWorkshops

## Cost-Monitor Integration

[Cost-monitor](https://github.com/rhpds/cost-monitor) is a multi-cloud cost monitoring dashboard.

- **Parsec queries cost-monitor**: The `query_cost_monitor` tool calls the cost-data-service API for aggregated cost summaries, AWS breakdowns, and drilldowns.
- **Cost-monitor links to Parsec**: The dashboard has a "Parsec AI Explorer" button for natural language investigation.
- **Shared cluster**: Both apps deploy to the same OpenShift cluster.

## Security

- **Provision DB**: Read-only access via Reporting MCP, SELECT-only SQL validation, 500-row limit
- **Cloud APIs**: Structured parameters with no injection surface
- **Babylon**: Read-only SA, secrets auto-stripped from all results
- **GitHub**: Read-only access via GitHub's remote MCP server, secrets auto-redacted from fetched files
- **Web UI**: OAuth proxy sidecar with group-based access control
- **Reports**: Served from server-side filesystem, behind same auth

## Tech Stack

- **Backend**: FastAPI, Anthropic SDK, boto3, azure-storage-blob, google-cloud-bigquery, httpx, MCP Python SDK (Streamable HTTP)
- **Frontend**: Plain HTML/CSS/JS with marked.js for Markdown rendering
- **Config**: Dynaconf (YAML + env var overrides)
- **Deployment**: Ansible playbook, Jinja2 manifests, UBI 9 container on OpenShift
- **CI**: GitHub Actions (black, ruff, mypy, bandit, Docker build)
