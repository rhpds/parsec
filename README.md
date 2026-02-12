# Parsec

Natural language cloud cost investigation tool for the RHDP platform. Type questions like "show me external users with 50+ provisions since December" or "what g6.12xlarge usage happened this month?" and get answers drawn from real data sources.

## How It Works

1. Investigator types a question in the chat UI
2. The question is sent to Claude with tool definitions for 4 data sources
3. Claude decides which tools to call and in what order
4. The backend executes tool calls against real data, returns results to Claude
5. Claude synthesizes findings into a natural language answer
6. The answer streams back to the UI as server-sent events

## Data Sources

| Tool | Source | Use Case |
|------|--------|----------|
| `query_provisions_db` | PostgreSQL (read-only) | User lookups, provision history, catalog items, account mappings |
| `query_aws_costs` | AWS Cost Explorer | AWS spending by service, instance type, or account |
| `query_azure_costs` | Azure billing CSVs | Azure spending by subscription, GPU VM detection |
| `query_gcp_costs` | GCP BigQuery | GCP spending by service or project |

Claude can chain multiple tools — for example, first querying the provision DB for account IDs, then querying Cost Explorer for those accounts.

## Reports

Ask for a report in the chat and Claude will generate a formatted Markdown or AsciiDoc document with executive summary, findings, and cost breakdowns. A download link appears in the UI.

## Setup

### Prerequisites

- Python 3.11+
- Access to the RHDP provision database (read-only user)
- AWS named profile configured for Cost Explorer
- Azure CLI logged in (or client credentials) for billing blob access
- GCP service account for BigQuery billing export
- Anthropic API key

### Local Development

```bash
# Configure
cp config/config.local.yaml.template config/config.local.yaml
# Edit config.local.yaml with your credentials

# Run
pip install -r requirements.txt
uvicorn src.app:app --host 0.0.0.0 --port 8000

# Or with Docker
export ANTHROPIC_API_KEY=sk-ant-...
docker-compose up --build
```

Open http://localhost:8000

### OpenShift

Parsec deploys with an OAuth proxy sidecar for SSO. Access is restricted to specific users via the `parsec-allowed-users` ConfigMap.

```bash
# Create required secrets first:
# - parsec-secrets: anthropic-api-key, db-host, db-name, db-user, db-password
# - oauth-proxy-secret: client-id, client-secret, session_secret
# - parsec-cloud-credentials: AWS/Azure/GCP credentials (optional)

# Deploy
oc apply -k openshift/overlays/dev/    # dev namespace
oc apply -k openshift/overlays/prod/   # prod namespace
```

### Access Control

Set allowed users (comma-separated emails) in:
- **Local**: `auth.allowed_users` in `config/config.local.yaml`
- **OpenShift**: `parsec-allowed-users` ConfigMap
- **Env var**: `PARSEC_AUTH__ALLOWED_USERS`

Empty value = all authenticated users allowed.

## Security

- **Provision DB**: Read-only PostgreSQL user, SELECT-only SQL validation, 30s statement timeout, 500-row limit
- **Cloud APIs**: Structured parameters with no injection surface
- **API key**: Server-side only, stored as OpenShift Secret
- **Web UI**: OAuth proxy sidecar with user-level access control
- **Reports**: Served from server-side filesystem, behind same auth

## Tech Stack

- **Backend**: FastAPI, asyncpg, Anthropic SDK, boto3, azure-storage-blob, google-cloud-bigquery
- **Frontend**: Plain HTML/CSS/JS with marked.js for Markdown rendering
- **Config**: Dynaconf (YAML + env var overrides)
- **Deployment**: UBI 9 container, OpenShift with Kustomize overlays
- **CI**: GitHub Actions (black, ruff, mypy, bandit, Docker build)
