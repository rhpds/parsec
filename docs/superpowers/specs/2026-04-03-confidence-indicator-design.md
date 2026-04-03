# Confidence Level Indicator Design

**Date:** 2026-04-03
**Status:** Approved

## Problem

Parsec investigations sometimes have data gaps (tool errors, empty results, unreachable
services) or require the agent to infer beyond the evidence. Investigators currently have
no signal about how much to trust a response — a clean answer looks the same whether it
was backed by 5 data sources or cobbled together from partial data with guesswork.

## Behavior

Three confidence levels with a "silent when green" model:

- **High** — No indicator shown. All tools returned data, findings are consistent, no
  inference beyond evidence. This is the default and most common case.
- **Medium** (yellow callout) — Some data gaps or light inference. E.g., one tool
  returned empty results but others provided enough to answer. One or more reasons listed.
- **Low** (red callout) — Major data gaps, tool errors, conflicting sources, or heavy
  inference. Multiple reasons listed.

## Placement

Inline in the response, only when confidence is NOT high. Appears at the end of the
agent's response text, before the Sources footer. Rendered as a left-border callout box
with bullet-pointed reasons.

## Architecture: Hybrid (Backend + Prompt)

### Backend — Deterministic Signals

The orchestrator tracks tool call outcomes during each sub-agent run.

**Tracking:** In `orchestrator.py`, maintain a list of tool outcome records during the
agent loop. Each record captures:
```python
{"tool": "query_aap2", "status": "error", "reason": "connection timeout"}
{"tool": "query_provisions_db", "status": "empty", "reason": "no matching rows"}
{"tool": "query_babylon_catalog", "status": "success"}
```

**Confidence computation** (after the agent loop completes):
- All tools succeeded with data → `high` (no SSE event emitted)
- Any tool returned empty results → `medium` (data gap, but not a hard failure)
- 1 tool errored but answer was still possible → `medium`
- Multiple tools errored → `low`
- Primary data source for the query unavailable → `low`

**SSE event:** New event type `confidence`, emitted once after the agent's final text:
```
event: confidence
data: {"level": "medium", "reasons": ["AAP2 east: connection timeout", "Provisions DB: no matching rows"]}
```

Not emitted when confidence is high.

### Prompt — Subjective Signals

Add to `config/prompts/shared_context.md`:

When the agent makes inferences, extrapolations, or educated guesses not directly
supported by tool results, it should include a confidence marker in its response:

```
[confidence: medium | Could not verify sandbox ownership — inferring from provision timestamps]
[confidence: low | No tool data available for this question — answer based on general knowledge]
```

The marker format is `[confidence: level | reason]`. Multiple markers can appear in a
single response. The frontend strips these from the displayed text.

### Frontend — Merge and Render

**Processing:**
1. Listen for `confidence` SSE event — store level and reasons
2. After the response is complete, scan the final text for `[confidence: ...]` markers
3. Strip markers from displayed text
4. Merge backend and prompt signals — take the **lower** of the two levels
5. Combine all reasons into a single list

**Rendering:**
- Only render when merged level is `medium` or `low`
- Render as a callout div at the end of the response content, before Sources
- Medium: yellow left-border, amber icon and text
- Low: red left-border, red icon and text
- Each reason rendered as a bullet point

**CSS:**
```css
.confidence-callout {
    padding: 8px 12px;
    border-radius: 4px;
    font-size: 13px;
    margin-top: 12px;
}
.confidence-callout.medium {
    background: rgba(237, 137, 54, 0.12);
    border-left: 3px solid #ed8936;
    color: #ed8936;
}
.confidence-callout.low {
    background: rgba(229, 62, 62, 0.12);
    border-left: 3px solid #e53e3e;
    color: #e53e3e;
}
```

## Confidence Reason Matrix

| Signal | Source | Level |
|--------|--------|-------|
| Tool returned error/timeout | Backend | Medium or Low |
| Tool returned empty results | Backend | Medium |
| Multiple tools failed | Backend | Low |
| Conflicting data between sources | Prompt | Medium or Low |
| Agent extrapolated beyond evidence | Prompt | Medium |
| Agent could not answer the question | Prompt | Low |

## What NOT to Flag

- Empty results that are expected (e.g., "no provisions for this user" is a valid answer,
  not a data gap)
- Tools that weren't called because they weren't relevant to the query
- Normal investigation flow where the agent narrows down from broad to specific queries

## Files to Modify

### Backend
- `src/agent/orchestrator.py` — Track tool outcomes in the agent loop, compute confidence
  after loop, emit SSE event
- `src/agent/agents.py` — Same tracking in `run_sub_agent_streaming` since sub-agents
  run their own tool loops
- `src/agent/streaming.py` — Add `sse_confidence()` helper for the new event type

### Prompts
- `config/prompts/shared_context.md` — Add confidence marker instructions

### Frontend
- `static/app.js` — Handle `confidence` SSE event, scan/strip markers, render callout
- `static/style.css` — Callout styling for medium and low levels
