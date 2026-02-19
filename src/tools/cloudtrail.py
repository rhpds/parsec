"""Tool: query_cloudtrail — query CloudTrail Lake for org-wide API events."""

import asyncio
import logging
import re
import time
from typing import Any

from botocore.exceptions import ClientError

from src.config import get_config
from src.connections.aws import get_aws_session

logger = logging.getLogger(__name__)

MAX_ROWS = 500
POLL_INTERVAL = 2
QUERY_TIMEOUT = 120  # seconds — CloudTrail Lake scans can take 30-90s

# Matches eventTime comparisons like: eventTime > '2026-01-15' or eventTime >= '2026-01-15T00:00:00Z'
_EVENT_TIME_RE = re.compile(
    r"eventTime\s*([><=!]+)\s*'(\d{4}-\d{2}-\d{2})(?:[T\s]\S*)?'",
    re.IGNORECASE,
)


def _inject_partition_key(query: str) -> str:
    """Inject calendarday partition key filter based on eventTime filters.

    The CloudTrail Lake event data store uses calendarday (YYYYMMDD bigint) as
    a partition key. Queries MUST include calendarday in the WHERE clause or they
    fail with InvalidQueryStatementException. Also, eventTime values must use
    full timestamp format ('YYYY-MM-DDT00:00:00Z'), not short dates.

    This function transparently adds calendarday filters and normalizes eventTime
    formats so the agent doesn't need to know about partition keys.
    """
    # Skip if calendarday is already present
    if re.search(r"\bcalendarday\b", query, re.IGNORECASE):
        return query

    # Find all eventTime comparisons and build calendarday equivalents
    matches = list(_EVENT_TIME_RE.finditer(query))
    if not matches:
        return query

    calendarday_clauses = []
    for m in matches:
        op = m.group(1)
        date_str = m.group(2)  # YYYY-MM-DD
        calendarday = date_str.replace("-", "")  # 20260115

        # Map eventTime operators to calendarday operators
        calendarday_clauses.append(f"calendarday {op} {calendarday}")

        # Normalize short dates to full timestamps in the original query
        # e.g. eventTime > '2026-01-15' → eventTime > '2026-01-15T00:00:00Z'
        full_match = m.group(0)
        date_value = full_match.split("'")[1]  # extract value between quotes
        if "T" not in date_value and " " not in date_value:
            normalized = f"eventTime {op} '{date_str}T00:00:00Z'"
            query = query.replace(full_match, normalized)

    # Inject calendarday clause(s) after WHERE
    calendarday_filter = " AND ".join(calendarday_clauses)
    query = re.sub(
        r"\bWHERE\b",
        f"WHERE {calendarday_filter} AND",
        query,
        count=1,
        flags=re.IGNORECASE,
    )

    return query


# Matches bare map columns used with LIKE/comparison operators
# e.g. requestParameters LIKE '%foo%' → CAST(requestParameters AS varchar) LIKE '%foo%'
_MAP_COLUMNS = ("requestParameters", "responseElements")
_MAP_LIKE_RE = re.compile(
    r"\b(" + "|".join(_MAP_COLUMNS) + r")\s+(LIKE|NOT\s+LIKE)\s+",
    re.IGNORECASE,
)


def _cast_map_columns(query: str) -> str:
    """Wrap map-type columns with CAST when used with LIKE.

    CloudTrail Lake stores requestParameters and responseElements as
    map(varchar, varchar). LIKE requires varchar, so we automatically
    wrap them: requestParameters LIKE → CAST(requestParameters AS varchar) LIKE.
    """

    def _replacer(m: re.Match) -> str:
        col = m.group(1)
        op = m.group(2)
        return f"CAST({col} AS varchar) {op} "

    return _MAP_LIKE_RE.sub(_replacer, query)


def _parse_java_map(s: str) -> dict:
    """Parse Java-style map strings like '{key=value, key2=value2}' into a dict.

    CloudTrail Lake returns requestParameters and responseElements in this format
    instead of JSON. Handles simple top-level key=value pairs.
    """
    if not s or not isinstance(s, str):
        return {}
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    result = {}
    for pair in s.split(","):
        pair = pair.strip()
        if "=" in pair:
            key, _, value = pair.partition("=")
            result[key.strip()] = value.strip()
    return result


def _run_query(ct_client: Any, query: str, max_results: int) -> dict:
    """Start a CloudTrail Lake query, poll until complete, paginate results."""
    response = ct_client.start_query(QueryStatement=query)
    query_id = response["QueryId"]

    # Poll until query finishes (with timeout)
    bytes_scanned = 0
    elapsed = 0
    while True:
        result = ct_client.get_query_results(QueryId=query_id, MaxQueryResults=max_results)
        status = result["QueryStatus"]

        if status == "FINISHED":
            stats = result.get("QueryStatistics", {})
            bytes_scanned = stats.get("BytesScanned", 0)
            break
        elif status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            error_msg = result.get("ErrorMessage", status)
            return {"error": f"CloudTrail Lake query {status}: {error_msg}"}

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        if elapsed >= QUERY_TIMEOUT:
            logger.warning("CloudTrail Lake query timed out after %ds: %s", elapsed, query_id)
            return {
                "error": f"CloudTrail Lake query timed out after {elapsed}s. "
                "Try narrowing the eventTime range to reduce data scanned."
            }

    # Collect rows from first page
    all_rows = list(result.get("QueryResultRows", []))
    next_token = result.get("NextToken")

    # Paginate remaining results
    while next_token and len(all_rows) < max_results:
        result = ct_client.get_query_results(
            QueryId=query_id,
            NextToken=next_token,
            MaxQueryResults=min(max_results - len(all_rows), MAX_ROWS),
        )
        all_rows.extend(result.get("QueryResultRows", []))
        next_token = result.get("NextToken")

    # Parse CloudTrail Lake row format: each row is [{col: val}, {col: val}, ...]
    # First row is the header
    if not all_rows:
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "bytes_scanned": bytes_scanned,
        }

    # Extract column names from the header row
    header = all_rows[0]
    columns = [list(cell.keys())[0] for cell in header]

    # Parse data rows, auto-parsing Java-style maps in responseElements/requestParameters
    parsed_rows = []
    for row in all_rows[1:]:
        parsed = {}
        for cell in row:
            for key, value in cell.items():
                if (
                    key in ("responseElements", "requestParameters")
                    and isinstance(value, str)
                    and value.startswith("{")
                    and "=" in value
                ):
                    parsed[key] = _parse_java_map(value)
                else:
                    parsed[key] = value
        parsed_rows.append(parsed)

    # Enforce max rows
    truncated = len(parsed_rows) > MAX_ROWS
    if truncated:
        parsed_rows = parsed_rows[:MAX_ROWS]

    return {
        "columns": columns,
        "rows": parsed_rows,
        "row_count": len(parsed_rows),
        "bytes_scanned": bytes_scanned,
        "truncated": truncated,
    }


async def query_cloudtrail(query: str, max_results: int = 100) -> dict:
    """Query CloudTrail Lake for org-wide API events.

    Args:
        query: SQL query using FROM cloudtrail_events (substituted with real EDS ID).
        max_results: Max rows to return. Default: 100, max: 500.

    Returns:
        Dict with columns, rows, row_count, bytes_scanned.
    """
    try:
        # Validate: only SELECT queries allowed
        stripped = query.strip().lstrip("(")
        if not re.match(r"(?i)^SELECT\b", stripped):
            return {"error": "Only SELECT queries are allowed against CloudTrail Lake"}

        cfg = get_config()
        eds_id = cfg.cloudtrail.get("event_data_store_id", "")
        if not eds_id:
            return {"error": "CloudTrail event data store ID not configured"}

        # Substitute the placeholder table name with the actual EDS ARN
        # Agent writes: FROM cloudtrail_events — we replace with the real EDS ID
        query = re.sub(
            r"\bcloudtrail_events\b",
            eds_id,
            query,
            flags=re.IGNORECASE,
        )

        # Inject calendarday partition key and normalize eventTime format
        query = _inject_partition_key(query)

        # Wrap map-type columns (requestParameters, responseElements) with CAST
        # when used with LIKE — they are map(varchar, varchar), not varchar
        query = _cast_map_columns(query)

        max_results = min(max_results, MAX_ROWS)

        session = get_aws_session()
        ct_client = session.client("cloudtrail", region_name="us-east-1")

        return await asyncio.to_thread(_run_query, ct_client, query, max_results)

    except ClientError as e:
        logger.exception("CloudTrail Lake query failed")
        return {"error": f"CloudTrail Lake query failed: {e}"}
    except Exception as e:
        logger.exception("CloudTrail Lake query failed")
        return {"error": f"CloudTrail Lake query failed: {e}"}
