"""In-cluster legacy-vs-SDK A/B that logs to MLflow via the real instrumentation.

This is the MLflow-backed successor to ``parsec-dependencies/pr2-test/test_icinga_ab.py``.
Instead of only printing token/cost numbers, it drives the *production* code paths
so the result lands in MLflow as two comparable, ``runtime``-tagged runs that the
team can pivot on:

  * LEGACY — one bare ``AnthropicVertex.messages.create`` with the **real Icinga
    system prompt** (``get_agent_prompt("icinga")``), recorded into a
    ``MetricsCollector`` (runtime defaults to ``legacy``); ``cost_usd`` is the
    collector's token-based estimate.
  * SDK x N — the **actual** ``AgentRunner(runtime=sdk).run_sub_agent(..., metrics=...)``
    path (icinga-triage skill loaded, server-side prompt caching), recorded into a
    ``MetricsCollector`` tagged ``runtime=sdk``; ``cost_usd`` is the SDK's
    authoritative ``total_cost_usd``. Run >1 should hit the warm cache.

Both arms share the same system prompt + task, and the run is deliberately tool-free
(no Icinga MCP needed), so the measured delta is the prompt-caching effect on the
large Icinga system+skill prefix — the same controlled microbenchmark as before, now
emitted through the real MLflow instrumentation. A production-equivalent multi-round
run (Icinga MCP reachable) remains the separate, creds-gated follow-up.

Env (set by the Job): Vertex ADC + ``CLAUDE_CODE_USE_VERTEX=1`` +
``ANTHROPIC_VERTEX_PROJECT_ID`` + ``CLOUD_ML_REGION``; MLflow via
``PARSEC_MLFLOW__TRACKING_URL`` (+ ``MLFLOW_TRACKING_USERNAME``/``_PASSWORD`` for the
basic-auth proxy) and ``PARSEC_MLFLOW__EXPERIMENT_NAME``; model via
``PARSEC_TEST_MODEL`` (also mirror to ``PARSEC_ANTHROPIC__MODEL`` so the SDK arm reads
the same id). No credentials are baked into the image.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

MODEL = os.environ.get("PARSEC_TEST_MODEL", "claude-sonnet-4-5@20250929")
SDK_RUNS = int(os.environ.get("SDK_RUNS", "3"))
RUN_TAG = os.environ.get("AB_RUN_TAG", str(int(time.time())))

# A representative Icinga alert to reason about (no tools needed) — identical to
# the original pr2-test harness so the numbers are comparable across runs.
ALERT = (
    "Icinga alert: service 'OCP Cluster Operators' on host 'cnv-us-east-ocp-3' is "
    "CRITICAL (HARD). last_check_result.output: 'authentication operator Degraded=True "
    "(OAuthServerDeploymentDegraded); 1/33 operators degraded'. command: "
    "['/home/icinga/monitoring-scripts/monitoring/check_ocp_cluster_operators.sh', "
    "'--degraded-crit', '1']. Not acknowledged, not in downtime."
)
TASK = (
    f"{ALERT}\n\nUsing the Icinga triage workflow, give: the platform, the "
    "state/severity/scope, the most likely root cause, and a 3-tier action plan. "
    "Do NOT call any tools — reason from the alert text and your knowledge."
)


def _fmt(label: str, c) -> str:  # type: ignore[no-untyped-def]
    return (
        f"{label:16} runtime={c.runtime:<6} in={c.input_tokens:>8,} out={c.output_tokens:>6,} "
        f"cache_w={c.cache_creation_tokens:>8,} cache_r={c.cache_read_tokens:>8,} "
        f"cost=${c.resolved_cost_usd():.6f}"
    )


async def run_legacy(system: str):  # type: ignore[no-untyped-def]
    """Bare AnthropicVertex call with the real Icinga prompt → runtime=legacy run."""
    from anthropic import AnthropicVertex

    from src.metrics.collector import MetricsCollector

    client = AnthropicVertex(
        project_id=os.environ["ANTHROPIC_VERTEX_PROJECT_ID"],
        region=os.environ.get("CLOUD_ML_REGION", "us-east5"),
    )
    c = MetricsCollector(conversation_id=f"ab-legacy-{RUN_TAG}")
    c.record_agent_dispatch("icinga", routing_method="ab-legacy")
    c.record_model(MODEL)
    c.start_timer()
    msg = await asyncio.to_thread(
        client.messages.create,
        model=MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": TASK}],
    )
    c.stop_timer()
    u = msg.usage
    c.record_tokens(
        int(getattr(u, "input_tokens", 0) or 0),
        int(getattr(u, "output_tokens", 0) or 0),
        int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        int(getattr(u, "cache_read_input_tokens", 0) or 0),
    )
    c.record_sub_agent_result(
        agent_type="icinga",
        duration_seconds=c.total_latency_ms / 1000,
        tool_calls=0,
        tool_errors=0,
        rounds_used=1,
        max_rounds=1,
        status="success",
    )
    await c.flush_to_mlflow()
    print(_fmt("LEGACY", c))
    return c


async def run_sdk(cfg, idx: int, label: str):  # type: ignore[no-untyped-def]
    """The real AgentRunner SDK path → runtime=sdk run (icinga-triage skill)."""
    from src.agent.runner import AgentRunner
    from src.llm import RUNTIME_SDK
    from src.metrics.collector import MetricsCollector

    c = MetricsCollector(conversation_id=f"ab-sdk-{label}-{RUN_TAG}")
    c.record_agent_dispatch("icinga", routing_method=f"ab-sdk-{label}")
    c.start_timer()
    result = await AgentRunner(cfg, runtime=RUNTIME_SDK).run_sub_agent("icinga", TASK, metrics=c)
    c.stop_timer()
    await c.flush_to_mlflow()
    ok = result.get("status") == "success"
    print(_fmt(f"SDK {label}", c) + f"  turns={c.rounds_used} ok={ok}")
    if not ok:
        print(f"   sdk error: {result.get('error')}")
    return c, ok


async def main() -> int:
    from src.agent.system_prompt import get_agent_prompt
    from src.config import get_config
    from src.connections.mlflow_tracking import get_mlflow_client, init_mlflow

    init_mlflow()
    cfg = get_config()
    system = get_agent_prompt("icinga") or "You are Parsec's Icinga triage sub-agent."

    print("=== Icinga legacy-vs-SDK A/B → MLflow ===")
    print(f"model: {MODEL}  vertex={os.environ.get('CLAUDE_CODE_USE_VERTEX', '<unset>')}")
    print(
        f"mlflow: {'ENABLED' if get_mlflow_client() else 'DISABLED (no client — runs not logged!)'}"
    )
    print(f"experiment: {os.environ.get('PARSEC_MLFLOW__EXPERIMENT_NAME', 'parsec-agent-metrics')}")
    print(f"run tag: {RUN_TAG}")
    print("-" * 88)

    legacy = await run_legacy(system)

    sdk_cols = []
    for i in range(1, SDK_RUNS + 1):
        label = "cold" if i == 1 else f"warm{i - 1}"
        col, ok = await run_sdk(cfg, i, label)
        sdk_cols.append((col, ok))

    # Give the fire-and-forget flush tasks a moment to reach MLflow.
    await asyncio.sleep(3)

    print("-" * 88)
    ok_cols = [c for c, ok in sdk_cols if ok]
    if ok_cols:
        cold = ok_cols[0].resolved_cost_usd()
        warm = min(c.resolved_cost_usd() for c in ok_cols[1:]) if len(ok_cols) > 1 else cold
        legacy_cost = legacy.resolved_cost_usd()
        print(f"legacy cost     : ${legacy_cost:.6f}  (runtime=legacy, estimated)")
        print(f"SDK cold cost   : ${cold:.6f}  (runtime=sdk, authoritative)")
        print(f"SDK warm cost   : ${warm:.6f}  (best warm)")
        if legacy_cost:
            print(f"SDK warm / legacy: {warm / legacy_cost:.2f}x")
        warm_cache = max((c.cache_read_tokens for c in ok_cols[1:]), default=0)
        print(
            f"warm cache_read : {warm_cache:,} tokens -> caching {'ENGAGED' if warm_cache else 'NOT seen'}"
        )
        print(
            f"\nMLflow: {1 + len(sdk_cols)} runs logged (1 runtime=legacy, {len(sdk_cols)} runtime=sdk)"
        )

    all_ok = bool(ok_cols) and len(ok_cols) == len(sdk_cols)
    print("RESULT: PASS" if all_ok else "RESULT: FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
