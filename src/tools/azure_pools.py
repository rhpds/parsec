"""Tool: query_azure_pools — query Azure subscription pool assignments from Cosmos DB."""

import asyncio
import logging
import re
from collections import defaultdict

from src.connections.azure_cosmos import get_cosmos_container

logger = logging.getLogger(__name__)

VALID_DATABASES = {"pools", "aro", "roadshow"}
MAX_RESULTS_CAP = 500
DEFAULT_MAX_RESULTS = 100


def _parse_pool_id(sub_id: str) -> str | None:
    """Extract pool ID from subscription name (e.g. 'pool-01-273' → '01')."""
    m = re.match(r"pool-(\d+)-", sub_id)
    return m.group(1) if m else None


def _list_pools(database: str) -> dict:
    """List all pools with utilization summary."""
    container = get_cosmos_container(database=database)
    if container is None:
        return {"error": "Azure Cosmos not configured"}

    items = list(
        container.query_items(
            "SELECT c.id, c.used, c.projecttag FROM c",
            enable_cross_partition_query=True,
        )
    )

    pools: dict[str, dict] = defaultdict(lambda: {"total": 0, "in_use": 0, "available": 0})
    for item in items:
        pool_id = _parse_pool_id(item["id"])
        if pool_id is None:
            continue
        pools[pool_id]["total"] += 1
        if item.get("used"):
            pools[pool_id]["in_use"] += 1
        else:
            pools[pool_id]["available"] += 1

    total_all = sum(p["total"] for p in pools.values())
    in_use_all = sum(p["in_use"] for p in pools.values())

    return {
        "database": database,
        "pools": dict(sorted(pools.items())),
        "summary": {
            "total_subscriptions": total_all,
            "in_use": in_use_all,
            "available": total_all - in_use_all,
            "pool_count": len(pools),
        },
    }


def _get_pool(pool_id: str, database: str, max_results: int) -> dict:
    """Get all subscriptions in a specific pool."""
    container = get_cosmos_container(database=database)
    if container is None:
        return {"error": "Azure Cosmos not configured"}

    prefix = f"pool-{pool_id}-"
    items = list(
        container.query_items(
            f'SELECT c.id, c.used, c.projecttag FROM c WHERE STARTSWITH(c.id, "{prefix}")',
            enable_cross_partition_query=True,
        )
    )

    def _sort_key(x: dict) -> int:
        m = re.search(r"-(\d+)$", x["id"])
        return int(m.group(1)) if m else 0

    items.sort(key=_sort_key)

    in_use = [i for i in items if i.get("used")]
    return {
        "database": database,
        "pool_id": pool_id,
        "subscriptions": items[:max_results],
        "total": len(items),
        "in_use": len(in_use),
        "available": len(items) - len(in_use),
        "truncated": len(items) > max_results,
    }


def _get_subscription(subscription_name: str, database: str) -> dict:
    """Look up a specific subscription by name."""
    container = get_cosmos_container(database=database)
    if container is None:
        return {"error": "Azure Cosmos not configured"}

    items = list(
        container.query_items(
            f'SELECT * FROM c WHERE c.id = "{subscription_name}"',
            enable_cross_partition_query=True,
        )
    )

    if not items:
        return {"error": f"Subscription '{subscription_name}' not found in {database} database"}

    item = items[0]
    return {
        "database": database,
        "subscription": {
            "id": item["id"],
            "used": item.get("used", False),
            "projecttag": item.get("projecttag"),
        },
    }


def _search_by_project(project_tag: str, database: str, max_results: int) -> dict:
    """Find all subscriptions assigned to a project tag."""
    container = get_cosmos_container(database=database)
    if container is None:
        return {"error": "Azure Cosmos not configured"}

    items = list(
        container.query_items(
            f'SELECT c.id, c.used, c.projecttag FROM c WHERE c.projecttag = "{project_tag}"',
            enable_cross_partition_query=True,
        )
    )

    items.sort(key=lambda x: x["id"])

    return {
        "database": database,
        "project_tag": project_tag,
        "subscriptions": items[:max_results],
        "count": len(items),
        "truncated": len(items) > max_results,
    }


async def query_azure_pools(
    action: str,
    pool_id: str | None = None,
    subscription_name: str | None = None,
    project_tag: str | None = None,
    database: str = "pools",
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """Query Azure subscription pool assignments from Cosmos DB."""
    if database not in VALID_DATABASES:
        return {
            "error": f"Invalid database '{database}'. Valid: {', '.join(sorted(VALID_DATABASES))}"
        }

    max_results = min(max_results, MAX_RESULTS_CAP)

    try:
        if action == "list_pools":
            return await asyncio.to_thread(_list_pools, database)

        elif action == "get_pool":
            if not pool_id:
                return {"error": "pool_id is required for get_pool (e.g. '00', '01')"}
            return await asyncio.to_thread(_get_pool, pool_id, database, max_results)

        elif action == "get_subscription":
            if not subscription_name:
                return {"error": "subscription_name is required (e.g. 'pool-01-273')"}
            return await asyncio.to_thread(_get_subscription, subscription_name, database)

        elif action == "search_by_project":
            if not project_tag:
                return {"error": "project_tag is required (e.g. 'sandbox-api')"}
            return await asyncio.to_thread(_search_by_project, project_tag, database, max_results)

        else:
            return {
                "error": f"Unknown action '{action}'. Valid: list_pools, get_pool, get_subscription, search_by_project"
            }

    except Exception as e:
        logger.exception("Azure pools query failed")
        return {"error": f"Azure pools query failed: {e}"}
