"""Tests for src.agent.runner.AgentRunner.

The runner is a routing seam between the legacy Anthropic loop
(``src.agent.agents.run_sub_agent``) and the Claude Agent SDK adapter
(``src.llm.AgentSdkClient``). These tests inject fakes for both heavy
dependencies via ``monkeypatch`` so the routing/normalization logic can be
exercised in isolation (no anthropic SDK, no claude_agent_sdk, no config files).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from src.agent.runner import (
    AgentRunner,
    _error_result,
    _sdk_result_to_dict,
    _with_context,
)
from src.llm import RUNTIME_LEGACY, RUNTIME_SDK, AgentSdkUnavailableError, SdkResult, SdkUsage

# --------------------------------------------------------------- runtime wiring


def test_runtime_defaults_to_legacy() -> None:
    assert AgentRunner({}).runtime == RUNTIME_LEGACY


def test_runtime_reads_flag_from_config() -> None:
    assert AgentRunner({"agent": {"runtime": "sdk"}}).runtime == RUNTIME_SDK


def test_runtime_explicit_override_wins() -> None:
    # Even with legacy in config, an explicit override forces the runtime
    # (used by the benchmark harness to drive both paths from one config).
    runner = AgentRunner({"agent": {"runtime": "legacy"}}, runtime=RUNTIME_SDK)
    assert runner.runtime == RUNTIME_SDK


def test_unknown_runtime_falls_back_to_legacy() -> None:
    assert AgentRunner({"agent": {"runtime": "bogus"}}).runtime == RUNTIME_LEGACY


# ------------------------------------------------------------- legacy dispatch


async def test_legacy_dispatch_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_sub_agent(agent_type: str, task: str, **kwargs: Any) -> dict:
        captured["agent_type"] = agent_type
        captured["task"] = task
        captured["kwargs"] = kwargs
        return {"agent": agent_type, "status": "success", "summary": "legacy answer"}

    fake_agents = types.ModuleType("src.agent.agents")
    fake_agents.run_sub_agent = _fake_run_sub_agent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.agent.agents", fake_agents)

    runner = AgentRunner({"agent": {"runtime": "legacy"}})
    out = await runner.run_sub_agent(
        "icinga", "triage alert X", context={"host": "h1"}, conversation_history=[{"x": 1}]
    )

    assert out["summary"] == "legacy answer"
    assert captured["agent_type"] == "icinga"
    assert captured["task"] == "triage alert X"
    # context/conversation_history forwarded verbatim to the legacy loop
    assert captured["kwargs"]["context"] == {"host": "h1"}
    assert captured["kwargs"]["conversation_history"] == [{"x": 1}]


# ---------------------------------------------------------------- sdk dispatch


class _FakeSdkClient:
    """Stand-in for AgentSdkClient: records the call and returns a real SdkResult."""

    calls: list[dict[str, Any]] = []

    def __init__(self, result: SdkResult) -> None:
        self._result = result

    @classmethod
    def from_config(cls, config: Any) -> _FakeSdkClient:
        return cls(
            SdkResult(
                text="sdk answer",
                tool_invocations=(
                    {"name": "query_icinga", "input": {}, "is_error": False},
                    {"name": "fetch_github_file", "input": {}, "is_error": True},
                ),
                model="claude-sonnet-4-5",
                session_id="sess-1",
                usage=SdkUsage(
                    input_tokens=1000,
                    output_tokens=200,
                    cache_creation_input_tokens=50,
                    cache_read_input_tokens=800,
                    total_cost_usd=0.0123,
                    num_turns=3,
                ),
            )
        )

    async def complete(self, *, prompt: str, system: str | None = None, **kwargs: Any) -> SdkResult:
        _FakeSdkClient.calls.append({"prompt": prompt, "system": system})
        return self._result


def _inject_prompt_loader(
    monkeypatch: pytest.MonkeyPatch, prompt: str = "ICINGA SYSTEM PROMPT"
) -> None:
    fake_sp = types.ModuleType("src.agent.system_prompt")
    fake_sp.get_agent_prompt = lambda agent_type: prompt  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.agent.system_prompt", fake_sp)


async def test_sdk_dispatch_maps_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSdkClient.calls = []
    _inject_prompt_loader(monkeypatch)
    monkeypatch.setattr("src.llm.AgentSdkClient", _FakeSdkClient)

    runner = AgentRunner({"agent": {"runtime": "sdk"}})
    out = await runner.run_sub_agent("icinga", "triage alert X", context={"host": "h1"})

    # routing reached the SDK and forwarded the agent's system prompt + task(+context)
    assert _FakeSdkClient.calls[0]["system"] == "ICINGA SYSTEM PROMPT"
    assert _FakeSdkClient.calls[0]["prompt"].startswith("triage alert X")
    assert "Context from orchestrator" in _FakeSdkClient.calls[0]["prompt"]

    # result normalized onto the legacy dict shape
    assert out["agent"] == "icinga"
    assert out["status"] == "success"
    assert out["summary"] == "sdk answer"
    assert out["tool_calls"] == 2
    assert out["tool_errors"] == 1
    assert out["rounds_used"] == 3
    # cost/cache usage surfaced for the benchmark
    assert out["data"]["runtime"] == "sdk"
    assert out["data"]["usage"]["cache_read_input_tokens"] == 800
    assert out["data"]["usage"]["total_cost_usd"] == pytest.approx(0.0123)


async def test_sdk_unavailable_returns_error_not_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _inject_prompt_loader(monkeypatch)

    class _Unavailable:
        @classmethod
        def from_config(cls, config: Any) -> Any:
            raise AgentSdkUnavailableError("claude_agent_sdk is not installed")

    monkeypatch.setattr("src.llm.AgentSdkClient", _Unavailable)

    runner = AgentRunner({"agent": {"runtime": "sdk"}})
    out = await runner.run_sub_agent("icinga", "triage")

    assert out["status"] == "error"
    assert "not installed" in out["summary"]
    assert out["data"]["runtime"] == "sdk"


# ------------------------------------------------------------------- helpers


def test_with_context_noop_when_empty() -> None:
    assert _with_context("do thing", None) == "do thing"
    assert _with_context("do thing", {}) == "do thing"


def test_with_context_appends_json() -> None:
    out = _with_context("do thing", {"account": "123"})
    assert out.startswith("do thing")
    assert "Context from orchestrator" in out
    assert '"account": "123"' in out


def test_error_result_shape() -> None:
    out = _error_result("icinga", "boom", 1.5)
    assert out == {
        "agent": "icinga",
        "status": "error",
        "summary": "boom",
        "findings": [],
        "data": {"runtime": "sdk"},
        "tool_calls": 0,
        "tool_errors": 0,
        "rounds_used": 0,
        "duration_seconds": 1.5,
        "error": "boom",
    }


def test_sdk_result_to_dict_error_status() -> None:
    result = SdkResult(text="", is_error=True, error_message="timeout")
    out = _sdk_result_to_dict("icinga", result, 2.0)
    assert out["status"] == "error"
    assert out["summary"] == "timeout"
    assert out["findings"] == []
    assert out["duration_seconds"] == 2.0
