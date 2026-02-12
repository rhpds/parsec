"""Tool: query_provisions_db — execute read-only SQL against the provision DB."""

import logging
import re

from src.config import get_config
from src.connections.postgres import get_pool

logger = logging.getLogger(__name__)

# Only allow SELECT statements (no DDL, DML, or DCL)
_FORBIDDEN_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE|"
    r"DO|CALL|SET|RESET|DISCARD|LOAD|VACUUM|ANALYZE|CLUSTER|REINDEX|LOCK|"
    r"PREPARE|DEALLOCATE|LISTEN|NOTIFY|UNLISTEN)\b",
    re.IGNORECASE,
)


def validate_sql(sql: str) -> str | None:
    """Validate that SQL is a read-only SELECT. Returns error message or None."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return "Empty SQL statement"

    # Must start with SELECT or WITH (for CTEs)
    first_word = stripped.split()[0].upper()
    if first_word not in ("SELECT", "WITH"):
        return f"Only SELECT queries allowed, got: {first_word}"

    # Check for forbidden keywords
    match = _FORBIDDEN_PATTERN.search(stripped)
    if match:
        return f"Forbidden SQL keyword: {match.group()}"

    # Block semicolons mid-statement (injection attempt)
    if ";" in stripped:
        return "Multiple statements not allowed"

    return None


async def execute_query(sql: str) -> dict:
    """Execute a read-only SQL query and return results.

    Returns dict with keys: columns, rows, row_count, truncated.
    """
    error = validate_sql(sql)
    if error:
        return {"error": error}

    cfg = get_config()
    max_rows = cfg.provision_db.get("max_rows", 500)
    pool = get_pool()

    try:
        async with pool.acquire() as conn:
            # Set statement timeout for this connection
            timeout_ms = cfg.provision_db.get("statement_timeout_ms", 30000)
            await conn.execute(f"SET statement_timeout = {timeout_ms}")

            # Add LIMIT if not present to enforce row cap
            trimmed = sql.strip().rstrip(";")
            if "limit" not in trimmed.lower().split("order")[-1]:
                trimmed = f"{trimmed} LIMIT {max_rows + 1}"

            rows = await conn.fetch(trimmed)

            truncated = len(rows) > max_rows
            if truncated:
                rows = rows[:max_rows]

            if not rows:
                return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

            columns = list(rows[0].keys())
            result_rows = []
            for row in rows:
                result_rows.append({col: _serialize(row[col]) for col in columns})

            return {
                "columns": columns,
                "rows": result_rows,
                "row_count": len(result_rows),
                "truncated": truncated,
            }

    except Exception as e:
        logger.exception("DB query failed")
        return {"error": f"Query execution failed: {e}"}


def _serialize(value):
    """Convert DB values to JSON-safe types."""
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    # datetime, date, UUID, etc.
    return str(value)
