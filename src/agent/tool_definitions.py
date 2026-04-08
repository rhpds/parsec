"""Claude API tool schemas for Parsec.

Tool definitions are organized in two ways:
1. TOOLS — static tool schemas (non-MCP tools)
2. Per-agent getter functions — get_cost_tools(), get_security_tools(), etc.
   These merge static tools with dynamically discovered Reporting MCP tools
   (db_list_tables, db_describe_table, db_read_knowledge, etc.).
"""

import logging

logger = logging.getLogger(__name__)

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
            "Only SELECT queries are allowed. Results are limited to 500 rows. "
            "If unsure about column names, call db_describe_table first. "
            "For complex business logic, call db_read_knowledge first."
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
                        "get_workshop",
                        "list_multiworkshops",
                        "get_multiworkshop",
                        "list_anarchy_actions",
                        "get_babylon_pod_logs",
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
                        "get_workshop: Deep traversal of a specific Workshop — fetches the "
                        "Workshop, its ResourceClaims, and ALL AnarchySubject components with "
                        "tower job references. Requires name. Namespace optional (searches "
                        "cluster-wide if omitted). Omit cluster to auto-search all clusters. "
                        "list_multiworkshops: List MultiWorkshops in a namespace (multi-asset "
                        "events with multiple workshop assets, seat counts, dates). "
                        "Requires namespace. Always check this alongside list_workshops. "
                        "get_multiworkshop: Deep traversal of a specific MultiWorkshop — "
                        "fetches the MultiWorkshop, all child Workshops, their ResourceClaims, "
                        "and ALL AnarchySubject components with tower job references. Returns "
                        "full hierarchy showing which components failed and their AAP2 job IDs. "
                        "Requires name. Namespace is optional — if omitted, searches cluster-wide. "
                        "Omit cluster to auto-search all clusters. "
                        "list_anarchy_actions: List AnarchyActions (provision/start/stop/"
                        "destroy lifecycle events). Filter by guid or search. "
                        "get_babylon_pod_logs: Get pod logs from Babylon management clusters "
                        "(e.g. poolboy, babylon-anarchy-*). Requires namespace. Use name to "
                        "filter pods, search or guid to grep log content."
                    ),
                },
                "cluster": {
                    "type": "string",
                    "description": (
                        "Babylon cluster name to query. If empty, resolved from "
                        "sandbox_comment. For list_anarchy_subjects, "
                        "list_anarchy_actions (with guid), and get_multiworkshop, "
                        "omit cluster to automatically search ALL configured "
                        "clusters until found. Use query_aws_account_db to get "
                        "the comment field first when cluster is unknown."
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
                        "Filter deployments or AnarchySubjects by provision GUID (e.g. 'qglkb')."
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
            "IMPORTANT: If you don't know the exact path, use search_github_repo "
            "first to find it — do NOT list directories one by one. "
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
        "name": "lookup_catalog_item",
        "description": (
            "Look up a catalog item across ALL agnosticv repos instantly using a "
            "cached index. Searches rhpds/agnosticv, partner-agnosticv, "
            "zt-ansiblebu-agnosticv, and zt-rhelbu-agnosticv. Returns the exact "
            "repo, account directory, path, list of files (common.yaml, prod.yaml, etc), "
            "and default_branch (the repo's default branch — use as ref for "
            "fetch_github_file and for constructing GitHub links). "
            "ALWAYS use this BEFORE fetch_github_file when looking for catalog items — "
            "it's instant (no API calls). If it returns found=false with no similar items, "
            "the catalog item does NOT exist — do NOT fall back to listing directories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": (
                        "Catalog item name to search for (case-insensitive, underscores "
                        "and hyphens are equivalent). Example: 'ocp4-cluster', "
                        "'ANS_BU_WKSP_RHEL_90', 'rosa-security-lab'."
                    ),
                },
            },
            "required": ["search"],
        },
    },
    {
        "name": "search_github_repo",
        "description": (
            "Search a GitHub repo's entire file tree for paths matching a substring. "
            "Returns matching file and directory paths in a single API call. "
            "Use for searching agnosticd repos or non-agnosticv repos. "
            "For agnosticv catalog item lookups, use lookup_catalog_item instead "
            "(it's instant and searches all agnosticv repos). "
            "IMPORTANT: This searches the COMPLETE repo tree. If it returns "
            "zero matches, the item does NOT exist — do NOT fall back to "
            "listing directories manually."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Repository owner or organization (e.g. 'rhpds').",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository name (e.g. 'agnosticd-v2').",
                },
                "search": {
                    "type": "string",
                    "description": (
                        "Substring to match against file/directory paths (case-insensitive). "
                        "Example: 'ocp4-cluster-destroy' to find all paths containing that string."
                    ),
                },
                "ref": {
                    "type": "string",
                    "description": "Optional git ref (branch, tag, SHA). Defaults to HEAD.",
                },
            },
            "required": ["owner", "repo", "search"],
        },
    },
    {
        "name": "search_agnosticv_prs",
        "description": (
            "Search open PRs across all agnosticv repos for a catalog item or keyword. "
            "Use this when lookup_catalog_item returns 'not found' but the catalog item "
            "is referenced in a running job — it may exist only on an unmerged PR branch. "
            "Searches PR titles and changed file paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": (
                        "Keyword to search for in PR titles and changed file paths. "
                        "Use the catalog item name (e.g. 'lb1912-infoscale')."
                    ),
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "PR state filter. Default: 'open'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max PRs to return (default 10).",
                },
            },
            "required": ["search"],
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
        "name": "query_splunk",
        "description": (
            "Search Splunk logs for Babylon Kubernetes pod logs and AAP2 controller logs. "
            "Use search_by_guid to find all logs for a provision GUID (appears in namespace names). "
            "Use search_aap2_logs to search AAP2 controller server logs. "
            "Use search_namespace for exact namespace searches. "
            "Use search_raw for custom SPL queries. "
            "Available indexes: rh_pds-001_ocp_app (OCP app logs), rh_pds-001_ocp_infra (OCP infra logs), rh_pds-001_aap (AAP2 logs)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search_by_guid",
                        "search_namespace",
                        "search_aap2_logs",
                        "search_raw",
                    ],
                    "description": (
                        "search_by_guid: Find OCP pod logs by GUID (in namespace name). "
                        "search_namespace: Find OCP pod logs by exact namespace. "
                        "search_aap2_logs: Search AAP2 controller logs. "
                        "search_raw: Run a custom SPL query."
                    ),
                },
                "guid": {
                    "type": "string",
                    "description": "Provision GUID to search for (used in search_by_guid, search_aap2_logs).",
                },
                "namespace": {
                    "type": "string",
                    "description": "Exact Kubernetes namespace name (used in search_namespace).",
                },
                "cluster_name": {
                    "type": "string",
                    "description": (
                        "OCP cluster domain to filter by (e.g. 'ocpv08.dal10.infra.demo.redhat.com'). "
                        "Optional filter for OCP log searches."
                    ),
                },
                "controller": {
                    "type": "string",
                    "description": (
                        "AAP2 controller hostname (e.g. 'aap2-prod-us-west-2-01.aap.infra.demo.redhat.com'). "
                        "Required for search_aap2_logs."
                    ),
                },
                "search_terms": {
                    "type": "string",
                    "description": "Additional text to search for in log messages.",
                },
                "earliest": {
                    "type": "string",
                    "description": "Earliest time for search (Splunk time format). Default: '-24h'. Examples: '-7d', '-1h', '2026-03-20T00:00:00'.",
                },
                "latest": {
                    "type": "string",
                    "description": "Latest time for search. Default: 'now'.",
                },
                "errors_only": {
                    "type": "boolean",
                    "description": "If true, only return error/warning/fatal level logs. Default: false.",
                },
                "raw_query": {
                    "type": "string",
                    "description": "Raw SPL query for search_raw action. Must start with 'search' or '|'. Read-only queries only.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return. Default: 200, max: 500.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "query_ocpv_cluster",
        "description": (
            "Query an OCPV (OpenShift Virtualization) cluster for infrastructure "
            "state. Used to inspect PVCs, PVs, VMs, pods, nodes, and storage "
            "classes on the CNV clusters where lab VMs run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "find_namespace",
                        "list_pvcs",
                        "list_pvs",
                        "list_storage_classes",
                        "list_vms",
                        "get_node_resources",
                        "get_ocpv_pod_logs",
                        "list_pods",
                        "nodes_top",
                        "pods_top",
                        "list_machines",
                    ],
                    "description": (
                        "Action to perform. "
                        "find_namespace: Search OCPV clusters for a namespace (auto-discovers cluster). "
                        "list_pvcs: PVCs in a namespace with status, storageClass, volumeMode. "
                        "list_pvs: Cluster-wide PV summary grouped by node and storageClass. "
                        "list_storage_classes: Available storage classes on the cluster. "
                        "list_vms: VirtualMachines and VMIs in a namespace with status and conditions. "
                        "get_node_resources: Node CPU, memory, and storage capacity. "
                        "get_ocpv_pod_logs: Pod logs from OCPV clusters with optional name filter and grep. "
                        "list_pods: Pods in a namespace with status and restart count. "
                        "nodes_top: Current CPU and memory utilization per node (from metrics API). "
                        "pods_top: Current CPU and memory usage per pod in a namespace. "
                        "list_machines: MachineSets and Machines from machine.openshift.io API."
                    ),
                },
                "cluster": {
                    "type": "string",
                    "description": (
                        "OCPV cluster short name (e.g., 'ocpv08'). If omitted, "
                        "resolved from sandbox_comment or by searching all clusters."
                    ),
                },
                "namespace": {
                    "type": "string",
                    "description": (
                        "Kubernetes namespace. Required for: list_pvcs, list_vms, "
                        "get_ocpv_pod_logs, list_pods, pods_top. Format: sandbox-{guid}-{catalog-item}."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Filter results by name substring.",
                },
                "search": {
                    "type": "string",
                    "description": "Grep filter for pod log content.",
                },
                "sandbox_comment": {
                    "type": "string",
                    "description": (
                        "Sandbox DynamoDB comment field for auto-resolving the OCPV cluster. "
                        "Pass the raw comment from query_aws_account_db."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default 50).",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "query_icinga",
        "description": (
            "Query the Icinga2 monitoring system for host and service status, "
            "current problems, downtimes, and comments. Can also acknowledge "
            "problems, schedule downtimes, and reschedule checks. Use this to "
            "investigate infrastructure monitoring alerts, check host/service "
            "health, and correlate monitoring state with RHDP provisioning issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "get_hosts",
                        "get_services",
                        "get_problems",
                        "get_downtimes",
                        "get_comments",
                        "acknowledge_problem",
                        "schedule_downtime",
                        "reschedule_check",
                        "add_comment",
                        "remove_comment",
                        "remove_downtime",
                        "remove_acknowledgement",
                        "send_custom_notification",
                    ],
                    "description": (
                        "Action to perform. "
                        "get_hosts: Search/filter Icinga hosts by name or filter expression. "
                        "get_services: Search/filter Icinga services, optionally by host. "
                        "get_problems: Get all hosts and services in non-OK state. "
                        "get_downtimes: Get active downtimes, optionally filtered by host/service. "
                        "get_comments: Get comments on hosts/services. "
                        "acknowledge_problem: Acknowledge a host or service problem. "
                        "schedule_downtime: Schedule a maintenance downtime window. "
                        "reschedule_check: Force an immediate recheck. "
                        "add_comment: Add a comment to a host or service. "
                        "remove_comment: Remove a specific comment by name. "
                        "remove_downtime: Remove all downtimes from a host or service. "
                        "remove_acknowledgement: Remove acknowledgement from a host or service. "
                        "send_custom_notification: Send a custom notification."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": (
                        "Simple text search for get_hosts/get_services "
                        "(e.g. search='web' to find web servers)."
                    ),
                },
                "host": {
                    "type": "string",
                    "description": (
                        "Host name filter for get_services, get_downtimes, get_comments "
                        "(fuzzy match)."
                    ),
                },
                "service": {
                    "type": "string",
                    "description": "Service name filter for get_downtimes, get_comments.",
                },
                "filter_expr": {
                    "type": "string",
                    "description": (
                        "Advanced Icinga filter expression for get_hosts/get_services "
                        "(e.g. 'host.state==1' for DOWN hosts)."
                    ),
                },
                "detailed": {
                    "type": "boolean",
                    "description": (
                        "If true, return full output including groups for "
                        "get_hosts/get_services. Default: false."
                    ),
                },
                "object_type": {
                    "type": "string",
                    "enum": ["Host", "Service"],
                    "description": (
                        "Required for write actions (acknowledge_problem, schedule_downtime, "
                        "reschedule_check, add_comment, remove_downtime, remove_acknowledgement, "
                        "send_custom_notification). Whether the target is a Host or Service."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Object name for write actions. For hosts: the host name. "
                        "For services: 'hostname!servicename' format."
                    ),
                },
                "author": {
                    "type": "string",
                    "description": "Author for write actions. Default: 'parsec'.",
                },
                "comment": {
                    "type": "string",
                    "description": "Comment text for acknowledge, downtime, add_comment, notification.",
                },
                "comment_name": {
                    "type": "string",
                    "description": (
                        "Full comment name for remove_comment (get this from get_comments results)."
                    ),
                },
                "start_time": {
                    "type": "number",
                    "description": "Unix timestamp for downtime start (schedule_downtime).",
                },
                "end_time": {
                    "type": "number",
                    "description": "Unix timestamp for downtime end (schedule_downtime).",
                },
            },
            "required": ["action"],
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
                        "For get_job_events: only return events that made changes. Default: false."
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
                        "For find_jobs: ISO timestamp or YYYY-MM-DD. Only jobs created after this."
                    ),
                },
                "created_before": {
                    "type": "string",
                    "description": (
                        "For find_jobs: ISO timestamp or YYYY-MM-DD. Only jobs created before this."
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


# ---------------------------------------------------------------------------
# Per-agent tool groupings for the sub-agent architecture
# ---------------------------------------------------------------------------


def _is_splunk_configured() -> bool:
    """Check if Splunk is configured (has host + auth credentials)."""
    try:
        from src.config import get_config

        cfg = get_config()
        splunk_cfg = cfg.get("splunk", {})
        host = splunk_cfg.get("host", "")
        token = splunk_cfg.get("token", "")
        username = splunk_cfg.get("username", "")
        password = splunk_cfg.get("password", "")  # noqa: S105
        session_cookie = splunk_cfg.get("session_cookie", "")
        return bool(host and (token or (username and password) or session_cookie))
    except Exception:
        return False


def _is_icinga_configured() -> bool:
    """Check if Icinga MCP is configured (has mcp_url)."""
    try:
        from src.config import get_config

        cfg = get_config()
        return bool(cfg.get("icinga", {}).get("mcp_url", ""))
    except Exception:
        return False


# Tools that require specific backends to be configured
_CONDITIONAL_TOOLS: dict[str, bool] = {
    "query_splunk": _is_splunk_configured(),
    "query_icinga": _is_icinga_configured(),
}


def _get_reporting_mcp_tools() -> list[dict]:
    """Return dynamically discovered Reporting MCP tool schemas.

    These are cached at startup by reporting_mcp.fetch_server_instructions().
    Returns an empty list if the MCP is not configured or discovery failed.
    """
    try:
        from src.connections.reporting_mcp import get_mcp_tools

        return get_mcp_tools()
    except Exception:
        return []


def _tools_by_name(*names: str, include_mcp: bool = False) -> list[dict]:
    """Return tool definitions from TOOLS matching the given names.

    Skips tools that require unconfigured backends (e.g. query_splunk
    when Splunk is not configured) so the agent never sees them.

    When include_mcp=True, appends all dynamically discovered Reporting
    MCP tools (db_list_tables, db_describe_table, db_read_knowledge, etc.).
    """
    by_name = {t["name"]: t for t in TOOLS}
    result = []
    for n in names:
        if n in _CONDITIONAL_TOOLS and not _CONDITIONAL_TOOLS[n]:
            continue
        if n in by_name:
            result.append(by_name[n])
        else:
            logger.warning("Unknown tool name in agent grouping: %s", n)
    if include_mcp:
        result.extend(_get_reporting_mcp_tools())
    return result


def get_cost_tools() -> list[dict]:
    """Cost agent tools (called at request time for dynamic MCP tools)."""
    return _tools_by_name(
        "query_aws_costs",
        "query_azure_costs",
        "query_gcp_costs",
        "query_aws_pricing",
        "query_cost_monitor",
        "query_aws_capacity_manager",
        "query_provisions_db",
        "query_aws_account_db",
        "render_chart",
        "generate_report",
        include_mcp=True,
    )


def get_aap2_tools() -> list[dict]:
    """AAP2 agent tools (called at request time for dynamic MCP tools)."""
    return _tools_by_name(
        "query_aap2",
        "query_splunk",
        "fetch_github_file",
        "lookup_catalog_item",
        "search_github_repo",
        "search_agnosticv_prs",
        "query_babylon_catalog",
        "query_provisions_db",
        "query_aws_account_db",
        "render_chart",
        "generate_report",
        include_mcp=True,
    )


def get_babylon_tools() -> list[dict]:
    """Babylon agent tools (called at request time for dynamic MCP tools)."""
    return _tools_by_name(
        "query_babylon_catalog",
        "query_splunk",
        "query_aap2",
        "fetch_github_file",
        "search_github_repo",
        "search_agnosticv_prs",
        "lookup_catalog_item",
        "query_provisions_db",
        "query_aws_account_db",
        "render_chart",
        "generate_report",
        include_mcp=True,
    )


def get_security_tools() -> list[dict]:
    """Security agent tools (called at request time for dynamic MCP tools)."""
    return _tools_by_name(
        "query_cloudtrail",
        "query_aws_account",
        "query_marketplace_agreements",
        "query_babylon_catalog",
        "query_provisions_db",
        "query_aws_account_db",
        "render_chart",
        "generate_report",
        include_mcp=True,
    )


def get_ocpv_tools() -> list[dict]:
    """OCPV agent tools (called at request time for dynamic MCP tools)."""
    return _tools_by_name(
        "query_ocpv_cluster",
        "query_babylon_catalog",
        "query_provisions_db",
        "query_aws_account_db",
        "render_chart",
        "generate_report",
        include_mcp=True,
    )


def get_icinga_tools() -> list[dict]:
    """Icinga agent tools (no MCP tools needed)."""
    return _tools_by_name(
        "query_icinga",
        "fetch_github_file",
        "search_github_repo",
        "render_chart",
        "generate_report",
    )


def get_orchestrator_direct_tools() -> list[dict]:
    """Orchestrator direct tools (called at request time for dynamic MCP tools)."""
    return _tools_by_name(
        "query_provisions_db",
        "query_aws_account_db",
        "render_chart",
        "generate_report",
        include_mcp=True,
    )


# ---------------------------------------------------------------------------
# Delegation tool schemas (used by the orchestrator to invoke sub-agents)
# ---------------------------------------------------------------------------

INVESTIGATE_COSTS_TOOL = {
    "name": "investigate_costs",
    "description": (
        "Delegate a cost investigation to the Cost Investigation agent. "
        "This agent can query AWS Cost Explorer, Azure billing, GCP BigQuery, "
        "EC2 pricing, the cost-monitor dashboard, and ODCR capacity metrics. "
        "Use this for questions about cloud spending, cost breakdowns, GPU "
        "abuse detection, pricing lookups, and capacity reservation waste."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "A clear, specific description of the cost investigation to perform. "
                    "Include account IDs, sandbox names, date ranges, and what aspect "
                    "of costs to investigate. The agent will use this to decide which "
                    "tools to call."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional context to pass to the agent, such as account_ids, "
                    "sandbox_names, or user info already looked up by the orchestrator."
                ),
            },
        },
        "required": ["task"],
    },
}

INVESTIGATE_AAP2_TOOL = {
    "name": "investigate_aap2_job",
    "description": (
        "Delegate an AAP2 job failure investigation to the AAP2 Investigation agent. "
        "This agent can query AAP2 controllers for job details, logs, and execution "
        "events, and trace failures through the agnosticv/agnosticd config hierarchy "
        "on GitHub. Use this when users ask about failed provisions, job logs, "
        "AAP2 errors, or need root cause analysis of provisioning failures."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "A clear description of the job failure investigation to perform. "
                    "Include any GUIDs, job IDs, catalog item names, or error messages. "
                    "The agent will query AAP2 for job details and trace the "
                    "failure through the agnosticv/agnosticd config hierarchy."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional context such as parsed job template fields, account "
                    "info, or provision data already looked up."
                ),
            },
        },
        "required": ["task"],
    },
}

INVESTIGATE_BABYLON_TOOL = {
    "name": "investigate_babylon",
    "description": (
        "Delegate a Babylon investigation to the Babylon Investigation agent. "
        "This agent can query Babylon clusters for catalog item definitions, "
        "active deployments (ResourceClaims), provision lifecycle state "
        "(AnarchySubjects), resource pools, and workshops. Use this when users "
        "ask what a catalog item deploys, check deployment state, inspect "
        "resource pools, or investigate workshop details."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "A clear description of the Babylon investigation to perform. "
                    "Include catalog item names, GUIDs, sandbox names, or namespace "
                    "info. The agent will query Babylon clusters for definitions, "
                    "deployments, and lifecycle state."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional context such as sandbox account data, provision info, "
                    "or cluster details already looked up."
                ),
            },
        },
        "required": ["task"],
    },
}

INVESTIGATE_SECURITY_TOOL = {
    "name": "investigate_security",
    "description": (
        "Delegate a security investigation to the Security Investigation agent. "
        "This agent can query CloudTrail Lake for org-wide API events, inspect "
        "AWS member accounts (EC2 instances, IAM users, marketplace agreements), "
        "and search the marketplace agreement inventory. Use this for questions "
        "about who did what on an account, IAM access keys, marketplace subscriptions, "
        "running instances, abuse indicators, or security concerns."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "A clear description of the security investigation to perform. "
                    "Include account IDs, sandbox names, time ranges, and what "
                    "security aspect to check. The agent will query CloudTrail, "
                    "inspect accounts, and check for abuse indicators."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional context such as account info, sandbox data, or "
                    "user details already looked up."
                ),
            },
        },
        "required": ["task"],
    },
}

INVESTIGATE_OCPV_TOOL = {
    "name": "investigate_ocpv",
    "description": (
        "Delegate an OCPV infrastructure investigation to the OCPV agent. "
        "This agent can inspect OpenShift Virtualization clusters: PVCs, PVs, "
        "VMs, pods, nodes, and storage classes. Use this when investigating "
        "CNV provision failures, storage issues (PVC pending, volume binding "
        "errors), VM scheduling problems, or node resource constraints on "
        "the OCPV clusters where lab VMs run."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "A clear description of the OCPV infrastructure investigation. "
                    "Include namespace names, GUIDs, or sandbox names. The agent "
                    "will resolve the cluster and inspect the infrastructure."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional context such as sandbox comment field for cluster "
                    "resolution, namespace, or Babylon data."
                ),
            },
        },
        "required": ["task"],
    },
}

INVESTIGATE_ICINGA_TOOL = {
    "name": "investigate_icinga",
    "description": (
        "Delegate a monitoring investigation to the Icinga Monitoring agent. "
        "This agent can query Icinga2 for host and service status, current "
        "problems, downtimes, and comments. It can also acknowledge problems, "
        "schedule downtimes, and force rechecks. Use this when users ask about "
        "infrastructure monitoring state, host/service health, monitoring alerts, "
        "or need to correlate Icinga problems with RHDP provisioning issues."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "A clear description of the monitoring investigation to perform. "
                    "Include host names, service names, or describe the monitoring "
                    "concern. The agent will query Icinga for current status and "
                    "active problems."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional context such as sandbox account data, provision info, "
                    "or host/service names already looked up."
                ),
            },
        },
        "required": ["task"],
    },
}

DELEGATION_TOOLS = [
    INVESTIGATE_COSTS_TOOL,
    INVESTIGATE_AAP2_TOOL,
    INVESTIGATE_BABYLON_TOOL,
    INVESTIGATE_SECURITY_TOOL,
    INVESTIGATE_OCPV_TOOL,
    INVESTIGATE_ICINGA_TOOL,
]


def get_orchestrator_tools() -> list[dict]:
    """Return the full orchestrator tool set (direct + delegation)."""
    return get_orchestrator_direct_tools() + DELEGATION_TOOLS
