"""Regression test for PR #34 review.

The SDK streaming fast-path must emit the SSE ``history`` event the legacy exits
yield, so the frontend's ``saveConversation()`` runs and the Icinga answer
survives a page refresh under ``agent.runtime=sdk``. Without it the answer is lost.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


async def test_sdk_streaming_emits_history_event(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.agent.agents as agents
    import src.agent.runner as runner_mod

    # Force the SDK branch regardless of the (default-legacy) config.
    monkeypatch.setattr(agents, "_should_use_sdk", lambda agent_type, cfg: True)

    class _FakeRunner:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def run_sub_agent(self, *args: Any, **kwargs: Any) -> dict:
            return {"agent": "icinga", "status": "success", "summary": "sdk answer"}

    monkeypatch.setattr(runner_mod, "AgentRunner", _FakeRunner)

    events = [
        ev
        async for ev in agents.run_sub_agent_streaming(agent_type="icinga", task="triage alert X")
    ]
    blob = "".join(events)

    # the history event is present, ordered agent_done -> history -> done (as legacy)
    assert "event: history" in blob
    assert (
        blob.index("event: agent_done") < blob.index("event: history") < blob.index("event: done")
    )

    # and it carries the conversation (user task + SDK answer) for saveConversation()
    history_ev = next(e for e in events if e.startswith("event: history"))
    msgs = json.loads(history_ev.split("data: ", 1)[1].strip())["messages"]
    assert msgs[-2] == {"role": "user", "content": "triage alert X"}
    assert msgs[-1] == {"role": "assistant", "content": "sdk answer"}
