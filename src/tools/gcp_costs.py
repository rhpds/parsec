"""Tool: query_gcp_costs — query GCP BigQuery billing data."""

import logging
from datetime import datetime

from src.config import get_config
from src.connections.gcp import get_bq_client

logger = logging.getLogger(__name__)


async def query_gcp_costs(
    start_date: str,
    end_date: str,
    group_by: str = "SERVICE",
    filter_services: list[str] | None = None,
    filter_projects: list[str] | None = None,
) -> dict:
    """Query GCP BigQuery billing export for cost data.

    Args:
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        group_by: One of SERVICE, PROJECT.
        filter_services: Optional list of service descriptions to filter by.
        filter_projects: Optional list of project IDs to filter by.

    Returns:
        Dict with cost breakdowns.
    """
    bq_client = get_bq_client()
    if bq_client is None:
        return {"error": "GCP BigQuery not configured"}

    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "Dates must be YYYY-MM-DD format"}

    cfg = get_config()
    gcp_cfg = cfg.gcp
    project_id = gcp_cfg.project_id
    dataset = gcp_cfg.billing_dataset
    billing_account_id = gcp_cfg.billing_account_id
    table_name = f"gcp_billing_export_v1_{billing_account_id.replace('-', '_')}"

    # Use America/Los_Angeles to match GCP Console date attribution
    tz = "America/Los_Angeles"

    select_columns = [
        f"FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, \"{tz}\")) as usage_date",
        "SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) as total_cost",
        "currency",
        "service.description as service_name",
        "project.id as project_id",
    ]

    group_columns = ["usage_date", "currency", "service.description", "project.id"]

    where_conditions = [
        f"DATE(usage_start_time, \"{tz}\") >= '{start_date}'",
        f"DATE(usage_start_time, \"{tz}\") <= '{end_date}'",
    ]

    if filter_services:
        services_list = "', '".join(filter_services)
        where_conditions.append(f"service.description IN ('{services_list}')")

    if filter_projects:
        projects_list = "', '".join(filter_projects)
        where_conditions.append(f"project.id IN ('{projects_list}')")

    query = f"""
        SELECT {', '.join(select_columns)}
        FROM `{project_id}.{dataset}.{table_name}`
        WHERE {' AND '.join(where_conditions)}
        GROUP BY {', '.join(group_columns)}
        ORDER BY usage_date
        LIMIT 5000
    """

    try:
        query_job = bq_client.query(query)
        results = query_job.result()

        rows = []
        total_cost = 0.0
        cost_by_group: dict[str, float] = {}

        for row in results:
            cost = float(row.total_cost) if row.total_cost else 0.0
            total_cost += cost

            if group_by.upper() == "PROJECT":
                key = row.project_id or "unknown"
            else:
                key = row.service_name or "unknown"

            cost_by_group[key] = cost_by_group.get(key, 0.0) + cost

            rows.append(
                {
                    "date": row.usage_date,
                    "service": row.service_name,
                    "project": row.project_id,
                    "cost": round(cost, 4),
                    "currency": row.currency,
                }
            )

        # Sort breakdown by cost descending
        breakdown = [
            {"name": k, "cost": round(v, 2)}
            for k, v in sorted(cost_by_group.items(), key=lambda x: -x[1])
        ]

        return {
            "period": {"start": start_date, "end": end_date},
            "group_by": group_by.upper(),
            "breakdown": breakdown,
            "daily_rows": len(rows),
            "total_cost": round(total_cost, 2),
        }

    except Exception as e:
        logger.exception("GCP BigQuery query failed")
        return {"error": f"GCP BigQuery query failed: {e}"}
