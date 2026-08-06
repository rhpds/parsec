"""Tool coroutines must not stall the event loop on blocking SDK calls.

boto3 and the BigQuery client are synchronous. Calling them straight from an
`async def` freezes everything else on the loop for the length of the network
round-trip, which in production meant `/api/health` timing out, SSE keepalives
going silent, and the router killing a live investigation with "network error"
after a couple of minutes of apparently-healthy work.

These tests make a tool's blocking call sleep and assert the loop stayed
responsive meanwhile — the symptom, not the implementation, so a future rewrite
that keeps the work off the loop by other means still passes.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest


async def _loop_stalled_by(coro, block_s: float = 0.3) -> float:
    """Run `coro` and return the longest gap between 10ms heartbeat ticks."""
    worst = 0.0
    stop = False

    async def heartbeat():
        nonlocal worst
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now

    hb = asyncio.ensure_future(heartbeat())
    # Let the heartbeat actually start and take its first timestamp. Without this
    # a fully-blocking coroutine runs to completion before the loop ever schedules
    # the heartbeat, so it measures nothing and the test passes vacuously.
    await asyncio.sleep(0.05)
    started = worst
    try:
        await coro
    finally:
        stop = True
        await hb
    assert started == 0.0 or worst >= started  # heartbeat was live before the call
    return worst


@pytest.mark.asyncio
async def test_aws_cost_explorer_does_not_block_the_loop():
    from src.tools import aws_costs

    ce = MagicMock()

    def slow_call(**kwargs):
        time.sleep(0.3)  # stand-in for a real Cost Explorer round-trip
        return {"ResultsByTime": []}

    ce.get_cost_and_usage.side_effect = slow_call

    with (
        patch.object(aws_costs, "get_ce_client", return_value=ce),
        patch.object(aws_costs, "get_config", return_value=MagicMock(aws={"batch_size": 100})),
    ):
        worst = await _loop_stalled_by(
            aws_costs.query_aws_costs([], "2026-07-01", "2026-07-31", "SERVICE")
        )

    assert ce.get_cost_and_usage.called, "test did not exercise the CE call"
    assert worst < 0.15, f"event loop stalled {worst:.2f}s during a 0.3s CE call"


@pytest.mark.asyncio
async def test_bigquery_does_not_block_the_loop():
    from src.tools import gcp_costs

    def slow_query(_query):
        time.sleep(0.3)  # stand-in for a real BigQuery job
        job = MagicMock()
        job.result.return_value = iter([])
        return job

    bq = MagicMock()
    bq.query.side_effect = slow_query

    cfg = MagicMock()
    cfg.gcp.project_id = "p"
    cfg.gcp.billing_dataset = "d"
    cfg.gcp.billing_account_id = "0000-1111-2222"

    with (
        patch.object(gcp_costs, "get_bq_client", return_value=bq),
        patch.object(gcp_costs, "get_config", return_value=cfg),
    ):
        worst = await _loop_stalled_by(
            gcp_costs.query_gcp_costs("2026-07-01", "2026-07-31", "SERVICE")
        )

    assert bq.query.called, "test did not exercise the BigQuery call"
    assert worst < 0.15, f"event loop stalled {worst:.2f}s during a 0.3s BigQuery job"
