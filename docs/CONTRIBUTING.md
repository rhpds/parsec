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

# Configure
cp config/config.local.yaml.template config/config.local.yaml
# Edit config.local.yaml with your credentials

# Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
uvicorn src.app:app --host 0.0.0.0 --port 8080

# If you need cost-monitor integration locally:
oc port-forward svc/cost-data-service 8001:8000 -n cost-monitor
```

Open http://localhost:8080

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

Manifests are in `openshift/base/` with Kustomize overlays in `openshift/overlays/`. The deploy script is `deploy.sh`.

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
