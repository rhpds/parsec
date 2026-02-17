"""Tool: query_azure_costs — query Azure billing CSVs for cost data."""

import csv
import logging
from collections.abc import Iterator
from datetime import datetime

from src.connections.azure import get_container_client

logger = logging.getLogger(__name__)


def query_azure_costs(
    start_date: str,
    end_date: str,
    subscription_names: list[str] | None = None,
    meter_filter: str | None = None,
) -> dict:
    """Query Azure billing CSVs for costs in specified subscriptions.

    This is a sync function (Azure SDK uses blocking I/O). The orchestrator
    runs it via asyncio.to_thread to avoid blocking the event loop.

    Args:
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        subscription_names: Optional list of subscription names (e.g. pool-01-374).
            If empty or None, queries all subscriptions (requires meter_filter).
        meter_filter: Case-insensitive search string matched against MeterCategory
            and MeterSubCategory. Required when subscription_names is omitted.

    Returns:
        Dict with results_by_subscription and total_cost.
    """
    container_client = get_container_client()
    if container_client is None:
        return {"error": "Azure blob storage not configured"}

    if not subscription_names and not meter_filter:
        return {
            "error": (
                "meter_filter is required when querying all subscriptions. "
                "Provide a search term to filter by MeterCategory or MeterSubCategory "
                "(e.g. 'Page Blob', 'Virtual Machines', 'NC Series')."
            )
        }

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "Dates must be YYYY-MM-DD format"}

    sub_set = set(subscription_names) if subscription_names else None
    meter_filter_upper = meter_filter.upper() if meter_filter else None

    try:
        # List billing CSV blobs in the date range
        blobs = _list_billing_blobs(container_client, start_dt, end_dt)
        if not blobs:
            return {
                "subscription_names": subscription_names or "all",
                "period": {"start": start_date, "end": end_date},
                "results": [],
                "total_cost": 0.0,
                "note": "No billing CSV blobs found for the date range",
            }

        # Parse each blob and collect costs
        cost_by_sub: dict[str, dict] = {}
        total_cost = 0.0

        for blob_name in blobs:
            for row in _stream_and_parse_csv(
                container_client, blob_name, sub_set, start_dt, end_dt, meter_filter_upper
            ):
                sub_name = row["subscription_name"]
                if sub_name not in cost_by_sub:
                    cost_by_sub[sub_name] = {
                        "subscription_name": sub_name,
                        "services": {},
                        "total": 0.0,
                        "gpu_cost": 0.0,
                    }

                entry = cost_by_sub[sub_name]
                service = row["meter_category"]
                cost = row["cost"]

                if service not in entry["services"]:
                    entry["services"][service] = {"cost": 0.0, "meter_subcategories": {}}

                entry["services"][service]["cost"] += cost
                sub_cat = row.get("meter_subcategory", "")
                if sub_cat:
                    if sub_cat not in entry["services"][service]["meter_subcategories"]:
                        entry["services"][service]["meter_subcategories"][sub_cat] = 0.0
                    entry["services"][service]["meter_subcategories"][sub_cat] += cost

                entry["total"] += cost
                total_cost += cost

                # Flag GPU VMs
                if _is_gpu_vm(row.get("meter_subcategory", "")):
                    entry["gpu_cost"] += cost

        # Round
        for sub in cost_by_sub.values():
            sub["total"] = round(sub["total"], 2)
            sub["gpu_cost"] = round(sub["gpu_cost"], 2)
            for svc in sub["services"].values():
                svc["cost"] = round(svc["cost"], 2)
                svc["meter_subcategories"] = {
                    k: round(v, 2) for k, v in svc["meter_subcategories"].items()
                }

        result = {
            "subscriptions_queried": len(subscription_names) if subscription_names else "all",
            "period": {"start": start_date, "end": end_date},
            "blobs_processed": len(blobs),
            "results": list(cost_by_sub.values()),
            "total_cost": round(total_cost, 2),
        }
        if meter_filter:
            result["meter_filter"] = meter_filter
        return result

    except Exception as e:
        logger.exception("Azure billing query failed")
        return {"error": f"Azure billing query failed: {e}"}


def _list_billing_blobs(container_client, start_dt: datetime, end_dt: datetime) -> list[str]:
    """List billing CSV blob names in the date range."""
    blobs = []
    for blob in container_client.list_blobs():
        name = blob.name
        # Only process part_1 CSV files (primary billing data)
        if "part_1" not in name or not name.endswith(".csv"):
            continue
        # Filter by date from blob path (e.g. 20250101-20250131/)
        try:
            parts = name.split("/")
            for part in parts:
                if len(part) == 17 and "-" in part:  # YYYYMMDD-YYYYMMDD
                    blob_start = datetime.strptime(part[:8], "%Y%m%d")
                    blob_end = datetime.strptime(part[9:17], "%Y%m%d")
                    if blob_end >= start_dt and blob_start <= end_dt:
                        blobs.append(name)
                    break
        except (ValueError, IndexError):
            # Include blobs we can't parse dates from
            blobs.append(name)
    return blobs


def _blob_line_iterator(blob_client) -> Iterator[str]:
    """Yield text lines from a blob, streaming chunk by chunk.

    Only one chunk (~4 MB) plus a partial-line buffer are held in memory
    at a time, instead of loading the entire blob with readall().
    """
    stream = blob_client.download_blob()
    buffer = ""
    first_chunk = True
    for chunk in stream.chunks():
        encoding = "utf-8-sig" if first_chunk else "utf-8"
        first_chunk = False
        buffer += chunk.decode(encoding)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer


def _stream_and_parse_csv(
    container_client,
    blob_name: str,
    subscription_names: set[str] | None,
    start_dt: datetime,
    end_dt: datetime,
    meter_filter: str | None = None,
) -> Iterator[dict]:
    """Stream a billing CSV and yield matching rows without loading entire blob."""
    blob_client = container_client.get_blob_client(blob_name)
    reader = csv.DictReader(_blob_line_iterator(blob_client))

    for row in reader:
        sub_name = row.get("SubscriptionName", row.get("subscriptionName", ""))
        if subscription_names is not None and sub_name not in subscription_names:
            continue

        # Apply meter filter early to skip non-matching rows fast
        if meter_filter is not None:
            category = row.get("MeterCategory", row.get("meterCategory", "")).upper()
            subcategory = row.get("MeterSubCategory", row.get("meterSubCategory", "")).upper()
            if meter_filter not in category and meter_filter not in subcategory:
                continue

        # Parse date
        date_str = row.get("Date", row.get("date", row.get("UsageDateTime", "")))
        if not date_str:
            continue

        try:
            # Handle multiple date formats
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
                try:
                    row_date = datetime.strptime(
                        date_str.split(" ")[0] if " " in date_str else date_str, fmt
                    )
                    break
                except ValueError:
                    continue
            else:
                continue

            if row_date < start_dt or row_date > end_dt:
                continue
        except (ValueError, IndexError):
            continue

        cost_str = row.get(
            "CostInBillingCurrency", row.get("costInBillingCurrency", row.get("Cost", "0"))
        )
        try:
            cost = float(cost_str)
        except (ValueError, TypeError):
            cost = 0.0

        yield {
            "subscription_name": sub_name,
            "date": row_date.strftime("%Y-%m-%d"),
            "meter_category": row.get("MeterCategory", row.get("meterCategory", "")),
            "meter_subcategory": row.get("MeterSubCategory", row.get("meterSubCategory", "")),
            "cost": cost,
        }


def _is_gpu_vm(meter_subcategory: str) -> bool:
    """Check if a meter subcategory indicates a GPU VM."""
    if not meter_subcategory:
        return False
    upper = meter_subcategory.upper()
    return any(series in upper for series in ("NC", "ND", "NV"))  # noqa: typos:ignore
