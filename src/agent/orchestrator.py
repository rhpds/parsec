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
    sse_report,
    sse_text,
    sse_tool_result,
    sse_tool_start,
)
from src.agent.system_prompt import SYSTEM_PROMPT
from src.agent.tool_definitions import TOOLS
from src.config import get_config
from src.tools.aws_costs import query_aws_costs
from src.tools.azure_costs import query_azure_costs
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

        # Set credentials path if provided (SA key file for OpenShift / CI)
        creds_path = cfg.anthropic.get("vertex_credentials_path", "")
        if creds_path:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", creds_path)

        logger.info("Using Vertex AI backend (project=%s, region=%s)", project_id, region)
        return AnthropicVertex(project_id=project_id, region=region)

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

    messages = list(conversation_history or [])
    messages.append({"role": "user", "content": question})

    for round_num in range(max_rounds):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
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

        # If no tool calls, we're done
        if not tool_use_blocks:
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

            # If this was a report generation, also send the report event
            if tool_name == "generate_report" and "error" not in result:
                download_url = f"/api/reports/{result['filename']}"
                yield sse_report(result["filename"], result["format"], download_url)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": json.dumps(result),
            })

        # Add tool results and loop back for Claude's next response
        messages.append({"role": "user", "content": tool_results})

    # If we exhausted max rounds
    yield sse_text("\n\n_Reached maximum tool call rounds. Please refine your question._")
    yield sse_done()
