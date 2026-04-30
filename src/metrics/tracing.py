"""MLflow tracing for verbose conversation tracking.

Logs full conversation traces with nested spans for orchestrator rounds,
sub-agent delegations, and individual tool calls. Each conversation turn
produces one trace visible in the MLflow Traces tab.

Uses the low-level MlflowClient API (start_trace/start_span/end_span/
end_trace) for explicit control in async code. All operations are
fire-and-forget — failures are logged but never block the request.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.connections.mlflow_tracking import get_experiment_name, get_mlflow_client, is_tracing_enabled

logger = logging.getLogger(__name__)

_last_error_time: float = 0
_ERROR_BACKOFF_SECONDS = 60.0


def _truncate(text: str, max_len: int = 8000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... [truncated, {len(text)} chars total]"


@dataclass
class ToolCallRecord:
    """Record of a single tool call within a span."""

    tool_name: str
    tool_input: dict
    result: dict | None = None
    error: str | None = None
    duration_ms: float = 0.0
    cached: bool = False
    start_time: float = field(default_factory=time.monotonic)


@dataclass
class SpanRecord:
    """Record of a span (orchestrator round, sub-agent, or LLM call)."""

    name: str
    span_type: str
    attributes: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    children: list[SpanRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0


class ConversationTracer:
    """Accumulates trace data for a single conversation turn, then flushes
    to MLflow as a single trace with nested spans."""

    def __init__(self, question: str) -> None:
        self._question = question
        self._response_text = ""
        self._prompt_files: list[str] = []
        self._tools: list[dict] = []
        self._initial_messages: list[dict] = []
        self._root_attributes: dict[str, Any] = {}
        self._spans: list[SpanRecord] = []
        self._current_span: SpanRecord | None = None
        self._start_time = time.monotonic()
        self._llm_messages: list[dict] = []
        self._model: str = ""
        self._agent_type: str = ""
        self._routing_method: str = ""

    def set_model(self, model: str) -> None:
        self._model = model

    def set_routing(self, agent_type: str, routing_method: str) -> None:
        self._agent_type = agent_type
        self._routing_method = routing_method

    def append_response(self, text: str) -> None:
        """Append text to the final response output."""
        self._response_text += text

    def set_llm_context(
        self, prompt_files: list[str], tools: list[dict], messages: list[dict]
    ) -> None:
        """Capture the LLM input context metadata for debugging.

        Args:
            prompt_files: List of markdown files used (e.g., ["shared_context.md", "cost_agent.md"])
            tools: List of tool definitions
            messages: Message history
        """
        self._prompt_files = prompt_files
        self._tools = tools
        self._initial_messages = messages

    def record_llm_call(
        self,
        round_num: int,
        label: str,
        input_tokens: int,
        output_tokens: int,
        model: str,
        response_text: str = "",
        tool_use_names: list[str] | None = None,
    ) -> None:
        tool_names = tool_use_names or []
        name = f"{label}_round_{round_num}"
        if tool_names:
            name += f" -> [{', '.join(tool_names)}]"

        span = SpanRecord(
            name=name,
            span_type="LLM",
            inputs={"round": round_num, "model": model},
            outputs={
                "response_preview": _truncate(response_text, 2000),
                "tool_calls_requested": tool_names,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            attributes={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
        span.end_time = time.monotonic()
        if self._current_span:
            self._current_span.children.append(span)
        else:
            self._spans.append(span)

    def start_agent_span(self, agent_type: str, agent_name: str) -> None:
        span = SpanRecord(
            name=f"agent:{agent_name}",
            span_type="AGENT",
            attributes={"agent_type": agent_type},
            inputs={"agent_type": agent_type},
        )
        self._spans.append(span)
        self._current_span = span

    def end_agent_span(
        self,
        status: str = "success",
        tool_calls: int = 0,
        duration_seconds: float = 0.0,
    ) -> None:
        if self._current_span and self._current_span.span_type == "AGENT":
            self._current_span.end_time = time.monotonic()
            tools_detail = [
                {
                    "tool": tc.tool_name,
                    "input": _safe_json(tc.tool_input),
                    "output": _safe_json(tc.result) if tc.result else None,
                    "error": tc.error,
                    "duration_ms": round(tc.duration_ms, 1),
                    "cached": tc.cached,
                }
                for tc in self._current_span.tool_calls
            ]
            self._current_span.outputs = {
                "status": status,
                "tool_calls_count": tool_calls,
                "duration_seconds": duration_seconds,
                "tools_called": [tc.tool_name for tc in self._current_span.tool_calls],
                "tools_detail": tools_detail,
            }
        self._current_span = None

    def record_tool_call(
        self,
        tool_name: str,
        tool_input: dict,
        result: dict | None = None,
        error: str | None = None,
        duration_ms: float = 0.0,
        cached: bool = False,
    ) -> None:
        record = ToolCallRecord(
            tool_name=tool_name,
            tool_input=tool_input,
            result=result,
            error=error,
            duration_ms=duration_ms,
            cached=cached,
        )
        if self._current_span:
            self._current_span.tool_calls.append(record)
        else:
            tool_outputs: dict[str, Any] = {"tool_name": tool_name}
            if error:
                tool_outputs["error"] = error
            else:
                tool_outputs["result"] = _safe_json(result) if result else {}
            tool_outputs["duration_ms"] = round(duration_ms, 1)
            tool_outputs["cached"] = cached

            span = SpanRecord(
                name=f"tool:{tool_name}",
                span_type="TOOL",
                inputs={"tool_name": tool_name, "tool_input": _safe_json(tool_input)},
                outputs=tool_outputs,
                attributes={"cached": cached, "duration_ms": round(duration_ms, 1)},
            )
            span.end_time = time.monotonic()
            self._spans.append(span)

    def flush_sync(self) -> None:
        """Synchronous flush — call from asyncio.to_thread."""
        global _last_error_time

        if not is_tracing_enabled():
            return

        client = get_mlflow_client()
        if client is None:
            return

        try:
            self._write_trace(client)
        except Exception:
            now = time.monotonic()
            if now - _last_error_time > _ERROR_BACKOFF_SECONDS:
                logger.warning("MLflow trace flush failed (non-fatal)", exc_info=True)
                _last_error_time = now

    def _write_trace(self, client: Any) -> None:
        experiment_name = get_experiment_name()
        experiment = client.get_experiment_by_name(experiment_name)
        if experiment is None:
            experiment_id = client.create_experiment(experiment_name)
        else:
            experiment_id = experiment.experiment_id

        # Aggregate token counts from all LLM spans
        total_input_tokens = 0
        total_output_tokens = 0
        for span_record in self._spans:
            # Direct LLM spans
            if span_record.span_type == "LLM":
                total_input_tokens += span_record.attributes.get("input_tokens", 0)
                total_output_tokens += span_record.attributes.get("output_tokens", 0)
            # Nested LLM spans (e.g., within AGENT spans)
            for child in span_record.children:
                if child.span_type == "LLM":
                    total_input_tokens += child.attributes.get("input_tokens", 0)
                    total_output_tokens += child.attributes.get("output_tokens", 0)

        logger.info(
            "MLflow trace tokens: input=%d, output=%d (from %d spans)",
            total_input_tokens,
            total_output_tokens,
            len(self._spans),
        )

        total_duration_ms = (time.monotonic() - self._start_time) * 1000

        # Build trace inputs with LLM context metadata
        trace_inputs: dict[str, Any] = {"question": self._question}

        # Add prompt file information (which markdown files were loaded)
        if self._prompt_files:
            trace_inputs["prompt_files"] = self._prompt_files

        # Add tool information
        if self._tools:
            trace_inputs["tool_names"] = [t.get("name", "unknown") for t in self._tools]
            trace_inputs["tool_count"] = len(self._tools)

        # Add message history context
        if self._initial_messages:
            trace_inputs["message_count"] = len(self._initial_messages)
            trace_inputs["has_history"] = len(self._initial_messages) > 1

        root_span = client.start_trace(
            name=f"parsec:{self._agent_type or 'orchestrator'}",
            experiment_id=experiment_id,
            inputs=trace_inputs,
            attributes={
                "agent_type": self._agent_type,
                "routing_method": self._routing_method,
                "model": self._model,
                "total_duration_ms": round(total_duration_ms, 1),
                "span_count": len(self._spans),
                # MLflow-recognized token usage attributes
                "gen_ai.usage.input_tokens": total_input_tokens,
                "gen_ai.usage.output_tokens": total_output_tokens,
                "gen_ai.usage.total_tokens": total_input_tokens + total_output_tokens,
            },
        )
        request_id = root_span.request_id
        root_span_id = root_span.span_id

        for span_record in self._spans:
            self._write_span(client, request_id, root_span_id, span_record)

        all_tool_calls = []
        for s in self._spans:
            if s.span_type == "TOOL":
                all_tool_calls.append({
                    "tool": s.name.removeprefix("tool:"),
                    "input": s.inputs.get("tool_input"),
                    "output": s.outputs.get("result"),
                    "error": s.outputs.get("error") or None,
                    "duration_ms": s.attributes.get("duration_ms", 0),
                    "cached": s.attributes.get("cached", False),
                })
            for tc in s.tool_calls:
                all_tool_calls.append({
                    "tool": tc.tool_name,
                    "input": _safe_json(tc.tool_input),
                    "output": _safe_json(tc.result) if tc.result else None,
                    "error": tc.error,
                    "duration_ms": round(tc.duration_ms, 1),
                    "cached": tc.cached,
                })

        outputs: dict[str, Any] = {
            "response": _truncate(self._response_text, 8000),
            "agent_type": self._agent_type,
            "routing_method": self._routing_method,
            "total_spans": len(self._spans),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tool_calls": len(all_tool_calls),
            "tools_called": [tc["tool"] for tc in all_tool_calls],
            "tools_detail": all_tool_calls,
        }

        client.end_trace(
            request_id=request_id,
            outputs=outputs,
            attributes={"status": "complete"},
        )

    def _write_span(
        self, client: Any, request_id: str, parent_id: str, record: SpanRecord
    ) -> None:
        inputs = _safe_serializable(record.inputs)
        if record.tool_calls:
            inputs["tool_calls_summary"] = [
                {
                    "tool": tc.tool_name,
                    "cached": tc.cached,
                    "error": tc.error or "",
                    "duration_ms": round(tc.duration_ms, 1),
                }
                for tc in record.tool_calls
            ]

        span = client.start_span(
            name=record.name,
            request_id=request_id,
            parent_id=parent_id,
            inputs=inputs,
            attributes={
                "span_type": record.span_type,
                "duration_ms": round(record.duration_ms, 1),
                **_safe_serializable(record.attributes),
            },
        )

        for child in record.children:
            self._write_span(client, request_id, span.span_id, child)

        for tc in record.tool_calls:
            tool_span = client.start_span(
                name=f"tool:{tc.tool_name}",
                request_id=request_id,
                parent_id=span.span_id,
                inputs={
                    "tool_name": tc.tool_name,
                    "tool_input": _safe_json(tc.tool_input),
                },
                attributes={
                    "span_type": "TOOL",
                    "cached": tc.cached,
                    "duration_ms": round(tc.duration_ms, 1),
                },
            )
            tool_outputs: dict[str, Any] = {}
            if tc.error:
                tool_outputs["error"] = tc.error
            elif tc.result:
                tool_outputs["result"] = _safe_json(tc.result)
            client.end_span(
                request_id=request_id,
                span_id=tool_span.span_id,
                outputs=tool_outputs,
                attributes={"status": "error" if tc.error else "success"},
            )

        outputs = _safe_serializable(record.outputs)
        client.end_span(
            request_id=request_id,
            span_id=span.span_id,
            outputs=outputs,
            attributes={"status": "complete"},
        )


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
