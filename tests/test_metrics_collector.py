"""Tests for MetricsCollector."""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.metrics.collector import MetricsCollector


def test_collector_records_agent_dispatch():
    """Recording an agent dispatch stores it as a param."""
    c = MetricsCollector(conversation_id="conv-123")
    c.record_agent_dispatch("cost", routing_method="fast-path")
    assert c.agent_type == "cost"
    assert c.routing_method == "fast-path"


def test_collector_records_timing():
    """Start/stop timing calculates duration."""
    c = MetricsCollector(conversation_id="conv-123")
    c.start_timer()
    time.sleep(0.01)
    c.stop_timer()
    assert c.total_latency_ms > 0


def test_collector_records_sub_agent_metrics():
    """Sub-agent results are extracted correctly."""
    c = MetricsCollector(conversation_id="conv-123")
    c.record_sub_agent_result(
        agent_type="cost",
        duration_seconds=2.5,
        tool_calls=4,
        tool_errors=1,
        rounds_used=3,
        max_rounds=8,
        status="success",
    )
    assert c.sub_agent_latency_ms == 2500.0
    assert c.tool_calls == 4
    assert c.tool_errors == 1
    assert c.rounds_used == 3
    assert c.max_rounds == 8


def test_collector_records_token_usage():
    """Token usage is accumulated."""
    c = MetricsCollector(conversation_id="conv-123")
    c.record_tokens(input_tokens=1000, output_tokens=500)
    c.record_tokens(input_tokens=200, output_tokens=100)
    assert c.input_tokens == 1200
    assert c.output_tokens == 600


def test_collector_records_model():
    """Model name is stored."""
    c = MetricsCollector(conversation_id="conv-123")
    c.record_model("claude-sonnet-4-6")
    assert c.model == "claude-sonnet-4-6"


def test_collector_records_confidence():
    """Confidence level is stored."""
    c = MetricsCollector(conversation_id="conv-123")
    c.record_confidence("high")
    assert c.confidence == "high"


def test_collector_to_mlflow_params():
    """Params dict contains the right keys and values."""
    c = MetricsCollector(conversation_id="conv-123")
    c.record_agent_dispatch("babylon", routing_method="llm")
    c.record_model("claude-sonnet-4-6")
    c.record_confidence("medium")
    params = c.to_params()
    assert params["agent_type"] == "babylon"
    assert params["routing_method"] == "llm"
    assert params["model"] == "claude-sonnet-4-6"
    assert params["confidence"] == "medium"


def test_collector_to_mlflow_metrics():
    """Metrics dict contains the right keys and values."""
    c = MetricsCollector(conversation_id="conv-123")
    c.start_timer()
    c.stop_timer()
    c.record_sub_agent_result(
        agent_type="cost",
        duration_seconds=1.0,
        tool_calls=3,
        tool_errors=0,
        rounds_used=2,
        max_rounds=8,
        status="success",
    )
    c.record_tokens(input_tokens=500, output_tokens=200)
    metrics = c.to_metrics()
    assert "total_latency_ms" in metrics
    assert metrics["sub_agent_latency_ms"] == 1000.0
    assert metrics["tool_calls"] == 3
    assert metrics["tool_errors"] == 0
    assert metrics["rounds_used"] == 2
    assert metrics["input_tokens"] == 500
    assert metrics["output_tokens"] == 200


@pytest.mark.asyncio
async def test_flush_noop_when_disabled():
    """Flush does nothing when MLflow client is None."""
    c = MetricsCollector(conversation_id="conv-123")
    with patch("src.metrics.collector.get_mlflow_client", return_value=None):
        await c.flush_to_mlflow()


@pytest.mark.asyncio
async def test_flush_logs_to_mlflow():
    """Flush creates a run and logs params + metrics."""
    c = MetricsCollector(conversation_id="conv-123")
    c.record_agent_dispatch("cost", routing_method="fast-path")
    c.record_model("claude-sonnet-4-6")
    c.start_timer()
    c.stop_timer()
    c.record_sub_agent_result(
        agent_type="cost",
        duration_seconds=1.0,
        tool_calls=2,
        tool_errors=0,
        rounds_used=1,
        max_rounds=8,
        status="success",
    )
    c.record_tokens(input_tokens=100, output_tokens=50)

    mock_client = MagicMock()
    mock_experiment = MagicMock()
    mock_experiment.experiment_id = "exp-1"
    mock_client.get_experiment_by_name.return_value = mock_experiment
    mock_run = MagicMock()
    mock_run.info.run_id = "run-1"
    mock_client.create_run.return_value = mock_run

    with patch("src.metrics.collector.get_mlflow_client", return_value=mock_client), patch(
        "src.metrics.collector.get_experiment_name",
        return_value="test-exp",
    ):
        await c.flush_to_mlflow()

    mock_client.create_run.assert_called_once()
    assert mock_client.log_param.call_count > 0
    assert mock_client.log_metric.call_count > 0
    mock_client.set_terminated.assert_called_once_with("run-1")
