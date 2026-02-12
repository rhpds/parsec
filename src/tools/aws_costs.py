"""Tool: query_aws_costs — query AWS Cost Explorer for cost data."""

import logging
from datetime import datetime, timedelta

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
    # Validate and adjust dates
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "Dates must be YYYY-MM-DD format"}

    # CE end_date is exclusive — if same day or end <= start, bump end by 1 day
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(days=1)

    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    # Validate account IDs (empty list = org-wide query)
    valid_ids = [aid for aid in account_ids if len(aid) == 12 and aid.isdigit()]

    group_by_upper = group_by.upper()
    if group_by_upper not in ("SERVICE", "INSTANCE_TYPE", "LINKED_ACCOUNT"):
        return {
            "error": f"Invalid group_by: {group_by}. Must be SERVICE, INSTANCE_TYPE, or LINKED_ACCOUNT"
        }

    ce = get_ce_client()
    cfg = get_config()
    batch_size = cfg.aws.get("batch_size", 100)

    all_results = []

    # If no account IDs, do a single org-wide query
    batches: list[list[str] | None] = (
        [valid_ids[i : i + batch_size] for i in range(0, len(valid_ids), batch_size)]
        if valid_ids
        else [None]
    )

    for batch in batches:
        group_by_dims = [{"Type": "DIMENSION", "Key": group_by_upper}]
        if group_by_upper != "LINKED_ACCOUNT" and batch:
            group_by_dims.append({"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"})

        try:
            kwargs = {
                "TimePeriod": {"Start": start_date, "End": end_date},
                "Granularity": "DAILY",
                "GroupBy": group_by_dims,
                "Metrics": ["UnblendedCost"],
            }

            if batch:
                kwargs["Filter"] = {
                    "Dimensions": {
                        "Key": "LINKED_ACCOUNT",
                        "Values": batch,
                    }
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
            error_msg = str(e)
            # If account filter causes "historical data" error, retry without it
            if "historical data" in error_msg and batch:
                logger.warning("CE historical data error with account filter — retrying org-wide")
                try:
                    kwargs.pop("Filter", None)
                    kwargs.pop("NextPageToken", None)
                    # Simplify GroupBy to just the primary dimension (remove LINKED_ACCOUNT)
                    kwargs["GroupBy"] = [{"Type": "DIMENSION", "Key": group_by_upper}]
                    response = ce.get_cost_and_usage(**kwargs)
                    all_results.extend(response.get("ResultsByTime", []))
                except Exception as retry_e:
                    logger.exception("AWS CE retry also failed")
                    return {"error": f"AWS Cost Explorer query failed: {retry_e}"}
            else:
                logger.exception("AWS CE query failed for batch")
                return {"error": f"AWS Cost Explorer query failed: {e}"}

    # Aggregate results
    cost_by_account: dict[str, dict] = {}
    total_cost = 0.0

    for time_result in all_results:
        date = time_result["TimePeriod"]["Start"]
        for group in time_result.get("Groups", []):
            keys = group["Keys"]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])

            # When filtering by account, keys = [dimension, account_id]
            # When org-wide (no filter), keys = [dimension] only
            if len(keys) == 1:
                dimension_value = keys[0]
                account_id = "org-wide"
            elif group_by_upper == "LINKED_ACCOUNT":
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
            entry["items"][dimension_value]["daily"].append(
                {"date": date, "cost": round(amount, 4)}
            )
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
