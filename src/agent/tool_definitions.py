"""Claude API tool schemas for Parsec."""

# Verdict tool for alert investigations (not included in the default TOOLS list —
# only appended during alert investigation mode).
SUBMIT_ALERT_VERDICT_TOOL = {
    "name": "submit_alert_verdict",
    "description": (
        "Submit your final verdict on whether this alert should fire. "
        "Call this exactly once at the end of your investigation. "
        "If in doubt, set should_alert=true — it is better to alert on a "
        "false positive than to suppress a real threat."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "should_alert": {
                "type": "boolean",
                "description": (
                    "Whether the alert should be posted to Slack. "
                    "Set to false only when you are confident the activity is benign."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low", "benign"],
                "description": (
                    "Severity level. Use 'benign' only when suppressing. "
                    "critical = confirmed abuse or major unauthorized spend. "
                    "high = likely abuse or significant cost risk. "
                    "medium = suspicious but inconclusive. "
                    "low = minor anomaly worth noting."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "1-3 sentence summary of your findings for the Slack message. "
                    "Include the user, account, what happened, and why it matters "
                    "(or why it's benign). Be specific and concise."
                ),
            },
        },
        "required": ["should_alert", "severity", "summary"],
    },
}

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
            "Use this after looking up account IDs from query_aws_account_db or the provision DB. "
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
            "Always include an eventTime filter to limit bytes scanned. "
            "IMPORTANT: If the user does not specify a timeframe, default to the past "
            "24 hours and inform them of this assumption. Avoid broad time ranges — "
            "large queries take a long time to complete."
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
        "name": "query_marketplace_agreements",
        "description": (
            "Query the marketplace agreement inventory for enriched data on AWS "
            "Marketplace subscriptions across all org accounts. Use this for questions "
            "about active marketplace agreements, auto-renew SaaS subscriptions, "
            "vendor products, or cost thresholds — without needing to scan CloudTrail "
            "or inspect individual accounts. Data is pre-enriched with account name, "
            "product title, vendor, classification, cost, and auto-renew status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "Filter by 12-digit AWS account ID.",
                },
                "account_name": {
                    "type": "string",
                    "description": ("Filter by account name (case-insensitive contains match)."),
                },
                "status": {
                    "type": "string",
                    "description": ("Filter by agreement status (e.g. ACTIVE, CLOSED)."),
                },
                "classification": {
                    "type": "string",
                    "description": (
                        "Filter by classification: 'SaaS (Auto-Renew)', "
                        "'SaaS (Auto-Renew Disabled)', 'Fixed/Upfront', 'Pay-As-You-Go'."
                    ),
                },
                "min_cost": {
                    "type": "number",
                    "description": "Minimum estimated cost (estimated_cost field) in USD.",
                },
                "product_name": {
                    "type": "string",
                    "description": ("Filter by product name (case-insensitive contains match)."),
                },
                "vendor_name": {
                    "type": "string",
                    "description": ("Filter by vendor name (case-insensitive contains match)."),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max agreements to return. Default: 100, max: 500.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "query_aws_account_db",
        "description": (
            "Query the sandbox account pool (DynamoDB) for AWS account metadata. "
            "Use this FIRST to resolve sandbox names to account IDs or vice versa, "
            "before querying the provision DB or cost tools. Returns current owner, "
            "availability, reservation type, DNS zone, envtype, and annotations. "
            "Credentials are stripped from results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Exact sandbox name for direct lookup (e.g. 'sandbox4440'). "
                        "Uses DynamoDB key lookup — fastest option."
                    ),
                },
                "account_id": {
                    "type": "string",
                    "description": "Filter by 12-digit AWS account ID.",
                },
                "available": {
                    "type": "boolean",
                    "description": "Filter by availability (true = idle, false = in use).",
                },
                "owner": {
                    "type": "string",
                    "description": "Filter by owner email (case-insensitive contains match).",
                },
                "zone": {
                    "type": "string",
                    "description": "Filter by DNS zone (case-insensitive contains match).",
                },
                "envtype": {
                    "type": "string",
                    "description": (
                        "Filter by environment type (case-insensitive contains match, "
                        "e.g. 'ocp4-cluster')."
                    ),
                },
                "reservation": {
                    "type": "string",
                    "description": (
                        "Filter by reservation type (case-insensitive contains match, "
                        "e.g. 'event', 'pgpu-event')."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max accounts to return. Default: 100, max: 500.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "query_babylon_catalog",
        "description": (
            "Query a Babylon cluster for catalog item definitions, active deployments, "
            "and provisioning state. Use this to find what cloud resources a catalog item "
            "SHOULD deploy (instance types, counts, cloud provider) and what IS currently "
            "deployed (ResourceClaims with sandbox account mappings). Requires a configured "
            "Babylon cluster connection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search_catalog",
                        "get_component",
                        "list_deployments",
                        "get_deployment",
                        "list_anarchy_subjects",
                        "list_resource_pools",
                        "list_workshops",
                        "list_multiworkshops",
                        "list_anarchy_actions",
                    ],
                    "description": (
                        "Action to perform. "
                        "search_catalog: Search CatalogItems by name/keyword across "
                        "babylon-catalog-* namespaces. "
                        "get_component: Get an AgnosticVComponent definition with extracted "
                        "expected instance types. "
                        "list_deployments: List active ResourceClaims (requires namespace). "
                        "get_deployment: Get a specific ResourceClaim with full details. "
                        "list_anarchy_subjects: List AnarchySubjects (active provisions) "
                        "across anarchy namespaces. "
                        "list_resource_pools: List ResourcePools from poolboy namespace "
                        "(pool sizing and pre-provisioned resources). "
                        "list_workshops: List Workshops in a namespace (attendee counts, "
                        "provision status). Requires namespace. "
                        "list_multiworkshops: List MultiWorkshops in a namespace (multi-asset "
                        "events with multiple workshop assets, seat counts, dates). "
                        "Requires namespace. Always check this alongside list_workshops. "
                        "list_anarchy_actions: List AnarchyActions (provision/start/stop/"
                        "destroy lifecycle events). Filter by guid or search."
                    ),
                },
                "cluster": {
                    "type": "string",
                    "description": (
                        "Babylon cluster name to query. If empty, resolved from "
                        "sandbox_comment. For list_anarchy_subjects and "
                        "list_anarchy_actions with a guid, omit cluster to "
                        "automatically search ALL configured clusters until the "
                        "GUID is found. Use query_aws_account_db to get the "
                        "comment field first when cluster is unknown."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Resource name. For get_component: AgnosticVComponent name "
                        "(e.g. 'clusterplatform.ocp4-aws.prod'). For get_deployment: "
                        "ResourceClaim name."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": (
                        "Search term for search/list actions (case-insensitive "
                        "contains match against name, display name, keywords)."
                    ),
                },
                "namespace": {
                    "type": "string",
                    "description": (
                        "Namespace for scoped queries. Required for list_deployments "
                        "and get_deployment (e.g. 'clusterplatform-prod')."
                    ),
                },
                "sandbox_comment": {
                    "type": "string",
                    "description": (
                        "Sandbox DynamoDB comment field value. Used to resolve which "
                        "Babylon cluster manages this sandbox. Get this from "
                        "query_aws_account_db."
                    ),
                },
                "env_type": {
                    "type": "string",
                    "description": "Filter catalog search by env_type.",
                },
                "account_id": {
                    "type": "string",
                    "description": "Filter deployments by sandbox AWS account ID.",
                },
                "guid": {
                    "type": "string",
                    "description": (
                        "Filter deployments or AnarchySubjects by provision GUID " "(e.g. 'qglkb')."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return. Default: 50.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "fetch_github_file",
        "description": (
            "Fetch a file or directory listing from any GitHub repository. This is "
            "the single tool for all GitHub file access — agnosticv configs, agnosticd "
            "source code, and any other repo content. Use this to: "
            "(1) Fetch agnosticv catalog item configs (common.yaml, prod.yaml) from "
            "rhpds/agnosticv or zt-*-agnosticv repos. "
            "(2) Fetch agnosticd Ansible roles (ansible/roles/{role}/tasks/) and "
            "env_type configs (ansible/configs/{env_type}/default_vars.yml) when "
            "tracing AAP2 job failures to source code. AgnosticD repos: "
            "agnosticd/agnosticd-v2 (v2, default ref: main) and "
            "redhat-cop/agnosticd (legacy, default ref: development). "
            "(3) List directories to discover folder names before fetching files. "
            "Supports fetching specific git refs (branches, tags, commit SHAs). "
            "Secrets in file content are automatically redacted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Repository owner or organization (e.g. 'rhpds', 'redhat-cop').",
                },
                "repo": {
                    "type": "string",
                    "description": (
                        "Repository name (e.g. 'agnosticv', 'partner-agnosticv', "
                        "'agnosticd-v2', 'agnosticd')."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Path to a file or directory within the repo. "
                        "Examples: 'sandboxes-gpte/OCP4_AWS/common.yaml', "
                        "'ansible/configs/ocp4-cluster/default_vars.yml'."
                    ),
                },
                "ref": {
                    "type": "string",
                    "description": (
                        "Optional git ref — branch name, tag, or commit SHA. "
                        "Use the job's Revision SHA or the scm_ref from agnosticv "
                        "to fetch the exact code version that ran. "
                        "If omitted, fetches from the repo's default branch."
                    ),
                },
            },
            "required": ["owner", "repo", "path"],
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
                    "enum": ["get_job", "get_job_log", "get_job_events", "find_jobs"],
                    "description": (
                        "Action to perform. "
                        "get_job: Get job metadata, status, duration, extra_vars, "
                        "and git context for a specific job ID. "
                        "get_job_log: Get job metadata AND the full trimmed execution "
                        "log (PLAY/TASK flow, failures, PLAY RECAP, timing). Best for "
                        "triage — gives complete context in one call. "
                        "get_job_events: Get structured execution events for a job. "
                        "Use failed_only=true to see only errors. "
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
                    "description": (
                        "For get_job_events: only return failed events. Default: false."
                    ),
                },
                "changed_only": {
                    "type": "boolean",
                    "description": (
                        "For get_job_events: only return events that made changes. "
                        "Default: false."
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
                        "For find_jobs: ISO timestamp or YYYY-MM-DD. "
                        "Only jobs created after this."
                    ),
                },
                "created_before": {
                    "type": "string",
                    "description": (
                        "For find_jobs: ISO timestamp or YYYY-MM-DD. "
                        "Only jobs created before this."
                    ),
                },
                "template_name": {
                    "type": "string",
                    "description": ("For find_jobs: filter by template name (contains match)."),
                },
                "max_results": {
                    "type": "integer",
                    "description": ("Maximum results to return. Default: 50, max: 200."),
                },
            },
            "required": ["action"],
        },
    },
]
