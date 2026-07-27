---
name: aap2-job-failure-triage
description: Triage a failed Ansible Automation Platform 2 (AAP2) controller job — find the failing task, extract the error from the job output and Splunk logs, and identify the root cause with a recommended fix. Use when an AAP2 job, workflow, or Babylon deployment fails and someone needs to know why.
license: MIT
allowed-tools:
  - mcp__reporting__*
metadata:
  author: parsec-team
  maturity: sample
parsec:
  version: "1.0.0"
  domain: aap2
  requires_mcp:
    - reporting
  cost_estimate_per_call_usd: 0.05
---

# AAP2 Job Failure Triage

Explain why an AAP2 controller job failed and how to fix it.

## When to use
- An AAP2 job or workflow job ended in "failed" or "error"
- A Babylon deployment backed by AAP2 didn't come up
- Someone pastes a job id or URL and asks "what went wrong?"

## Procedure
1. **Locate the job.** Resolve the job id on the right AAP2 controller and read
   its status, the failed task, and the module and return code.
2. **Get the error.** Pull the relevant stdout for the failed task. If the
   controller output is truncated, query Splunk for the job's pod logs.
3. **Classify the failure.** Common buckets: missing or non-existent Galaxy
   collection / git ref, credential or permission error, unreachable host,
   template or variable error, timeout.
4. **Find the root cause.** Tie the error to a concrete config — for example a
   non-existent git tag referenced by a stage's `prod.yaml`, a wrong tower
   credential, or an unreachable target host.
5. **Recommend a fix.** Give the smallest correct change plus the file(s) to
   edit.

## Output
A short brief: failing task, exact error, root cause, and the recommended fix
with the file(s) to change.
