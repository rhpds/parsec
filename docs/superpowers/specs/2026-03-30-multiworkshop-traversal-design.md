# Multi-Workshop Traversal Design

**Date:** 2026-03-30
**Status:** Approved

## Problem

Parsec cannot investigate multi-workshop events. The agent has no tool to traverse the full hierarchy from MultiWorkshop down to individual AnarchySubject components and their AAP2 jobs. A real failure case showed the agent searching the provisions DB, AAP2 controllers, and never finding the resource — because it didn't know to look for a MultiWorkshop in Babylon.

Additionally, `_extract_deployment_info()` only inspects `status.resources[0]`, missing failures on secondary components (e.g., the `zt-lab-developer-cnv` component fails while the `azure` sandbox component succeeds).

## Resource Hierarchy

```
MultiWorkshop (babylon.gpte.redhat.com/v1)
  └─ N Workshops (label: babylon.gpte.redhat.com/multiworkshop=<mw-name>)
       └─ ResourceClaim (from workshop.status.resourceClaims)
            └─ M AnarchySubjects (from rc.status.resources[])
                 ├─ spec.vars: current_state, desired_state, guid, job_vars
                 └─ status.towerJobs: provision/start/stop/destroy job refs
                      └─ deployerJob ID + towerHost + jobStatus
```

Key facts discovered from `aws-test-zz7zn`:
- MultiWorkshop has 8 Workshop assets
- Each Workshop has 1 ResourceClaim (tracked in `status.resourceClaims`)
- Each ResourceClaim can have **multiple resources** (e.g., `azure` sandbox + `zt-lab-developer-cnv` lab)
- Failures can occur on any component — only iterating all resources reveals the root cause
- Each AnarchySubject resource has `status.towerJobs` with AAP2 job ID and controller hostname

## Changes

### 1. New `get_multiworkshop` action in `query_babylon_catalog`

**Parameters:** `name` (required), `namespace` (required), `cluster` (optional — auto-searches all clusters if omitted)

**Traversal:**
1. Fetch MultiWorkshop by name in namespace
2. List child Workshops via label selector `babylon.gpte.redhat.com/multiworkshop=<name>`
3. For each Workshop, fetch each ResourceClaim from `status.resourceClaims`
4. For each ResourceClaim, extract info from **all** `status.resources[]` entries (not just [0])
5. For each resource, extract: name, healthy, ready, GUID, current_state, desired_state, tower jobs

**Return structure:**
```json
{
  "cluster": "east",
  "name": "aws-test-zz7zn",
  "namespace": "user-wharris-redhat-com",
  "display_name": "AWS Test",
  "requester": "wharris@redhat.com",
  "seats": 1,
  "start_date": "2026-03-30T19:34:48Z",
  "end_date": "2026-04-01T03:34:48Z",
  "purpose": "QA",
  "summary": {
    "total_workshops": 8,
    "healthy": 4,
    "failed": 4,
    "active": 8
  },
  "workshops": [
    {
      "name": "aws-test-zz7zn-zt-ansiblebu.zt-ans-bu-cloud-azure-visibil-984tp",
      "display_name": "Hybrid Cloud automation - Azure Visibility",
      "catalog_item": "zt-ansiblebu.zt-ans-bu-cloud-azure-visibility-aap.prod",
      "workshop_id": "fwqpsg",
      "provision_count": {"ordered": 1, "active": 1, "failed": 1, "retries": 0},
      "resource_claims": [
        {
          "name": "zt-...-65qq2",
          "state": "provision-failed",
          "resources": [
            {
              "name": "azure",
              "healthy": true,
              "ready": true,
              "guid": "jvvsr",
              "current_state": "started",
              "tower_jobs": {
                "provision": {"job_id": 2318393, "controller": "aap2-prod-us-east-2...", "status": "successful"}
              }
            },
            {
              "name": "zt-lab-developer-cnv",
              "healthy": false,
              "ready": false,
              "guid": "jvvsr-1",
              "current_state": "provision-failed",
              "tower_jobs": {
                "provision": {"job_id": 2318424, "controller": "aap2-prod-us-east-2...", "status": "failed"}
              }
            }
          ]
        }
      ]
    }
  ]
}
```

### 2. Fix `_extract_deployment_info` — all resources, not just [0]

Current code only checks `resources[0]` for AnarchySubject state. Add a new helper `_extract_resource_components()` that iterates all `status.resources[]` and extracts per-component:
- `name`, `healthy`, `ready`
- AnarchySubject reference (name, namespace)
- `current_state`, `desired_state`, GUID (from `spec.vars` and `job_vars`)
- `towerJobs` (all actions: provision, start, stop, destroy — job ID + controller + status)

Use this in `get_multiworkshop` traversal. Leave the existing `_extract_deployment_info` unchanged for backward compatibility with other actions.

### 3. Auto-search all clusters

If `cluster` is not specified and `sandbox_comment` is not available, iterate all configured Babylon clusters looking for the MultiWorkshop by name. Stop at first match (same pattern as `_search_all_clusters_for_guid`).

### 4. Tool schema update (`tool_definitions.py`)

Add `get_multiworkshop` to the action enum description in the `query_babylon_catalog` tool definition.

### 5. Prompt guidance (`babylon_agent.md`)

Add a "Multi-Workshop Investigation" section:
- If user provides a name like `xxx-yyyyy` or mentions "multi-workshop", use `get_multiworkshop` action
- URL pattern: `catalog.demo.redhat.com/multi-workshop/<namespace>/<name>` → extract namespace + name
- The result includes all tower job references — use `query_aap2` with the failed job ID to get logs
- For multi-component items, the failure is often on a secondary resource (not the sandbox), so always check all resources

### 6. Fast-path classifier update (`agents.py`)

Add `multi.?workshop` to `_BABYLON_PATTERNS` regex so explicit multi-workshop mentions route directly to the Babylon agent.

### 7. Orchestrator fallback guidance (`orchestrator.md`)

Add routing guidance: MultiWorkshops don't exist in the provisions DB. If the user provides an identifier that isn't found in the provisions DB and the query mentions failures or status, delegate to `investigate_babylon` — it may be a MultiWorkshop or Workshop name. The Babylon agent's `get_multiworkshop` action can search all clusters by name.
