"""Tool: query_aws_costs — query AWS Cost Explorer for cost data."""

import logging
from datetime import datetime

from src.config import get_config
from src.connections.aws import get_ce_client

logger = logging.getLogger(__name__)


async def query_aws_costs(
    account_ids: list[str],
    start_date: str,
    end_date: str,
    group_by: str = "SERVICE",
) -> dict:
    """Query AWS Cost Explorer for costs across specified accounts.

    Args:
        account_ids: List of 12-digit AWS account IDs.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        group_by: One of SERVICE, INSTANCE_TYPE, LINKED_ACCOUNT.

    Returns:
        Dict with results_by_account and total_cost.
    """
    # Validate dates
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "Dates must be YYYY-MM-DD format"}

    if not account_ids:
        return {"error": "No account_ids provided"}

    # Validate account IDs
    valid_ids = [aid for aid in account_ids if len(aid) == 12 and aid.isdigit()]
    if not valid_ids:
        return {"error": "No valid 12-digit account IDs provided"}

    group_by_upper = group_by.upper()
    if group_by_upper not in ("SERVICE", "INSTANCE_TYPE", "LINKED_ACCOUNT"):
        return {"error": f"Invalid group_by: {group_by}. Must be SERVICE, INSTANCE_TYPE, or LINKED_ACCOUNT"}

    ce = get_ce_client()
    cfg = get_config()
    batch_size = cfg.aws.get("batch_size", 100)

    all_results = []

    # Batch accounts to avoid CE limits
    for i in range(0, len(valid_ids), batch_size):
        batch = valid_ids[i : i + batch_size]

        group_by_dims = [{"Type": "DIMENSION", "Key": group_by_upper}]
        if group_by_upper != "LINKED_ACCOUNT":
            group_by_dims.append({"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"})

        try:
            kwargs = {
                "TimePeriod": {"Start": start_date, "End": end_date},
                "Granularity": "DAILY",
                "Filter": {
                    "Dimensions": {
                        "Key": "LINKED_ACCOUNT",
                        "Values": batch,
                    }
                },
                "GroupBy": group_by_dims,
                "Metrics": ["UnblendedCost"],
            }

            results_by_time = []
            while True:
                response = ce.get_cost_and_usage(**kwargs)
                results_by_time.extend(response.get("ResultsByTime", []))

                token = response.get("NextPageToken")
                if not token:
                    break
                kwargs["NextPageToken"] = token

            all_results.extend(results_by_time)

        except Exception as e:
            logger.exception("AWS CE query failed for batch starting at %d", i)
            return {"error": f"AWS Cost Explorer query failed: {e}"}

    # Aggregate results
    cost_by_account: dict[str, dict] = {}
    total_cost = 0.0

    for time_result in all_results:
        date = time_result["TimePeriod"]["Start"]
        for group in time_result.get("Groups", []):
            keys = group["Keys"]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])

            if group_by_upper == "LINKED_ACCOUNT":
                account_id = keys[0]
                dimension_value = keys[0]
            else:
                dimension_value = keys[0]
                account_id = keys[1]

            if account_id not in cost_by_account:
                cost_by_account[account_id] = {"account_id": account_id, "items": {}, "total": 0.0}

            entry = cost_by_account[account_id]
            if dimension_value not in entry["items"]:
                entry["items"][dimension_value] = {"cost": 0.0, "daily": []}

            entry["items"][dimension_value]["cost"] += amount
            entry["items"][dimension_value]["daily"].append({"date": date, "cost": round(amount, 4)})
            entry["total"] += amount
            total_cost += amount

    # Round totals
    for account in cost_by_account.values():
        account["total"] = round(account["total"], 2)
        for item in account["items"].values():
            item["cost"] = round(item["cost"], 2)

    return {
        "accounts_queried": len(valid_ids),
        "period": {"start": start_date, "end": end_date},
        "group_by": group_by_upper,
        "results": list(cost_by_account.values()),
        "total_cost": round(total_cost, 2),
    }
