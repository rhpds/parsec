# AAP2 Job Investigation Tool — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `query_aap2` tool that queries AAP2 controller REST APIs for job metadata, execution events, and job search — enabling investigation of provisioning failures from the Parsec chat UI.

**Architecture:** Single `query_aap2` tool with three actions (`get_job`, `get_job_events`, `find_jobs`). Connection module manages httpx clients with HTTP Basic Auth per controller. Controller resolution matches short names or full hostnames from AnarchySubject `towerHost`.

**Tech Stack:** httpx (async HTTP client, already a dependency), AAP2 REST API (`/api/v2/`), Dynaconf config.

**Spec:** `docs/superpowers/specs/2026-03-12-aap2-job-investigation-design.md`

---

## Task 1: Connection Module (`src/connections/aap2.py`)

**Files:**
- Create: `src/connections/aap2.py`

- [ ] **Step 1: Create the connection module**

This module manages httpx clients, controller resolution, and API helpers. It follows the same pattern as `src/connections/babylon.py`.

```python
"""AAP2 controller connections — httpx-based REST API clients."""

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)

# Parsed cluster configs: {name: {url, username, password}}
_cluster_configs: dict[str, dict[str, str]] = {}
# Cached clients keyed by cluster name
_clients: dict[str, httpx.AsyncClient] = {}


def init_aap2() -> None:
    """Initialize AAP2 controller connections from config."""
    cfg = get_config()
    aap2_cfg = cfg.get("aap2", {})
    clusters = aap2_cfg.get("clusters", {})

    if not clusters:
        logger.info("No AAP2 controllers configured — job lookups disabled")
        return

    for name, cluster_cfg in clusters.items():
        name_lower = name.lower()
        if not isinstance(cluster_cfg, dict):
            continue
        url = cluster_cfg.get("url", "") or cluster_cfg.get("URL", "")
        username = cluster_cfg.get("username", "") or cluster_cfg.get("USERNAME", "")
        password = cluster_cfg.get("password", "") or cluster_cfg.get("PASSWORD", "")
        if not url:
            logger.warning("AAP2 cluster '%s' has no URL", name_lower)
            continue
        if not username or not password:
            logger.warning("AAP2 cluster '%s' has no credentials", name_lower)
            continue

        _cluster_configs[name_lower] = {
            "url": url.rstrip("/"),
            "username": username,
            "password": password,
        }
        logger.info("AAP2 cluster '%s' configured (url=%s)", name_lower, url)

    logger.info("AAP2: %d controllers configured", len(_cluster_configs))


def get_configured_controllers() -> list[str]:
    """Return list of configured AAP2 controller names."""
    return list(_cluster_configs.keys())


def resolve_controller(controller: str) -> str:
    """Resolve a controller input to a configured cluster name.

    Accepts:
      - Short name: "east" -> exact match against config keys
      - Full hostname: "aap2-prod-us-east-2.aap.infra.demo.redhat.com"
        -> contains match against configured URLs

    Returns the cluster name, or raises ValueError if not found.
    """
    if not controller:
        raise ValueError(
            "No controller specified. "
            f"Configured: {', '.join(_cluster_configs.keys())}"
        )

    key = controller.lower().strip()

    # Exact match on cluster name
    if key in _cluster_configs:
        return key

    # Contains match on URL hostname
    for name, cfg in _cluster_configs.items():
        parsed = urlparse(cfg["url"])
        hostname = (parsed.hostname or "").lower()
        if key in hostname or hostname in key:
            return name

    raise ValueError(
        f"Unknown AAP2 controller: '{controller}'. "
        f"Configured: {', '.join(_cluster_configs.keys())}"
    )


async def _get_client(cluster_name: str) -> httpx.AsyncClient:
    """Get or create an httpx client for an AAP2 controller."""
    if cluster_name in _clients:
        return _clients[cluster_name]

    if cluster_name not in _cluster_configs:
        raise ValueError(
            f"Unknown AAP2 controller: '{cluster_name}'. "
            f"Configured: {list(_cluster_configs.keys())}"
        )

    cfg = _cluster_configs[cluster_name]
    client = httpx.AsyncClient(
        base_url=cfg["url"],
        auth=httpx.BasicAuth(cfg["username"], cfg["password"]),
        verify=False,  # noqa: S501 — self-signed certs on AAP2 controllers
        timeout=30.0,
        headers={"Accept": "application/json"},
    )
    _clients[cluster_name] = client
    return client


async def api_get(cluster_name: str, path: str, params: dict | None = None) -> dict:
    """Make a GET request to the AAP2 REST API.

    Returns the JSON response body. Raises on HTTP errors with clear messages.
    """
    client = await _get_client(cluster_name)
    resp = await client.get(path, params=params or {})

    if resp.status_code == 401:
        raise PermissionError(
            f"Authentication failed for controller '{cluster_name}' (HTTP 401)"
        )
    if resp.status_code == 404:
        raise LookupError(
            f"Not found on controller '{cluster_name}': {path} (HTTP 404)"
        )

    resp.raise_for_status()
    return resp.json()


async def api_paginate(
    cluster_name: str,
    path: str,
    params: dict | None = None,
    max_results: int = 50,
) -> list[dict]:
    """Paginate through AAP2 API results.

    The AAP2 API returns paginated responses with 'next' URLs.
    Collects up to max_results items.
    """
    params = dict(params or {})
    params.setdefault("page_size", min(max_results, 200))

    results: list[dict] = []
    data = await api_get(cluster_name, path, params)
    results.extend(data.get("results", []))

    while len(results) < max_results and data.get("next"):
        # The 'next' URL is absolute — extract just the path + query
        next_url = data["next"]
        parsed = urlparse(next_url)
        next_path = parsed.path
        if parsed.query:
            next_path += f"?{parsed.query}"

        data = await api_get(cluster_name, next_path)
        results.extend(data.get("results", []))

    return results[:max_results]


async def close_clients() -> None:
    """Close all httpx clients."""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()
```

- [ ] **Step 2: Commit**

```bash
git add src/connections/aap2.py
git commit -m "Add AAP2 connection module with httpx clients and controller resolution"
```

---

## Task 2: Tool Implementation (`src/tools/aap2.py`)

**Files:**
- Create: `src/tools/aap2.py`

- [ ] **Step 1: Create the tool module**

This module implements the three actions: `get_job`, `get_job_events`, `find_jobs`. It imports the secret-stripping utilities from `src/tools/babylon.py`.

```python
"""Tool: query_aap2 — query AAP2 controllers for job data."""

import asyncio
import json
import logging
import re
from typing import Any

from src.connections.aap2 import (
    api_get,
    api_paginate,
    get_configured_controllers,
    resolve_controller,
)

logger = logging.getLogger(__name__)

# Reuse secret-stripping from babylon tool
from src.tools.babylon import _SECRET_KEYS, _SECRET_PATTERNS


def _strip_secrets(obj: Any) -> Any:
    """Recursively strip secret values from a dict/list."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in _SECRET_KEYS or _SECRET_PATTERNS.search(k):
                result[k] = "[REDACTED]"
            else:
                result[k] = _strip_secrets(v)
        return result
    elif isinstance(obj, list):
        return [_strip_secrets(item) for item in obj]
    return obj


def _extract_variable(extra_vars: dict, key: str, default: str = "unknown") -> Any:
    """Extract a variable from extra_vars, checking common nested locations."""
    if not extra_vars or not isinstance(extra_vars, dict):
        return default

    # Check root level
    if key in extra_vars:
        return extra_vars[key]

    # Check job_metadata
    jm = extra_vars.get("job_metadata", {})
    if isinstance(jm, dict) and key in jm:
        return jm[key]

    return default


def _extract_guid(extra_vars: dict, job_name: str) -> str:
    """Extract GUID from extra_vars or parse from job name."""
    guid = _extract_variable(extra_vars, "guid", "")
    if guid:
        return guid

    # Parse from job name pattern:
    # "RHPDS agd-v2.sovereign-cloud.prod-gm5ld-2-provision-..."
    match = re.search(r"-([0-9a-z]{5})-\d+-(?:provision|destroy|stop|start)", job_name)
    if match:
        return match.group(1)

    return "unknown"


def _parse_job_metadata(data: dict, cluster_name: str) -> dict:
    """Parse AAP2 job API response into a clean metadata dict."""
    extra_vars_raw = data.get("extra_vars", "{}")
    try:
        extra_vars = json.loads(extra_vars_raw) if isinstance(extra_vars_raw, str) else extra_vars_raw
    except (json.JSONDecodeError, TypeError):
        extra_vars = {}

    job_name = data.get("name", "")
    summary = data.get("summary_fields", {})

    started = data.get("started", "")
    finished = data.get("finished", "")
    elapsed = data.get("elapsed")
    duration = int(float(elapsed)) if elapsed else None

    result = {
        "job_id": data.get("id"),
        "job_name": job_name,
        "status": data.get("status", ""),
        "failed": data.get("failed", False),
        "started": started,
        "finished": finished,
        "duration_seconds": duration,
        "launch_type": data.get("launch_type", ""),
        "job_explanation": data.get("job_explanation", ""),
        "template_name": summary.get("job_template", {}).get("name", ""),
        "env_type": _extract_variable(extra_vars, "env_type", "unknown"),
        "guid": _extract_guid(extra_vars, job_name),
        "action": _extract_variable(extra_vars, "ACTION", "unknown"),
        "cloud_provider": _extract_variable(extra_vars, "cloud_provider", "unknown"),
        "display_name": _extract_variable(extra_vars, "display_name", ""),
        "controller": cluster_name,
    }

    # Git context from __meta__.deployer in extra_vars
    deployer = extra_vars.get("__meta__", {}).get("deployer", {}) if isinstance(extra_vars, dict) else {}
    if deployer:
        result["git_url"] = deployer.get("scm_url", "")
        result["git_branch"] = deployer.get("scm_ref", deployer.get("scm_branch", ""))
        result["git_revision"] = deployer.get("scm_revision", "")

    return result


def _parse_event(event: dict) -> dict:
    """Parse an AAP2 job event into a clean dict."""
    event_data = event.get("event_data", {})
    if isinstance(event_data, str):
        try:
            event_data = json.loads(event_data)
        except (json.JSONDecodeError, TypeError):
            event_data = {}

    # Extract error message from event_data.res
    error_msg = ""
    res = event_data.get("res", {}) if isinstance(event_data, dict) else {}
    if isinstance(res, dict):
        error_msg = res.get("msg", "")
        if not error_msg:
            error_msg = res.get("module_stderr", "")

    # Truncate stdout to keep response manageable
    stdout = event.get("stdout", "") or ""
    if len(stdout) > 500:
        stdout = stdout[:500] + "... [truncated]"

    return {
        "event": event.get("event", ""),
        "task": event.get("task", ""),
        "play": event.get("play", ""),
        "role": event.get("role", ""),
        "host": event.get("host_name", ""),
        "failed": event.get("failed", False),
        "changed": event.get("changed", False),
        "stdout": stdout,
        "error_msg": str(error_msg)[:500] if error_msg else "",
        "counter": event.get("counter", 0),
    }


def _parse_job_summary(data: dict, cluster_name: str) -> dict:
    """Parse a job list entry into a summary dict."""
    elapsed = data.get("elapsed")
    duration = int(float(elapsed)) if elapsed else None
    summary = data.get("summary_fields", {})

    return {
        "job_id": data.get("id"),
        "job_name": data.get("name", ""),
        "status": data.get("status", ""),
        "failed": data.get("failed", False),
        "started": data.get("started", ""),
        "finished": data.get("finished", ""),
        "duration_seconds": duration,
        "template_name": summary.get("job_template", {}).get("name", ""),
        "controller": cluster_name,
    }


async def _get_job(cluster_name: str, job_id: int) -> dict:
    """Fetch job metadata from an AAP2 controller."""
    data = await api_get(cluster_name, f"/api/v2/jobs/{job_id}/")
    return _parse_job_metadata(data, cluster_name)


async def _get_job_events(
    cluster_name: str,
    job_id: int,
    failed_only: bool = False,
    changed_only: bool = False,
    max_results: int = 50,
) -> dict:
    """Fetch job events from an AAP2 controller."""
    params: dict[str, Any] = {"order_by": "counter"}
    if failed_only:
        params["failed"] = "true"
    if changed_only:
        params["changed"] = "true"

    raw_events = await api_paginate(
        cluster_name,
        f"/api/v2/jobs/{job_id}/job_events/",
        params=params,
        max_results=max_results,
    )

    # Filter out non-task events (playbook_on_stats, etc.) unless they failed
    events = []
    for e in raw_events:
        event_type = e.get("event", "")
        if event_type in (
            "runner_on_ok", "runner_on_failed", "runner_on_unreachable",
            "runner_on_skipped", "runner_retry",
        ) or e.get("failed"):
            events.append(_parse_event(e))

    return {
        "job_id": job_id,
        "controller": cluster_name,
        "event_count": len(events),
        "filters": {
            "failed_only": failed_only,
            "changed_only": changed_only,
        },
        "events": events,
    }


async def _find_jobs_on_controller(
    cluster_name: str,
    status: str = "",
    created_after: str = "",
    created_before: str = "",
    template_name: str = "",
    max_results: int = 50,
) -> list[dict]:
    """Search for jobs on a single AAP2 controller."""
    params: dict[str, Any] = {"order_by": "-finished"}
    if status:
        params["status"] = status
    if created_after:
        params["created__gt"] = created_after
    if created_before:
        params["created__lt"] = created_before
    if template_name:
        params["name__icontains"] = template_name

    raw_jobs = await api_paginate(
        cluster_name, "/api/v2/jobs/", params=params, max_results=max_results
    )
    return [_parse_job_summary(j, cluster_name) for j in raw_jobs]


async def _find_jobs(
    controller: str = "",
    status: str = "",
    created_after: str = "",
    created_before: str = "",
    template_name: str = "",
    max_results: int = 50,
) -> dict:
    """Search for jobs across one or all controllers."""
    if controller:
        cluster_name = resolve_controller(controller)
        jobs = await _find_jobs_on_controller(
            cluster_name, status, created_after, created_before,
            template_name, max_results,
        )
    else:
        # Query all controllers in parallel
        controllers = get_configured_controllers()
        if not controllers:
            return {"error": "No AAP2 controllers configured"}

        per_controller = max(max_results // len(controllers), 10)
        tasks = [
            _find_jobs_on_controller(
                name, status, created_after, created_before,
                template_name, per_controller,
            )
            for name in controllers
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        jobs = []
        errors = []
        for name, result in zip(controllers, results):
            if isinstance(result, Exception):
                errors.append(f"{name}: {result}")
                logger.warning("AAP2 find_jobs failed on %s: %s", name, result)
            else:
                jobs.extend(result)

        # Sort merged results by finished time (descending)
        jobs.sort(key=lambda j: j.get("finished", "") or "", reverse=True)
        jobs = jobs[:max_results]

        if errors and not jobs:
            return {"error": f"All controllers failed: {'; '.join(errors)}"}

    return {
        "total": len(jobs),
        "jobs": jobs,
    }


async def query_aap2(
    action: str,
    controller: str = "",
    job_id: int | None = None,
    failed_only: bool = False,
    changed_only: bool = False,
    status: str = "",
    created_after: str = "",
    created_before: str = "",
    template_name: str = "",
    max_results: int = 50,
) -> dict:
    """Main entry point for the query_aap2 tool."""
    max_results = min(max_results, 200)

    try:
        if action == "get_job":
            if not job_id:
                return {"error": "job_id is required for get_job"}
            if not controller:
                return {"error": "controller is required for get_job"}
            cluster_name = resolve_controller(controller)
            return await _get_job(cluster_name, job_id)

        elif action == "get_job_events":
            if not job_id:
                return {"error": "job_id is required for get_job_events"}
            if not controller:
                return {"error": "controller is required for get_job_events"}
            cluster_name = resolve_controller(controller)
            return await _get_job_events(
                cluster_name, job_id, failed_only, changed_only, max_results,
            )

        elif action == "find_jobs":
            return await _find_jobs(
                controller, status, created_after, created_before,
                template_name, max_results,
            )

        else:
            return {"error": f"Unknown action: '{action}'. Use get_job, get_job_events, or find_jobs."}

    except (ValueError, LookupError, PermissionError) as e:
        return {"error": str(e)}
    except httpx.ConnectError as e:
        return {"error": f"Cannot reach AAP2 controller: {e}"}
    except httpx.HTTPStatusError as e:
        return {"error": f"AAP2 API error: {e.response.status_code} {e.response.reason_phrase}"}
    except Exception:
        logger.exception("Unexpected error in query_aap2")
        return {"error": "Internal error querying AAP2 — check logs"}
```

- [ ] **Step 2: Commit**

```bash
git add src/tools/aap2.py
git commit -m "Add AAP2 tool with get_job, get_job_events, and find_jobs actions"
```

---

## Task 3: Wire Into Agent (tool definition + orchestrator + app startup)

**Files:**
- Modify: `src/agent/tool_definitions.py` — append tool schema to TOOLS list
- Modify: `src/agent/orchestrator.py` — add import + dispatch case
- Modify: `src/app.py` — add init_aap2 to startup

- [ ] **Step 1: Add tool schema to `src/agent/tool_definitions.py`**

Add the following entry to the end of the `TOOLS` list (before the closing `]`), after the `generate_report` tool:

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
                        "AnarchySubject towerHost. Required for get_job and "
                        "get_job_events. For find_jobs, omit to search all controllers."
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
                    "description": (
                        "For get_job_events: only return events that made changes. Default: false."
                    ),
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
                    "description": (
                        "For find_jobs: ISO timestamp or YYYY-MM-DD. Only jobs created after this."
                    ),
                },
                "created_before": {
                    "type": "string",
                    "description": (
                        "For find_jobs: ISO timestamp or YYYY-MM-DD. Only jobs created before this."
                    ),
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
    },
```

- [ ] **Step 2: Add dispatch to `src/agent/orchestrator.py`**

Add import at the top (after the babylon import, around line 37):

```python
from src.tools.aap2 import query_aap2
```

Add dispatch case in `_execute_tool()` (before the `render_chart` case, around line 170):

```python
    elif tool_name == "query_aap2":
        return await query_aap2(
            action=tool_input["action"],
            controller=tool_input.get("controller", ""),
            job_id=tool_input.get("job_id"),
            failed_only=tool_input.get("failed_only", False),
            changed_only=tool_input.get("changed_only", False),
            status=tool_input.get("status", ""),
            created_after=tool_input.get("created_after", ""),
            created_before=tool_input.get("created_before", ""),
            template_name=tool_input.get("template_name", ""),
            max_results=tool_input.get("max_results", 50),
        )
```

Also add to `_SLOW_TOOL_LABELS` (around line 52):

```python
    "query_aap2": "Querying AAP2 controller",
```

- [ ] **Step 3: Add init_aap2 to app startup in `src/app.py`**

Add import (after the babylon import, line 13):

```python
from src.connections.aap2 import init_aap2
```

Add to the sync init loop (after the Babylon entry, around line 46):

```python
        ("AAP2", init_aap2),
```

- [ ] **Step 4: Commit**

```bash
git add src/agent/tool_definitions.py src/agent/orchestrator.py src/app.py
git commit -m "Wire query_aap2 tool into agent orchestrator and app startup"
```

---

## Task 4: Configuration

**Files:**
- Modify: `config/config.yaml` — add aap2 section
- Modify: `config/config.local.yaml` — add credentials (gitignored)

- [ ] **Step 1: Add aap2 section to `config/config.yaml`**

Add after the `babylon` section (after line 59), before `alert_api_key`:

```yaml
aap2:
  # AAP2 controller connections for job investigation.
  # Each controller needs a URL and credentials (username/password).
  # Set credentials in config.local.yaml or via env vars
  # (PARSEC_AAP2__CLUSTERS__EAST__PASSWORD, etc.).
  clusters: {}
  #   east:
  #     url: "https://aap2-prod-us-east-2.aap.infra.demo.redhat.com"
  #     username: "monitor"
  #     password: ""
```

- [ ] **Step 2: Add credentials to `config/config.local.yaml`**

Add after the `babylon` section:

```yaml
aap2:
  clusters:
    east:
      url: "https://aap2-prod-us-east-2.aap.infra.demo.redhat.com"
      username: "monitor"
      password: "<east-password>"  # pragma: allowlist secret
    west:
      url: "https://aap2-prod-us-west-2.aap.infra.demo.redhat.com"
      username: "monitor"
      password: "<west-password>"  # pragma: allowlist secret
    event0:
      url: "https://event0.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
      username: "monitor"
      password: "<event0-password>"  # pragma: allowlist secret
    partner0:
      url: "https://aap2-partner0-prod-us-east-2.aap.infra.partner.demo.redhat.com"
      username: "monitor"
      password: "<partner0-password>"  # pragma: allowlist secret
```

- [ ] **Step 3: Commit config.yaml only** (config.local.yaml is gitignored)

```bash
git add config/config.yaml
git commit -m "Add AAP2 controller config section"
```

---

## Task 5: Agent Instructions

**Files:**
- Modify: `config/agent_instructions.md` — add AAP2 investigation guidance

- [ ] **Step 1: Add AAP2 section to agent instructions**

Add the following section after the existing Babylon-related content in `config/agent_instructions.md`. Find the appropriate location near other tool documentation sections.

```markdown
## AAP2 Job Investigation

The `query_aap2` tool queries AAP2 controllers for job metadata and execution events.
Use this to investigate provisioning failures, slow jobs, and retry patterns.

### Investigation Flow

1. Get the provision GUID from the user's question or the provision DB
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

- The job name encodes the catalog item and GUID:
  `RHPDS agd-v2.sovereign-cloud.prod-gm5ld-2-provision-...`
- Use `find_jobs` with `status=failed` to find recent failures across all controllers
- Failed events include the error message in `error_msg` — this is usually the root cause
- Job `elapsed` is wall-clock seconds; long durations may suggest retries or waiting
- The `controller` parameter accepts both short names (`east`) and full hostnames
  from `towerHost` — so you can pass the value directly from the AnarchySubject
```

- [ ] **Step 2: Commit**

```bash
git add config/agent_instructions.md
git commit -m "Add AAP2 investigation guidance to agent instructions"
```

---

## Task 6: Manual Validation

- [ ] **Step 1: Start local server and verify startup**

```bash
source .venv/bin/activate
python3 -m uvicorn src.app:app --host 0.0.0.0 --port 8000
```

Check logs for: `AAP2: 4 controllers configured`

- [ ] **Step 2: Test via chat UI**

Open `http://localhost:8000` and test these queries:

1. `"Show me the last 5 failed AAP2 jobs"` — should trigger `find_jobs` across all controllers
2. `"Get AAP2 job 2228225 on east"` — should return job metadata with secrets stripped
3. `"Show me the failed events for AAP2 job 2245189 on east"` — should return the failed task

- [ ] **Step 3: Verify no secrets leak**

Check that `get_job` results do NOT contain raw values for keys like `aws_secret_access_key`, `bastion_ansible_ssh_pass`, `ocp4_pull_secret`, etc. They should show `[REDACTED]`.

---

## Task 7: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — add AAP2 section

- [ ] **Step 1: Add AAP2 section to CLAUDE.md**

Add a new section after the Babylon Cluster Integration section documenting:
- What the tool does
- The 4 controllers and their hostnames
- How controller resolution works (towerHost from AnarchySubject)
- Configuration pattern (username/password per cluster)
- The investigation flow (Babylon -> towerJobs -> query_aap2)

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "Document AAP2 job investigation tool in CLAUDE.md"
```
