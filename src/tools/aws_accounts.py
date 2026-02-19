"""Tool: query_aws_accounts — query the DynamoDB sandbox account pool."""

import asyncio
import logging
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from src.connections.aws import get_aws_session

logger = logging.getLogger(__name__)

TABLE_NAME = "accounts"
MAX_RESULTS_CAP = 500
DEFAULT_MAX_RESULTS = 100

# Fields to strip from results (credentials and DynamoDB replication metadata)
_STRIP_FIELDS = {
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws:rep:deleting",
    "aws:rep:updateregion",
    "aws:rep:updatetime",
}


def _clean_item(item: dict) -> dict:
    """Strip sensitive/internal fields and flatten DynamoDB types."""
    cleaned = {k: v for k, v in item.items() if k not in _STRIP_FIELDS}
    # Flatten annotations map if present
    if "annotations" in cleaned and isinstance(cleaned["annotations"], dict):
        # Keep as-is — it's already a dict after DynamoDB resource deserialization
        pass
    return cleaned


def _matches_text_filters(
    item: dict,
    owner: str | None,
    zone: str | None,
    envtype: str | None,
    reservation: str | None,
) -> bool:
    """Apply case-insensitive contains filters client-side."""
    if owner:
        val = str(item.get("owner", "") or item.get("owner_email", "")).lower()
        if owner.lower() not in val:
            return False
    if zone:
        val = str(item.get("zone", "")).lower()
        if zone.lower() not in val:
            return False
    if envtype:
        val = str(item.get("envtype", "")).lower()
        if envtype.lower() not in val:
            return False
    if reservation:
        val = str(item.get("reservation", "")).lower()
        if reservation.lower() not in val:
            return False
    return True


def _build_filter_expression(
    available: bool | None,
    account_id: str | None,
) -> Any | None:
    """Build a DynamoDB filter expression from exact-match parameters."""
    conditions: list[Any] = []

    if available is not None:
        conditions.append(Attr("available").eq(available))

    if account_id:
        conditions.append(Attr("account_id").eq(account_id))

    if not conditions:
        return None

    expr: Any = conditions[0]
    for c in conditions[1:]:
        expr = expr & c
    return expr


def _run_query(
    name: str | None,
    account_id: str | None,
    available: bool | None,
    owner: str | None,
    zone: str | None,
    envtype: str | None,
    reservation: str | None,
    max_results: int,
) -> dict:
    """Execute the DynamoDB query (blocking — called via asyncio.to_thread)."""
    session = get_aws_session()
    dynamodb = session.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.Table(TABLE_NAME)

    # Direct key lookup by sandbox name
    if name:
        response = table.get_item(Key={"name": name})
        item = response.get("Item")
        if not item:
            return {"accounts": [], "count": 0, "truncated": False}
        return {"accounts": [_clean_item(item)], "count": 1, "truncated": False}

    # Scan with server-side filters
    filter_expr = _build_filter_expression(available=available, account_id=account_id)

    scan_kwargs: dict[str, Any] = {}
    if filter_expr is not None:
        scan_kwargs["FilterExpression"] = filter_expr

    has_text_filters = any([owner, zone, envtype, reservation])
    accounts: list[dict] = []
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            if has_text_filters and not _matches_text_filters(
                item, owner, zone, envtype, reservation
            ):
                continue
            accounts.append(_clean_item(item))
            if len(accounts) >= max_results:
                return {
                    "accounts": accounts,
                    "count": len(accounts),
                    "truncated": True,
                }
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return {
        "accounts": accounts,
        "count": len(accounts),
        "truncated": False,
    }


async def query_aws_account_db(
    name: str | None = None,
    account_id: str | None = None,
    available: bool | None = None,
    owner: str | None = None,
    zone: str | None = None,
    envtype: str | None = None,
    reservation: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """Query the sandbox account pool in DynamoDB.

    Args:
        name: Exact sandbox name lookup (e.g. "sandbox4440"). Uses key lookup.
        account_id: Filter by 12-digit AWS account ID.
        available: Filter by availability (true = idle, false = in use).
        owner: Filter by owner email (case-insensitive contains match).
        zone: Filter by DNS zone (case-insensitive contains match).
        envtype: Filter by environment type (case-insensitive contains match).
        reservation: Filter by reservation type (case-insensitive contains match).
        max_results: Max accounts to return. Default: 100, max: 500.

    Returns:
        Dict with accounts list, count, and truncated flag.
    """
    try:
        max_results = min(max_results, MAX_RESULTS_CAP)

        return await asyncio.to_thread(
            _run_query,
            name=name,
            account_id=account_id,
            available=available,
            owner=owner,
            zone=zone,
            envtype=envtype,
            reservation=reservation,
            max_results=max_results,
        )

    except ClientError as e:
        logger.exception("AWS accounts query failed")
        return {"error": f"AWS accounts query failed: {e}"}
    except Exception as e:
        logger.exception("AWS accounts query failed")
        return {"error": f"AWS accounts query failed: {e}"}
