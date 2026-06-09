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

# Approximate list pricing in USD/token, used ONLY to *estimate* the legacy
# arm's cost so it is comparable to the SDK arm (which reports an authoritative
# ``total_cost_usd``). Keyed by a substring of the model id. Cache-write is
# billed at 1.25x the input rate and cache-read at 0.1x (5-minute TTL).
_PRICING_USD_PER_TOKEN: dict[str, tuple[float, float]] = {
    # model-id substring: (input $/token, output $/token)
    "haiku": (1e-6, 5e-6),
    "sonnet": (3e-6, 15e-6),
    "opus": (15e-6, 75e-6),
}
_DEFAULT_PRICING_USD_PER_TOKEN = (3e-6, 15e-6)  # assume Sonnet-class when unknown
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


@dataclass
class MetricsCollector:
    """Accumulates metrics during a single conversation turn."""

    conversation_id: str

    # Params
    agent_type: str = ""
    routing_method: str = ""
    model: str = ""
    confidence: str = ""
    # Which LLM runtime produced this turn ("legacy" | "sdk"). Logged as a run
    # tag + param so legacy and SDK populations can be pivoted in one experiment.
    runtime: str = "legacy"

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
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Authoritative cost (USD) when the runtime reports one (the SDK does); 0.0
    # means "estimate from token counts" — see :meth:`resolved_cost_usd`.
    cost_usd: float = 0.0
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

    def record_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_tokens += cache_creation_tokens
        self.cache_read_tokens += cache_read_tokens

    def record_model(self, model: str) -> None:
        self.model = model

    def record_runtime(self, runtime: str) -> None:
        self.runtime = runtime

    def record_cost(self, cost_usd: float) -> None:
        """Record an authoritative cost (the SDK reports ``total_cost_usd``).

        Idempotent-friendly: keeps the largest value seen so a late zero can't
        wipe a real cost.
        """
        self.cost_usd = max(self.cost_usd, cost_usd or 0.0)

    def record_confidence(self, confidence: str) -> None:
        self.confidence = confidence

    def _estimate_cost_usd(self) -> float:
        """Estimate cost from token counts + list pricing (for the legacy arm)."""
        model = (self.model or "").lower()
        in_rate, out_rate = _DEFAULT_PRICING_USD_PER_TOKEN
        for key, rates in _PRICING_USD_PER_TOKEN.items():
            if key in model:
                in_rate, out_rate = rates
                break
        return (
            self.input_tokens * in_rate
            + self.output_tokens * out_rate
            + self.cache_creation_tokens * in_rate * _CACHE_WRITE_MULTIPLIER
            + self.cache_read_tokens * in_rate * _CACHE_READ_MULTIPLIER
        )

    def resolved_cost_usd(self) -> float:
        """USD cost for this turn: the runtime's own figure if it reported one
        (SDK ``total_cost_usd``), otherwise a token-based estimate (legacy)."""
        return self.cost_usd if self.cost_usd > 0 else self._estimate_cost_usd()

    def to_params(self) -> dict[str, str]:
        return {
            k: v
            for k, v in {
                "agent_type": self.agent_type,
                "routing_method": self.routing_method,
                "model": self.model,
                "confidence": self.confidence,
                "status": self.status,
                "runtime": self.runtime,
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
            "cache_creation_tokens": float(self.cache_creation_tokens),
            "cache_read_tokens": float(self.cache_read_tokens),
            "cost_usd": self.resolved_cost_usd(),
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

        run = client.create_run(
            experiment_id,
            tags={"conversation_id": self.conversation_id, "runtime": self.runtime},
        )
        run_id = run.info.run_id

        for param_key, param_value in self.to_params().items():
            client.log_param(run_id, param_key, param_value)

        for metric_key, metric_value in self.to_metrics().items():
            client.log_metric(run_id, metric_key, metric_value)

        client.set_terminated(run_id)
