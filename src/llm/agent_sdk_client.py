"""Thin async adapter around ``claude_agent_sdk.query``.

The Claude Agent SDK is imported lazily so this module can be imported
in environments where the SDK isn't installed (CI without the optional
dependency, unit tests with a mocked sys.modules). Attempting to call
:meth:`AgentSdkClient.complete` without the SDK installed raises
:class:`AgentSdkUnavailableError`.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from src.llm.config_section import section
from src.llm.sdk_tracing import build_tracing_env
from src.llm.types import SdkResult, SdkUsage

logger = logging.getLogger(__name__)


class AgentSdkUnavailableError(RuntimeError):
    """Raised when ``claude_agent_sdk`` is not importable but was requested."""


@dataclass(frozen=True)
class AgentSdkConfig:
    """Resolved configuration for a single ``complete()`` call.

    Built once from Parsec config so we don't reach into Dynaconf during
    the hot path. Immutable so it's safe to share across concurrent calls.
    """

    model: str
    max_turns: int = 10
    cwd: str | None = None
    setting_sources: tuple[str, ...] = ("project",)
    extra_env: dict[str, str] = field(default_factory=dict)
    # Wall-clock ceiling for a single complete() call. ``sdk.query()`` runs an
    # agentic loop (up to max_turns rounds of possibly-slow tools), so a hung
    # query would otherwise leak the coroutine and child CLI process. ``None``
    # disables the limit. Per-call ``complete(timeout=...)`` overrides this.
    timeout: float | None = 300.0


class AgentSdkClient:
    """Adapter that runs one agentic task via the Claude Agent SDK.

    Each call to :meth:`complete` runs a fresh SDK ``query()`` to completion
    and aggregates the streamed messages into a single :class:`SdkResult`.
    Stateless — safe to reuse across requests.

    The orchestrator's existing tool loop (``client.messages.create`` →
    inspect tool_use blocks → dispatch → append tool_result → repeat)
    is **not** modeled here. The SDK runs its own loop internally; the
    adapter surfaces the final aggregated outcome. Phase 1 wires this
    behind a feature flag for narrow tasks; the orchestrator integration
    in Phase 2 will pass full conversations through.
    """

    def __init__(self, sdk_config: AgentSdkConfig):
        self._cfg = sdk_config

    # ------------------------------------------------------------------ ctor

    @classmethod
    def from_config(cls, config: Any) -> AgentSdkClient:
        """Build from a Dynaconf-style config object.

        Reads:
        - ``anthropic.model`` (or the SDK-specific ``agent.sdk.model`` override)
        - ``anthropic.max_tool_rounds`` (renamed to ``max_turns`` for the SDK)
        - ``agent.sdk.cwd`` — working directory for the SDK subprocess
        - ``agent.sdk.setting_sources`` — defaults to ``["project"]`` so
          mounted skills under cwd are discovered
        - ``agent.sdk.timeout`` — per-call wall-clock ceiling in seconds
          (default 300; ``null``/``0`` disables it)
        """
        agent_section = section(config, "agent")
        sdk_section = section(agent_section, "sdk") if agent_section else {}
        anthropic_section = section(config, "anthropic")

        model = (
            sdk_section.get("model")
            or (anthropic_section.get("model") if anthropic_section else None)
            or "claude-sonnet-4-6"
        )
        max_turns = (
            sdk_section.get("max_turns")
            or (anthropic_section.get("max_tool_rounds") if anthropic_section else None)
            or 10
        )
        cwd = sdk_section.get("cwd") or os.getcwd()
        setting_sources_raw = sdk_section.get("setting_sources", ["project"]) or ["project"]
        # MLflow tracing env is derived from mlflow.* so the SDK subprocess
        # exports its own claude_code.* spans when tracking is enabled. An
        # explicit agent.sdk.env wins on conflict (operator override).
        explicit_env = sdk_section.get("env", {}) or {}
        extra_env = {**build_tracing_env(config), **explicit_env}
        timeout_raw = sdk_section.get("timeout", 300.0)
        timeout = float(timeout_raw) if timeout_raw else None

        return cls(
            AgentSdkConfig(
                model=str(model),
                max_turns=int(max_turns),
                cwd=str(cwd) if cwd else None,
                setting_sources=tuple(setting_sources_raw),
                extra_env=dict(extra_env),
                timeout=timeout,
            )
        )

    # ------------------------------------------------------------------ api

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        skills: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        max_turns: int | None = None,
        timeout: float | None = None,
    ) -> SdkResult:
        """Run a single agentic task via ``claude_agent_sdk.query()``.

        Args:
            prompt: User-visible task. Becomes the initial user message.
            system: System prompt prepended to the SDK conversation.
            skills: Skill names to enable. The SDK discovers them via
                ``setting_sources``; this list whitelists which to activate.
                ``None`` enables all discoverable skills (SDK default).
            allowed_tools: Tool name whitelist passed to ``ClaudeAgentOptions``.
            mcp_servers: MCP server config dict, passed through.
            max_turns: Override the per-call turn cap (defaults to config).
            timeout: Per-call wall-clock ceiling in seconds. Falls back to the
                configured ``agent.sdk.timeout`` (default 300) when ``None``.
                On expiry the query is cancelled and the result is marked an
                error instead of hanging and leaking the child CLI process.

        Raises:
            AgentSdkUnavailableError: if ``claude_agent_sdk`` isn't installed.
        """
        sdk = _import_sdk()

        options_kwargs: dict[str, Any] = {
            "model": self._cfg.model,
            "max_turns": max_turns or self._cfg.max_turns,
            "setting_sources": list(self._cfg.setting_sources),
            "env": {**os.environ, **self._cfg.extra_env},
        }
        if self._cfg.cwd:
            options_kwargs["cwd"] = self._cfg.cwd
        if system:
            options_kwargs["system_prompt"] = system
        if skills is not None:
            options_kwargs["skills"] = skills
        if allowed_tools is not None:
            options_kwargs["allowed_tools"] = allowed_tools
        if mcp_servers:
            options_kwargs["mcp_servers"] = mcp_servers

        options = sdk.ClaudeAgentOptions(**options_kwargs)

        text_parts: list[str] = []
        tool_invocations: list[dict[str, Any]] = []
        model: str | None = None
        session_id: str | None = None
        usage = SdkUsage()
        is_error = False
        error_message: str | None = None

        state: dict[str, Any] = {
            "text_parts": text_parts,
            "tool_invocations": tool_invocations,
            "model": model,
            "session_id": session_id,
            "usage": usage,
            "is_error": is_error,
            "error_message": error_message,
        }

        effective_timeout = timeout if timeout is not None else self._cfg.timeout

        try:
            # asyncio.timeout(None) is a valid no-op, so this one path covers
            # both the bounded and unbounded cases. On expiry it cancels the
            # async-for, which closes the SDK's async generator and tears down
            # the child CLI process instead of leaking it.
            async with asyncio.timeout(effective_timeout):
                async for message in sdk.query(prompt=prompt, options=options):
                    if isinstance(message, sdk.AssistantMessage):
                        _ingest_assistant(sdk, message, state)
                    elif isinstance(message, sdk.UserMessage):
                        _ingest_user(sdk, message, state)
                    elif isinstance(message, sdk.ResultMessage):
                        _ingest_result(message, state)
        except TimeoutError:
            logger.warning("Claude Agent SDK query timed out (limit=%ss)", effective_timeout)
            state["is_error"] = True
            state["error_message"] = (
                f"SDK query timed out after {effective_timeout}s"
                if effective_timeout is not None
                else "SDK query timed out"
            )
        except Exception as e:
            logger.exception("Claude Agent SDK query failed")
            state["is_error"] = True
            state["error_message"] = f"{type(e).__name__}: {e}"

        model = state["model"]
        session_id = state["session_id"]
        usage = state["usage"]
        is_error = state["is_error"]
        error_message = state["error_message"]

        return SdkResult(
            text="".join(text_parts),
            tool_invocations=tuple(tool_invocations),
            model=model,
            session_id=session_id,
            usage=usage,
            is_error=is_error,
            error_message=error_message,
        )


# ---------------------------------------------------------------------- helpers


def _import_sdk() -> Any:
    """Lazy import of ``claude_agent_sdk``; raises if missing."""
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as e:
        raise AgentSdkUnavailableError(
            "claude_agent_sdk is not installed. Install with "
            "'pip install claude-agent-sdk' to enable the SDK runtime."
        ) from e


def _ingest_assistant(sdk: Any, message: Any, state: dict[str, Any]) -> None:
    """Capture model/session_id and accumulate text + tool_use blocks.

    ``AssistantMessage.model`` is the source of truth for the running model
    name — ``ResultMessage`` (per SDK 0.2.x) carries cost/usage but has no
    ``model`` field. Last assistant message wins if the conversation switches
    models mid-flight; that's unexpected, so warn rather than silently overwrite.
    """
    msg_model = getattr(message, "model", None)
    if msg_model:
        if state["model"] and state["model"] != msg_model:
            logger.warning("Model changed mid-conversation: %s -> %s", state["model"], msg_model)
        state["model"] = msg_model
    msg_session = getattr(message, "session_id", None)
    if msg_session:
        state["session_id"] = msg_session

    for block in getattr(message, "content", []) or []:
        if isinstance(block, sdk.TextBlock):
            state["text_parts"].append(getattr(block, "text", ""))
        elif isinstance(block, sdk.ToolUseBlock):
            state["tool_invocations"].append(
                {
                    "name": getattr(block, "name", None),
                    "input": getattr(block, "input", {}),
                    "id": getattr(block, "id", None),
                }
            )


def _ingest_user(sdk: Any, message: Any, state: dict[str, Any]) -> None:
    """Pair tool_result blocks back to their tool_use entries by id."""
    for block in getattr(message, "content", []) or []:
        if not isinstance(block, sdk.ToolResultBlock):
            continue
        tool_id = getattr(block, "tool_use_id", None)
        for inv in state["tool_invocations"]:
            if inv.get("id") == tool_id and "result" not in inv:
                inv["result"] = getattr(block, "content", None)
                inv["is_error"] = bool(getattr(block, "is_error", False))
                break


def _ingest_result(message: Any, state: dict[str, Any]) -> None:
    """Capture usage/cost from ResultMessage.

    ``ResultMessage.session_id`` is a required ``str`` per the SDK spec, so take
    it when present, but don't clobber a valid id already captured from an
    AssistantMessage if it's somehow empty.
    """
    result_session = getattr(message, "session_id", None)
    if result_session:
        state["session_id"] = result_session
    raw_usage = getattr(message, "usage", None) or {}
    state["usage"] = _coerce_usage(
        raw_usage,
        total_cost_usd=float(getattr(message, "total_cost_usd", 0.0) or 0.0),
        num_turns=int(getattr(message, "num_turns", 0) or 0),
    )
    if bool(getattr(message, "is_error", False)):
        state["is_error"] = True
        state["error_message"] = getattr(message, "result", None) or "SDK reported error"


def _coerce_usage(raw: Any, *, total_cost_usd: float, num_turns: int) -> SdkUsage:
    """Normalize an SDK usage dict/object into our SdkUsage dataclass."""

    def _get(obj: Any, name: str, default: int = 0) -> int:
        if isinstance(obj, dict):
            return int(obj.get(name, default) or default)
        return int(getattr(obj, name, default) or default)

    return SdkUsage(
        input_tokens=_get(raw, "input_tokens"),
        output_tokens=_get(raw, "output_tokens"),
        cache_creation_input_tokens=_get(raw, "cache_creation_input_tokens"),
        cache_read_input_tokens=_get(raw, "cache_read_input_tokens"),
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
    )
