"""Claude API tool schemas for Parsec."""

TOOLS = [
    {
        "name": "query_provisions_db",
        "description": (
            "Execute a read-only SQL query against the RHDP provision database. "
            "Use this to look up users, provisions, catalog items, and cloud account mappings. "
            "Only SELECT queries are allowed. Results are limited to 500 rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "A SELECT SQL query to execute against the provision DB. "
                        "Available tables: provisions, users, catalog_items, provision_request, catalog_resource. "
                        "See the system prompt for schema details and join patterns."
                    ),
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "query_aws_costs",
        "description": (
            "Query AWS Cost Explorer for cost data across specified AWS accounts. "
            "Use this after looking up account IDs from the provision DB. "
            "Supports grouping by SERVICE, INSTANCE_TYPE, or LINKED_ACCOUNT."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 12-digit AWS account IDs to query.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format.",
                },
                "group_by": {
                    "type": "string",
                    "enum": ["SERVICE", "INSTANCE_TYPE", "LINKED_ACCOUNT"],
                    "description": "Dimension to group costs by. Default: SERVICE.",
                },
            },
            "required": ["account_ids", "start_date", "end_date"],
        },
    },
    {
        "name": "query_azure_costs",
        "description": (
            "Query Azure billing data for cost analysis. "
            "Can query specific subscriptions or all subscriptions. "
            "When investigating specific users/provisions, look up subscription names "
            "(sandbox_name) from the provision DB first. For broad cost searches across "
            "all Azure data, omit subscription_names. Use meter_filter to narrow results "
            "by MeterCategory or MeterSubCategory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subscription_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of Azure subscription names (e.g. pool-01-374). "
                        "If omitted, queries all subscriptions."
                    ),
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format.",
                },
                "meter_filter": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive search string matched against MeterCategory "
                        "and MeterSubCategory (e.g. 'Page Blob', 'Virtual Machines', "
                        "'NC Series')."
                    ),
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "query_gcp_costs",
        "description": (
            "Query GCP BigQuery billing export for cost data. "
            "Use this to check GCP spending by service or project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format.",
                },
                "group_by": {
                    "type": "string",
                    "enum": ["SERVICE", "PROJECT"],
                    "description": "Dimension to group costs by. Default: SERVICE.",
                },
                "filter_services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of GCP service names to filter by.",
                },
                "filter_projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of GCP project IDs to filter by.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "query_aws_pricing",
        "description": (
            "Look up on-demand pricing for an AWS EC2 instance type. "
            "Returns hourly, daily, and monthly costs along with instance specs "
            "(vCPU, memory, GPU, storage, network)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_type": {
                    "type": "string",
                    "description": "EC2 instance type (e.g. g4dn.xlarge, m5.large, p3.2xlarge).",
                },
                "region": {
                    "type": "string",
                    "description": "AWS region code (e.g. us-east-1). Default: us-east-1.",
                },
                "os_type": {
                    "type": "string",
                    "enum": ["Linux", "Windows", "RHEL", "SUSE"],
                    "description": "Operating system. Default: Linux.",
                },
            },
            "required": ["instance_type"],
        },
    },
    {
        "name": "query_cost_monitor",
        "description": (
            "Query the cost-monitor dashboard API for aggregated, cached cost data. "
            "Faster than raw Cost Explorer queries and includes cross-provider summaries. "
            "Use this for broad cost overviews, account breakdowns, and drilldowns. "
            "Available endpoints: summary (overall costs), breakdown (top accounts or "
            "instance types), drilldown (details for a specific account), providers "
            "(sync status)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {
                    "type": "string",
                    "enum": ["summary", "breakdown", "drilldown", "providers"],
                    "description": "API endpoint to query.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format.",
                },
                "providers": {
                    "type": "string",
                    "description": "Comma-separated provider filter (e.g. 'aws', 'aws,azure').",
                },
                "group_by": {
                    "type": "string",
                    "enum": ["LINKED_ACCOUNT", "INSTANCE_TYPE"],
                    "description": "For breakdown: how to group results.",
                },
                "top_n": {
                    "type": "integer",
                    "description": "For breakdown: number of top results. Default: 25.",
                },
                "drilldown_type": {
                    "type": "string",
                    "enum": ["account_services", "instance_details"],
                    "description": "For drilldown: type of drill-down.",
                },
                "selected_key": {
                    "type": "string",
                    "description": "For drilldown: the account ID or instance type to drill into.",
                },
            },
            "required": ["endpoint", "start_date", "end_date"],
        },
    },
    {
        "name": "query_aws_capacity_manager",
        "description": (
            "Query AWS EC2 Capacity Manager for On-Demand Capacity Reservation (ODCR) "
            "metrics from the payer account. Use this to find unused or underutilized "
            "capacity reservations, estimate wasted spend, and list ODCR inventory. "
            "Runs against the payer account in us-east-1 with cross-account visibility."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["utilization", "unused_cost", "inventory"],
                    "description": (
                        "Metric preset to query. "
                        "'utilization' = avg utilization, total vs unused capacity, cost. "
                        "'unused_cost' = unused estimated cost breakdown. "
                        "'inventory' = list all ODCRs with details."
                    ),
                },
                "group_by": {
                    "type": "string",
                    "enum": [
                        "instance-type",
                        "instance-family",
                        "account-id",
                        "resource-region",
                        "availability-zone-id",
                        "reservation-id",
                        "reservation-state",
                        "tenancy",
                        "instance-platform",
                    ],
                    "description": "Dimension to group results by. Default varies by metric.",
                },
                "instance_type": {
                    "type": "string",
                    "description": "Filter to a specific EC2 instance type (e.g. g4dn.xlarge).",
                },
                "account_id": {
                    "type": "string",
                    "description": "Filter to a specific 12-digit AWS account ID.",
                },
                "reservation_state": {
                    "type": "string",
                    "enum": ["active", "expired", "cancelled", "pending", "failed"],
                    "description": "Filter by reservation state. Default: active.",
                },
                "hours": {
                    "type": "integer",
                    "description": "Hours of history to query. Default: 168 (7 days). Max: 2160 (90 days).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "query_cloudtrail",
        "description": (
            "Query CloudTrail Lake for org-wide AWS API events across all accounts in "
            "the organization. Use this to investigate marketplace subscriptions "
            "(AcceptAgreementRequest), service quota increases, IAM activity, "
            "RunInstances events, and other API calls. Write SQL using "
            "FROM cloudtrail_events (the tool substitutes the real event data store ID). "
            "Always include an eventTime filter to limit bytes scanned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "SQL query against CloudTrail Lake. Use FROM cloudtrail_events. "
                        "Key columns: eventTime, eventName, eventSource, awsRegion, "
                        "recipientAccountId, userIdentity.arn, userIdentity.accountId, "
                        "requestParameters, responseElements. "
                        "Always include WHERE eventTime > 'YYYY-MM-DD' to limit scan costs."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum rows to return. Default: 100, max: 500.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_aws_account",
        "description": (
            "Inspect an individual AWS member account using cross-account read-only "
            "access. Use this to check running instances, IAM users, recent CloudTrail "
            "events, or marketplace agreements on a specific account. The session is "
            "scoped to read-only actions via an inline session policy — no writes are "
            "possible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "12-digit AWS account ID to inspect.",
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "describe_instances",
                        "lookup_events",
                        "list_users",
                        "describe_marketplace",
                    ],
                    "description": (
                        "Action to perform. "
                        "describe_instances: list EC2 instances (with optional state/ID filters). "
                        "lookup_events: recent CloudTrail events (with optional event_name filter). "
                        "list_users: IAM users and their access keys. "
                        "describe_marketplace: marketplace agreements and terms."
                    ),
                },
                "region": {
                    "type": "string",
                    "description": "AWS region for EC2/CloudTrail queries. Default: us-east-1.",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Optional action-specific filters. "
                        "describe_instances: {state: 'running', instance_ids: [...]}. "
                        "lookup_events: {event_name: 'RunInstances'}. "
                        "list_users: no filters. "
                        "describe_marketplace: {agreement_ids: ['agmt-...']} to enrich "
                        "specific agreements found via query_cloudtrail. Without this, "
                        "attempts SearchAgreements discovery (may not work on all accounts)."
                    ),
                },
            },
            "required": ["account_id", "action"],
        },
    },
    {
        "name": "render_chart",
        "description": (
            "Render a chart in the chat UI. Use this to visualize cost data, "
            "trends, breakdowns, or comparisons. The chart is rendered client-side."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "pie", "doughnut"],
                    "description": "Type of chart to render.",
                },
                "title": {
                    "type": "string",
                    "description": "Chart title.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels for each data point (x-axis for bar/line, segments for pie).",
                },
                "datasets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Dataset label (legend entry).",
                            },
                            "data": {
                                "type": "array",
                                "items": {"type": "number"},
                                "description": "Data values corresponding to labels.",
                            },
                        },
                        "required": ["label", "data"],
                    },
                    "description": "One or more datasets to plot.",
                },
            },
            "required": ["chart_type", "title", "labels", "datasets"],
        },
    },
    {
        "name": "generate_report",
        "description": (
            "Generate a formatted investigation report (Markdown or AsciiDoc) from the findings "
            "gathered so far in this conversation. Call this when the user asks for a report, "
            "summary document, or export. You provide the report content and format."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Report title.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The full report content in the chosen format. Use proper headings, "
                        "tables, and sections. Include an executive summary, findings, and "
                        "any cost breakdowns."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "asciidoc"],
                    "description": "Output format. Default: markdown.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Suggested filename without extension (extension added automatically). "
                        "Default: investigation_report_YYYY-MM-DD."
                    ),
                },
            },
            "required": ["title", "content"],
        },
    },
]
