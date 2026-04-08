"""Tool: query_provisions_db — execute read-only SQL via the Reporting MCP.

All database access goes through the Reporting MCP server. Schema discovery,
domain knowledge, and investigation prompts are exposed as dynamically
discovered MCP tools (see src/connections/reporting_mcp.py).
"""

import logging
import re

from src.config import get_config

logger = logging.getLogger(__name__)

_FORBIDDEN_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE|"
    r"DO|CALL|SET|RESET|DISCARD|LOAD|VACUUM|ANALYZE|CLUSTER|REINDEX|LOCK|"
    r"PREPARE|DEALLOCATE|LISTEN|NOTIFY|UNLISTEN)\b",
    re.IGNORECASE,
)

_ROW_COUNT_PATTERN = re.compile(r"(\d+) rows? returned")


def validate_sql(sql: str) -> str | None:
    """Validate that SQL is a read-only SELECT. Returns error message or None."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return "Empty SQL statement"

    first_word = stripped.split()[0].upper()
    if first_word not in ("SELECT", "WITH"):
        return f"Only SELECT queries allowed, got: {first_word}"

    match = _FORBIDDEN_PATTERN.search(stripped)
    if match:
        return f"Forbidden SQL keyword: {match.group()}"

    if ";" in stripped:
        return "Multiple statements not allowed"

    return None


async def execute_query(sql: str) -> dict:
    """Execute a read-only SQL query via Reporting MCP."""
    error = validate_sql(sql)
    if error:
        return {"error": error}

    cfg = get_config()
    max_rows = cfg.provision_db.get("max_rows", 500)

    from src.connections.reporting_mcp import call_tool

    result = await call_tool(
        "query",
        {
            "sql": sql,
            "limit": max_rows,
            "output_format": "markdown",
        },
    )

    if "error" not in result:
        match = _ROW_COUNT_PATTERN.search(result.get("result", ""))
        if match:
            result["row_count"] = int(match.group(1))

    return result
