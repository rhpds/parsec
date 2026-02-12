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
            "Query Azure billing CSVs for cost data in specified subscriptions. "
            "Use this after looking up subscription names (sandbox_name) from the provision DB. "
            "Subscription names look like 'pool-01-374'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subscription_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Azure subscription names (e.g. pool-01-374).",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format.",
                },
            },
            "required": ["subscription_names", "start_date", "end_date"],
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
