"""Tests for src.llm.agent_sdk_client.

The real claude_agent_sdk isn't installed in CI by default — adapter is
ship-able without it (lazy import). Tests inject a fake module into
sys.modules so we can exercise the message-stream aggregation logic
without the actual SDK.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.llm import (
    RUNTIME_LEGACY,
    RUNTIME_SDK,
    AgentSdkClient,
    AgentSdkUnavailableError,
    SdkResult,
    get_runtime,
)
from src.llm.agent_sdk_client import AgentSdkConfig

# ---------------------------------------------------------------- fake SDK


@dataclass
class _TextBlock:
    text: str


@dataclass
class _ToolUseBlock:
    name: str
    input: dict
    id: str


@dataclass
class _ToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False


@dataclass
class _AssistantMessage:
    content: list
    model: str | None = None
    session_id: str | None = None


@dataclass
class _UserMessage:
    content: list


@dataclass
class _ResultMessage:
    model: str | None = None
    session_id: str | None = None
    usage: dict | None = field(default_factory=dict)
    total_cost_usd: float = 0.0
    num_turns: int = 0
    is_error: bool = False
    result: str | None = None


@dataclass
class _ClaudeAgentOptions:
    """Records the kwargs it was constructed with so tests can inspect them."""

    model: str = ""
    max_turns: int = 0
    cwd: str | None = None
    setting_sources: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    system_prompt: str | None = None
    skills: list | None = None
    allowed_tools: list | None = None
    mcp_servers: dict | None = None


class _FakeSdk:
    """Drop-in for the claude_agent_sdk module surface we touch."""

    def __init__(self):
        self.captured_options: _ClaudeAgentOptions | None = None
        self.captured_prompt: str | None = None
        self.stream: list = []
        self.raise_on_query: Exception | None = None
        self.delay: float = 0.0  # seconds to sleep before streaming (simulate a slow/hung query)

    ClaudeAgentOptions = _ClaudeAgentOptions
    AssistantMessage = _AssistantMessage
    UserMessage = _UserMessage
    ResultMessage = _ResultMessage
    TextBlock = _TextBlock
    ToolUseBlock = _ToolUseBlock
    ToolResultBlock = _ToolResultBlock

    def query(self, *, prompt: str, options: _ClaudeAgentOptions):
        self.captured_prompt = prompt
        self.captured_options = options

        async def _gen():
            if self.raise_on_query is not None:
                raise self.raise_on_query
            if self.delay:
                await asyncio.sleep(self.delay)
            for msg in self.stream:
                yield msg

        return _gen()


@pytest.fixture
def fake_sdk(monkeypatch):
    """Inject a fake claude_agent_sdk module into sys.modules for the test."""
    fake = _FakeSdk()
    module = types.ModuleType("claude_agent_sdk")
    module.ClaudeAgentOptions = fake.ClaudeAgentOptions  # type: ignore[attr-defined]
    module.AssistantMessage = fake.AssistantMessage  # type: ignore[attr-defined]
    module.UserMessage = fake.UserMessage  # type: ignore[attr-defined]
    module.ResultMessage = fake.ResultMessage  # type: ignore[attr-defined]
    module.TextBlock = fake.TextBlock  # type: ignore[attr-defined]
    module.ToolUseBlock = fake.ToolUseBlock  # type: ignore[attr-defined]
    module.ToolResultBlock = fake.ToolResultBlock  # type: ignore[attr-defined]
    module.query = fake.query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return fake


# ---------------------------------------------------------------- runtime


def test_get_runtime_defaults_to_legacy_when_unset():
    assert get_runtime({}) == RUNTIME_LEGACY


def test_get_runtime_returns_sdk_when_configured():
    assert get_runtime({"agent": {"runtime": "sdk"}}) == RUNTIME_SDK


def test_get_runtime_returns_legacy_when_configured():
    assert get_runtime({"agent": {"runtime": "legacy"}}) == RUNTIME_LEGACY


def test_get_runtime_falls_back_on_unknown_value():
    """Typo in config doesn't accidentally enable the experimental path."""
    assert get_runtime({"agent": {"runtime": "openai"}}) == RUNTIME_LEGACY


def test_get_runtime_handles_dynaconf_like_object():
    class _Cfg:
        def __init__(self, data):
            self._data = data

        def get(self, key, default=None):
            return self._data.get(key, default)

    cfg = _Cfg({"agent": {"runtime": "sdk"}})
    assert get_runtime(cfg) == RUNTIME_SDK


# ---------------------------------------------------------------- from_config


def test_from_config_uses_anthropic_defaults_when_no_sdk_overrides():
    cfg = {"anthropic": {"model": "claude-haiku-4-5", "max_tool_rounds": 7}}
    client = AgentSdkClient.from_config(cfg)
    assert client._cfg.model == "claude-haiku-4-5"
    assert client._cfg.max_turns == 7
    assert client._cfg.setting_sources == ("project",)


def test_from_config_prefers_sdk_overrides_over_anthropic_section():
    cfg = {
        "anthropic": {"model": "claude-sonnet-4-6", "max_tool_rounds": 10},
        "agent": {
            "sdk": {
                "model": "claude-opus-4-7",
                "max_turns": 30,
                "cwd": "/srv/parsec",
                "setting_sources": ["project", "user"],
                "env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"},
            }
        },
    }
    client = AgentSdkClient.from_config(cfg)
    assert client._cfg.model == "claude-opus-4-7"
    assert client._cfg.max_turns == 30
    assert client._cfg.cwd == "/srv/parsec"
    assert client._cfg.setting_sources == ("project", "user")
    assert client._cfg.extra_env == {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}


def test_from_config_falls_back_to_hardcoded_defaults_with_empty_config():
    client = AgentSdkClient.from_config({})
    assert client._cfg.model == "claude-sonnet-4-6"
    assert client._cfg.max_turns == 10


def test_from_config_defaults_timeout_to_300():
    client = AgentSdkClient.from_config({})
    assert client._cfg.timeout == 300.0


def test_from_config_reads_sdk_timeout_override():
    client = AgentSdkClient.from_config({"agent": {"sdk": {"timeout": 45}}})
    assert client._cfg.timeout == 45.0


def test_from_config_timeout_zero_disables_limit():
    """A 0/null timeout means no wall-clock ceiling (stored as None)."""
    client = AgentSdkClient.from_config({"agent": {"sdk": {"timeout": 0}}})
    assert client._cfg.timeout is None


# ---------------------------------------------------------------- complete()


async def test_complete_raises_when_sdk_not_installed(monkeypatch):
    """If claude_agent_sdk is not importable, complete() raises AgentSdkUnavailableError."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    client = AgentSdkClient(AgentSdkConfig(model="x"))
    with pytest.raises(AgentSdkUnavailableError):
        await client.complete(prompt="hi")


async def test_complete_aggregates_text_blocks_in_order(fake_sdk):
    """Verify model + session_id are captured from AssistantMessage, not ResultMessage.

    Mirrors the real SDK shape (claude_agent_sdk >=0.2.x): ResultMessage has
    usage/cost but no model field; AssistantMessage is the source of truth for
    the model name. Regression test for the smoke-test finding on 2026-05-21.
    """
    fake_sdk.stream = [
        _AssistantMessage(
            content=[_TextBlock(text="Hello, ")],
            model="claude-sonnet-4-6",
            session_id="sess-1",
        ),
        _AssistantMessage(
            content=[_TextBlock(text="world.")],
            model="claude-sonnet-4-6",
            session_id="sess-1",
        ),
        _ResultMessage(
            session_id="sess-1",
            usage={"input_tokens": 100, "output_tokens": 20},
            total_cost_usd=0.0123,
            num_turns=2,
        ),
    ]
    client = AgentSdkClient(AgentSdkConfig(model="claude-sonnet-4-6"))
    result = await client.complete(prompt="Say hi")

    assert result.text == "Hello, world."
    assert result.model == "claude-sonnet-4-6"
    assert result.session_id == "sess-1"
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.total_cost_usd == 0.0123
    assert result.usage.num_turns == 2
    assert result.succeeded is True


async def test_complete_captures_model_from_assistant_even_when_result_has_none(fake_sdk):
    """Real-SDK regression: ResultMessage has no .model field at all."""
    fake_sdk.stream = [
        _AssistantMessage(
            content=[_TextBlock(text="ok")],
            model="claude-opus-4-7",
            session_id="sess-42",
        ),
        _ResultMessage(session_id="sess-42", usage={"input_tokens": 5, "output_tokens": 1}),
    ]
    client = AgentSdkClient(AgentSdkConfig(model="claude-opus-4-7"))
    result = await client.complete(prompt="hi")
    assert result.model == "claude-opus-4-7"
    assert result.session_id == "sess-42"


async def test_complete_pairs_tool_use_with_tool_result(fake_sdk):
    fake_sdk.stream = [
        _AssistantMessage(
            content=[_ToolUseBlock(name="Read", input={"path": "/etc/hosts"}, id="t1")]
        ),
        _UserMessage(content=[_ToolResultBlock(tool_use_id="t1", content="127.0.0.1 localhost")]),
        _AssistantMessage(content=[_TextBlock(text="Done.")]),
        _ResultMessage(model="m", usage={}),
    ]
    client = AgentSdkClient(AgentSdkConfig(model="m"))
    result = await client.complete(prompt="Read hosts file")

    assert len(result.tool_invocations) == 1
    inv = result.tool_invocations[0]
    assert inv["name"] == "Read"
    assert inv["input"] == {"path": "/etc/hosts"}
    assert inv["result"] == "127.0.0.1 localhost"
    assert inv["is_error"] is False


async def test_complete_marks_tool_error(fake_sdk):
    fake_sdk.stream = [
        _AssistantMessage(content=[_ToolUseBlock(name="Bash", input={"cmd": "false"}, id="t1")]),
        _UserMessage(content=[_ToolResultBlock(tool_use_id="t1", content="exit 1", is_error=True)]),
        _ResultMessage(model="m", usage={}),
    ]
    client = AgentSdkClient(AgentSdkConfig(model="m"))
    result = await client.complete(prompt="run failing cmd")
    assert result.tool_invocations[0]["is_error"] is True


async def test_complete_surfaces_sdk_error_message(fake_sdk):
    fake_sdk.stream = [_ResultMessage(model="m", usage={}, is_error=True, result="rate limited")]
    client = AgentSdkClient(AgentSdkConfig(model="m"))
    result = await client.complete(prompt="hi")
    assert result.is_error is True
    assert result.error_message == "rate limited"
    assert result.succeeded is False


async def test_complete_catches_query_exception(fake_sdk):
    """Exception inside the SDK stream is captured, not propagated."""
    fake_sdk.raise_on_query = ConnectionError("oops")
    client = AgentSdkClient(AgentSdkConfig(model="m"))
    result = await client.complete(prompt="hi")
    assert result.is_error is True
    assert result.error_message is not None
    assert "ConnectionError" in result.error_message


async def test_complete_times_out_and_marks_error(fake_sdk):
    """A query that runs past the timeout is cancelled and surfaced as an error,
    not left hanging. The per-call timeout overrides the (large) config default."""
    fake_sdk.delay = 30.0  # would hang far past the tiny per-call timeout below
    fake_sdk.stream = [_ResultMessage(session_id="s", usage={})]
    client = AgentSdkClient(AgentSdkConfig(model="m"))  # config timeout defaults to 300
    result = await client.complete(prompt="hi", timeout=0.01)
    assert result.is_error is True
    assert result.error_message is not None
    assert "timed out" in result.error_message.lower()


async def test_complete_times_out_via_config_default(fake_sdk):
    """When no per-call timeout is passed, the configured ceiling still applies."""
    fake_sdk.delay = 30.0
    fake_sdk.stream = [_ResultMessage(session_id="s", usage={})]
    client = AgentSdkClient(AgentSdkConfig(model="m", timeout=0.01))
    result = await client.complete(prompt="hi")
    assert result.is_error is True
    assert "timed out" in (result.error_message or "").lower()


async def test_complete_warns_when_model_changes_mid_conversation(fake_sdk, caplog):
    """Two AssistantMessages with different models: last wins, but we warn."""
    fake_sdk.stream = [
        _AssistantMessage(
            content=[_TextBlock(text="a")], model="claude-sonnet-4-6", session_id="s"
        ),
        _AssistantMessage(content=[_TextBlock(text="b")], model="claude-opus-4-7", session_id="s"),
        _ResultMessage(session_id="s", usage={}),
    ]
    client = AgentSdkClient(AgentSdkConfig(model="m"))
    with caplog.at_level(logging.WARNING, logger="src.llm.agent_sdk_client"):
        result = await client.complete(prompt="hi")
    assert result.model == "claude-opus-4-7"  # last assistant message wins
    assert any("Model changed mid-conversation" in r.message for r in caplog.records)


async def test_complete_keeps_assistant_session_id_when_result_session_empty(fake_sdk):
    """An empty ResultMessage.session_id must not clobber the id from the
    AssistantMessage (the fallback the review flagged)."""
    fake_sdk.stream = [
        _AssistantMessage(content=[_TextBlock(text="ok")], model="m", session_id="sess-assistant"),
        _ResultMessage(session_id=None, usage={}),
    ]
    client = AgentSdkClient(AgentSdkConfig(model="m"))
    result = await client.complete(prompt="hi")
    assert result.session_id == "sess-assistant"


async def test_complete_passes_through_options(fake_sdk):
    """system, skills, allowed_tools, mcp_servers, max_turns all reach the SDK."""
    fake_sdk.stream = [_ResultMessage(model="m", usage={})]
    client = AgentSdkClient(
        AgentSdkConfig(
            model="claude-opus-4-7",
            max_turns=15,
            cwd="/srv/parsec",
            setting_sources=("project",),
            extra_env={"OTEL_SERVICE_NAME": "parsec"},
        )
    )
    await client.complete(
        prompt="Investigate cost spike",
        system="You are Parsec.",
        skills=["cost-anomaly-triage"],
        allowed_tools=["Read", "Bash", "mcp__reporting__*"],
        mcp_servers={"reporting": {"type": "http", "url": "http://r:8080"}},
        max_turns=25,
    )

    opts = fake_sdk.captured_options
    assert opts is not None
    assert opts.model == "claude-opus-4-7"
    assert opts.max_turns == 25  # per-call override beats config
    assert opts.cwd == "/srv/parsec"
    assert opts.setting_sources == ["project"]
    assert opts.system_prompt == "You are Parsec."
    assert opts.skills == ["cost-anomaly-triage"]
    assert opts.allowed_tools == ["Read", "Bash", "mcp__reporting__*"]
    assert opts.mcp_servers == {"reporting": {"type": "http", "url": "http://r:8080"}}
    # extra_env merges into env
    assert opts.env.get("OTEL_SERVICE_NAME") == "parsec"
    assert fake_sdk.captured_prompt == "Investigate cost spike"


async def test_complete_omits_optional_kwargs_when_not_provided(fake_sdk):
    """Calling without skills/allowed_tools/mcp_servers should leave them unset on options."""
    fake_sdk.stream = [_ResultMessage(model="m", usage={})]
    client = AgentSdkClient(AgentSdkConfig(model="m"))
    await client.complete(prompt="hi")
    opts = fake_sdk.captured_options
    assert opts is not None
    assert opts.skills is None
    assert opts.allowed_tools is None
    assert opts.mcp_servers is None


async def test_complete_uses_config_max_turns_when_not_overridden(fake_sdk):
    fake_sdk.stream = [_ResultMessage(model="m", usage={})]
    client = AgentSdkClient(AgentSdkConfig(model="m", max_turns=42))
    await client.complete(prompt="hi")
    assert fake_sdk.captured_options.max_turns == 42


# ---------------------------------------------------------------- result types


def test_sdk_result_succeeded_property():
    ok = SdkResult(text="hi", is_error=False)
    bad = SdkResult(text="", is_error=True, error_message="boom")
    assert ok.succeeded is True
    assert bad.succeeded is False
