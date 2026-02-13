"""Tool: query_aws_capacity_manager — ODCR metrics from EC2 Capacity Manager."""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from src.connections.aws import get_aws_session

logger = logging.getLogger(__name__)

# Metric presets
_UTILIZATION_METRICS = [
    "reservation-avg-utilization-inst",
    "reservation-total-capacity-hrs-inst",
    "reservation-unused-total-capacity-hrs-inst",
    "reservation-total-estimated-cost",
    "reservation-unused-total-estimated-cost",
    "reservation-total-count",
]

_UNUSED_COST_METRICS = [
    "reservation-unused-total-estimated-cost",
    "reservation-unused-total-capacity-hrs-inst",
]

# Default groupings per metric preset
_DEFAULT_GROUP_BY = {
    "utilization": "account-id",
    "unused_cost": "account-id",
}


def _build_filters(
    instance_type: str | None,
    account_id: str | None,
    reservation_state: str,
) -> list[dict]:
    """Build FilterBy conditions for Capacity Manager API calls."""
    filters = []
    if reservation_state:
        filters.append(
            {
                "DimensionCondition": {
                    "Dimension": "reservation-state",
                    "Comparison": "equals",
                    "Values": [reservation_state],
                }
            }
        )
    if instance_type:
        filters.append(
            {
                "DimensionCondition": {
                    "Dimension": "instance-type",
                    "Comparison": "equals",
                    "Values": [instance_type],
                }
            }
        )
    if account_id:
        filters.append(
            {
                "DimensionCondition": {
                    "Dimension": "account-id",
                    "Comparison": "equals",
                    "Values": [account_id],
                }
            }
        )
    return filters


def _query_metric_data(
    ec2_client,
    metric_names: list[str],
    group_by: str,
    filters: list[dict],
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Call get_capacity_manager_metric_data with pagination."""
    kwargs = {
        "MetricNames": metric_names,
        "GroupBy": [group_by],
        "StartTime": start_time,
        "EndTime": end_time,
        "Period": 3600,
    }
    if filters:
        kwargs["FilterBy"] = filters

    all_results = []
    while True:
        response = ec2_client.get_capacity_manager_metric_data(**kwargs)
        all_results.extend(response.get("MetricDataResults", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token

    return all_results


def _aggregate_metric_results(
    metric_data: list[dict],
    group_by: str,
    min_hours: int = 24,
) -> tuple[list[dict], dict]:
    """Aggregate time-series metric data into per-dimension summaries.

    The API returns MetricDataResults — a flat list where each entry has:
      Dimension: {InstanceType: "c5.4xlarge"} (key varies by group_by)
      Timestamp: "2026-02-12T10:00:00+00:00"
      MetricValues: [{Metric: "reservation-total-count", Value: 1.0}, ...]

    When grouping by account-id or reservation-id, dimensions with fewer than
    min_hours of data are excluded as transient (normal provisioning behavior).

    Returns (results_list, totals_dict).
    """
    # Map group_by dimension names to the response key format (kebab → PascalCase)
    dim_key_map = {
        "instance-type": "InstanceType",
        "instance-family": "InstanceFamily",
        "account-id": "AccountId",
        "resource-region": "ResourceRegion",
        "availability-zone-id": "AvailabilityZoneId",
        "reservation-id": "ReservationId",
        "reservation-state": "ReservationState",
        "tenancy": "Tenancy",
        "instance-platform": "InstancePlatform",
    }
    dim_key = dim_key_map.get(group_by, group_by)

    # Build per-dimension accumulators
    dim_data: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for entry in metric_data:
        dim_value = entry.get("Dimension", {}).get(dim_key, "unknown")
        for mv in entry.get("MetricValues", []):
            metric_name = mv.get("Metric", "")
            value = mv.get("Value")
            if value is not None:
                dim_data[dim_value][metric_name].append(value)

    # Filter out transient dimensions for account/reservation groupings
    filter_transient = group_by in ("account-id", "reservation-id") and min_hours > 0
    transient_count = 0
    transient_cost = 0.0

    # Summarize each dimension
    results = []
    total_cost = 0.0
    total_unused_cost = 0.0
    total_capacity_hrs = 0.0
    total_unused_hrs = 0.0

    for dim_value, metrics in sorted(dim_data.items()):
        # Check if this dimension is transient (few hours of data)
        if filter_transient:
            data_points = len(metrics.get("reservation-total-count", []))
            if data_points < min_hours:
                transient_count += 1
                ucost = metrics.get("reservation-unused-total-estimated-cost", [])
                transient_cost += sum(ucost)
                continue

        entry = {"dimension": dim_value}

        # Average utilization (already a percentage, take mean of data points)
        util_vals = metrics.get("reservation-avg-utilization-inst", [])
        if util_vals:
            entry["avg_utilization_pct"] = round(sum(util_vals) / len(util_vals), 1)

        # Sum metrics (cumulative over the period)
        cap_vals = metrics.get("reservation-total-capacity-hrs-inst", [])
        if cap_vals:
            entry["total_capacity_hrs"] = round(sum(cap_vals), 1)
            total_capacity_hrs += sum(cap_vals)

        unused_vals = metrics.get("reservation-unused-total-capacity-hrs-inst", [])
        if unused_vals:
            entry["unused_capacity_hrs"] = round(sum(unused_vals), 1)
            total_unused_hrs += sum(unused_vals)

        cost_vals = metrics.get("reservation-total-estimated-cost", [])
        if cost_vals:
            entry["total_estimated_cost_usd"] = round(sum(cost_vals), 2)
            total_cost += sum(cost_vals)

        ucost_vals = metrics.get("reservation-unused-total-estimated-cost", [])
        if ucost_vals:
            entry["unused_estimated_cost_usd"] = round(sum(ucost_vals), 2)
            total_unused_cost += sum(ucost_vals)

        count_vals = metrics.get("reservation-total-count", [])
        if count_vals:
            # Take the max count seen (represents peak concurrent reservations)
            entry["reservation_count"] = int(max(count_vals))

        results.append(entry)

    # Sort by unused cost descending (worst offenders first)
    results.sort(
        key=lambda r: r.get("unused_estimated_cost_usd", 0),
        reverse=True,
    )

    overall_util = 0.0
    if total_capacity_hrs > 0:
        overall_util = round((1 - total_unused_hrs / total_capacity_hrs) * 100, 1)

    totals = {
        "total_estimated_cost_usd": round(total_cost, 2),
        "unused_estimated_cost_usd": round(total_unused_cost, 2),
        "overall_utilization_pct": overall_util,
        "total_dimensions": len(results),
    }

    if filter_transient and transient_count > 0:
        totals["transient_excluded"] = transient_count
        totals["transient_cost_usd"] = round(transient_cost, 2)

    # Cap results to top 50 to avoid blowing Claude's context window
    if len(results) > 50:
        results = results[:50]
        totals["results_truncated"] = True

    return results, totals


def _query_inventory(
    ec2_client,
    filters: list[dict],
    start_time: datetime,
    end_time: datetime,
    min_hours: int = 24,
) -> dict:
    """List ODCRs using metric data grouped by reservation-id.

    Filters out transient ODCRs (active < min_hours) that are normal
    provisioning behavior. Only persistent ODCRs are waste.
    """
    metric_data = _query_metric_data(
        ec2_client,
        metric_names=[
            "reservation-total-count",
            "reservation-avg-utilization-inst",
            "reservation-unused-total-estimated-cost",
        ],
        group_by="reservation-id",
        filters=filters,
        start_time=start_time,
        end_time=end_time,
    )

    # Aggregate per reservation ID
    res_data: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for entry in metric_data:
        res_id = entry.get("Dimension", {}).get("ReservationId", "unknown")
        for mv in entry.get("MetricValues", []):
            metric_name = mv.get("Metric", "")
            value = mv.get("Value")
            if value is not None:
                res_data[res_id][metric_name].append(value)

    persistent = []
    transient_count = 0
    transient_cost = 0.0
    for res_id, metrics in sorted(res_data.items()):
        util_vals = metrics.get("reservation-avg-utilization-inst", [])
        ucost_vals = metrics.get("reservation-unused-total-estimated-cost", [])
        count_vals = metrics.get("reservation-total-count", [])
        hours_active = len(count_vals)
        unused_cost = round(sum(ucost_vals), 2) if ucost_vals else 0.0

        if hours_active < min_hours:
            transient_count += 1
            transient_cost += unused_cost
            continue

        persistent.append(
            {
                "reservation_id": res_id,
                "avg_utilization_pct": round(sum(util_vals) / len(util_vals), 1)
                if util_vals
                else None,
                "unused_estimated_cost_usd": unused_cost,
                "hours_active": hours_active,
            }
        )

    # Sort by unused cost descending
    persistent.sort(
        key=lambda r: r.get("unused_estimated_cost_usd") or 0,
        reverse=True,
    )

    total_persistent = len(persistent)
    truncated = total_persistent > 100
    if truncated:
        persistent = persistent[:100]

    return {
        "metric": "inventory",
        "persistent_reservations": total_persistent,
        "transient_excluded": transient_count,
        "transient_cost_usd": round(transient_cost, 2),
        "results_truncated": truncated,
        "reservations": persistent,
    }


def _parse_available_range(error_msg: str) -> tuple[datetime, datetime] | None:
    """Parse available data range from InvalidParameterValue error message."""
    match = re.search(
        r"Available range: (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) - (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)",
        error_msg,
    )
    if not match:
        return None
    avail_start = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    avail_end = datetime.strptime(match.group(2), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return avail_start, avail_end


async def _clamp_to_available_range(
    ec2_client,
    metric: str,
    filters: list[dict],
    start_time: datetime,
    end_time: datetime,
) -> tuple[datetime, datetime]:
    """Probe the API to discover the available data range and clamp times.

    Makes a minimal query; if it fails with InvalidParameterValue, parses the
    available range from the error and clamps start_time/end_time accordingly.
    """
    try:
        await asyncio.to_thread(
            ec2_client.get_capacity_manager_metric_data,
            MetricNames=["reservation-total-count"],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            GroupBy=["reservation-state"],
            MaxResults=1,
        )
    except ClientError as e:
        if "Available range:" in str(e):
            bounds = _parse_available_range(str(e))
            if bounds:
                avail_start, avail_end = bounds
                start_time = max(start_time, avail_start)
                end_time = min(end_time, avail_end)
                logger.info(
                    "Clamped time range to available data: %s — %s",
                    start_time.isoformat(),
                    end_time.isoformat(),
                )
        else:
            raise

    return start_time, end_time


async def query_aws_capacity_manager(
    metric: str = "utilization",
    group_by: str | None = None,
    instance_type: str | None = None,
    account_id: str | None = None,
    reservation_state: str = "active",
    hours: int = 168,
) -> dict:
    """Query EC2 Capacity Manager for ODCR metrics.

    Args:
        metric: Preset - "utilization", "unused_cost", or "inventory".
        group_by: Dimension to group by (default varies by metric).
        instance_type: Filter to specific instance type.
        account_id: Filter to specific AWS account.
        reservation_state: Filter by state. Default: "active".
        hours: Hours of history. Default: 168 (7 days). Max: 2160 (90 days).

    Returns:
        Dict with aggregated results.
    """
    try:
        session = get_aws_session()
        # Capacity Manager runs in us-east-1 on the payer account
        ec2_client = session.client("ec2", region_name="us-east-1")

        hours = min(hours, 2160)
        # Truncate to the hour and lag by 4h — data pipeline has ~3-4h delay
        end_time = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=4)
        start_time = end_time - timedelta(hours=hours)

        filters = _build_filters(instance_type, account_id, reservation_state)

        # Clamp to available data range (retry once if out of range)
        start_time, end_time = await _clamp_to_available_range(
            ec2_client,
            metric,
            filters,
            start_time,
            end_time,
        )

        if metric == "inventory":
            return await asyncio.to_thread(
                _query_inventory,
                ec2_client,
                filters,
                start_time,
                end_time,
            )

        # Metrics-based queries (utilization or unused_cost)
        metric_names = _UNUSED_COST_METRICS if metric == "unused_cost" else _UTILIZATION_METRICS

        effective_group_by = group_by or _DEFAULT_GROUP_BY.get(metric, "instance-type")

        response = await asyncio.to_thread(
            _query_metric_data,
            ec2_client,
            metric_names,
            effective_group_by,
            filters,
            start_time,
            end_time,
        )

        results, totals = _aggregate_metric_results(response, effective_group_by)

        return {
            "metric": metric,
            "period": {
                "start": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "group_by": effective_group_by,
            "results": results,
            "totals": totals,
        }

    except Exception as e:
        logger.exception("AWS Capacity Manager query failed")
        return {"error": f"AWS Capacity Manager query failed: {e}"}
