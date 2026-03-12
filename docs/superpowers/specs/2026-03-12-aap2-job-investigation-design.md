# AAP2 Job Investigation Tool — Design Spec

## Summary

Add a `query_aap2` tool to Parsec that queries AAP2 (Ansible Automation Platform 2)
controller REST APIs for job metadata, execution events, and job search. This enables
investigating provisioning failures, slow jobs, and retry patterns directly from the
Parsec chat UI.

## Architecture

### Data Flow

```
User question ("why did provision xyz fail?")
  -> Claude agent
    -> query_babylon_catalog (get AnarchySubject -> towerHost + deployerJob)
    -> query_aap2 (get_job / get_job_events from the right controller)
  -> Claude analyzes job status, failed tasks, error messages
  -> Answer with root cause
```

### Connection Pattern

Uses the AAP2 Controller REST API (`/api/v2/`) with HTTP Basic Auth (username/password),
NOT direct database access. This works across all clusters including partner infrastructure
where DB access is unavailable.

**httpx-based async client** — same pattern as the Babylon connection module. One
persistent `httpx.AsyncClient` per controller, created on first use with basic auth
credentials and TLS verification disabled (self-signed certs).

### Controller Resolution

The tool resolves which AAP2 controller to query by matching a `controller` parameter
against configured cluster hostnames. Sources for the controller hostname:

1. **AnarchySubject `status.towerJobs.<action>.towerHost`** — the definitive source
   when investigating a specific provision. The agent gets this from `query_babylon_catalog`.
2. **Direct specification** — the agent or user specifies a controller name (e.g., `east`).
3. **Search all** — for `find_jobs`, if no controller specified, query all configured controllers.

Matching logic: the `controller` parameter is matched against configured cluster names
(exact match) or URLs (contains match on hostname), case-insensitive. This handles both
`"east"` and `"aap2-prod-us-east-2.aap.infra.demo.redhat.com"`.

## Configuration

### config.yaml (base — no secrets)

```yaml
aap2:
  # AAP2 controller connections for job investigation.
  # Each controller needs a URL and credentials (username/password).
  # Set credentials in config.local.yaml or via env vars.
  clusters: {}
  #   east:
  #     url: "https://aap2-prod-us-east-2.aap.infra.demo.redhat.com"
  #     username: "monitor"
  #     password: ""
  verify_ssl: false
```

### config.local.yaml (secrets — gitignored)

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

### OpenShift Deployment

Credentials stored in a `parsec-aap2-credentials` secret, injected via env vars:

```
PARSEC_AAP2__CLUSTERS__EAST__URL=https://aap2-prod-us-east-2.aap.infra.demo.redhat.com
PARSEC_AAP2__CLUSTERS__EAST__USERNAME=monitor
PARSEC_AAP2__CLUSTERS__EAST__PASSWORD=<password>
```

The Ansible playbook (`playbooks/deploy.yaml`) will be updated to create this secret
and add the env vars to the Deployment manifest.

## Tool Definition

### `query_aap2`

Single tool with three actions, following the `query_babylon_catalog` pattern.

```python
{
    "name": "query_aap2",
    "description": (
        "Query an AAP2 (Ansible Automation Platform) controller for job details, "
        "execution events, and job search. Use this to investigate provisioning "
        "failures, slow jobs, and retry patterns. The controller hostname comes "
        "from AnarchySubject status.towerJobs.<action>.towerHost (get it from "
        "query_babylon_catalog first). The job ID comes from "
        "status.towerJobs.<action>.deployerJob."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get_job", "get_job_events", "find_jobs"],
                "description": (
                    "Action to perform. "
                    "get_job: Get job metadata, status, duration, extra_vars, "
                    "and git context for a specific job ID. "
                    "get_job_events: Get execution events for a job. Use "
                    "failed_only=true to see only errors. "
                    "find_jobs: Search for jobs by status, time range, or "
                    "template name across one or all controllers."
                ),
            },
            "controller": {
                "type": "string",
                "description": (
                    "AAP2 controller to query. Can be a short name (east, west, "
                    "partner0, event0) or the full hostname from "
                    "AnarchySubject towerHost. For find_jobs, omit to search "
                    "all controllers."
                ),
            },
            "job_id": {
                "type": "integer",
                "description": (
                    "AAP2 job ID. Required for get_job and get_job_events. "
                    "Get this from AnarchySubject status.towerJobs.<action>.deployerJob."
                ),
            },
            "failed_only": {
                "type": "boolean",
                "description": "For get_job_events: only return failed events. Default: false.",
            },
            "changed_only": {
                "type": "boolean",
                "description": "For get_job_events: only return events that made changes. Default: false.",
            },
            "status": {
                "type": "string",
                "description": (
                    "For find_jobs: filter by job status "
                    "(failed, successful, running, canceled, error)."
                ),
            },
            "created_after": {
                "type": "string",
                "description": "For find_jobs: ISO timestamp or YYYY-MM-DD. Only jobs created after this.",
            },
            "created_before": {
                "type": "string",
                "description": "For find_jobs: ISO timestamp or YYYY-MM-DD. Only jobs created before this.",
            },
            "template_name": {
                "type": "string",
                "description": "For find_jobs: filter by template name (contains match).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return. Default: 50, max: 200.",
            },
        },
        "required": ["action"],
    },
}
```

## File Structure

```
src/
  connections/
    aap2.py              # httpx clients, auth, controller resolution
  tools/
    aap2.py              # query_aap2() — tool implementation
  agent/
    orchestrator.py      # Add dispatch for query_aap2
    tool_definitions.py  # Add QUERY_AAP2 tool schema
config/
  config.yaml            # Add aap2 section (no secrets)
  agent_instructions.md  # Add AAP2 investigation guidance
```

## Implementation Details

### src/connections/aap2.py

- `init_aap2()` — called during app startup, reads config, validates URLs
- `_cluster_configs: dict[str, dict]` — stores url, username, password per cluster
- `resolve_controller(controller: str) -> str` — matches input against cluster names/URLs
- `get_client(cluster_name: str) -> httpx.AsyncClient` — cached async client with basic auth
- `api_get(cluster: str, path: str, params: dict) -> dict` — GET request with error handling
- `api_paginate(cluster: str, path: str, params: dict, max_results: int) -> list` — handles
  AAP2 API pagination (`next` URL)

### src/tools/aap2.py

**`get_job(cluster, job_id)`** — `GET /api/v2/jobs/{job_id}/`
Returns:
```json
{
  "job_id": 2228225,
  "job_name": "RHPDS agd-v2.sovereign-cloud.prod-gm5ld-2-provision-...",
  "status": "successful",
  "failed": false,
  "started": "2026-03-10T10:30:08Z",
  "finished": "2026-03-10T11:21:56Z",
  "duration_seconds": 3108,
  "launch_type": "manual",
  "job_explanation": "",
  "template_name": "...",
  "env_type": "sovereign-cloud",
  "guid": "gm5ld",
  "action": "provision",
  "cloud_provider": "ec2",
  "git_url": "https://github.com/...",
  "git_branch": "...",
  "git_revision": "...",
  "controller": "east"
}
```

Extra vars parsing: uses the same `extract_variable_key()` logic from the aap_extractor
to pull `env_type`, `guid`, `cloud_provider`, `ACTION`, `display_name` from the JSON
extra_vars blob. **Secrets are stripped** — any key matching `_SECRET_PATTERNS` or
`_SECRET_KEYS` (reused from babylon.py) is redacted before returning.

**`get_job_events(cluster, job_id, failed_only, changed_only)`** —
`GET /api/v2/jobs/{job_id}/job_events/?failed=true&changed=true`

Returns a list of events, each with:
```json
{
  "event": "runner_on_failed",
  "task": "Wait for Quay API to be available",
  "play": "Configure Quay",
  "role": "quay_setup",
  "host": "bastion.example.com",
  "failed": true,
  "changed": false,
  "stdout": "...",
  "error_msg": "Status code was 503...",
  "counter": 142
}
```

Event data filtering: the `event_data.res` subtree can be very large. Only essential
fields are kept: `msg`, `rc`, `changed`, `failed`, `module_stdout`, `module_stderr`.
The raw `stdout` field is truncated to 500 chars per event.

**`find_jobs(cluster, status, created_after, created_before, template_name)`** —
`GET /api/v2/jobs/?status=failed&created__gt=2026-03-11&order_by=-finished`

If no controller specified, queries all configured controllers in parallel using
`asyncio.gather()` and merges results sorted by finished time.

Returns a list of job summaries:
```json
{
  "job_id": 2245189,
  "job_name": "RHPDS agd-v2.openshift-days-...",
  "status": "failed",
  "started": "2026-03-12T15:20:00Z",
  "finished": "2026-03-12T15:35:19Z",
  "duration_seconds": 919,
  "template_name": "...",
  "controller": "east"
}
```

### Secret Stripping

Reuses the `_SECRET_PATTERNS` regex and `_SECRET_KEYS` set from `src/tools/babylon.py`
(moved to a shared location or imported). Applied to extra_vars before returning job
metadata. This prevents leaking AWS keys, passwords, tokens etc. that are commonly
passed as Ansible extra vars.

### Error Handling

- Controller not found: `{"error": "Unknown AAP2 controller: '...'. Configured: east, west, event0, partner0"}`
- Job not found: `{"error": "Job 12345 not found on controller east (HTTP 404)"}`
- Auth failure: `{"error": "Authentication failed for controller east (HTTP 401)"}`
- Connection failure: `{"error": "Cannot reach controller east: connection refused"}`
- No controllers configured: `{"error": "No AAP2 controllers configured"}`

### System Prompt Additions

Add to `config/agent_instructions.md`:

```markdown
## AAP2 Job Investigation

The `query_aap2` tool queries AAP2 controllers for job metadata and execution events.
Use this to investigate provisioning failures, slow jobs, and retry patterns.

### Investigation Flow

1. Get the provision GUID from the user's question or provision DB
2. Use `query_babylon_catalog` with `list_anarchy_subjects` + guid filter to find
   the AnarchySubject
3. Read `status.towerJobs` to get the `towerHost` and `deployerJob` (job ID)
4. Call `query_aap2` with `get_job` to get job status, duration, env_type
5. If the job failed, call `query_aap2` with `get_job_events` + `failed_only=true`
   to see the error details
6. Report findings: what task failed, on which host, with what error message

### Available Controllers

- east: aap2-prod-us-east-2 (primary production)
- west: aap2-prod-us-west-2 (secondary production)
- event0: event controller on ocpv-infra01
- partner0: partner Babylon controller

### Tips

- The job name contains the catalog item and GUID:
  `RHPDS agd-v2.sovereign-cloud.prod-gm5ld-2-provision-...`
- Use `find_jobs` with `status=failed` to find recent failures across all controllers
- Failed events include the error message in `error_msg` — this is usually the root cause
- Job `elapsed` is wall-clock seconds; long durations suggest retries or waiting
```

## Testing

### Manual Testing

```bash
# Start Parsec locally, then in the chat UI:
"Show me the last 5 failed AAP2 jobs"
"Why did provision gm5ld fail?"
"What's the status of AAP2 job 2228225 on east?"
```

### Validation Checklist

- [ ] `get_job` returns metadata with secrets stripped from extra_vars
- [ ] `get_job_events` with `failed_only=true` returns only failed events
- [ ] `get_job_events` with `changed_only=true` returns only changed events
- [ ] `find_jobs` across all controllers returns merged, sorted results
- [ ] Controller resolution works with short names (`east`) and full hostnames
- [ ] Auth failures return clear error messages
- [ ] Connection timeouts are handled gracefully (30s timeout)
- [ ] Large event lists are paginated correctly (AAP2 default page_size=25)
- [ ] No secrets leak in extra_vars or event stdout
