---
name: provision-lookup
description: >
  Find RHDP provisions and sandboxes in the provision database by user, catalog item,
  time window, status, GUID, or sandbox name, and summarise what came back — counts,
  the active/retired split, top catalog items, top users, and the owning account.
  Use for any "who / what / how many / when" provision question, including
  "what did <user> provision", "how many <catalog item> last month", and
  "who had sandboxNNNN on <date>".
license: MIT
allowed-tools:
  - mcp__parsec__query_provisions_db
  - mcp__parsec__db_list_tables
  - mcp__parsec__db_describe_table
  - mcp__parsec__db_table_sample
  - mcp__parsec__db_read_knowledge
  - mcp__parsec__query_aws_account_db
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

# Provision Lookup

Answer provision-inventory questions from the provision DB in as few queries as
possible, without guessing column names.

## When to use

- "What did `<email>` provision?", "who owns / owned `sandboxNNNN`?"
- "How many `<catalog item>` provisions in `<window>`?", "how many are still active?"
- Any provision lookup another investigation needs as its first step.

## Tools

Under the Agent SDK runtime Parsec's own tools are served by the in-process `parsec`
MCP bridge, so every name below carries the `mcp__parsec__` prefix. The Reporting
MCP's discovered tools keep their `db_` prefix and are bridged the same way
(`mcp__parsec__db_describe_table`). Use these **EXACT** names — an unknown tool name
fails outright.

## Procedure

1. **Pin the identifier before writing SQL.** Email, GUID, sandbox name, catalog item,
   status, or a date range — each takes a different filter. `sandboxNNNN` is AWS,
   `pool-XX-NNN` is Azure, `sandbox-XXXXX-zt-*` is OpenShift CNV. For an AWS sandbox
   name or account ID, `query_aws_account_db` is faster and authoritative for the
   current pool state; use the DB for history.
2. **Describe before you query.** Call `db_describe_table` for every table you have not
   already described in this conversation, even ones you think you know — column
   guesses are the largest source of wasted rounds. Call `db_read_knowledge` first for
   chargeback, sales, or capacity questions.
3. **Use the effective-catalog-item pattern.** A provision may be a component of a
   larger request, so the name on `provisions.catalog_id` is not always the name a
   human means. Select:

   ```sql
   COALESCE(ci_root.name, ci_component.name) AS catalog_item
   ```

   where `ci_root` is the catalog item reached through `provision_request` (the root
   request) and `ci_component` is the one joined on `provisions.catalog_id = ci.id`.
   Confirm the join columns with `db_describe_table` — do not assume them.
4. **Use the external-user filter verbatim** when the question is about external
   users, and join `users` to get an email at all (`provisions` has no `email` column;
   join via `user_id`):

   ```sql
   WHERE u.email NOT LIKE '%@redhat.com'
     AND u.email NOT LIKE '%@opentlc.com'
     AND u.email NOT LIKE '%@demo.redhat.com'
   ```

   Inverting it (all three `LIKE`, `OR`-joined) is the internal-user filter.
5. **Select only what you need.** `validate_sql` blocks non-SELECT and multi-statement
   SQL — it does **not** scope which tables you may read. Name the columns you will
   actually show, filter to the smallest window that answers the question, and add
   your own `LIMIT`. Results are capped at 500 rows; a `truncated` result means the
   filter was too loose, not that you should retry the same SQL.
6. **Aggregate rather than list** when the answer is a count — one `GROUP BY` query
   beats paging rows and counting them in prose.
7. **Handle the empty result honestly.** Zero rows is an answer; do not retry with
   different column guesses. A GUID or name that returns nothing may be a Workshop or
   MultiWorkshop that exists only as a Babylon K8s resource — say so and hand off.

## Common column pitfalls

- `provisions.catalog_id` — not `catalog_item_id`, not `catalog_item_name`.
- `provisions` has both `updated_at` and `modified_at` — use `modified_at`.
- The requesting user's name is in `ordered_by`; the email comes from `users`.
- `lifecycle_log` joins via `provision_uuid` (the provision `uuid`, not `babylon_guid`).
- `provision_cost` is partitioned — always filter `month_ts`.
- Alias every table when joining; shared column names produce ambiguous-column errors.

## Output

A Markdown table of the matching provisions (or the aggregate), preceded by one line
stating the filters actually applied — identifier, window, status, internal/external —
and the row count. Call out truncation explicitly. For counts over time, one
`render_chart` bar or line series. Close with the Sources footer.

## Relationship to other skills

- This skill is the **lookup primitive**, not an investigation: it answers *who, what,
  how many, when*. The moment the question becomes *why*, hand off.
- Feeds `cost-anomaly-triage` and `cost-spike-investigation` the account, owner, and
  provision window they need to attribute spend — those skills own the cost tools,
  this one does not touch them. Feeds `abuse-account-detection` the external-user and
  high-volume candidate list; that skill owns the scoring and the containment call.
- For a failed provision, stop here and hand off to `aap2-job-failure-triage` (fast
  answer) or `aap2-job-failure-rca` (cited root cause).
