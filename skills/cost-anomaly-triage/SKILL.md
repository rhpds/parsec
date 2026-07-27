---
name: cost-anomaly-triage
description: >
  Detect and characterise a statistical outlier in RHDP cloud spend — establish a
  baseline from comparable prior periods, quantify the deviation in absolute dollars
  and percent, attribute it to an account / service / resource / catalog item, then
  decide whether it is expected seasonality, a legitimate new workload, or an
  incident. Use when the open question is whether a number is normal at all — "is $X for
  this account / catalog item / day expected?", or a cost-monitor threshold fired and needs
  a real-or-not verdict before anyone investigates. Not for "why did the bill go up", which
  already assumes the answer.
license: MIT
allowed-tools:
  - mcp__parsec__query_cost_monitor
  - mcp__parsec__query_aws_costs
  - mcp__parsec__query_azure_costs
  - mcp__parsec__query_gcp_costs
  - mcp__parsec__query_azure_pools
  - mcp__parsec__query_aws_pricing
  - mcp__parsec__query_aws_capacity_manager
  - mcp__parsec__query_aws_account_db
  - mcp__parsec__query_provisions_db
  - mcp__parsec__db_describe_table
  - mcp__parsec__db_read_knowledge
  - mcp__parsec__render_chart
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

# Cost Anomaly Triage

Decide whether a cost number is actually anomalous, and if it is, say what it is.

## When to use

- A cost figure looks like an outlier and someone wants to know if it is real.
- A budget threshold or cost-monitor alert tripped and needs a verdict.
- Someone asks "is $X for this account / catalog item / day normal?"

## Tools

Under the Agent SDK runtime Parsec's own tools are served by the in-process `parsec`
MCP bridge, so every name below carries the `mcp__parsec__` prefix. The Reporting
MCP's discovered tools keep their `db_` prefix and are bridged the same way
(`mcp__parsec__db_describe_table`). Use these **EXACT** names — an unknown tool name
fails outright. `generate_report` is deliberately not in this skill's tool set; keep
the verdict in chat.

## Procedure

1. **Fix the window and the unit.** State the period under test, the comparison
   periods, and what is being measured (org total, one provider, one account, one
   instance type, one catalog item). An "anomaly" with no stated unit is not gradeable.
2. **Build the baseline.** `query_cost_monitor(endpoint="summary")` over at least
   three comparable prior periods — same length, same weekday alignment, same
   `providers` filter. Record each total, the median, and the spread. Confirm the
   data is synced first with `query_cost_monitor(endpoint="providers")`.
3. **Quantify the deviation.** Report absolute $ and % against the baseline median,
   and say how it compares to the normal spread. **Cost Explorer lags ~24 hours** —
   for activity from today do not call `query_aws_costs`; estimate from
   `query_aws_pricing` and say the estimate is an estimate. Azure and GCP dates are
   inclusive and may also be delayed.
4. **Attribute it, narrowing one dimension at a time.** AWS:
   `query_cost_monitor(endpoint="breakdown", group_by="LINKED_ACCOUNT")`, then
   `group_by="INSTANCE_TYPE"`, then `endpoint="drilldown"` with
   `drilldown_type="account_services"` on the top account. Azure and GCP are **not**
   covered by breakdown/drilldown — use `query_azure_costs` / `query_gcp_costs`
   directly, and `query_azure_pools` to resolve a subscription to its pool. Name the
   single largest contributor before naming the next two.
5. **Bind it to an owner and a provision.** `query_aws_account_db` resolves sandbox
   name ↔ account ID; then the provision DB for the owning user and window. Accounts
   are **pooled and reused, never shared** — match the cost date against the
   `provisioned_at` → `retired_at` window. Cost falling outside every provision
   window is residual spend from incomplete cleanup, not the current holder's.
6. **Classify the anomaly.** Choose one and give the discriminator you used:
   - **Seasonality** — the same account / service / catalog item moved the same way
     in prior equivalent periods, and cost moved with provision count.
   - **Legitimate new workload** — a new catalog item, reservation type, or region
     appears; cost per provision is flat; the owner is internal.
   - **Incident** — cost per provision jumps, GPU or `*.metal` families appear, the
     owner is external, resources run outside any provision window, or ODCR unused
     cost climbs (`query_aws_capacity_manager(metric="unused_cost")`).
7. **Chart only if it helps.** One `render_chart` line series of the baseline periods
   plus the period under test. Skip it for a single-number answer.

## Output

A verdict, in this order: **the number and its baseline** (observed, expected,
delta $ and %), **the attribution chain** (provider → account → service → resource →
catalog item / owner), **the classification** (seasonality / new workload / incident)
with the evidence that decided it, and **what to do** — nothing, monitor, or contain.
Close with the Sources footer. Add a `[confidence: medium|low | reason]` marker when
a provider was unsynced or the baseline had fewer than three comparable periods.

## Relationship to other skills

- **`cost-spike-investigation`** is the broad narrative path: the user already accepts
  that the bill went up and wants the breakdown and remediation options. This skill
  runs *before* that one — it establishes whether the movement is outside normal range
  at all, and "expected" is a legitimate place to stop. Once an anomaly is confirmed
  and the user wants the full breakdown and remediation menu, hand off.
- **`abuse-account-detection`** owns the abuse verdict. Hand off as soon as step 6
  lands on *incident* with an abuse indicator — GPU or oversized instance families, a
  "Web-Created-VM" name, an external user with unusual volume. Do not issue a
  containment recommendation from here.
- **`provision-lookup`** owns the provision-DB query shapes step 5 needs.
