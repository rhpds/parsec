---
name: aap2-job-failure-rca
description: >
  Trace a failed AAP2 controller job to its root cause in the AgnosticV/AgnosticD
  configuration and produce a categorised, evidence-cited finding — every config or
  code claim carries an `owner/repo:path:line` citation and the verdict carries one
  category from the fixed taxonomy. Use when a triage answer is not enough: the fix
  is unclear, the error is a symptom, the same job keeps failing, or someone asks for
  a root cause analysis / RCA / post-mortem of a specific job.
license: MIT
allowed-tools:
  - mcp__parsec__query_aap2
  - mcp__parsec__lookup_catalog_item
  - mcp__parsec__fetch_github_file
  - mcp__parsec__search_github_repo
  - mcp__parsec__search_agnosticv_prs
  - mcp__parsec__query_babylon_catalog
  - mcp__parsec__query_provisions_db
  - mcp__parsec__db_describe_table
metadata:
  author: parsec-team
  maturity: sample
parsec:
  version: "1.0.0"
  domain: aap2
  requires_mcp:
    - reporting
    - github
  cost_estimate_per_call_usd: 0.05
---

# AAP2 Job Failure RCA

Take one failed AAP2 job from symptom to a cited root cause in the config chain.

## When to use

- Triage produced an error string but not a cause, or the error is plainly a symptom
  ("pod failed to start", "CrashLoopBackOff", "non-zero return code").
- The same catalog item or template keeps failing and someone needs the actual defect.
- Someone explicitly asks for a root cause analysis, an RCA, or a post-mortem of a job.
- The answer has to name a file to change, not describe the log.

## Tools

Under the Agent SDK runtime Parsec's own tools are served by the in-process `parsec`
MCP bridge, so every name below carries the `mcp__parsec__` prefix. The Reporting
MCP's discovered tools keep their `db_` prefix and are bridged the same way
(`mcp__parsec__db_describe_table`). Use these **EXACT** names — an unknown tool name
fails outright. Splunk is **out of scope** for this skill: the interactive path uses
the AAP2 API and GitHub only. If the cause genuinely needs container logs, say so and
name it as the gap rather than guessing.

## Procedure

1. **Get the job context.** `query_aap2(action="get_job_log")` — always `get_job_log`,
   never `get_job`. Record job id, status, start/finish, Project URL (which decides
   AgnosticD v1 vs v2), and the **Revision SHA**. Parse the template name
   `RHPDS {account}.{catalog-item}.{stage}-{guid}-{action} {uuid}` into account,
   catalog item, stage, GUID. Job id typos are common — if it is not on the expected
   controller, confirm the number before sweeping the rest in one batch.
2. **Locate the failure precisely.** `query_aap2(action="get_job_events",
   failed_only=true)`. Capture the failing `role : task`, host, module, return code,
   and `delta`. Timing is a discriminator: under ~10s means auth failure, missing
   resource, or bad config; minutes means timeout, network, or resource pressure.
3. **Resolve the config chain.** `lookup_catalog_item` first — it searches every
   agnosticv repo instantly and returns `owner`, `repo`, `path`, `files`,
   `default_branch`. Fetch `common.yaml` and `{stage}.yaml` at that `default_branch`.
   If `found: false` but the item ran, check `search_agnosticv_prs` — it may exist
   only on an unmerged branch. Resolve `__meta__.components`: Virtual CI
   (`deployer.type: null`, all real config lives in the component) vs Chained CI (own
   deployer plus components). Follow components recursively; stage propagates down.
4. **Apply variable precedence, and write down where each value came from.** The stage
   file overrides `common.yaml`; a component's own config overrides an empty parent
   placeholder. Extract `env_type` (v1) or `config` (v2) and
   `__meta__.deployer.scm_ref` from whichever layer actually won.
5. **Read the code that ran, at the version that ran.** Take `owner`/`repo` from the
   job's Project URL — do not hardcode them — and pass the job's Revision SHA as `ref`,
   falling back to `scm_ref` from step 4. Fetch
   `ansible/configs/{env_type}/default_vars.yml` and
   `ansible/roles/{role}/tasks/main.yml` for the failing role. Use
   `search_github_repo` to confirm a path in one call; never list directories one at a
   time. Config names differ between v1 and v2 (`ocp4-cluster` → `openshift-cluster`).
6. **Cite every config and code claim.** Format: `owner/repo:path/to/file.yml:line`,
   using the exact `owner`, `repo`, `ref`, and `path` you actually passed to
   `fetch_github_file`. Link as
   `https://github.com/{owner}/{repo}/blob/{ref}/{path}`. **A claim about
   configuration or code that has no citation does not go in the report** — state it
   as an open question instead.
7. **Assign exactly one category.** Prefer the operational set —
   `platform_failure`, `connectivity_failure`, `authentication_failure`,
   `resource_failure`, `timeout_failure`, `automation_failure`,
   `infrastructure_failure`. Fall back to `configuration`, `infrastructure`,
   `application_bug`, `secrets`, `resource`, or `dependency` only for a novel failure
   that none of the above fits. Pair it with a confidence of `high`, `medium`, or `low`.
8. **Recommend a change, not a direction.** Each recommendation gets a priority
   (high / medium / low), the file to edit, and the specific change.

## Output

- **Job Analysis** — job id, status, duration, controller, GUID.
- **Configuration Trace** — one table row per layer you actually fetched (agnosticv
  catalog item, agnosticv stage, component, agnosticd config, content repo), each with
  its `owner/repo:path` and the key values it contributed.
- **Failure Analysis** — failing `role : task`, host, and the real error, not the symptom.
- **Root Cause** — one-line summary, exactly one `category` from step 7, `confidence`.
- **Evidence** — a list, each item tagged `aap_job` / `agnosticv_config` /
  `agnosticd_code`, with every config and code item carrying its `owner/repo:path:line`.
- **Recommendations** — priority, file, change.
- The Sources footer with the GitHub links and the AAP2 job link.

## Relationship to other skills

- **`aap2-job-failure-triage`** covers the same subject and its description already
  claims a root cause and a recommended fix, so the two genuinely overlap on trigger.
  The discriminator is the **standard of evidence**, not the ambition. Triage reasons
  from the job output and Splunk and may *name* a likely config; this skill may not
  assert a configuration or code claim it has not fetched and cited as
  `owner/repo:path:line`, and it must land on exactly one category from the taxonomy
  with a stated confidence. That costs extra rounds, and it is worth them only when the
  error is a symptom, the fix is not obvious from the message, or the failure repeats —
  otherwise use triage. Reconciling the two descriptions, or retiring one, is a separate
  one-file change rather than part of adding this skill.
- **Scheduled, fleet-wide RCA is not this skill's job.** The rhdp-rca-plugin
  `batch-rca-automation` OpenShift CronJob owns discovery of failed jobs, known-issue
  pre-filtering, batch fan-out, aggregation, and writing the results table. This skill
  is the **interactive, single-job** path and writes nothing back. If the ask is
  "analyse all of yesterday's failures", point at the batch pipeline.
- Method borrowed from that plugin's root-cause-analysis skill — the mandatory
  `owner/repo:path:line` citation and the fixed category taxonomy — but none of its
  runtime: no scripts, no SSH or jumpbox, no Splunk client, no artifact directory.
- For provision or workshop state around the job (AnarchySubject, ResourceClaim,
  MultiWorkshop hierarchy) hand off to the Babylon path; for who owned the sandbox and
  when, use `provision-lookup`.
