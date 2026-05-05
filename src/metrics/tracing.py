"""MLflow tracing utilities.

Provides helper functions for setting consistent span attributes
across the agent codebase. Used with MLflow's fluent tracing API
(mlflow.start_span context managers) in orchestrator.py and agents.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mlflow.entities import SpanType

__all__ = ["SpanType"]

logger = logging.getLogger(__name__)


def _truncate(text: str, max_len: int = 8000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... [truncated, {len(text)} chars total]"


def _safe_json(obj: Any) -> Any:
    """Convert to JSON-safe dict, truncating large values."""
    try:
        s = json.dumps(obj, default=str)
        if len(s) > 8000:
            return {"_truncated": True, "preview": s[:8000]}
        return obj
    except (TypeError, ValueError):
        return str(obj)[:8000]


def _safe_serializable(d: dict) -> dict:
    """Ensure all dict values are JSON-serializable."""
    result = {}
    for k, v in d.items():
        try:
            json.dumps(v, default=str)
            result[k] = v
        except (TypeError, ValueError):
            result[k] = str(v)[:2000]
    return result


def set_llm_span_outputs(
    span: Any,
    round_num: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    response_text: str = "",
    tool_use_names: list[str] | None = None,
) -> None:
    """Set standardized inputs/outputs/attributes on an LLM span."""
    tool_names = tool_use_names or []
    span.set_inputs({"round": round_num, "model": model})
    span.set_outputs(
        {
            "response_preview": _truncate(response_text, 2000),
            "tool_calls_requested": tool_names,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    )
    span.set_attributes(
        {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    )


def set_tool_span_outputs(
    span: Any,
    tool_name: str,
    tool_input: dict,
    result: dict | None = None,
    error: str | None = None,
    duration_ms: float = 0.0,
    cached: bool = False,
) -> None:
    """Set standardized inputs/outputs/attributes on a tool span."""
    span.set_inputs(
        {
            "tool_name": tool_name,
            "tool_input": _safe_json(tool_input),
        }
    )
    outputs: dict[str, Any] = {"tool_name": tool_name}
    if error:
        outputs["error"] = error
    elif result is not None:
        outputs["result"] = _safe_json(result)
    outputs["duration_ms"] = round(duration_ms, 1)
    outputs["cached"] = cached
    span.set_outputs(outputs)
    span.set_attributes(
        {
            "cached": cached,
            "duration_ms": round(duration_ms, 1),
            "status": "error" if error else "success",
        }
    )


def set_root_span_outputs(
    span: Any,
    response_text: str,
    agent_type: str,
    routing_method: str,
    model: str,
    total_input_tokens: int,
    total_output_tokens: int,
    tool_calls: list[dict[str, Any]] | None = None,
) -> None:
    """Set standardized outputs/attributes on the root conversation span."""
    all_tools = tool_calls or []
    span.set_outputs(
        {
            "response": _truncate(response_text, 8000),
            "agent_type": agent_type,
            "routing_method": routing_method,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tool_calls": len(all_tools),
            "tools_called": [tc.get("tool", "") for tc in all_tools],
        }
    )
    span.set_attributes(
        {
            "agent_type": agent_type,
            "routing_method": routing_method,
            "model": model,
            "gen_ai.usage.input_tokens": total_input_tokens,
            "gen_ai.usage.output_tokens": total_output_tokens,
            "gen_ai.usage.total_tokens": total_input_tokens + total_output_tokens,
        }
    )
