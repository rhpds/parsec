"""Claude tool-use orchestrator — the core agent loop."""

import json
import logging
import os
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import anthropic

from src.agent.streaming import (
    sse_done,
    sse_error,
    sse_event,
    sse_report,
    sse_text,
    sse_tool_result,
    sse_tool_start,
)
from src.agent.system_prompt import SYSTEM_PROMPT
from src.agent.tool_definitions import TOOLS
from src.config import get_config
from src.tools.aws_costs import query_aws_costs
from src.tools.aws_pricing import query_aws_pricing
from src.tools.azure_costs import query_azure_costs
from src.tools.cost_monitor import query_cost_monitor
from src.tools.gcp_costs import query_gcp_costs
from src.tools.provision_db import execute_query

logger = logging.getLogger(__name__)

# Directory for saved reports
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


async def _execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch a tool call to the appropriate handler."""
    if tool_name == "query_provisions_db":
        return await execute_query(tool_input["sql"])

    elif tool_name == "query_aws_costs":
        return await query_aws_costs(
            account_ids=tool_input["account_ids"],
            start_date=tool_input["start_date"],
            end_date=tool_input["end_date"],
            group_by=tool_input.get("group_by", "SERVICE"),
        )

    elif tool_name == "query_azure_costs":
        return await query_azure_costs(
            subscription_names=tool_input["subscription_names"],
            start_date=tool_input["start_date"],
            end_date=tool_input["end_date"],
        )

    elif tool_name == "query_gcp_costs":
        return await query_gcp_costs(
            start_date=tool_input["start_date"],
            end_date=tool_input["end_date"],
            group_by=tool_input.get("group_by", "SERVICE"),
            filter_services=tool_input.get("filter_services"),
            filter_projects=tool_input.get("filter_projects"),
        )

    elif tool_name == "query_aws_pricing":
        return await query_aws_pricing(
            instance_type=tool_input["instance_type"],
            region=tool_input.get("region", "us-east-1"),
            os_type=tool_input.get("os_type", "Linux"),
        )

    elif tool_name == "query_cost_monitor":
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

    elif tool_name == "render_chart":
        # Charts are rendered client-side — just return the input as-is
        return tool_input

    elif tool_name == "generate_report":
        return _save_report(tool_input)

    else:
        return {"error": f"Unknown tool: {tool_name}"}


def _save_report(tool_input: dict) -> dict:
    """Save a report to disk and return metadata."""
    title = tool_input["title"]
    content = tool_input["content"]
    fmt = tool_input.get("format", "markdown")
    filename = tool_input.get("filename", "")

    ext = ".adoc" if fmt == "asciidoc" else ".md"
    if not filename:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"investigation_report_{date_str}"

    full_filename = f"{filename}{ext}"
    filepath = os.path.join(REPORTS_DIR, full_filename)

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


def _build_client(cfg) -> anthropic.Anthropic:
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
            raise ValueError("anthropic.vertex_project_id or gcp.project_id required for Vertex backend")

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
            logger.info("Using Vertex AI backend (project=%s, region=%s, sa=%s)", project_id, region, creds_path)
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


def _trim_history(history: list, max_tokens: int = 150000) -> list:
    """Trim conversation history to fit within token limits.

    Keeps the most recent turns. Truncates large tool results in older turns.
    """
    if not history:
        return []

    messages = list(history)

    # If under limit, return as-is
    if _estimate_tokens(messages) <= max_tokens:
        return messages

    # First pass: truncate large tool_result content in older messages
    for msg in messages[:-4]:  # Keep last 2 turns (4 messages) intact
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_str = block.get("content", "")
                    if isinstance(result_str, str) and len(result_str) > 2000:
                        # Truncate but keep enough for context
                        try:
                            result_data = json.loads(result_str)
                            if isinstance(result_data, dict):
                                # Keep summary fields, drop row data
                                if "rows" in result_data:
                                    result_data["rows"] = result_data["rows"][:5]
                                    result_data["_truncated_for_context"] = True
                                if "results" in result_data and isinstance(result_data["results"], list):
                                    result_data["results"] = result_data["results"][:5]
                                    result_data["_truncated_for_context"] = True
                                block["content"] = json.dumps(result_data)
                        except (json.JSONDecodeError, TypeError):
                            block["content"] = result_str[:2000] + "... [truncated]"

    # If still over, drop oldest turns
    while len(messages) > 2 and _estimate_tokens(messages) > max_tokens:
        # Remove oldest user+assistant pair
        messages.pop(0)
        if messages and messages[0].get("role") == "assistant":
            messages.pop(0)
        # Also remove any orphaned tool_result
        if messages and messages[0].get("role") == "user":
            content = messages[0].get("content")
            if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                messages.pop(0)

    return messages


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
            serialized_content = []
            for block in content:
                if isinstance(block, dict):
                    serialized_content.append(block)
                elif hasattr(block, "model_dump"):
                    serialized_content.append(block.model_dump())
                elif hasattr(block, "to_dict"):
                    serialized_content.append(block.to_dict())
                else:
                    serialized_content.append({"type": "text", "text": str(block)})
            result.append({"role": role, "content": serialized_content})
        else:
            # SDK content object list (from response.content)
            try:
                serialized_content = [
                    b.model_dump() if hasattr(b, "model_dump") else {"type": "text", "text": str(b)}
                    for b in content
                ]
                result.append({"role": role, "content": serialized_content})
            except TypeError:
                result.append({"role": role, "content": str(content)})

    return result


async def run_agent(question: str, conversation_history: list | None = None) -> AsyncGenerator[str, None]:
    """Run the Claude tool-use loop and yield SSE events.

    Args:
        question: The user's natural language question.
        conversation_history: Optional prior messages for multi-turn context.

    Yields:
        SSE-formatted strings.
    """
    cfg = get_config()
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")
    max_tokens = cfg.anthropic.get("max_tokens", 4096)
    max_rounds = cfg.anthropic.get("max_tool_rounds", 10)

    try:
        client = _build_client(cfg)
    except ValueError as e:
        yield sse_error(str(e))
        yield sse_done()
        return

    # Inject today's date so Claude knows the current date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system = f"{SYSTEM_PROMPT}\n\nToday's date is {today}."

    messages = _trim_history(conversation_history or [])
    messages.append({"role": "user", "content": question})

    for round_num in range(max_rounds):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError as e:
            logger.exception("Claude API error")
            yield sse_error(f"Claude API error: {e}")
            yield sse_done()
            return

        # Process response content blocks
        assistant_content = response.content
        tool_use_blocks = []
        text_parts = []

        for block in assistant_content:
            if block.type == "text":
                text_parts.append(block.text)
                yield sse_text(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        # If no tool calls, we're done — send the full history for multi-turn
        if not tool_use_blocks:
            yield sse_event("history", {"messages": _serialize_messages(messages)})
            yield sse_done()
            return

        # Append assistant message
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tool calls and build tool results
        tool_results = []
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input

            yield sse_tool_start(tool_name, tool_input)

            result = await _execute_tool(tool_name, tool_input)

            yield sse_tool_result(tool_name, result)

            # Special SSE events for certain tools
            if tool_name == "generate_report" and "error" not in result:
                download_url = f"/api/reports/{result['filename']}"
                yield sse_report(result["filename"], result["format"], download_url)
            elif tool_name == "render_chart" and "error" not in result:
                yield sse_event("chart", result)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": json.dumps(result),
            })

        # Add tool results and loop back for Claude's next response
        messages.append({"role": "user", "content": tool_results})

    # If we exhausted max rounds
    yield sse_text("\n\n_Reached maximum tool call rounds. Please refine your question._")
    yield sse_event("history", {"messages": _serialize_messages(messages)})
    yield sse_done()
