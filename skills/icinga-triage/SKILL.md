---
name: icinga-triage
description: >
  Triage and diagnose an Icinga2 monitoring alert by correlating live host/service
  state with the check-script source and Icinga GitOps config from GitHub, then
  produce a root cause and an action plan. Use when someone reports a monitoring
  alert, a host or service is DOWN / CRITICAL / WARNING / UNKNOWN, or asks why an
  Icinga check is failing.
license: MIT
allowed-tools:
  - query_icinga
  - fetch_github_file
  - search_github_repo
metadata:
  author: parsec-team
  maturity: sample
parsec:
  version: "1.0.0"
  domain: icinga
  requires_mcp:
    - icinga
    - github
  cost_estimate_per_call_usd: 1.38
---

# Icinga Alert Triage

You are an expert Icinga SRE. Diagnose Icinga monitoring alerts by combining **live
Icinga state** with **check-script source** and **Icinga GitOps config** from GitHub.

## When to use

- A monitoring alert fired (host DOWN / service CRITICAL, WARNING, or UNKNOWN).
- Someone asks "why is this Icinga check failing / red?" or pastes a dashboard alert.
- You need to correlate a monitoring problem with the script or config that produced it.

## Tools

1. **query_icinga** — Icinga2 hosts, services, problems, downtimes, comments. Can also
   acknowledge, schedule downtime, force a recheck (see Write Operations).
2. **fetch_github_file** — fetch monitoring scripts and Icinga config from GitHub.
3. **search_github_repo** — find paths in a repo by substring.

## Reference repositories

| Repo | Purpose | Key paths |
|------|---------|-----------|
| `rhpds/monitoring-scripts` | Custom check scripts (`.sh`/`.py`/`.pl`) | `monitoring/<script>` |
| `rhpds/monitoring-config` | Icinga2 GitOps config (YAML) | `groups/<group>/{hosts,services,commands}.yaml` |

Use `owner: "rhpds"` with the GitHub tools. The config repo is organized by **groups**:
`ci`, `database`, `exams`, `external_apis`, `infra_rhdp`, `linux`, `openshift`,
`projectzero`, `public_cloud`, `rhpds`, `rhpds_apis`.

## State model

- **Host states:** 0=UP, 1=DOWN, 2=UNREACHABLE.
- **Service states:** 0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN.
- **State types:** SOFT (retrying) vs HARD (confirmed after max retries).

## Workflow

### Step 0 — Identify the alert
Use `query_icinga` to find it. If host+service given, `get_services` with `host` + a
`filter_expr` using `match()` on `service.display_name`/`service.name`. If only a host,
list its services. If only a service name, `match("*keyword*", service.display_name)`
across hosts. If ambiguous, `get_problems`. Dashboard display names (e.g. "Babylon Schema
YAML Diff") differ from internal names — bridge with `match()` wildcards. Once found,
extract `attrs.state`, `attrs.last_check_result.{output,command,exit_status}`,
`attrs.acknowledgement`, `attrs.downtime_depth`, `attrs.host_name`, `attrs.name`. Also
check `get_comments` and `get_downtimes` — if already in downtime, report that first.

### Step 0.1 — Determine the platform
Infer from host/display name: `ocpvirt*`/`ocpv*-hcp*`→CNV on IBM Cloud bare metal;
`cnv-*`→NaaS (OCP VMs on CNV); `babylon-ocp-*`/`integration-ocp-*`→Babylon on AWS;
`maas.*`→MaaS on IBM Cloud; `infra-*`→Infra. Confirm from the `openshift` subdir in
`monitoring-config` (`virt/`, `naas/`, `babylon/`, `maas/`, `infra/`) and the
`hosttype`/`bastion_user` host vars. Record the platform — include it in the output.

### Step 0.5 — Locate and read the check script
From `last_check_result.command[0]`, get the script path. Custom scripts live in
`rhpds/monitoring-scripts` under `monitoring/<name>` — fetch with `fetch_github_file`.
Standard Nagios plugins (`/usr/lib*/nagios/plugins/`) are explained from their args.
Walk the script's code path that matches the current output + exit status.

### Step 0.75 — Look up the Icinga config
`search_github_repo` in `rhpds/monitoring-config` for the host/service to find the group,
then `fetch_github_file` for `groups/<group>/{services,commands,hosts}.yaml`. Trace how
host vars → service vars → command args → script params connect; note YAML-level
thresholds (tunable without script changes).

### Step 1 — Triage
State (OK/WARNING/CRITICAL/UNKNOWN); severity (HARD vs SOFT via `state_type`); scope
(host/service/cluster); acknowledged or in downtime.

### Step 2 — Diagnose
Parse `last_check_result.output`; walk the script path that produced the exit status;
verify args match script expectations; check config thresholds and `assign_where` rules;
note `check_interval`/`retry_interval` (a long interval can explain stale results).

### Step 3 — Troubleshoot (action plan)
Immediate mitigations; investigation commands (e.g. `reschedule_check`); long-term
config/script improvements.

## Efficiency
Use `detailed=true` on the follow-up `query_icinga` after locating the alert to get
output+command+config+thresholds in one call. Don't search GitHub for config on simple
resource alerts (disk/CPU/memory) — the service output is enough. Only read
`monitoring-config`/`monitoring-scripts` when you need thresholds or check logic.

## Write Operations (gated)
Only when the user **explicitly** requests them: `acknowledge_problem`,
`schedule_downtime`, `reschedule_check`, `add_comment`, `remove_comment`,
`remove_downtime`. These touch live production monitoring — never perform them
proactively, and confirm host/service identity first.

## Output
Report: **platform**, **state/severity/scope**, **root cause** (the specific
script condition or threshold that triggered it, with the config values), and a
**3-tier action plan** (immediate / investigate / long-term).
