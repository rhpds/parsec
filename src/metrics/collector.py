"""Metrics collector for agent performance tracking.

Accumulates operational metrics during a request and flushes them
to MLflow in a background task. Thread-safe for the flush (uses
asyncio.to_thread for the sync MLflow SDK).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from src.connections.mlflow_tracking import get_experiment_name, get_mlflow_client

logger = logging.getLogger(__name__)

_last_error_time: float = 0
_ERROR_BACKOFF_SECONDS = 60.0


@dataclass
class MetricsCollector:
    """Accumulates metrics during a single conversation turn."""

    conversation_id: str

    # Params
    agent_type: str = ""
    routing_method: str = ""
    model: str = ""
    confidence: str = ""

    # Metrics
    _start_time: float = 0.0
    total_latency_ms: float = 0.0
    sub_agent_latency_ms: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    rounds_used: int = 0
    max_rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    status: str = ""

    def start_timer(self) -> None:
        self._start_time = time.monotonic()

    def stop_timer(self) -> None:
        if self._start_time:
            self.total_latency_ms = (time.monotonic() - self._start_time) * 1000

    def record_agent_dispatch(self, agent_type: str, routing_method: str = "") -> None:
        self.agent_type = agent_type
        self.routing_method = routing_method

    def record_sub_agent_result(
        self,
        agent_type: str,
        duration_seconds: float,
        tool_calls: int,
        tool_errors: int,
        rounds_used: int,
        max_rounds: int,
        status: str,
    ) -> None:
        self.agent_type = agent_type
        self.sub_agent_latency_ms = duration_seconds * 1000
        self.tool_calls = tool_calls
        self.tool_errors = tool_errors
        self.rounds_used = rounds_used
        self.max_rounds = max_rounds
        self.status = status

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def record_model(self, model: str) -> None:
        self.model = model

    def record_confidence(self, confidence: str) -> None:
        self.confidence = confidence

    def to_params(self) -> dict[str, str]:
        return {
            k: v
            for k, v in {
                "agent_type": self.agent_type,
                "routing_method": self.routing_method,
                "model": self.model,
                "confidence": self.confidence,
                "status": self.status,
            }.items()
            if v
        }

    def to_metrics(self) -> dict[str, float]:
        return {
            "total_latency_ms": self.total_latency_ms,
            "sub_agent_latency_ms": self.sub_agent_latency_ms,
            "tool_calls": float(self.tool_calls),
            "tool_errors": float(self.tool_errors),
            "rounds_used": float(self.rounds_used),
            "input_tokens": float(self.input_tokens),
            "output_tokens": float(self.output_tokens),
        }

    async def flush_to_mlflow(self) -> None:
        """Flush accumulated metrics to MLflow. Fire-and-forget safe."""
        global _last_error_time

        client = get_mlflow_client()
        if client is None:
            return

        try:
            await asyncio.to_thread(self._flush_sync, client)
        except Exception:
            now = time.monotonic()
            if now - _last_error_time > _ERROR_BACKOFF_SECONDS:
                logger.warning("MLflow metrics flush failed (non-fatal)", exc_info=True)
                _last_error_time = now

    def _flush_sync(self, client) -> None:  # type: ignore[no-untyped-def]
        """Synchronous flush — runs in a thread via asyncio.to_thread."""
        experiment_name = get_experiment_name()
        experiment = client.get_experiment_by_name(experiment_name)
        if experiment is None:
            experiment_id = client.create_experiment(experiment_name)
        else:
            experiment_id = experiment.experiment_id

        run = client.create_run(experiment_id, tags={"conversation_id": self.conversation_id})
        run_id = run.info.run_id

        for param_key, param_value in self.to_params().items():
            client.log_param(run_id, param_key, param_value)

        for metric_key, metric_value in self.to_metrics().items():
            client.log_metric(run_id, metric_key, metric_value)

        client.set_terminated(run_id)
