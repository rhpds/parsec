# Contributing to Parsec

Parsec is a natural language cloud cost investigation tool for RHDP. Contributions are welcome — especially improvements to the agent instructions that make Parsec smarter about answering questions.

## Quick Start

### Prerequisites

- Python 3.11+
- Access to the RHDP provision database
- AWS named profile `athena` configured
- GCP credentials (for Vertex AI and BigQuery)
- Azure CLI or client credentials (for billing CSVs)

### Local Development

```bash
git clone https://github.com/rhpds/parsec.git
cd parsec

# Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Testing Locally

1. **Create a local config** by copying the base config:

   ```bash
   cp config/config.yaml config/config.local.yaml
   ```

2. **Disable auth** in `config/config.local.yaml` so you can access the UI without
   OpenShift OAuth. Set both `allowed_groups` and `allowed_users` to empty strings:

   ```yaml
   auth:
     allowed_groups: ""
     allowed_users: ""
   ```

3. **Fill in credentials** — add your API keys if needed and connection details to
   `config/config.local.yaml` (this file is gitignored).

4. **Start the server** using the local dev script:

   ```bash
   scripts/local-server.sh start    # start the server (port 8000)
   scripts/local-server.sh stop     # stop the server
   scripts/local-server.sh restart  # restart the server
   scripts/local-server.sh status   # check if the server is running
   ```

   Open http://localhost:8000

The start command automatically activates the venv, launches the Uvicorn server in the
background, and sets up any configured MCP sidecars (Icinga, Reporting MCP port-forward).
Logs are written to `logs/server.log`.

If you need cost-monitor integration locally:

```bash
oc port-forward svc/cost-data-service 8001:8000 -n cost-monitor
```

### Pre-commit Hooks

Install and run pre-commit before pushing:

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## What to Contribute

### Agent Instructions (easiest, highest impact)

The file `config/agent_instructions.md` controls how Parsec answers questions. This is the single most impactful thing to improve. Examples:

- Better query patterns for common questions
- New abuse indicators or investigation workflows
- Corrections to the DB schema documentation
- Improved date handling guidance
- Domain knowledge about RHDP provisioning

Edit the file, test locally, and submit a PR.

### New Tools

Tools live in `src/tools/` and are registered in `src/agent/tool_definitions.py`. To add a new tool:

1. Create `src/tools/your_tool.py` with an async function
2. Add the tool schema to `src/agent/tool_definitions.py`
3. Wire it into `src/agent/orchestrator.py` (`_execute_tool`)
4. Document it in `config/agent_instructions.md`

### Frontend Improvements

The frontend is plain HTML/CSS/JS in `static/` — no build step. Charts use Chart.js from CDN, markdown uses marked.js.

### OpenShift / Deployment

**Method 1: Helm (recommended)**

The Helm chart in `helm/` generates all OpenShift manifests. Deploy with:

```bash
# Dev environment
helm template parsec helm/ \
  -f helm/values-dev.yaml \
  | oc apply -f -

# Cleanup
helm template parsec helm/ \
  -f helm/values-dev.yaml \
  | oc delete -f -
```

Environment overrides go in `helm/values-dev.yaml` (only values that differ from `helm/values.yaml` defaults). Secrets are managed via BitwardenSyncSecret — create them in the Bitwarden `parsec` project before deploying.

**Method 2: Ansible (legacy)**

The Ansible playbook in `playbooks/` is still available:

```bash
ansible-playbook playbooks/deploy.yaml -e env=dev
```

## Project Structure

```
src/
  app.py                     # FastAPI app, lifespan
  config.py                  # Dynaconf settings
  agent/
    orchestrator.py           # Claude tool-use loop
    tool_definitions.py       # Tool schemas
    system_prompt.py          # Loads config/agent_instructions.md
    streaming.py              # SSE helpers
  tools/
    provision_db.py           # SQL against provision DB
    aws_costs.py              # AWS Cost Explorer
    aws_pricing.py            # AWS Pricing API
    azure_costs.py            # Azure billing CSVs
    gcp_costs.py              # GCP BigQuery
    cost_monitor.py           # cost-monitor dashboard API
  connections/                # DB pool, boto3, Azure, GCP clients
  routes/                     # /api/query, /api/health, /api/reports
config/
  agent_instructions.md       # Agent behavior (edit this!)
  config.yaml                 # Base config
static/                       # Chat UI
```

## Branching

- `main` — development, deploys to `parsec-dev`
- `production` — stable, deploys to `parsec`

## Submitting Changes

1. Fork or create a branch
2. Make your changes
3. Run `pre-commit run --all-files`
4. Test locally
5. Submit a PR with a clear description of what changed and why
