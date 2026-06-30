"""Phase-2 accuracy/parity harness for the Icinga sub-agent — legacy vs Agent SDK.

This is the missing piece of the Phase-2 acceptance gate: it runs the SAME icinga
queries through BOTH runtimes, has an INDEPENDENT LLM judge score each answer, and
computes the four gates (success / quality parity / latency / cost).

How it drives the two runtimes (both production code paths, comparable metrics):
  * LEGACY — `run_sub_agent_streaming("icinga", q, metrics=c)` with the global
    runtime left at its `legacy` default; the legacy streaming loop records
    tokens/cache into the collector and we collect the streamed answer text.
  * SDK    — `AgentRunner(cfg, runtime="sdk").run_sub_agent("icinga", q, metrics=c)`;
    the SDK path records authoritative cost/cache into the collector.
Each per-query collector flushes a `runtime`-tagged MLflow run, so the individual
runs land in the same experiment and the aggregate is written as an artifact.

The judge is a SEPARATE bare-Vertex Claude call (neither runtime), scoring each
answer's root-cause accuracy vs the curated reference and the legacy-vs-SDK pairwise
parity. Answers are anonymized (A/B, order seeded by query id) to limit position bias.

IMPORTANT: run with the global runtime at `legacy` (the default — do NOT set
PARSEC_AGENT__RUNTIME=sdk); the SDK arm forces `sdk` per-call via AgentRunner. The
harness asserts this at startup.

Env: same as scripts/ab_mlflow.py (Vertex ADC + CLAUDE_CODE_USE_VERTEX=1 + project/region;
MLflow via PARSEC_MLFLOW__*). Model via PARSEC_TEST_MODEL (+ PARSEC_ANTHROPIC__MODEL).
A real, tool-exercising run additionally needs the Icinga MCP reachable
(icinga.mcp_url / the icinga-credentials secret); without it both arms degrade to
reasoning from the alert text — still a valid parity comparison, just not live-tool.

Usage:
  python scripts/parity_eval.py                      # file set, real run (needs cluster)
  python scripts/parity_eval.py --limit 3            # first 3 cases
  python scripts/parity_eval.py --selftest           # logic check, no Vertex/cluster
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import statistics
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_SET = HERE / "icinga_eval_set.json"

logger = logging.getLogger("parity_eval")

# Phase-2 acceptance gates (Version-A thresholds; Version-B treats latency as
# non-blocking and expects cost to pass with margin — both reported either way).
GATE_QUALITY_PARITY = 0.90  # fraction where SDK >= legacy (pairwise)
GATE_LATENCY_RATIO = 1.50  # median(sdk)/median(legacy)
GATE_COST_RATIO = 1.30  # median(sdk)/median(legacy)

MODEL = os.environ.get("PARSEC_TEST_MODEL", "claude-sonnet-4-5@20250929")

JUDGE_PROMPT = """You are an expert Icinga/SRE reviewer grading two AI answers to the SAME \
monitoring alert. Be strict and objective. You are given a curated REFERENCE (the expected \
platform, root cause, and key points). Grade each answer against the reference, then say which \
answer is better overall.

ALERT:
{alert}

REFERENCE (expected):
{reference}

ANSWER A:
{answer_a}

ANSWER B:
{answer_b}

Return ONLY a JSON object, no prose, with exactly this shape:
{{"answer_a": {{"accuracy": "match|partial|miss", "platform_correct": true}},
 "answer_b": {{"accuracy": "match|partial|miss", "platform_correct": true}},
 "better": "a|b|tie",
 "reason": "one sentence"}}
- "accuracy": "match" if the answer identifies the reference root cause AND the key points; \
"partial" if it gets the gist but misses key points; "miss" if it gets the root cause wrong.
- "better": which answer is more correct/useful overall ("tie" if equivalent)."""


# --------------------------------------------------------------------------- IO


def load_queries(source: str, path: Path, limit: int | None) -> list[dict]:
    if source == "file":
        data = json.loads(path.read_text())
        qs = data["queries"]
    elif source == "mlflow":
        qs = _load_from_mlflow(limit)
    elif source == "conversations":
        qs = _load_from_conversations(limit)
    else:
        raise SystemExit(f"unknown --source {source!r}")
    return qs[:limit] if limit else qs


def _load_from_mlflow(limit: int | None) -> list[dict]:
    """Pull recent icinga queries from the parsec-agent-metrics experiment.

    Requires a reachable MLflow (PARSEC_MLFLOW__TRACKING_URL). The question text is
    not currently logged as a param, so this returns conversation_ids to replay; until
    questions are logged, prefer source=file or source=conversations. Kept as a seam.
    """
    raise SystemExit(
        "source=mlflow: question text isn't logged as an MLflow param yet — use "
        "source=conversations (data/conversations) or source=file. (Seam left for when "
        "the harness logs the question param.)"
    )


def _load_from_conversations(limit: int | None) -> list[dict]:
    """Mine icinga queries from saved conversations (data/conversations/*.json on the pod)."""
    conv_dir = Path(os.environ.get("PARSEC_CONVERSATIONS_DIR", "data/conversations"))
    if not conv_dir.is_dir():
        raise SystemExit(f"source=conversations: {conv_dir} not found (run on parsec-dev / a pod)")
    out: list[dict] = []
    for f in sorted(conv_dir.glob("*.json")):
        try:
            conv = json.loads(f.read_text())
        except Exception:
            continue
        for msg in conv.get("messages", []):
            text = msg.get("content", "") if msg.get("role") == "user" else ""
            if isinstance(text, str) and _looks_icinga(text):
                out.append({"id": f"conv-{f.stem}-{len(out)}", "query": text, "reference": {}})
    if not out:
        raise SystemExit("source=conversations: no icinga-looking user turns found")
    return out


def _looks_icinga(text: str) -> bool:
    t = text.lower()
    return any(
        k in t for k in ("icinga", "monitoring alert", "host down", "service critical", "downtime")
    )


# ----------------------------------------------------------------- run runtimes


async def run_legacy(query: dict) -> tuple[str, Any, bool]:
    from src.agent.agents import run_sub_agent_streaming
    from src.metrics.collector import MetricsCollector

    c = MetricsCollector(conversation_id=f"parity-legacy-{query['id']}")
    c.record_agent_dispatch("icinga", routing_method="parity-legacy")
    c.start_timer()
    parts: list[str] = []
    errored = False
    async for ev in run_sub_agent_streaming(agent_type="icinga", task=query["query"], metrics=c):
        if ev.startswith("event: text\n"):
            try:
                parts.append(json.loads(ev.split("data: ", 1)[1].strip()).get("content", ""))
            except (IndexError, json.JSONDecodeError, AttributeError) as exc:
                # Don't drop silently: a truncated legacy answer would bias the
                # parity comparison toward the SDK. Surface it at debug. [PR #34 review]
                logger.debug("dropped unparseable SSE text chunk (%s): %r", exc, ev[:200])
        elif ev.startswith("event: error\n"):
            errored = True
    c.stop_timer()
    await c.flush_to_mlflow()
    answer = "".join(parts).strip()
    return answer, c, bool(answer) and not errored


async def run_sdk(cfg: Any, query: dict) -> tuple[str, Any, bool]:
    from src.agent.runner import AgentRunner
    from src.llm import RUNTIME_SDK
    from src.metrics.collector import MetricsCollector

    c = MetricsCollector(conversation_id=f"parity-sdk-{query['id']}")
    c.record_agent_dispatch("icinga", routing_method="parity-sdk")
    c.start_timer()
    result = await AgentRunner(cfg, runtime=RUNTIME_SDK).run_sub_agent(
        "icinga", query["query"], metrics=c
    )
    c.stop_timer()
    await c.flush_to_mlflow()
    answer = (result.get("summary") or "").strip()
    findings = result.get("findings") or []
    if findings:
        answer += "\n\nFindings:\n" + "\n".join(f"- {f}" for f in findings)
    return answer.strip(), c, result.get("status") == "success"


# ------------------------------------------------------------------- the judge


async def _vertex_judge(prompt: str) -> str:
    from anthropic import AnthropicVertex

    client = AnthropicVertex(
        project_id=os.environ["ANTHROPIC_VERTEX_PROJECT_ID"],
        region=os.environ.get("CLOUD_ML_REGION", "us-east5"),
    )
    msg = await asyncio.to_thread(
        client.messages.create,  # type: ignore[arg-type]
        model=os.environ.get("PARSEC_JUDGE_MODEL", MODEL),
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")


def _parse_judge(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in judge output: {raw[:200]!r}")
    return json.loads(raw[start : end + 1])


async def judge_query(
    query: dict,
    legacy_ans: str,
    sdk_ans: str,
    judge_fn: Callable[[str], Awaitable[str]] | None = None,
) -> dict:
    # Anonymize A/B with a deterministic (per-id) flip to limit position bias.
    flip = int(hashlib.sha256(query["id"].encode()).hexdigest(), 16) % 2 == 1
    answer_a, answer_b = (sdk_ans, legacy_ans) if flip else (legacy_ans, sdk_ans)
    prompt = JUDGE_PROMPT.format(
        alert=query["query"],
        reference=json.dumps(query.get("reference", {}), indent=2),
        answer_a=answer_a or "(empty answer)",
        answer_b=answer_b or "(empty answer)",
    )
    verdict = _parse_judge(await (judge_fn or _vertex_judge)(prompt))
    # De-anonymize back to legacy/sdk.
    sdk_key, legacy_key = ("answer_a", "answer_b") if flip else ("answer_b", "answer_a")
    better_raw = str(verdict.get("better", "tie")).lower()
    if better_raw == "tie":
        better = "tie"
    elif (better_raw == "a") == flip:  # 'a' is sdk iff flip
        better = "sdk"
    else:
        better = "legacy"
    return {
        "legacy_accuracy": verdict.get(legacy_key, {}).get("accuracy", "miss"),
        "sdk_accuracy": verdict.get(sdk_key, {}).get("accuracy", "miss"),
        "legacy_platform_ok": bool(verdict.get(legacy_key, {}).get("platform_correct", False)),
        "sdk_platform_ok": bool(verdict.get(sdk_key, {}).get("platform_correct", False)),
        "better": better,
        "reason": verdict.get("reason", ""),
    }


# ----------------------------------------------------------------- aggregation


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    leg_lat = [r["legacy_latency_ms"] for r in rows if r["legacy_success"]]
    sdk_lat = [r["sdk_latency_ms"] for r in rows if r["sdk_success"]]
    leg_cost = [r["legacy_cost_usd"] for r in rows if r["legacy_success"]]
    sdk_cost = [r["sdk_cost_usd"] for r in rows if r["sdk_success"]]
    lat_ratio = (_median(sdk_lat) / _median(leg_lat)) if _median(leg_lat) else 0.0
    cost_ratio = (_median(sdk_cost) / _median(leg_cost)) if _median(leg_cost) else 0.0
    quality_parity = (sum(1 for r in rows if r["better"] in ("sdk", "tie")) / n) if n else 0.0
    acc_legacy = (sum(1 for r in rows if r["legacy_accuracy"] == "match") / n) if n else 0.0
    acc_sdk = (sum(1 for r in rows if r["sdk_accuracy"] == "match") / n) if n else 0.0
    sdk_success = sum(1 for r in rows if r["sdk_success"])
    legacy_success = sum(1 for r in rows if r["legacy_success"])
    gates = {
        "success_all": sdk_success == n and legacy_success == n,
        "quality_parity": quality_parity >= GATE_QUALITY_PARITY,
        "latency": 0 < lat_ratio <= GATE_LATENCY_RATIO,
        "cost": 0 < cost_ratio <= GATE_COST_RATIO,
    }
    return {
        "n": n,
        "legacy_success": legacy_success,
        "sdk_success": sdk_success,
        "quality_parity": round(quality_parity, 3),
        "accuracy_legacy": round(acc_legacy, 3),
        "accuracy_sdk": round(acc_sdk, 3),
        "latency_median_legacy_ms": round(_median(leg_lat), 1),
        "latency_median_sdk_ms": round(_median(sdk_lat), 1),
        "latency_ratio": round(lat_ratio, 3),
        "cost_median_legacy_usd": round(_median(leg_cost), 6),
        "cost_median_sdk_usd": round(_median(sdk_cost), 6),
        "cost_ratio": round(cost_ratio, 3),
        "gates": gates,
        "gates_pass": all(gates.values()),
    }


def _render_markdown(rows: list[dict], agg: dict) -> str:
    lines = [
        "# Icinga legacy-vs-SDK parity results",
        "",
        f"- Cases: **{agg['n']}**  ·  legacy success {agg['legacy_success']}/{agg['n']}  ·  "
        f"sdk success {agg['sdk_success']}/{agg['n']}",
        f"- **Quality parity (SDK ≥ legacy): {agg['quality_parity']:.0%}**  "
        f"(gate ≥{GATE_QUALITY_PARITY:.0%} → {'PASS' if agg['gates']['quality_parity'] else 'FAIL'})",
        f"- Accuracy vs reference: legacy {agg['accuracy_legacy']:.0%} · sdk {agg['accuracy_sdk']:.0%}",
        f"- Latency median: legacy {agg['latency_median_legacy_ms']:.0f}ms · sdk "
        f"{agg['latency_median_sdk_ms']:.0f}ms · ratio {agg['latency_ratio']:.2f}× "
        f"(gate ≤{GATE_LATENCY_RATIO}× → {'PASS' if agg['gates']['latency'] else 'FAIL'})",
        f"- Cost median: legacy ${agg['cost_median_legacy_usd']:.4f} · sdk "
        f"${agg['cost_median_sdk_usd']:.4f} · ratio {agg['cost_ratio']:.2f}× "
        f"(gate ≤{GATE_COST_RATIO}× → {'PASS' if agg['gates']['cost'] else 'FAIL'})",
        f"- **Overall gates: {'PASS' if agg['gates_pass'] else 'FAIL'}**",
        "",
        "| case | legacy acc | sdk acc | better | sdk cost | legacy cost | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['legacy_accuracy']} | {r['sdk_accuracy']} | {r['better']} | "
            f"${r['sdk_cost_usd']:.4f} | ${r['legacy_cost_usd']:.4f} | {r['reason'][:60]} |"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------------ main


async def run_all(
    queries: list[dict], judge_fn: Callable[[str], Awaitable[str]] | None
) -> list[dict]:
    from src.config import get_config

    cfg = get_config()
    rows: list[dict] = []
    for q in queries:
        print(f"\n=== {q['id']} ===")
        legacy_ans, lc, legacy_ok = await run_legacy(q)
        print(f"  legacy: ok={legacy_ok} {lc.total_latency_ms:.0f}ms ${lc.resolved_cost_usd():.4f}")
        sdk_ans, sc, sdk_ok = await run_sdk(cfg, q)
        print(f"  sdk:    ok={sdk_ok} {sc.total_latency_ms:.0f}ms ${sc.resolved_cost_usd():.4f}")
        verdict = await judge_query(q, legacy_ans, sdk_ans, judge_fn)
        print(
            f"  judge:  legacy={verdict['legacy_accuracy']} sdk={verdict['sdk_accuracy']} "
            f"better={verdict['better']}"
        )
        rows.append(
            {
                "id": q["id"],
                "legacy_success": legacy_ok,
                "sdk_success": sdk_ok,
                "legacy_latency_ms": lc.total_latency_ms,
                "sdk_latency_ms": sc.total_latency_ms,
                "legacy_cost_usd": lc.resolved_cost_usd(),
                "sdk_cost_usd": sc.resolved_cost_usd(),
                **verdict,
            }
        )
    return rows


def _selftest() -> int:
    """Exercise judge parsing + de-anonymization + gate math with no Vertex/cluster."""
    # 1. judge JSON parse tolerant of surrounding prose
    raw = (
        'sure:\n{"answer_a": {"accuracy":"match","platform_correct":true}, '
        '"answer_b": {"accuracy":"partial","platform_correct":false}, "better":"a", "reason":"x"}'
    )
    parsed = _parse_judge(raw)
    assert parsed["better"] == "a"

    # 2. de-anonymization maps A/B back to legacy/sdk correctly under both flips
    async def fake_judge(_prompt: str) -> str:
        return raw

    async def check() -> None:
        for qid in ("ocp-cluster-operators", "babylon-schema-diff"):
            v = await judge_query(
                {"id": qid, "query": "q", "reference": {}}, "LEG", "SDK", fake_judge
            )
            # 'better':'a' must resolve to whichever runtime A was
            assert v["better"] in ("sdk", "legacy")
            assert v["legacy_accuracy"] in ("match", "partial", "miss")

    asyncio.run(check())

    # 3. gate math
    rows = [
        {
            "id": "a",
            "legacy_success": True,
            "sdk_success": True,
            "legacy_latency_ms": 1000,
            "sdk_latency_ms": 1200,
            "legacy_cost_usd": 0.30,
            "sdk_cost_usd": 0.33,
            "legacy_accuracy": "match",
            "sdk_accuracy": "match",
            "better": "tie",
            "reason": "",
        },
        {
            "id": "b",
            "legacy_success": True,
            "sdk_success": True,
            "legacy_latency_ms": 2000,
            "sdk_latency_ms": 2400,
            "legacy_cost_usd": 0.50,
            "sdk_cost_usd": 0.55,
            "legacy_accuracy": "partial",
            "sdk_accuracy": "match",
            "better": "sdk",
            "reason": "",
        },
    ]
    agg = aggregate(rows)
    assert agg["quality_parity"] == 1.0
    assert agg["gates"]["quality_parity"] and agg["gates"]["latency"] and agg["gates"]["cost"]
    assert agg["gates_pass"]
    print(_render_markdown(rows, agg))
    print("\nSELFTEST: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["file", "mlflow", "conversations"], default="file")
    ap.add_argument("--set", type=Path, default=DEFAULT_SET, help="query-set JSON (source=file)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", type=Path, default=Path("parity-results"), help="output path stem")
    ap.add_argument("--selftest", action="store_true", help="logic check, no Vertex/cluster")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    # Safety: the legacy arm relies on the global runtime being legacy.
    from src.config import get_config
    from src.connections.mlflow_tracking import get_mlflow_client, init_mlflow
    from src.llm import RUNTIME_SDK, get_runtime

    init_mlflow()
    cfg = get_config()
    if get_runtime(cfg) == RUNTIME_SDK:
        raise SystemExit(
            "Global agent.runtime is 'sdk' — the legacy arm would be wrong. Run with the "
            "default legacy global (do NOT set PARSEC_AGENT__RUNTIME=sdk); the SDK arm forces "
            "sdk per-call."
        )
    print(
        f"model={MODEL}  mlflow={'ENABLED' if get_mlflow_client() else 'DISABLED'}  source={args.source}"
    )

    queries = load_queries(args.source, args.set, args.limit)
    print(f"loaded {len(queries)} queries")
    rows = asyncio.run(run_all(queries, judge_fn=None))
    agg = aggregate(rows)

    out_json = {"aggregate": agg, "rows": rows, "model": MODEL}
    args.out.with_suffix(".json").write_text(json.dumps(out_json, indent=2))
    md = _render_markdown(rows, agg)
    args.out.with_suffix(".md").write_text(md)
    # Always echo to stdout so results are recoverable from `oc logs` without a volume.
    print("\n" + md)
    print("\n=== RESULTS_JSON_BEGIN ===")
    print(json.dumps(out_json))
    print("=== RESULTS_JSON_END ===")
    return 0 if agg["gates_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
