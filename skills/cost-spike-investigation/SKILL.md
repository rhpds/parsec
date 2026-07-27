---
name: cost-spike-investigation
description: Investigate an unexpected cloud cost spike across AWS, Azure, and GCP — pinpoint the driving account, service, and resource, then produce a cost breakdown with concrete remediation options. Use when someone reports a budget overrun, a sudden bill increase, or asks why costs went up.
license: MIT
allowed-tools:
  - mcp__reporting__*
metadata:
  author: parsec-team
  maturity: sample
parsec:
  version: "1.0.0"
  domain: cost
  requires_mcp:
    - reporting
  cost_estimate_per_call_usd: 0.05
---

# Cost Spike Investigation

Investigate a sudden increase in cloud spend and explain it in business terms.

## When to use
- A budget alert fired, or a stakeholder asks "why did our bill jump?"
- Month-over-month or day-over-day cost rose unexpectedly
- A specific account, team, or catalog item looks more expensive than usual

## Procedure
1. **Scope the spike.** Establish the time window and the magnitude (absolute $
   and %). Pull the provision records for the window from the provision DB
   (Reporting MCP) to map spend back to accounts, users, and catalog items.
2. **Find the driver.** Break the cost down by provider and service:
   - AWS: Cost Explorer grouped by service / usage type / linked account.
   - Azure: billing export grouped by meter category.
   - GCP: BigQuery billing export grouped by SKU.
   Identify the single largest contributor first, then the top three.
3. **Trace to a resource.** For the top contributor, drill to the specific
   resource (instance type, region, account) and the owning user or team.
4. **Check for abuse.** Cross-reference the abuse indicators (GPU instance
   families, `*.metal` / oversized instances, "Web-Created-VM" names). If the
   spike looks like abuse, hand off to the `abuse-account-detection` skill.
5. **Report.** Produce the headline number, the breakdown, the root resource,
   and two to three remediation options (right-size, terminate, quota, budget
   alert).

## Output
A short written brief: what spiked, by how much, who or what drove it, and the
recommended action — with the supporting cost breakdown.
