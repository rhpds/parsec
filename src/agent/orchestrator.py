"""Claude tool-use orchestrator — the core agent loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time as _time
import uuid
from collections.abc import AsyncGenerator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anthropic
import mlflow

from src.metrics.collector import MetricsCollector
from src.metrics.tracing import (
    SpanType,
    set_llm_span_outputs,
    set_root_span_outputs,
    set_tool_span_outputs,
)

if TYPE_CHECKING:
    from anthropic import AnthropicBedrock, AnthropicVertex

from src.agent.streaming import (
    sse_done,
    sse_error,
    sse_event,
    sse_report,
    sse_status,
    sse_text,
    sse_tool_result,
    sse_tool_start,
)
from src.agent.system_prompt import ALERT_INVESTIGATION_PROMPT
from src.agent.tool_definitions import SUBMIT_ALERT_VERDICT_TOOL
from src.config import get_config
from src.tools.aap2 import query_aap2
from src.tools.aws_account import query_aws_account
from src.tools.aws_accounts import query_aws_account_db
from src.tools.aws_capacity_manager import query_aws_capacity_manager
from src.tools.aws_costs import query_aws_costs
from src.tools.aws_pricing import query_aws_pricing
from src.tools.azure_costs import query_azure_costs
from src.tools.azure_pools import query_azure_pools
from src.tools.babylon import query_babylon_catalog
from src.tools.cloudtrail import query_cloudtrail
from src.tools.cost_monitor import query_cost_monitor
from src.tools.gcp_costs import query_gcp_costs
from src.tools.gcp_projects import query_gcp_projects
from src.tools.github_files import (
    fetch_github_file,
    lookup_catalog_item,
    search_agnosticv_prs,
    search_github_repo,
)
from src.tools.icinga import query_icinga
from src.tools.marketplace_agreements import query_marketplace_agreements
from src.tools.ocpv import query_ocpv_cluster
from src.tools.provision_db import execute_query
from src.tools.splunk import query_splunk

logger = logging.getLogger(__name__)

# Background tasks must be saved to prevent garbage collection (S7502)
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]

# Directory for saved reports (on shared PVC so all pods can serve them)
REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "reports"
)
os.makedirs(REPORTS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Tool result cache — per-request, keyed by (tool_name, canonical_input)
# ---------------------------------------------------------------------------

_tool_cache: ContextVar[dict[str, dict] | None] = ContextVar("_tool_cache", default=None)

_UNCACHEABLE_TOOLS = frozenset(
    {
        "render_chart",
        "generate_report",
        "query_icinga",
    }
)


def _cache_key(tool_name: str, tool_input: dict) -> str:
    return tool_name + ":" + json.dumps(tool_input, sort_keys=True, default=str)


def _is_reporting_mcp_tool(name: str) -> bool:
    """Check if a tool was dynamically discovered from the Reporting MCP."""
    from src.connections.reporting_mcp import is_mcp_tool

    return is_mcp_tool(name)


async def _execute_db_tool(tool_name: str, tool_input: dict) -> dict | None:
    """Dispatch database and Reporting MCP tools. Returns None if not handled."""
    if tool_name == "query_provisions_db":
        return await execute_query(tool_input["sql"])

    if tool_name == "db_read_knowledge":
        from src.connections import reporting_mcp

        domain = tool_input["domain"]
        return await reporting_mcp.read_resource(f"database://knowledge/{domain}")

    if tool_name == "db_get_prompt":
        from src.connections import reporting_mcp

        return await reporting_mcp.get_prompt(
            tool_input["prompt_name"],
            tool_input.get("arguments", {}),
        )

    if _is_reporting_mcp_tool(tool_name):
        from src.connections import reporting_mcp

        original_name = reporting_mcp.get_mcp_tool_original(tool_name)
        return await reporting_mcp.call_tool(original_name, tool_input)

    return None


async def _execute_cost_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch cost, pricing, and capacity tools."""
    if tool_name == "query_aws_costs":
        return await query_aws_costs(
            account_ids=tool_input["account_ids"],
            start_date=tool_input["start_date"],
            end_date=tool_input["end_date"],
            group_by=tool_input.get("group_by", "SERVICE"),
        )

    if tool_name == "query_azure_costs":
        return await asyncio.to_thread(
            query_azure_costs,
            start_date=tool_input["start_date"],
            end_date=tool_input["end_date"],
            subscription_names=tool_input.get("subscription_names"),
            meter_filter=tool_input.get("meter_filter"),
        )

    if tool_name == "query_gcp_costs":
        return await query_gcp_costs(
            start_date=tool_input["start_date"],
            end_date=tool_input["end_date"],
            group_by=tool_input.get("group_by", "SERVICE"),
            filter_services=tool_input.get("filter_services"),
            filter_projects=tool_input.get("filter_projects"),
        )

    if tool_name == "query_aws_pricing":
        return await query_aws_pricing(
            instance_type=tool_input["instance_type"],
            region=tool_input.get("region", "us-east-1"),
            os_type=tool_input.get("os_type", "Linux"),
        )

    if tool_name == "query_cost_monitor":
        return await query_cost_monitor(
            endpoint=tool_input["endpoint"],
            start_date=tool_input["start_date"],
            end_date=tool_input["end_date"],
            providers=tool_input.get("providers", ""),
            group_by=tool_input.get("group_by", ""),
            top_n=tool_input.get("top_n", 25),
            drilldown_type=tool_input.get("drilldown_type", ""),
            selected_key=tool_input.get("selected_key", ""),
        )

    # query_aws_capacity_manager
    return await query_aws_capacity_manager(
        metric=tool_input.get("metric", "utilization"),
        group_by=tool_input.get("group_by"),
        instance_type=tool_input.get("instance_type"),
        account_id=tool_input.get("account_id"),
        reservation_state=tool_input.get("reservation_state", "active"),
        hours=tool_input.get("hours", 168),
    )


async def _execute_cloud_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch cloud account and resource query tools."""
    if tool_name == "query_azure_pools":
        return await query_azure_pools(
            action=tool_input["action"],
            pool_id=tool_input.get("pool_id"),
            subscription_name=tool_input.get("subscription_name"),
            project_tag=tool_input.get("project_tag"),
            database=tool_input.get("database", "pools"),
            max_results=tool_input.get("max_results", 100),
        )

    if tool_name == "query_gcp_projects":
        return await query_gcp_projects(
            action=tool_input["action"],
            project_id=tool_input.get("project_id"),
            state_filter=tool_input.get("state_filter"),
            max_results=tool_input.get("max_results", 100),
        )

    if tool_name == "query_cloudtrail":
        return await query_cloudtrail(
            query=tool_input["query"],
            max_results=tool_input.get("max_results", 100),
        )

    if tool_name == "query_aws_account":
        return await query_aws_account(
            account_id=tool_input["account_id"],
            action=tool_input["action"],
            region=tool_input.get("region", "us-east-1"),
            filters=tool_input.get("filters"),
        )

    if tool_name == "query_aws_account_db":
        return await query_aws_account_db(
            name=tool_input.get("name"),
            account_id=tool_input.get("account_id"),
            available=tool_input.get("available"),
            owner=tool_input.get("owner"),
            zone=tool_input.get("zone"),
            envtype=tool_input.get("envtype"),
            reservation=tool_input.get("reservation"),
            max_results=tool_input.get("max_results", 100),
        )

    # query_marketplace_agreements
    return await query_marketplace_agreements(
        account_id=tool_input.get("account_id"),
        account_name=tool_input.get("account_name"),
        status=tool_input.get("status"),
        classification=tool_input.get("classification"),
        min_cost=tool_input.get("min_cost"),
        product_name=tool_input.get("product_name"),
        vendor_name=tool_input.get("vendor_name"),
        max_results=tool_input.get("max_results", 100),
    )


async def _execute_infra_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch infrastructure platform tools (Babylon, OCPV, AAP2, Splunk, Icinga)."""
    if tool_name == "query_babylon_catalog":
        return await query_babylon_catalog(
            action=tool_input["action"],
            cluster=tool_input.get("cluster", ""),
            name=tool_input.get("name", ""),
            search=tool_input.get("search", ""),
            namespace=tool_input.get("namespace", ""),
            sandbox_comment=tool_input.get("sandbox_comment", ""),
            env_type=tool_input.get("env_type", ""),
            account_id=tool_input.get("account_id", ""),
            guid=tool_input.get("guid", ""),
            max_results=tool_input.get("max_results", 50),
        )

    if tool_name == "query_ocpv_cluster":
        return await query_ocpv_cluster(
            action=tool_input["action"],
            cluster=tool_input.get("cluster", ""),
            namespace=tool_input.get("namespace", ""),
            name=tool_input.get("name", ""),
            search=tool_input.get("search", ""),
            sandbox_comment=tool_input.get("sandbox_comment", ""),
            max_results=tool_input.get("max_results", 50),
        )

    if tool_name == "query_aap2":
        return await query_aap2(
            action=tool_input["action"],
            controller=tool_input.get("controller", ""),
            job_id=tool_input.get("job_id"),
            failed_only=tool_input.get("failed_only", False),
            changed_only=tool_input.get("changed_only", False),
            status=tool_input.get("status", ""),
            created_after=tool_input.get("created_after", ""),
            created_before=tool_input.get("created_before", ""),
            template_name=tool_input.get("template_name", ""),
            max_results=tool_input.get("max_results", 50),
        )

    if tool_name == "query_splunk":
        return await query_splunk(
            action=tool_input["action"],
            guid=tool_input.get("guid", ""),
            namespace=tool_input.get("namespace", ""),
            cluster_name=tool_input.get("cluster_name", ""),
            controller=tool_input.get("controller", ""),
            search_terms=tool_input.get("search_terms", ""),
            earliest=tool_input.get("earliest", "-24h"),
            latest=tool_input.get("latest", "now"),
            errors_only=tool_input.get("errors_only", False),
            raw_query=tool_input.get("raw_query", ""),
            max_results=tool_input.get("max_results", 200),
        )

    # query_icinga
    return await query_icinga(
        action=tool_input["action"],
        search=tool_input.get("search", ""),
        host=tool_input.get("host", ""),
        service=tool_input.get("service", ""),
        filter_expr=tool_input.get("filter_expr", ""),
        detailed=tool_input.get("detailed", False),
        object_type=tool_input.get("object_type", ""),
        name=tool_input.get("name", ""),
        author=tool_input.get("author", "parsec"),
        comment=tool_input.get("comment", ""),
        comment_name=tool_input.get("comment_name", ""),
        start_time=tool_input.get("start_time"),
        end_time=tool_input.get("end_time"),
    )


async def _execute_github_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch GitHub and source code tools."""
    if tool_name == "fetch_github_file":
        return await fetch_github_file(
            owner=tool_input["owner"],
            repo=tool_input["repo"],
            path=tool_input["path"],
            ref=tool_input.get("ref", ""),
        )

    if tool_name == "lookup_catalog_item":
        return await lookup_catalog_item(
            search=tool_input["search"],
        )

    if tool_name == "search_github_repo":
        return await search_github_repo(
            owner=tool_input["owner"],
            repo=tool_input["repo"],
            search=tool_input["search"],
            ref=tool_input.get("ref", ""),
        )

    # search_agnosticv_prs
    return await search_agnosticv_prs(
        search=tool_input["search"],
        state=tool_input.get("state", "open"),
        max_results=tool_input.get("max_results", 10),
    )


# Tool name sets for dispatcher routing
_COST_TOOLS = frozenset(
    {
        "query_aws_costs",
        "query_azure_costs",
        "query_gcp_costs",
        "query_aws_pricing",
        "query_cost_monitor",
        "query_aws_capacity_manager",
    }
)

_CLOUD_TOOLS = frozenset(
    {
        "query_azure_pools",
        "query_gcp_projects",
        "query_cloudtrail",
        "query_aws_account",
        "query_aws_account_db",
        "query_marketplace_agreements",
    }
)

_INFRA_TOOLS = frozenset(
    {
        "query_babylon_catalog",
        "query_ocpv_cluster",
        "query_aap2",
        "query_splunk",
        "query_icinga",
    }
)

_GITHUB_TOOLS = frozenset(
    {
        "fetch_github_file",
        "lookup_catalog_item",
        "search_github_repo",
        "search_agnosticv_prs",
    }
)


async def _execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch a tool call to the appropriate domain handler."""
    # DB and MCP tools (checked first due to dynamic name matching)
    db_result = await _execute_db_tool(tool_name, tool_input)
    if db_result is not None:
        return db_result

    if tool_name in _COST_TOOLS:
        return await _execute_cost_tool(tool_name, tool_input)

    if tool_name in _CLOUD_TOOLS:
        return await _execute_cloud_tool(tool_name, tool_input)

    if tool_name in _INFRA_TOOLS:
        return await _execute_infra_tool(tool_name, tool_input)

    if tool_name in _GITHUB_TOOLS:
        return await _execute_github_tool(tool_name, tool_input)

    if tool_name == "render_chart":
        # Charts are rendered client-side — just return the input as-is
        return tool_input

    if tool_name == "generate_report":
        return _save_report(tool_input)

    return {"error": f"Unknown tool: {tool_name}"}


def _save_report(tool_input: dict) -> dict:
    """Save a report to disk and return metadata.

    ``filename`` is model-controlled (``generate_report`` is in every agent's
    tool list), so it is reduced to a bare basename and the resolved path is
    asserted to stay inside ``REPORTS_DIR`` — the same containment the download
    route already applies (``src/routes/query.py``).

    Without it this is an arbitrary-write primitive: ``filename`` was joined
    onto ``REPORTS_DIR`` unsanitised, and the extension is forced to
    ``.md``/``.adoc``, so ``"../../skills/icinga-triage/SKILL"`` resolves to
    ``/app/skills/icinga-triage/SKILL.md``. ``dockerfiles/Dockerfile`` symlinks
    ``/app/skills`` to ``/app/.claude/skills``, which is the Agent SDK's only
    skill-discovery root, and ``/app`` is group-0 writable for the arbitrary
    OpenShift UID — so a model could persist instructions into every later SDK
    run. The download path was hardened in ce9bbc8; the write path was not.
    """
    title = tool_input["title"]
    content = tool_input["content"]
    fmt = tool_input.get("format", "markdown")
    filename = tool_input.get("filename", "")

    ext = ".adoc" if fmt == "asciidoc" else ".md"
    # Strip any directory component the model supplied before using the name.
    stem = os.path.basename(str(filename)).strip()
    if not stem or stem in {".", ".."}:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        stem = f"investigation_report_{date_str}"

    full_filename = f"{stem}{ext}"
    reports_root = os.path.realpath(REPORTS_DIR)
    filepath = os.path.realpath(os.path.join(reports_root, full_filename))
    # Defence in depth: basename() already blocks traversal, but this also
    # catches a symlink planted inside REPORTS_DIR that points elsewhere.
    if not filepath.startswith(reports_root + os.sep):
        logger.warning("Rejected report write outside REPORTS_DIR: %r", filename)
        return {"error": "Invalid filename"}

    with open(filepath, "w") as f:
        f.write(content)

    logger.info("Report saved: %s", filepath)

    return {
        "filename": full_filename,
        "format": fmt,
        "title": title,
        "path": filepath,
        "size_bytes": len(content.encode("utf-8")),
    }


def _build_client(cfg) -> anthropic.Anthropic | AnthropicVertex | AnthropicBedrock:
    """Build the appropriate Anthropic client based on config.

    Supports three backends:
    - vertex: Claude via Google Vertex AI (uses GCP credentials)
    - bedrock: Claude via AWS Bedrock
    - api: Direct Anthropic API (default)
    """
    backend = cfg.anthropic.get("backend", "api")

    if backend == "vertex":
        from anthropic import AnthropicVertex

        project_id = cfg.anthropic.get("vertex_project_id", "") or cfg.gcp.get("project_id", "")
        region = cfg.anthropic.get("vertex_region", "us-east5")
        if not project_id:
            raise ValueError(
                "anthropic.vertex_project_id or gcp.project_id required for Vertex backend"
            )

        # Use explicit SA credentials if provided, otherwise fall back to ADC
        creds_path = cfg.anthropic.get("vertex_credentials_path", "")
        kwargs = {"project_id": project_id, "region": region}
        if creds_path and os.path.isfile(creds_path):
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            kwargs["credentials"] = credentials
            logger.info(
                "Using Vertex AI backend (project=%s, region=%s, sa=%s)",
                project_id,
                region,
                creds_path,
            )
        else:
            logger.info("Using Vertex AI backend (project=%s, region=%s, ADC)", project_id, region)

        return AnthropicVertex(**kwargs)

    elif backend == "bedrock":
        from anthropic import AnthropicBedrock

        region = cfg.anthropic.get("bedrock_region", cfg.aws.get("region", "us-east-1"))
        logger.info("Using Bedrock backend (region=%s)", region)
        return AnthropicBedrock(aws_region=region)

    else:
        api_key = cfg.anthropic.get("api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not configured")
        logger.info("Using direct Anthropic API backend")
        return anthropic.Anthropic(api_key=api_key)


def _estimate_tokens(obj) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(json.dumps(obj, default=str)) // 4


MAX_TOOL_RESULT_CHARS = 100_000


def _try_smart_truncation(data: dict) -> str | None:
    """Try to intelligently truncate a dict result by capping list fields.

    Returns truncated JSON string if successful, or None if still too large.
    """
    for key in ("rows", "results", "items", "subscriptions", "projects", "events"):
        if key in data and isinstance(data[key], list) and len(data[key]) > 20:
            original_len = len(data[key])
            data[key] = data[key][:20]
            data["_truncated"] = f"{key} capped from {original_len} to 20 items"
            capped = json.dumps(data, default=str)
            if len(capped) <= MAX_TOOL_RESULT_CHARS:
                return capped

    if "result" in data and isinstance(data["result"], str) and len(data["result"]) > 10_000:
        data["result"] = data["result"][:10_000] + "\n... [truncated]"
        data["_truncated"] = "result field capped at 10000 chars"
        capped = json.dumps(data, default=str)
        if len(capped) <= MAX_TOOL_RESULT_CHARS:
            return capped

    return None


def _cap_tool_result(result_str: str) -> str:
    """Cap a JSON tool result string to prevent blowing the context window.

    Tries to intelligently truncate list fields first; falls back to hard
    truncation if the result is still too large.
    """
    if len(result_str) <= MAX_TOOL_RESULT_CHARS:
        return result_str

    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str[:MAX_TOOL_RESULT_CHARS] + "\n... [truncated — result too large]"

    if isinstance(data, dict):
        smart = _try_smart_truncation(data)
        if smart is not None:
            return smart

    return (
        json.dumps(data, default=str)[:MAX_TOOL_RESULT_CHARS]
        + "\n... [truncated — result too large]"
    )


def _truncate_tool_result_content(block: dict) -> None:
    """Truncate a large tool_result content block in-place for context management."""
    result_str = block.get("content", "")
    if not isinstance(result_str, str) or len(result_str) <= 2000:
        return

    try:
        result_data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        block["content"] = result_str[:2000] + "... [truncated]"
        return

    if not isinstance(result_data, dict):
        block["content"] = result_str[:2000] + "... [truncated]"
        return

    if "rows" in result_data:
        result_data["rows"] = result_data["rows"][:5]
        result_data["_truncated_for_context"] = True
    if "results" in result_data and isinstance(result_data["results"], list):
        result_data["results"] = result_data["results"][:5]
        result_data["_truncated_for_context"] = True
    if (
        "result" in result_data
        and isinstance(result_data["result"], str)
        and len(result_data["result"]) > 2000
    ):
        result_data["result"] = result_data["result"][:2000] + "\n... [truncated]"
        result_data["_truncated_for_context"] = True
    block["content"] = json.dumps(result_data)


_TRUNCATION_SUFFIX = "\n\n[Earlier analysis truncated — use current tool results only]"


def _truncate_old_content_block(block: dict) -> None:
    """Truncate a single content block in an older history turn (in-place)."""
    btype = block.get("type")
    if btype == "tool_result":
        _truncate_tool_result_content(block)
    elif btype == "text":
        text = block.get("text", "")
        if isinstance(text, str) and len(text) > 3000:
            block["text"] = text[:3000] + _TRUNCATION_SUFFIX


def _truncate_old_message(msg: dict) -> None:
    """Truncate large content in an older history message in-place.

    Truncates both tool results and assistant text symmetrically to prevent
    the model from anchoring on stale analysis when supporting data is gone.
    """
    content = msg.get("content")

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                _truncate_old_content_block(block)

    if msg.get("role") == "assistant" and isinstance(content, str) and len(content) > 3000:
        msg["content"] = content[:3000] + _TRUNCATION_SUFFIX


def _drop_oldest_turn(messages: list) -> None:
    """Drop the oldest user+assistant pair (and any orphaned tool_result) in-place."""
    messages.pop(0)
    if messages and messages[0].get("role") == "assistant":
        messages.pop(0)
    # Remove orphaned tool_result that followed the dropped assistant message
    if messages and messages[0].get("role") == "user":
        content = messages[0].get("content")
        if isinstance(content, list) and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            messages.pop(0)


def _trim_history(history: list, max_tokens: int = 150000) -> list:
    """Trim conversation history to fit within token limits.

    Keeps the most recent turns. Truncates large tool results AND large
    assistant text in older turns — both sides must be trimmed symmetrically
    to prevent the model from anchoring on stale analysis text when the
    supporting tool data has been stripped.
    """
    if not history:
        return []

    messages = list(history)

    # If under limit, return as-is
    if _estimate_tokens(messages) <= max_tokens:
        return messages

    # First pass: truncate old messages (keep last 2 turns = 4 messages intact)
    for msg in messages[:-4]:
        _truncate_old_message(msg)

    # If still over, drop oldest turns
    while len(messages) > 2 and _estimate_tokens(messages) > max_tokens:
        _drop_oldest_turn(messages)

    return messages


def _clean_content_block(block) -> dict:
    """Serialize a content block, keeping only fields the API accepts.

    The Anthropic SDK preserves unknown fields (e.g. ``caller: null``) from
    API responses via Pydantic's ``extra='allow'``.  Sending those back in
    subsequent requests causes 400 errors.  Handles both SDK model objects
    and plain dicts (from cached frontend history).
    """
    if hasattr(block, "model_dump"):
        d = block.model_dump()
    elif isinstance(block, dict):
        d = block
    else:
        return {"type": "text", "text": str(block)}

    btype = d.get("type")
    if btype == "tool_use":
        return {"type": "tool_use", "id": d["id"], "name": d["name"], "input": d["input"]}
    if btype == "text":
        return {"type": "text", "text": d["text"]}
    if btype == "tool_result":
        cleaned: dict = {"type": "tool_result", "tool_use_id": d["tool_use_id"]}
        if "content" in d:
            cleaned["content"] = d["content"]
        if d.get("is_error"):
            cleaned["is_error"] = d["is_error"]
        return cleaned
    return d


def _serialize_content_block(block) -> dict:
    """Serialize a single content block (SDK object or dict) to a clean dict."""
    if isinstance(block, dict):
        return _clean_content_block(block)
    if hasattr(block, "model_dump"):
        return _clean_content_block(block.model_dump())
    if hasattr(block, "to_dict"):
        return _clean_content_block(block.to_dict())
    return {"type": "text", "text": str(block)}


def _serialize_messages(messages: list) -> list:
    """Serialize the messages array to JSON-safe dicts.

    Claude API content blocks are SDK objects — convert them to dicts
    so the frontend can store and resend them.
    """
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            result.append({"role": role, "content": content})
        elif isinstance(content, list):
            result.append({"role": role, "content": [_serialize_content_block(b) for b in content]})
        else:
            # SDK content object list (from response.content)
            try:
                serialized = [_serialize_content_block(b) for b in content]
                result.append({"role": role, "content": serialized})
            except TypeError:
                result.append({"role": role, "content": str(content)})

    return result


def _dump_api_request(
    label: str,
    system: str,
    messages: list,
    tools: list,
    model: str,
) -> None:
    """Write the full API request payload to data/debug/ for prompt inspection.

    Only runs when debug.dump_prompts is enabled in config.
    """
    cfg = get_config()
    if not cfg.get("debug", {}).get("dump_prompts", False):
        return

    debug_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "debug"
    )
    os.makedirs(debug_dir, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    safe_label = re.sub(r"[^a-z0-9_-]", "", label.replace(" ", "_").lower())
    filepath = os.path.join(debug_dir, f"{safe_label}_{ts}.json")

    try:
        payload = {
            "label": label,
            "model": model,
            "system_prompt_length": len(system),
            "system": system,
            "messages": _serialize_messages(messages),
            "tools": tools,
            "message_count": len(messages),
        }
        with open(filepath, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        logger.info("Prompt dump written: %s", filepath)
    except Exception:
        logger.exception("Failed to dump prompt to %s", filepath)


_DELEGATION_TOOL_MAP = {
    "investigate_costs": "cost",
    "investigate_aap2_job": "aap2",
    "investigate_babylon": "babylon",
    "investigate_security": "security",
    "investigate_ocpv": "ocpv",
    "investigate_icinga": "icinga",
}


async def _handle_delegation(
    agent_type: str,
    tool_block: Any,
    tool_input: dict,
    client: Any,
    incoming_history: list,
    collector: MetricsCollector | None = None,
) -> AsyncGenerator[tuple[str | None, dict | None], None]:
    """Run a sub-agent delegation, yielding (sse_event, None) or (None, tool_result) at the end."""
    from src.agent.agents import AGENTS, run_sub_agent
    from src.agent.streaming import sse_agent_done, sse_agent_start

    agent_cfg = AGENTS.get(agent_type)
    agent_name = agent_cfg.name if agent_cfg else agent_type

    logger.info(
        "Orchestrator delegating to %s agent: %s",
        agent_type,
        tool_input.get("task", "")[:120],
    )

    agent_span_ctx = mlflow.start_span(
        name=f"agent:{agent_name}",
        span_type=SpanType.AGENT,
    )
    agent_span = agent_span_ctx.__enter__()
    agent_span.set_inputs({"agent_type": agent_type, "task": tool_input.get("task", "")[:500]})
    result: dict = {"status": "error", "tool_calls": 0, "duration_seconds": 0}
    delegation_status = "error"

    try:
        yield sse_agent_start(agent_type, agent_name), None
        yield sse_tool_start(tool_block.name, tool_input), None

        event_queue: asyncio.Queue[str] = asyncio.Queue()

        sub_task = asyncio.create_task(
            run_sub_agent(
                agent_type=agent_type,
                task=tool_input.get("task", ""),
                context=tool_input.get("context"),
                client=client,
                event_queue=event_queue,
                conversation_history=incoming_history,
            )
        )

        while not sub_task.done():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                yield event, None
            except TimeoutError:
                pass

        while not event_queue.empty():
            yield event_queue.get_nowait(), None

        try:
            result = sub_task.result()
            delegation_status = result.get("status", "error")
        except Exception as e:
            logger.exception("Sub-agent %s delegation failed", agent_type)
            result = {
                "agent": agent_type,
                "status": "error",
                "summary": f"Sub-agent failed: {e}",
                "tool_calls": 0,
                "duration_seconds": 0,
            }

        if collector:
            collector.record_sub_agent_result(
                agent_type=agent_type,
                duration_seconds=result.get("duration_seconds", 0),
                tool_calls=result.get("tool_calls", 0),
                tool_errors=result.get("tool_errors", 0),
                rounds_used=result.get("rounds_used", 0),
                max_rounds=AGENTS[agent_type].max_rounds if agent_type in AGENTS else 0,
                status=result.get("status", "error"),
            )

        logger.info(
            "Sub-agent %s delegation complete: status=%s tool_calls=%d duration=%.1fs",
            agent_type,
            result.get("status"),
            result.get("tool_calls", 0),
            result.get("duration_seconds", 0),
        )

        yield (
            sse_tool_result(
                tool_block.name,
                {
                    "agent": result.get("agent"),
                    "status": result.get("status"),
                    "summary": result.get("summary", "")[:2000],
                    "tool_calls": result.get("tool_calls", 0),
                    "duration_seconds": result.get("duration_seconds", 0),
                },
            ),
            None,
        )
        yield sse_agent_done(agent_type), None

        yield (
            None,
            {
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": json.dumps(
                    {
                        "agent": result.get("agent"),
                        "status": result.get("status"),
                        "summary": result.get("summary", ""),
                        "findings": result.get("findings", []),
                        "tool_calls": result.get("tool_calls", 0),
                        "duration_seconds": result.get("duration_seconds", 0),
                        "note": (
                            "The detailed analysis above was already streamed "
                            "to the user. Do NOT repeat or re-summarize it. "
                            "Only add brief follow-up suggestions or source "
                            "citations if useful."
                        ),
                    }
                ),
            },
        )
    finally:
        agent_span.set_outputs(
            {
                "status": delegation_status,
                "tool_calls_count": result.get("tool_calls", 0),
                "duration_seconds": result.get("duration_seconds", 0),
            }
        )
        agent_span_ctx.__exit__(*sys.exc_info())


def _check_tool_cache(tool_name: str, tool_input: dict) -> tuple[dict | None, bool]:
    """Check tool cache for a cached result. Returns (result, True) or (None, False)."""
    cache = _tool_cache.get(None)
    if cache is None or tool_name in _UNCACHEABLE_TOOLS:
        return None, False
    key = _cache_key(tool_name, tool_input)
    if key in cache:
        logger.info("Cache hit for %s (direct tool)", tool_name)
        return cache[key], True
    return None, False


def _store_tool_cache(tool_name: str, tool_input: dict, result: dict) -> None:
    """Store a tool result in the cache if applicable."""
    cache = _tool_cache.get(None)
    if cache is not None and tool_name not in _UNCACHEABLE_TOOLS and "error" not in result:
        cache[_cache_key(tool_name, tool_input)] = result


def _yield_output_events(tool_name: str, result: dict) -> list[str]:
    """Build any extra SSE events for output-producing tools (reports, charts)."""
    events: list[str] = []
    if tool_name == "generate_report" and "error" not in result:
        download_url = f"/api/reports/{result['filename']}"
        events.append(sse_report(result["filename"], result["format"], download_url))
    elif tool_name == "render_chart" and "error" not in result:
        events.append(sse_event("chart", result))
    return events


async def _handle_direct_tool(
    tool_block: Any,
    tool_input: dict,
) -> AsyncGenerator[tuple[str | None, dict | None], None]:
    """Execute a direct tool call, yielding (sse_event, None) or (None, tool_result)."""
    tool_name = tool_block.name
    yield sse_tool_start(tool_name, tool_input), None

    tool_start = _time.monotonic()
    cached_result, cached = _check_tool_cache(tool_name, tool_input)

    if cached and cached_result is not None:
        result = cached_result
        yield sse_event("cache_hit", {"tool": tool_name}), None
    else:
        # Execute with progress reporting
        task = asyncio.create_task(_execute_tool(tool_name, tool_input))
        elapsed = 0
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=10)
            if not done:
                elapsed += 10
                yield sse_status(f"Processing {tool_name}... ({elapsed}s)"), None
        result = task.result()
        _store_tool_cache(tool_name, tool_input, result)

    tool_duration_ms = (_time.monotonic() - tool_start) * 1000

    # Span records metadata only (tool already executed above with
    # async progress polling); actual duration is in duration_ms attr.
    with mlflow.start_span(
        name=f"tool:{tool_name}",
        span_type=SpanType.TOOL,
    ) as tool_span:
        set_tool_span_outputs(
            tool_span,
            tool_name=tool_name,
            tool_input=tool_input,
            result=result if "error" not in result else None,
            error=result.get("error") if "error" in result else None,
            duration_ms=tool_duration_ms,
            cached=cached,
        )

    yield sse_tool_result(tool_name, result), None

    for evt in _yield_output_events(tool_name, result):
        yield evt, None

    yield (
        None,
        {
            "type": "tool_result",
            "tool_use_id": tool_block.id,
            "content": _cap_tool_result(json.dumps(result)),
        },
    )


def _flush_collector(collector: MetricsCollector) -> None:
    """Stop collector timer and schedule async flush to MLflow."""
    collector.stop_timer()
    task = asyncio.create_task(collector.flush_to_mlflow())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _parse_response_blocks(
    response_content: list,
) -> tuple[list[str], list]:
    """Separate response content into text parts and tool_use blocks."""
    text_parts: list[str] = []
    tool_use_blocks: list = []
    for block in response_content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_use_blocks.append(block)
    return text_parts, tool_use_blocks


def _record_llm_span(
    llm_span,
    round_num: int,
    response,
    default_model: str,
    response_text: str,
    tool_use_names: list[str],
) -> None:
    """Record LLM span outputs with safe attribute access on the response."""
    set_llm_span_outputs(
        llm_span,
        round_num=round_num,
        model=response.model if hasattr(response, "model") else default_model,
        input_tokens=response.usage.input_tokens if hasattr(response, "usage") else 0,
        output_tokens=response.usage.output_tokens if hasattr(response, "usage") else 0,
        response_text=response_text,
        tool_use_names=tool_use_names,
    )


def _record_usage(collector: MetricsCollector, response, model: str) -> None:
    """Record token usage and model info from an API response."""
    if not hasattr(response, "usage"):
        return
    collector.record_tokens(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
    )
    if not collector.model:
        collector.record_model(response.model)
        collector.record_agent_dispatch("orchestrator", routing_method="llm")


def _extract_text_from_sse(event: str) -> str:
    """Extract text content from an SSE text event. Returns empty string if not a text event."""
    if not event.startswith("event: text\n"):
        return ""
    try:
        data_line = event.split("data: ", 1)[1].strip()
        return json.loads(data_line).get("content", "")
    except (IndexError, json.JSONDecodeError, AttributeError):
        return ""


async def _poll_api_progress(
    api_task: asyncio.Task, status_prefix: str
) -> AsyncGenerator[str, None]:
    """Poll an API task and yield SSE status updates at 10-second intervals."""
    elapsed = 0
    while not api_task.done():
        await asyncio.sleep(10)
        if not api_task.done():
            elapsed += 10
            yield sse_status(f"{status_prefix}... ({elapsed}s)")


async def _dispatch_tool_blocks(
    tool_use_blocks: list,
    client: Any,
    incoming_history: list,
    collector: MetricsCollector | None = None,
) -> AsyncGenerator[tuple[str | None, dict | None], None]:
    """Dispatch each tool block to the appropriate handler, yielding SSE events and results."""
    for tool_block in tool_use_blocks:
        agent_type = _DELEGATION_TOOL_MAP.get(tool_block.name)

        if agent_type:
            handler = _handle_delegation(
                agent_type,
                tool_block,
                tool_block.input,
                client,
                incoming_history,
                collector=collector,
            )
        else:
            handler = _handle_direct_tool(tool_block, tool_block.input)

        async for sse_evt, tool_result in handler:
            yield sse_evt, tool_result


_MAX_ROUNDS_TEXT = (
    "\n\nI've used all my planned tool calls but haven't finished. "
    "Would you like me to keep going?\n\n"
    "{{choices}}\n"
    "Keep investigating\n"
    "That's enough, thanks\n"
    "{{/choices}}"
)


async def run_agent(
    question: str,
    conversation_history: list | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Run the orchestrator agent loop and yield SSE events.

    1. Fast-path: if the query clearly maps to one domain, run that sub-agent
       directly as the top-level agent (no orchestrator LLM call).
    2. Otherwise: run the orchestrator agent which can call delegation tools
       (investigate_costs, investigate_aap2_job, investigate_security) plus
       direct tools (query_provisions_db, query_aws_account_db, render_chart,
       generate_report).
    """
    from src.agent.agents import AGENTS, classify_fast, run_sub_agent_streaming
    from src.agent.system_prompt import get_agent_prompt, get_prompt_files
    from src.agent.tool_definitions import get_orchestrator_tools
    from src.metrics.collector import MetricsCollector

    orchestrator_tools = get_orchestrator_tools()

    logger.info(
        "run_agent: question=%s session_id=%s conversation_id=%s",
        question[:120],
        session_id,
        conversation_id,
    )

    session_id = session_id or conversation_id or str(uuid.uuid4())

    _tool_cache.set({})

    collector = MetricsCollector(conversation_id=session_id)
    collector.start_timer()

    cfg = get_config()
    response_parts: list[str] = []

    # Root MLflow trace span — manually managed for async generator compatibility
    span_ctx = mlflow.start_span(name="parsec:orchestrator", span_type=SpanType.CHAIN)
    root_span = span_ctx.__enter__()
    root_span.set_inputs({"question": question})
    with contextlib.suppress(Exception):
        mlflow.update_current_trace(
            client_request_id=session_id,
            metadata={"mlflow.trace.session": session_id},
        )

    try:
        # Fast-path: skip orchestrator for obvious single-domain queries
        fast_agent = classify_fast(question)
        if fast_agent and fast_agent in AGENTS:
            logger.info("Fast-path routing to %s agent", fast_agent)
            collector.record_agent_dispatch(fast_agent, routing_method="fast-path")
            root_span.set_attribute("routing_method", "fast-path")
            root_span.set_attribute("agent_type", fast_agent)
            async for event in run_sub_agent_streaming(
                agent_type=fast_agent,
                task=question,
                conversation_history=conversation_history,
                metrics=collector,
            ):
                text = _extract_text_from_sse(event)
                if text:
                    response_parts.append(text)
                yield event
            _flush_collector(collector)
            return

        # Full orchestrator mode
        model = cfg.anthropic.get("orchestrator_model", "") or cfg.anthropic.get(
            "model", "claude-sonnet-4-20250514"
        )
        max_tokens = cfg.anthropic.get("max_tokens", 4096)
        max_rounds = cfg.anthropic.get("max_tool_rounds", 10)

        try:
            client = _build_client(cfg)
        except ValueError as e:
            yield sse_error(str(e))
            _flush_collector(collector)
            yield sse_done()
            return

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        system = f"{get_agent_prompt('orchestrator')}\n\nToday's date is {today}."

        incoming_history = conversation_history or []
        messages = _serialize_messages(_trim_history(incoming_history))
        logger.info(
            "Orchestrator loop: %d history messages received, %d after trim",
            len(incoming_history),
            len(messages),
        )
        messages.append({"role": "user", "content": question})

        root_span.set_inputs(
            {
                "question": question,
                "prompt_files": get_prompt_files("orchestrator"),
                "tool_names": [t.get("name", "unknown") for t in orchestrator_tools],
                "tool_count": len(orchestrator_tools),
                "message_count": len(messages),
            }
        )

        for _round in range(max_rounds):
            _dump_api_request(
                f"orchestrator_round_{_round}",
                system,
                messages,
                orchestrator_tools,
                model,
            )

            try:

                def _call_api() -> anthropic.types.Message:
                    return client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        tools=orchestrator_tools,  # type: ignore[arg-type]
                        messages=messages,
                    )

                with mlflow.start_span(
                    name=f"orchestrator_round_{_round}",
                    span_type=SpanType.LLM,
                ) as llm_span:
                    yield sse_status("Orchestrator analyzing...")
                    api_task: asyncio.Task[anthropic.types.Message] = asyncio.ensure_future(
                        asyncio.to_thread(_call_api)
                    )
                    async for status in _poll_api_progress(api_task, "Orchestrator analyzing"):
                        yield status
                    response = api_task.result()
                    _record_usage(collector, response, model)

                    response_text_parts, tool_use_blocks = _parse_response_blocks(response.content)
                    _record_llm_span(
                        llm_span,
                        _round,
                        response,
                        model,
                        "\n".join(response_text_parts),
                        [b.name for b in tool_use_blocks],
                    )

                # Yield text outside the LLM span
                for text in response_text_parts:
                    yield sse_text(text)
                    response_parts.append(text)

            except anthropic.APIError as e:
                logger.exception("Claude API error in orchestrator")
                yield sse_error(f"Claude API error: {e}")
                _flush_collector(collector)
                yield sse_done()
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": [_clean_content_block(b) for b in response.content],
                }
            )

            if not tool_use_blocks:
                yield sse_event("history", {"messages": _serialize_messages(messages)})
                _flush_collector(collector)
                yield sse_done()
                return

            tool_results = []
            async for sse_evt, tool_result in _dispatch_tool_blocks(
                tool_use_blocks, client, incoming_history, collector
            ):
                if sse_evt is not None:
                    yield sse_evt
                if tool_result is not None:
                    tool_results.append(tool_result)

            messages.append({"role": "user", "content": tool_results})
            yield sse_event("history", {"messages": _serialize_messages(messages)})

        yield sse_text(_MAX_ROUNDS_TEXT)
        messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": _MAX_ROUNDS_TEXT}]}
        )
        yield sse_event("history", {"messages": _serialize_messages(messages)})
        _flush_collector(collector)
        yield sse_done()

    finally:
        set_root_span_outputs(
            root_span,
            response_text="\n".join(response_parts),
            agent_type=collector.agent_type or "orchestrator",
            routing_method=collector.routing_method or "llm",
            model=collector.model,
            total_input_tokens=collector.input_tokens,
            total_output_tokens=collector.output_tokens,
        )
        span_ctx.__exit__(*sys.exc_info())


def _build_alert_user_message(
    alert_type: str,
    account_id: str,
    alert_text: str,
    account_name: str = "",
    user_arn: str = "",
    event_time: str = "",
    region: str = "",
    event_details: dict | None = None,
) -> str:
    """Construct the user message for an alert investigation."""
    parts = [
        "Investigate this alert and submit your verdict.\n",
        f"**Alert type:** {alert_type}",
        f"**Account ID:** {account_id}",
    ]
    if account_name:
        parts.append(f"**Account name:** {account_name}")
    if user_arn:
        parts.append(f"**User ARN:** {user_arn}")
    if event_time:
        parts.append(f"**Event time:** {event_time}")
    if region:
        parts.append(f"**Region:** {region}")
    parts.append(f"\n**Alert text:**\n{alert_text}")
    if event_details:
        details_str = json.dumps(event_details, default=str)
        parts.append(f"\n**Event details:**\n```json\n{details_str}\n```")
    return "\n".join(parts)


async def _process_alert_tool_call(
    tool_block: Any,
    investigation_log: list[str],
) -> tuple[dict | None, dict]:
    """Execute a single tool call during alert investigation.

    Returns (verdict_or_none, tool_result_dict).
    """
    tool_name = tool_block.name
    tool_input = tool_block.input

    if tool_name == "submit_alert_verdict":
        verdict = {
            "should_alert": tool_input.get("should_alert", True),
            "severity": tool_input.get("severity", "medium"),
            "summary": tool_input.get("summary", ""),
        }
        investigation_log.append(
            f"[Verdict] should_alert={verdict['should_alert']}, "
            f"severity={verdict['severity']}: {verdict['summary']}"
        )
        tool_result = {
            "type": "tool_result",
            "tool_use_id": tool_block.id,
            "content": json.dumps({"status": "verdict_recorded"}),
        }
        return verdict, tool_result

    investigation_log.append(
        f"[Tool: {tool_name}] input={json.dumps(tool_input, default=str)[:200]}"
    )

    tool_start_t = _time.monotonic()
    try:
        result = await _execute_tool(tool_name, tool_input)
    except Exception as e:
        logger.exception("Tool %s failed during alert investigation", tool_name)
        result = {"error": str(e)}
    tool_duration_ms = (_time.monotonic() - tool_start_t) * 1000

    with mlflow.start_span(
        name=f"tool:{tool_name}",
        span_type=SpanType.TOOL,
    ) as tool_span:
        set_tool_span_outputs(
            tool_span,
            tool_name=tool_name,
            tool_input=tool_input,
            result=result if "error" not in result else None,
            error=result.get("error") if "error" in result else None,
            duration_ms=tool_duration_ms,
            cached=False,
        )

    result_str = json.dumps(result, default=str)
    log_suffix = f"{result_str[:300]}..." if len(result_str) > 300 else result_str
    investigation_log.append(f"[Tool: {tool_name}] result: {log_suffix}")

    tool_result = {
        "type": "tool_result",
        "tool_use_id": tool_block.id,
        "content": _cap_tool_result(json.dumps(result, default=str)),
    }
    return None, tool_result


def _parse_alert_response_blocks(
    response_content: list,
    investigation_log: list[str],
) -> tuple[list[str], list]:
    """Separate alert response content into text/log parts and tool_use blocks."""
    text_parts: list[str] = []
    tool_use_blocks: list = []
    for block in response_content:
        if block.type == "text" and block.text.strip():
            investigation_log.append(block.text)
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_use_blocks.append(block)
    return text_parts, tool_use_blocks


async def _process_alert_round_tools(
    tool_use_blocks: list,
    investigation_log: list[str],
) -> tuple[dict | None, list[dict], int]:
    """Process all tool calls in one alert investigation round.

    Returns (verdict_or_none, tool_results, new_tool_call_count).
    """
    tool_results: list[dict] = []
    tool_call_count = 0
    verdict: dict | None = None
    for tool_block in tool_use_blocks:
        block_verdict, tool_result = await _process_alert_tool_call(tool_block, investigation_log)
        tool_results.append(tool_result)
        if block_verdict is not None:
            verdict = block_verdict
        else:
            tool_call_count += 1
    return verdict, tool_results, tool_call_count


def _make_error_verdict(
    error_msg: str,
    investigation_log: list[str],
    start: float,
) -> dict:
    """Build a fallback verdict dict when investigation encounters an error."""
    return {
        "should_alert": True,
        "severity": "medium",
        "summary": error_msg,
        "investigation_log": "\n".join(investigation_log),
        "duration_seconds": round(_time.monotonic() - start, 1),
    }


async def run_alert_investigation(
    alert_type: str,
    account_id: str,
    alert_text: str,
    account_name: str = "",
    user_arn: str = "",
    event_time: str = "",
    region: str = "",
    event_details: dict | None = None,
) -> dict:
    """Run a non-streaming Claude investigation for an automated alert.

    Returns a structured verdict dict with should_alert, severity, summary,
    investigation_log, and duration_seconds.
    """
    start = _time.monotonic()
    cfg = get_config()
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
    max_tokens = cfg.anthropic.get("max_tokens", 4096)
    max_rounds = cfg.anthropic.get("max_tool_rounds", 10)

    try:
        client = _build_client(cfg)
    except ValueError as e:
        return _make_error_verdict(f"Investigation failed: {e}", [], start)

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    from src.agent.system_prompt import get_agent_prompt
    from src.agent.tool_definitions import get_security_tools

    system = (
        f"{get_agent_prompt('security')}\n\nToday's date is {today}.\n{ALERT_INVESTIGATION_PROMPT}"
    )
    alert_tools = list(get_security_tools())
    alert_tools.append(SUBMIT_ALERT_VERDICT_TOOL)

    user_message = _build_alert_user_message(
        alert_type,
        account_id,
        alert_text,
        account_name=account_name,
        user_arn=user_arn,
        event_time=event_time,
        region=region,
        event_details=event_details,
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    investigation_log: list[str] = []
    verdict: dict | None = None
    tool_call_count = 0

    with mlflow.start_span(
        name=f"parsec:alert_investigation:{alert_type}",
        span_type=SpanType.CHAIN,
    ) as root_span:
        root_span.set_inputs(
            {
                "alert_type": alert_type,
                "account_id": account_id,
                "alert_text": alert_text[:500],
            }
        )

        for _round in range(max_rounds):
            _dump_api_request(
                f"alert_{alert_type}_round_{_round}",
                system,
                messages,
                alert_tools,
                model,
            )

            try:

                def _call_api() -> anthropic.types.Message:
                    return client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        tools=alert_tools,  # type: ignore[arg-type]
                        messages=messages,  # type: ignore[arg-type]
                    )

                with mlflow.start_span(
                    name=f"alert_{alert_type}_round_{_round}",
                    span_type=SpanType.LLM,
                ) as llm_span:
                    response = await asyncio.to_thread(_call_api)

                    response_text_parts, tool_use_blocks = _parse_alert_response_blocks(
                        response.content, investigation_log
                    )
                    _record_llm_span(
                        llm_span,
                        _round,
                        response,
                        model,
                        "\n".join(response_text_parts),
                        [b.name for b in tool_use_blocks],
                    )

            except anthropic.APIError as e:
                logger.exception("Claude API error during alert investigation")
                root_span.set_outputs({"status": "error", "error": str(e)})
                return _make_error_verdict(
                    f"Investigation failed: Claude API error ({e})",
                    investigation_log,
                    start,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": [_clean_content_block(b) for b in response.content],
                }
            )

            if not tool_use_blocks:
                break

            round_verdict, tool_results, round_tool_calls = await _process_alert_round_tools(
                tool_use_blocks, investigation_log
            )
            tool_call_count += round_tool_calls
            if round_verdict is not None:
                verdict = round_verdict

            messages.append({"role": "user", "content": tool_results})
            messages[:] = _trim_history(messages)

            if verdict is not None:
                break

        elapsed = round(_time.monotonic() - start, 1)

        if verdict is None:
            verdict = {
                "should_alert": True,
                "severity": "medium",
                "summary": "Investigation did not produce a verdict — alerting as a precaution.",
            }
            investigation_log.append("[No verdict submitted — defaulting to should_alert=true]")

        verdict["investigation_log"] = "\n".join(investigation_log)
        verdict["duration_seconds"] = elapsed

        root_span.set_outputs(
            {
                "alert_type": alert_type,
                "should_alert": verdict["should_alert"],
                "severity": verdict["severity"],
                "summary": verdict["summary"],
                "tool_calls": tool_call_count,
                "duration_seconds": elapsed,
            }
        )

    logger.info(
        "Alert investigation complete: type=%s account=%s should_alert=%s severity=%s duration=%.1fs",
        alert_type,
        account_id,
        verdict["should_alert"],
        verdict["severity"],
        elapsed,
    )

    return verdict
