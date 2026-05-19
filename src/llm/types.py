"""Shared result types for the LLM adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SdkUsage:
    """Token + cost usage from a single SDK ``query()`` invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0


@dataclass(frozen=True)
class SdkResult:
    """The aggregated output of one ``AgentSdkClient.complete()`` call.

    Mirrors the relevant fields of the SDK's ``ResultMessage`` plus the
    text accumulated from intermediate AssistantMessage blocks, so callers
    don't have to re-walk the stream.
    """

    text: str
    """Concatenated text content from assistant messages."""

    tool_invocations: tuple[dict[str, Any], ...] = ()
    """Per-tool invocation records: ``{name, input, result, is_error}``."""

    model: str | None = None
    session_id: str | None = None
    usage: SdkUsage = field(default_factory=SdkUsage)
    is_error: bool = False
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return not self.is_error
