"""Prove the SDK startup hookup persists real MLflow traces without a model call."""

from __future__ import annotations

import asyncio
import json

import mlflow
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal.transport import Transport
from mlflow.tracking import fluent

from src.connections import mlflow_tracking
from src.llm.sdk_tracing import build_tracing_env

QUESTION = "Why did fixture costs increase?"
ANSWER = "The fixture account used two instances."
SESSION = "fixture-sdk-session"


def _message(kind, **block):
    return {"type": kind, "message": {"model": "fixture-model", "content": [block]}}


class WireTransport(Transport):
    """Answer SDK wire messages; keep its real constructor/parser/stream lifecycle."""

    def __init__(self, *_args, **_kwargs):
        self.queue = asyncio.Queue()

    async def connect(self):
        pass

    def is_ready(self):
        return True

    async def close(self):
        await self.queue.put(None)

    async def end_input(self):
        pass

    async def write(self, data):
        request = json.loads(data)
        if request["type"] == "control_request":
            response = dict(subtype="success", request_id=request["request_id"], response={})
            await self.queue.put({"type": "control_response", "response": response})
        elif request["type"] == "user":
            for response in [
                _message(
                    "assistant",
                    type="tool_use",
                    id="tool-1",
                    name="mcp__parsec__query_fixture",
                    input={"host": "fixture-host"},
                ),
                _message(
                    "user",
                    type="tool_result",
                    tool_use_id="tool-1",
                    content='{"instances":2}',
                    is_error=False,
                ),
                _message("assistant", type="text", text=ANSWER),
                {
                    "type": "result",
                    "session_id": SESSION,
                    "subtype": "success",
                    "duration_ms": 20,
                    "duration_api_ms": 10,
                    "is_error": False,
                    "num_turns": 1,
                    "result": ANSWER,
                },
            ]:
                await self.queue.put(response)

    async def read_messages(self):
        while (message := await self.queue.get()) is not None:
            yield message


async def test_startup_enables_missing_sdk_autolog_hook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "MLFLOW_EXPERIMENT_ID",
        "MLFLOW_EXPERIMENT_NAME",
        "MLFLOW_TRACE_DESTINATION",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DO_NOT_TRACK", "true")
    monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")
    monkeypatch.setenv("MLFLOW_TRACE_SAMPLING_RATIO", "1.0")
    monkeypatch.setattr(
        "claude_agent_sdk._internal.transport.subprocess_cli.SubprocessCLITransport", WireTransport
    )
    previous_uri, previous_experiment = mlflow.get_tracking_uri(), fluent._active_experiment_id
    uri = f"sqlite:///{tmp_path / 'tracking.db'}"
    config = {
        "mlflow": {"tracking_url": uri, "experiment_name": "sdk-tracing-regression"},
        "agent": {"runtime": "sdk", "sdk": {"orchestrator": True}},
    }
    monkeypatch.setattr(mlflow_tracking, "get_config", lambda: config)
    monkeypatch.setattr(mlflow_tracking, "_client", None)
    monkeypatch.setattr(mlflow_tracking, "_experiment_name", None)
    mlflow.set_tracking_uri(uri)
    experiment = mlflow.set_experiment(config["mlflow"]["experiment_name"])
    mlflow.tracing.reset()
    mlflow.tracing.enable()
    mlflow.anthropic.autolog(disable=True)

    async def turn():
        async with ClaudeSDKClient(ClaudeAgentOptions(env=build_tracing_env(config))) as client:
            await client.query(QUESTION)
            return [message async for message in client.receive_response()]

    def traces():
        mlflow.flush_trace_async_logging()
        return mlflow.search_traces(experiment_ids=[experiment.experiment_id], return_type="list")

    try:
        await turn()
        assert traces() == [], "environment flags alone do not install SDK instrumentation"
        mlflow_tracking.init_mlflow()
        await turn()
        stored = traces()
        assert len(stored) == 1, "startup must install the missing SDK autolog hookup"
        trace = stored[0]
        assert trace.info.state.name == "OK"
        assert trace.info.trace_metadata["mlflow.trace.session"] == SESSION
        root = next(span for span in trace.data.spans if span.parent_id is None)
        assert root.name == "claude_code_conversation"
        assert root.inputs["prompt"] == QUESTION
        assert root.outputs["response"] == ANSWER
        children = [span for span in trace.data.spans if span.parent_id == root.span_id]
        assert {span.span_type for span in children} == {"LLM", "TOOL"}
        tool = next(span for span in children if span.span_type == "TOOL")
        assert tool.inputs == {"host": "fixture-host"}
        assert json.loads(tool.outputs["result"]) == {"instances": 2}
    finally:
        mlflow.anthropic.autolog(disable=True)
        mlflow.tracing.reset()
        mlflow.set_tracking_uri(previous_uri)
        fluent._active_experiment_id = previous_experiment
