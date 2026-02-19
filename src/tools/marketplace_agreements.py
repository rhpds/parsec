"""Tool: query_marketplace_agreements — query DynamoDB marketplace agreement inventory."""

import asyncio
import logging
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from src.connections.aws import get_aws_session

logger = logging.getLogger(__name__)

TABLE_NAME = "marketplace-agreement-inventory"
ACCOUNT_ID_INDEX = "account_id-index"
MAX_RESULTS_CAP = 500
DEFAULT_MAX_RESULTS = 100

# Internal fields to strip from results
_STRIP_FIELDS = {"pk", "error", "term_types"}


def _decimal_to_float(obj: Any) -> Any:
    """Convert DynamoDB Decimal values to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    return obj


def _build_filter_expression(
    status: str | None,
    classification: str | None,
    min_cost: float | None,
) -> Any | None:
    """Build a DynamoDB filter expression from exact-match/numeric parameters.

    Text contains filters (account_name, product_name, vendor_name) are applied
    client-side for case-insensitive matching — DynamoDB contains() is case-sensitive.
    """
    conditions: list[Any] = []

    if status:
        conditions.append(Attr("status").eq(status))

    if classification:
        conditions.append(Attr("classification").eq(classification))

    if min_cost is not None:
        conditions.append(Attr("estimated_cost").gte(Decimal(str(min_cost))))

    if not conditions:
        return None

    expr: Any = conditions[0]
    for c in conditions[1:]:
        expr = expr & c
    return expr


def _matches_text_filters(
    item: dict,
    account_name: str | None,
    product_name: str | None,
    vendor_name: str | None,
) -> bool:
    """Apply case-insensitive contains filters client-side."""
    if account_name:
        val = str(item.get("account_name", "")).lower()
        if account_name.lower() not in val:
            return False
    if product_name:
        val = str(item.get("product_name", "")).lower()
        if product_name.lower() not in val:
            return False
    if vendor_name:
        val = str(item.get("vendor_name", "")).lower()
        if vendor_name.lower() not in val:
            return False
    return True


def _scan_table(table: Any, filter_expr: Any | None) -> list[dict]:
    """Scan the full table with optional filter expression, paginating all pages."""
    scan_kwargs: dict[str, Any] = {}
    if filter_expr is not None:
        scan_kwargs["FilterExpression"] = filter_expr

    items: list[dict] = []
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return items


def _query_by_account(table: Any, account_id: str, filter_expr: Any | None) -> list[dict]:
    """Query the account_id GSI for a specific account, with optional filters."""
    query_kwargs: dict[str, Any] = {
        "IndexName": ACCOUNT_ID_INDEX,
        "KeyConditionExpression": Key("account_id").eq(account_id),
    }
    if filter_expr is not None:
        query_kwargs["FilterExpression"] = filter_expr

    items: list[dict] = []
    while True:
        response = table.query(**query_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key

    return items


def _clean_item(item: dict) -> dict:
    """Strip internal fields and convert Decimals to floats."""
    cleaned = {k: v for k, v in item.items() if k not in _STRIP_FIELDS}
    return _decimal_to_float(cleaned)


def _run_query(
    account_id: str | None,
    account_name: str | None,
    status: str | None,
    classification: str | None,
    min_cost: float | None,
    product_name: str | None,
    vendor_name: str | None,
    max_results: int,
) -> dict:
    """Execute the DynamoDB scan/query (blocking — called via asyncio.to_thread)."""
    session = get_aws_session()
    dynamodb = session.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.Table(TABLE_NAME)

    # Server-side: exact match and numeric filters
    filter_expr = _build_filter_expression(
        status=status,
        classification=classification,
        min_cost=min_cost,
    )

    if account_id:
        items = _query_by_account(table, account_id, filter_expr)
    else:
        items = _scan_table(table, filter_expr)

    # Client-side: case-insensitive text contains filters
    has_text_filters = any([account_name, product_name, vendor_name])
    agreements = []
    for item in items:
        if has_text_filters and not _matches_text_filters(
            item, account_name, product_name, vendor_name
        ):
            continue
        agreements.append(_clean_item(item))
        if len(agreements) >= max_results:
            break

    return {
        "agreements": agreements,
        "count": len(agreements),
        "truncated": len(agreements) >= max_results,
    }


async def query_marketplace_agreements(
    account_id: str | None = None,
    account_name: str | None = None,
    status: str | None = None,
    classification: str | None = None,
    min_cost: float | None = None,
    product_name: str | None = None,
    vendor_name: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """Query the marketplace agreement inventory in DynamoDB.

    Args:
        account_id: Filter by 12-digit AWS account ID (uses GSI for fast lookup).
        account_name: Filter by account name (case-insensitive contains match).
        status: Filter by agreement status (e.g. ACTIVE, CLOSED).
        classification: Filter by classification (e.g. "SaaS (Auto-Renew)").
        min_cost: Minimum estimated cost threshold in USD.
        product_name: Filter by product name (case-insensitive contains match).
        vendor_name: Filter by vendor name (case-insensitive contains match).
        max_results: Max agreements to return. Default: 100, max: 500.

    Returns:
        Dict with agreements list, count, and truncated flag.
    """
    try:
        max_results = min(max_results, MAX_RESULTS_CAP)

        return await asyncio.to_thread(
            _run_query,
            account_id=account_id,
            account_name=account_name,
            status=status,
            classification=classification,
            min_cost=min_cost,
            product_name=product_name,
            vendor_name=vendor_name,
            max_results=max_results,
        )

    except ClientError as e:
        logger.exception("Marketplace agreements query failed")
        return {"error": f"Marketplace agreements query failed: {e}"}
    except Exception as e:
        logger.exception("Marketplace agreements query failed")
        return {"error": f"Marketplace agreements query failed: {e}"}
