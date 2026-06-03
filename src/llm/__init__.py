"""Pluggable LLM runtimes for Parsec.

Today: the existing orchestrator/sub-agent loops call the raw ``anthropic``
SDK at six sites (see ``src/agent/orchestrator.py`` and ``src/agent/agents.py``).

This package introduces a second runtime — the Claude Agent SDK — behind a
feature flag (``agent.runtime: legacy|sdk``). Legacy stays default; the new
adapter ships as additive code so Phase 1 lands with zero behavior change.

Typical use::

    from src.llm import AgentSdkClient, AgentSdkUnavailableError, get_runtime

    if get_runtime(cfg) == "sdk":
        client = AgentSdkClient.from_config(cfg)
        result = await client.complete(
            prompt="What's the total cost for account 12345 this month?",
            system="You are Parsec, a cloud cost investigator.",
            skills=["cost-anomaly-triage"],
        )
        print(result.text, result.usage.total_cost_usd)

See ``artifacts/parsec-agent-sdk-migration-plan.md`` for the full plan.
"""

from src.llm.agent_sdk_client import AgentSdkClient, AgentSdkUnavailableError
from src.llm.runtime import RUNTIME_LEGACY, RUNTIME_SDK, RuntimeName, get_runtime
from src.llm.types import SdkResult, SdkUsage

__all__ = [
    "RUNTIME_LEGACY",
    "RUNTIME_SDK",
    "AgentSdkClient",
    "AgentSdkUnavailableError",
    "RuntimeName",
    "SdkResult",
    "SdkUsage",
    "get_runtime",
]
