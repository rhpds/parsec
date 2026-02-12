"""Tool: query_cost_monitor — query the cost-monitor API for aggregated cost data."""

import logging

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    """Get the cost-monitor API base URL."""
    cfg = get_config()
    return cfg.get("cost_monitor", {}).get(
        "api_url", "http://cost-data-service:8000"
    )


def _get_dashboard_url() -> str:
    """Get the cost-monitor dashboard URL (user-facing)."""
    cfg = get_config()
    return cfg.get("cost_monitor", {}).get("dashboard_url", "")


async def query_cost_monitor(
    endpoint: str,
    start_date: str,
    end_date: str,
    providers: str = "",
    group_by: str = "",
    top_n: int = 25,
    drilldown_type: str = "",
    selected_key: str = "",
) -> dict:
    """Query the cost-monitor data service API.

    Args:
        endpoint: One of "summary", "breakdown", "drilldown", "providers".
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        providers: Comma-separated provider filter (e.g. "aws,azure").
        group_by: For breakdown: LINKED_ACCOUNT or INSTANCE_TYPE.
        top_n: For breakdown: number of top results (default 25).
        drilldown_type: For drilldown: account_services or instance_details.
        selected_key: For drilldown: the account ID or instance type to drill into.

    Returns:
        Dict with cost data from cost-monitor.
    """
    base_url = _get_base_url()

    # Build URL and params based on endpoint
    params = {"start_date": start_date, "end_date": end_date}
    # FastAPI expects repeated params for lists: ?providers=aws&providers=gcp
    # httpx handles this when you pass a list of tuples
    provider_list = [p.strip() for p in providers.split(",") if p.strip()] if providers else []

    if endpoint == "summary":
        url = f"{base_url}/api/v1/costs/summary"

    elif endpoint == "breakdown":
        url = f"{base_url}/api/v1/costs/aws/breakdown"
        if group_by:
            params["group_by"] = group_by
        params["top_n"] = str(top_n)

    elif endpoint == "drilldown":
        url = f"{base_url}/api/v1/costs/aws/drilldown"
        if drilldown_type:
            params["drilldown_type"] = drilldown_type
        if selected_key:
            params["selected_key"] = selected_key

    elif endpoint == "providers":
        url = f"{base_url}/api/v1/providers"

    else:
        return {"error": f"Unknown endpoint: {endpoint}. Use summary, breakdown, drilldown, or providers."}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Build params as list of tuples so providers repeat correctly
            param_tuples = list(params.items())
            for p in provider_list:
                param_tuples.append(("providers", p))
            response = await client.get(url, params=param_tuples)
            response.raise_for_status()
            data = response.json()

            # Attach dashboard link if available
            dashboard_url = _get_dashboard_url()
            if dashboard_url and isinstance(data, dict):
                data["_dashboard_link"] = dashboard_url
            elif dashboard_url and isinstance(data, list):
                data = {"results": data, "_dashboard_link": dashboard_url}

            return data

    except httpx.ConnectError:
        return {"error": "Cannot reach cost-monitor service. It may not be running or accessible from this environment."}
    except httpx.HTTPStatusError as e:
        return {"error": f"cost-monitor API returned {e.response.status_code}: {e.response.text[:500]}"}
    except Exception as e:
        logger.exception("cost-monitor query failed")
        return {"error": f"cost-monitor query failed: {e}"}
