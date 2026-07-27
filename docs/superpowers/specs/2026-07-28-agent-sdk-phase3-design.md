# Agent SDK Phase 3 — Remaining Sub-Agents on the SDK

**Date:** 2026-07-28
**Status:** Draft

## Summary

Phase 2 put one sub-agent (icinga) on the Claude Agent SDK behind `agent.runtime: sdk`.
Phase 3 puts the other five on it — cost, aap2, babylon, security, ocpv — plus three new
native skills and the `rhdp-rca-plugin` skills.

This spec settles the six architectural decisions those PRs all depend on, so the argument
happens once here instead of five times in review. It does not schedule the work.

## Problem

The SDK path today is two hardcoded string comparisons:

- `src/agent/agents.py:295` — `return agent_type == "icinga" and get_runtime(cfg) == RUNTIME_SDK`
- `src/agent/icinga_sdk.py:33-35` — `sdk_profile_for()` returns `{}` for every other agent

Flipping any other agent to the SDK today runs it with **no skill, no MCP servers and no
tools** — it would answer cost questions from the system prompt alone. Icinga was portable
because two of its five tools are already real MCP servers (monitoring-mcp SSE, GitHub
remote MCP) and the other three were dropped from its profile (`src/agent/icinga_sdk.py:38-68`).
The other five agents run in-process Python against Postgres, boto3, the Azure SDK,
BigQuery, Splunk and AAP2 REST. None of that is reachable from an SDK subprocess.

## Decision 1 — One in-process MCP bridge, not a sidecar

Build a single in-process SDK MCP server named `parsec` (`src/agent/parsec_mcp.py`) by
looping over the tool schemas an `AgentConfig` already returns and dispatching each one to
`orchestrator._execute_tool`. Per-agent SDK profiles then become nothing but an
`allowed_tools` name list over that one server.

**Why in-process is the only option.** Four behaviours live in the app process, and a
sidecar reaches none of them:

| Behaviour | Where |
|---|---|
| per-request tool result cache | `src/agent/orchestrator.py:86-98` (`_tool_cache` ContextVar, `_UNCACHEABLE_TOOLS`, `_cache_key`) |
| 100k-char result cap | `src/agent/orchestrator.py:521-570` (`MAX_TOOL_RESULT_CHARS`) |
| GitHub secret redaction | `src/tools/github_files.py:30-47` (`_redact_secrets`) |
| AAP2 log trimming | `src/tools/aap2.py:11, 252` (`trim_ansible_log`) |

A sidecar also duplicates every AWS/Azure/GCP/Splunk/AAP2 credential into a second pod.

**Why a loop and not 23 hand-written wrappers.** `_execute_tool(tool_name, tool_input) -> dict`
(`src/agent/orchestrator.py:401`) is the single dispatch entry point, and the per-tool kwarg
unpacking it delegates to is already written (`_execute_db_tool` / `_execute_cost_tool` /
`_execute_cloud_tool` / `_execute_infra_tool` / `_execute_github_tool`,
`src/agent/orchestrator.py:108-398`), so a generic handler is one `await`.

**No adapter re-architecture is needed.** `AgentSdkClient.complete()` already declares and
forwards `mcp_servers` (declared at `src/llm/agent_sdk_client.py:157`, set on
`ClaudeAgentOptions` by `_build_options()` at `src/llm/agent_sdk_client.py:147`), and
in-process SDK MCP servers work on the one-shot `query()` transport the adapter already uses — the SDK holds
stdin open for the control protocol whenever `sdk_mcp_servers` are present. No migration to
`ClaudeSDKClient`.

### Sketch

```python
# src/agent/parsec_mcp.py — built LAZILY per request, never at import time:
# db_* schemas are discovered from the Reporting MCP by a startup coroutine.

SERVER_NAME = "parsec"

#: The only state-mutating surface in the app: enum values of `action` on ONE tool
#: (`src/agent/tool_definitions.py:1142-1156`). No allowed_tools list can express
#: them — Python is the only gate.
_WRITE_ACTIONS = frozenset({"acknowledge_problem", "schedule_downtime", "reschedule_check",
                            "add_comment", "remove_comment", "remove_downtime",
                            "remove_acknowledgement", "send_custom_notification"})

#: Set by the caller BEFORE asyncio.create_task() — context is snapshotted at task creation.
_sse_sink: ContextVar[Callable[[str], Awaitable[None]] | None] = ContextVar("_sse_sink", default=None)


def build_server(tool_schemas: list[dict], *, allow_writes: bool = False):
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from src.agent.orchestrator import (_cache_key, _cap_tool_result, _execute_tool,
                                        _tool_cache, _UNCACHEABLE_TOOLS)
    from src.agent.streaming import sse_event, sse_report, sse_tool_result, sse_tool_start

    handlers = []
    for schema in tool_schemas:          # pass AGENTS[t].tools — the late-bound property
        name = schema["name"]            # (agents.py:74-80), so dynamic db_* stays correct

        async def _handler(args: dict, _name: str = name) -> dict:
            if (_name == "query_icinga" and not allow_writes
                    and str(args.get("action", "")) in _WRITE_ACTIONS):
                return {"content": [{"type": "text", "text": "write action disabled"}],
                        "is_error": True}
            sink = _sse_sink.get()
            if sink:
                await sink(sse_tool_start(_name, args))
            cache, key = _tool_cache.get(None), _cache_key(_name, args)
            cacheable = cache is not None and _name not in _UNCACHEABLE_TOOLS
            if cacheable and key in cache:
                result = cache[key]
            else:
                try:
                    result = await _execute_tool(_name, args)
                except Exception as e:                       # parity with the legacy loop
                    result = {"error": str(e)}
                if cacheable and "error" not in result:
                    cache[key] = result
            if sink:
                await sink(sse_tool_result(_name, result))
                # same side-effects the legacy loops emit (agents.py:414-422,
                # orchestrator.py:953-961); sse_report takes three positional strings
                if _name == "render_chart" and "error" not in result:
                    await sink(sse_event("chart", result))
                elif _name == "generate_report" and "error" not in result:
                    await sink(sse_report(result["filename"], result["format"],
                                          f"/api/reports/{result['filename']}"))
            payload = _cap_tool_result(json.dumps(result, default=str))   # 100k parity
            return {"content": [{"type": "text", "text": payload}], "is_error": "error" in result}

        handlers.append(tool(name, schema.get("description", "")[:1024],
                             schema["input_schema"])(_handler))
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=handlers)
```

Set `ToolAnnotations(readOnlyHint=True)` on the `query_*` tools so the model can call them in
parallel. Two rules follow from the bridge:

- **Do not pass `tools=[]`.** That disables the built-in tools including `ToolSearch`, which
  the model uses to load deferred MCP schemas — with ~24 bridged tools it may then be unable
  to call any of them. Use an explicit allowlist retaining `ToolSearch` and `Skill`, plus
  `disallowed_tools` for `Bash`/`Write`/`Edit`/`NotebookEdit`/`WebFetch`, plus
  `strict_mcp_config=True` so the pod cannot inherit a stray `.mcp.json` server.
- **No remote MCP server whose results are not passed through Parsec code may appear in an
  SDK profile.** That covers the Reporting MCP (`query_provisions_db` is a guardrail wrapper
  running `validate_sql()` and injecting `limit` — `src/tools/provision_db.py:45-70`) and
  GitHub (redaction). Route both through the bridge; `db_*` already falls through the
  `_is_reporting_mcp_tool` catch-all in the DB handler `_execute_tool` delegates to
  (`src/agent/orchestrator.py:127-131`).

## Decision 2 — Per-agent enablement via `agent.sdk.enabled_agents`

Replace the hardcoded comparison in `_should_use_sdk` (`src/agent/agents.py:285`):

```python
def _should_use_sdk(agent_type: str, cfg: Any) -> bool:
    from src.llm import RUNTIME_SDK, get_runtime
    return get_runtime(cfg) == RUNTIME_SDK and agent_type in enabled_sdk_agents(cfg)
```

`enabled_sdk_agents(cfg)` reads `agent.sdk.enabled_agents`, **intersects** it with the agents
that actually have both a profile builder and a discoverable SKILL.md, logs and drops unknown
entries, and returns `frozenset()` on malformed input — the same fail-safe posture as
`get_runtime` (`src/llm/runtime.py:34-41`), where a typo coerces to legacy rather than routing
traffic to an untested path.

```yaml
agent:
  runtime: "legacy"                # universal kill switch — see below
  sdk:
    enabled_agents: ["icinga"]     # agents allowed on the SDK when runtime: sdk
    max_concurrency: 2
    setting_sources: ["project"]
    timeout: 300
```

**`agent.runtime: legacy` stays a universal kill switch. No agent is ever SDK-only.** That is
the invariant behind the Phase-2 approval and the entire rollback story. Every agent — including
any future RCA agent — is gated as `runtime == sdk AND agent in enabled_agents`; when the flag
is legacy it runs its legacy loop or returns a graceful "requires the SDK runtime" error. No
`sdk_only` escape hatch.

Report the enabled/discovered/effective sets in `GET /api/health`, but do **not** hard-fail at
startup on a bad allowlist entry — with `replicas: 1` and a hand-edited ConfigMap,
CrashLoopBackOff is worse than a warning.

## Decision 3 — `sdk_profiles.py` replaces `icinga_sdk.py`

New `src/agent/sdk_profiles.py` holding a registry keyed by agent type. Each entry resolves
the skill name, the `allowed_tools` list over `mcp__parsec__*`, and `max_turns`.

Delete `src/agent/icinga_sdk.py` rather than leaving a re-export shim — `pyproject.toml:60`
sets `no_implicit_reexport = true`, so a bare re-export fails mypy, and a net-negative diff
reviews better. Its duplicated `_section` helper (`src/agent/icinga_sdk.py:84-96`, with an
in-file TODO) goes too; import `section()` from `src/llm/config_section.py`.

**`max_turns` derives from each agent's `max_rounds` plus headroom.** Today
`AgentSdkConfig.max_turns` falls back to `anthropic.max_tool_rounds` = 10
(`src/llm/agent_sdk_client.py:92-96`, `config/config.yaml:12`), while icinga's legacy
`max_rounds` is 15 (`src/agent/agents.py:159`). **Every Phase-2 parity and cost number was
therefore measured with the SDK arm capped at 10 turns against a 15-round legacy twin.** The
SDK also spends turns on `ToolSearch` loading deferred MCP schemas, which legacy does not:

```python
max_turns = AGENTS[agent_type].max_rounds + TOOLSEARCH_HEADROOM   # headroom = 2
```

Re-baseline icinga after this lands, and label the existing figures "measured at
max_turns=10" wherever they appear.

## Decision 4 — Prompt-to-skill extraction, with a CI drift gate

Skills are extracted from the agent prompts, not written alongside them, so the prompt stays
the single source of truth. Mark the extractable region in `config/prompts/<agent>_agent.md`:

```markdown
<!-- skill:begin name=ocpv-storage-triage description="..." -->
## Investigation Playbooks
...
<!-- skill:end -->
```

`scripts/extract_skill.py` renders each marked block into `skills/<name>/SKILL.md` with
frontmatter. CI runs `scripts/extract_skill.py --check && git diff --exit-code`, so editing a
prompt without regenerating fails the build.

**What moves:** investigation playbooks — the procedural "when the user asks X, do A then B"
blocks. For ocpv that is `## Investigation Playbooks` (`config/prompts/ocpv_agent.md:38-85`),
four self-contained procedures.

**What stays in the system prompt:** `## Available Tools` and `## Tool Response Formats`
(`config/prompts/ocpv_agent.md:7-14, 93-128`) and the cluster inventory. These are schema
reference. Copying them into a skill guarantees the skill goes stale the moment a tool schema
changes, and the model already receives the schemas from the bridge.

**The recipe applies to skills derived from a prompt, and the three new native skills are
not among them.** `cost-anomaly-triage`, `provision-lookup` and `aap2-job-failure-rca` are
hand-authored, because their content is not in any prompt today:
`config/prompts/cost_agent.md:108-147` holds four playbooks (GPU abuse, cross-cloud costs,
sandbox/account costs, one user's costs) and none of them is anomaly triage; and the
`owner/repo:path:line` citation rule and the failure-category taxonomy in
`aap2-job-failure-rca` come from rhdp-rca-plugin, not from `config/prompts/aap2_agent.md`
(grep finds neither string there). Wiring them into `extract_skill.py --check` would make
CI either fail or overwrite them. Generated skills carry the markers and the drift gate;
authored skills carry neither, and `scripts/extract_skill.py` operates only on the set that
does.

## Decision 5 — One skill root, and a loader-driven SDK root that cannot diverge from it

**There are two independent skill-discovery planes and they do not share a root.**

1. **Parsec's loader.** `src/skills/loader.py:66-76` builds sources from
   `skills.project_root`, `skills.plugin_paths` and `skills.user_root`. Its only importer in
   `src/` is `src/routes/skills.py`, whose module docstring says outright: "Does not invoke
   skills — that's the agent runtime's job" (`src/routes/skills.py:1-6`).
2. **The Agent SDK.** It discovers skills only at `<cwd>/.claude/skills/`, via
   `setting_sources: ["project"]` and `cwd` (`src/llm/agent_sdk_client.py:97-98, 135-143`). It
   never reads `skills.plugin_paths`.

So a plugin path added today makes skills appear in the Skills sidebar tab and be **completely
unexecutable**. `config/config.yaml:171-174` pre-declares `/opt/rhdp-rca-plugin/skills` as
exactly that kind of mount point. This is the worst available failure mode for a cross-team
dependency, because it looks like success.

**Fix, part one — packaging: standardise on `/app/skills/` in production. `plugin_paths` is
the dev path, not the production RCA mount point.** Skills from another repo get vendored
into the image under `skills/<name>/`,
reviewed in the diff like any Parsec-authored skill, and `plugin_paths` stays `[]` in
production. That is what keeps the review gate, avoids GitHub egress at pod start, and keeps
third-party content inside the sha-pinned surface that Sonar and the lint paths cover. A
runtime plugin mount reopens all three. This decision is packaging policy and it does not
depend on the mechanism below.

**Fix, part two — mechanism: publish every discovered skill into `<cwd>/.claude/skills/`,
driven by the loader.** The app cannot simply point the SDK root at a source root, because
there can be several (`project_root` plus N `plugin_paths`) and `.claude/skills` is one path.
So the root becomes a real directory holding one symlink per discovered skill, rebuilt at
startup from exactly the manifest list the loader returns. That makes the set the SDK can
execute identical to the set `GET /api/skills` reports, by construction rather than by
convention. Note what this is for: it is a guardrail that makes "listed but unexecutable"
unrepresentable, **not** an invitation to start mounting skill roots at runtime.

Implemented as `src/skills/sync_sdk_skill_root(manifests, cwd=...)`, called from the app
lifespan after the source roots are known. It also prunes links for skills that are no longer
discovered (an unmounted ConfigMap), and never deletes a real directory — so a bug there
cannot destroy baked content. `GET /api/skills` gains `sdk_visible` per skill plus
`sdk_skills_root`, so a mount the SDK cannot see is visibly broken instead of silently inert.

**Where symlinks are safe, and where they are not.** The loader rejects a symlinked *child* of
a source root — `if child.is_symlink(): logger.warning("Skipping symlinked skill directory
%s", child)` (`src/skills/loader.py:129-130`) — as path-traversal defence. That rule is why
plugin skills must **not** be symlinked into `skills/`: they would vanish from the UI. It does
not apply to `.claude/skills`, because the loader never scans that path — it only scans the
configured source roots. The two planes stay disjoint: **real directories for the loader,
symlinks for the SDK.** Getting this backwards silently empties `GET /api/skills`, so it is
pinned by a regression test.

The Dockerfile's `ln -sfn /app/skills /app/.claude/skills` (`dockerfiles/Dockerfile:45`)
therefore becomes a real directory seeded with per-skill links at build time, and
`_ensure_real_directory()` migrates a pre-existing root symlink in place so an older image
still converges on startup.

Exercised against the real `redhat-et/rhdp-rca-plugin` tree at `upstream/main` (`4f3fd68`):
**13 skills load (7 native + 6 plugin)**, with `root-cause-analysis` readable through the SDK
root. The plugin's `skills/` root holds six directories — `context-fetcher`,
`feedback-capture`, `logs-fetcher`, `rca-annotator`, `root-cause-analysis`, `template-skill`
— and the further SKILL.md files in that repo live under `experiments/`, not on that root.
(A checkout more than a few commits behind shows five; `rca-annotator` is recent. Count
against `upstream/main`, not a local branch.)

One of the six loads **with a warning** rather than clean: `logs-fetcher` declares
`name: log-fetcher`, so the loader reports `name 'log-fetcher' does not match folder name
'logs-fetcher'`. That is an upstream frontmatter fix to ask for, and it is why
`tests/test_shipped_skills.py` splits native from vendored — only Parsec-authored skills are
held to a zero-warning bill of health.

**Which root to use, and when.** The two positions above are not in tension once dev and
production are separated:

- **Dev / verification — `plugin_paths`.** Point it at a checkout or a mounted copy of
  `rhdp-rca-plugin/skills/`, confirm the skills appear in `GET /api/skills` with
  `sdk_visible: true`, and iterate without rebuilding the image. This is what the mechanism
  above is for.
- **Production — vendored under `skills/`,** with `plugin_paths` left `[]`, for the
  review-gate, egress and static-analysis-coverage reasons in part one.

So `plugin_paths` graduates from "a trap that silently does nothing" to "the supported dev
path", and the production packaging decision is unchanged.

Delete the misleading `plugin_paths` comment at `config/config.yaml:171-174` in the same PR
that lands the mechanism.

## Decision 6 — Port order, and the evidence that flips each agent

Ordered by surface area and by how much of the flip is *measurable*, not by demand.

| # | Agent | Why here | Evidence that flips it |
|---|---|---|---|
| 1 | **ocpv** | Smallest surface: 6 tools (`tool_definitions.py:1486-1496`), smallest prompt, read-only httpx K8s backend, `max_rounds=8` | The only agent with a measured legacy per-query baseline (median **$0.73**). Blinded human non-inferiority, ≥20 pairs, ≥2 raters |
| 2 | **security** | 8 tools, read-only *by IAM* — `query_aws_account` assumes `OrganizationAccountAccessRole` with an inline read-only session policy (`src/tools/aws_account.py:17`). `max_rounds=20` makes it the real test of the streaming fix | Non-inferiority plus a clean run at 20 rounds with the SSE restructuring in place |
| 3 | **cost** | Largest static surface (12 tools); the first that genuinely needs `render_chart` / `generate_report` back through the bridge | Non-inferiority plus zero regression on chart and report SSE events |
| 4 | **aap2** | 23KB prompt, `max_rounds=20`. Cannot mutate AAP2 — `src/connections/aap2.py` exposes only `api_get` / `api_get_text` / `api_paginate` | Non-inferiority plus the compaction check below |
| 5 | **babylon** | Last, possibly never — its prompt is domain reference (naming conventions, Jinja formulas, the resource model), not procedure, so it yields the least skill value | Only worth flipping if 1-4 show a clear win |

**Compaction.** For any agent with `max_rounds >= 15`, log `usage.input_tokens` and `num_turns`
and mark whether the SDK arm compacted. Legacy enforces its own 150k history budget; the SDK
compacts internally with no Parsec policy and no visibility. If it compacted, that pair's
quality comparison is void, not merely noisy.

**Cost is reported per agent** as `cache_creation_input_tokens` vs `cache_read_input_tokens`
with the cold fraction published — not as a single $/call. The prompt cache is content-keyed
with a short TTL, so warmth is a function of per-agent arrival rate and **cold is the normal
case**. The icinga A/B measured a legacy bare call at $0.0096, an SDK cold call at $0.14
(a 28,665-token cache write) and an SDK warm call at $0.039. `GATE_COST_RATIO = 1.30` must be
re-set deliberately or every Phase-3 run fails its own harness and gets waived by hand —
replace it with a per-agent budget plus the SDK's native `max_budget_usd` as a hard stop.

**The LLM judge is a screen, not a gate.** On the icinga set it scored 0.60 agreement,
Cohen's kappa 0.13 — chance level. A judge fail blocks; a judge pass never approves.

## Known limitation — tool events cannot stream yet

The SDK branch is a single blocking `await` inside an async generator (`_try_sdk_streaming`,
`src/agent/agents.py:483-516`): `sdk_result = await AgentRunner(...).run_sub_agent(...)` at
`src/agent/agents.py:502`, followed by `yield sse_text(answer)` at `src/agent/agents.py:510`.
While that await is pending the generator cannot yield, so anything the bridge handler pushes
onto a queue sits there and flushes *after* the answer. The in-line comment that documented this
as a Phase-2 pilot limitation no longer exists — it was dropped when the S3776 refactor extracted
the SDK branch out of `run_sub_agent_streaming` into `_try_sdk_streaming`, whose docstring records
only that the helper is the Phase-2 pilot path. The limitation itself is unchanged, and is now
undocumented in code.

The bridge does not fix it. The fix is the pattern `_handle_delegation` already uses
(`src/agent/orchestrator.py:830-851`): `asyncio.create_task(...)`, then a `while not task.done()`
loop draining an `asyncio.Queue` with `asyncio.wait_for(..., timeout=1.0)` and yielding each
event. The `_sse_sink` ContextVar must be set **before** `create_task` — context is snapshotted
at task creation, so a later `.set()` is invisible inside the task.

This is a prerequisite for porting **any** agent, not a follow-up, and not only the
long-running ones: `tool_start` / `tool_result` parity is part of the zero-functional-regression
bar every flip has to clear, and ocpv — first in the port order — has `max_rounds=8`. The
`max_rounds >= 15` threshold above governs the *compaction* check, which is a different
question.

## Non-goals

- **The orchestrator on the SDK.** It builds an Anthropic client directly and has no
  `AgentConfig` to translate. Sub-agents only.
- **Native SDK subagents** (`ClaudeAgentOptions.agents`). The six `investigate_*` delegation
  tools map onto them naturally, but that is a re-architecture, not a port.
- **The alert endpoint.** `/api/alert/investigate` drives its own legacy loop that
  `_should_use_sdk` never sees. It stays legacy, and Phase 3 asserts that in a test.
- **SSH / rsync / bastion log acquisition from the Parsec pod.** Writing `~/.ssh/config` from
  a shared server process is not acceptable regardless of what the base image ships.
- **SDK session resume / true multi-turn.** `SdkResult.session_id` is captured but
  round-tripping it as `resume=` needs a session store.

## Open questions

1. **Does icinga move onto the bridge, or stay on the remote `mcp__icinga` server?** Moving it
   gates the 8 write actions in Python and gets GitHub redaction back; leaving it means
   enumerating the sidecar's actual tool names in-cluster before its `allowed_tools` can be
   narrowed from the current whole-server grant (`src/agent/icinga_sdk.py:71-81`).
   Recommendation: move it, as the bridge's first consumer.
2. **What is the minimum viable built-in tool allowlist?** `ToolSearch` and `Skill` are
   required; the rest needs one live in-cluster call to settle.
3. **Does the pinned in-cluster `claude` binary behave like the one the bridge was proven on?**
   The bridge was verified against a newer CLI than the image pins. One in-cluster call is the
   gate on the bridge PR.
4. **What is the DB role grant behind `query_provisions_db`?** `validate_sql()` blocks
   non-SELECT and multi-statement SQL but does **not** scope tables. The real control is the
   grant, documented in neither repo.
5. **One MLflow experiment or two**, once rhdp-rca-plugin skills are in the pod? Parsec uses
   `parsec-agent-metrics`; the plugin defaults to its own. Cross-team decision.

## Files to Modify

**New:** `src/agent/parsec_mcp.py` (the bridge); `src/agent/sdk_profiles.py` (profile registry,
replacing the deleted `src/agent/icinga_sdk.py`); `scripts/extract_skill.py`; the three new
hand-authored skill directories (`skills/cost-anomaly-triage/`, `skills/provision-lookup/`,
`skills/aap2-job-failure-rca/`) plus the generated ocpv skill.

**Modified**
- `src/agent/agents.py` — `_should_use_sdk` reads the allowlist; SDK branch restructured around
  `create_task` + queue drain
- `src/agent/runner.py` — import profiles from `sdk_profiles`; pass `max_turns`
- `src/llm/agent_sdk_client.py` — accept `disallowed_tools` / `strict_mcp_config`; explicit
  subprocess env allowlist instead of `{**os.environ, ...}` (`agent_sdk_client.py:136`, in
  `_build_options()`)
- `src/routes/skills.py` — report `sdk_visible` per skill
- `config/config.yaml` — `agent.sdk.enabled_agents`, `max_concurrency`; fix the `plugin_paths` comment
- `config/prompts/*.md` — `<!-- skill:begin -->` markers
- `dockerfiles/Dockerfile` — `/app/.claude/skills` becomes a real directory of per-skill
  links instead of the root symlink at line 45
- `.github/workflows/ci.yml` — run pytest; run `extract_skill.py --check`

## Testing

- `enabled_sdk_agents()` drops unknown agents, returns empty on malformed input, and never
  returns an agent lacking a profile or a discoverable SKILL.md.
- `_should_use_sdk` returns `False` for every agent when `agent.runtime: legacy`, including
  agents listed in `enabled_agents`.
- Bridge handler honours `_tool_cache`, applies `_cap_tool_result` at 100k, converts a raised
  exception into `{"error": ...}`, and rejects each of the 8 icinga write actions when
  `allow_writes=False`.
- `chart` / `report` events from the bridge match the shape emitted at `src/agent/agents.py:414-422`.
- The SDK subprocess env contains no `PARSEC_`-prefixed key; `run_alert_investigation` never
  routes to the SDK.
- Integration: one in-cluster `query()` on the pinned CLI with the bridge registered —
  `tools/list` returns the agent's full schema set, schemas preserved verbatim.
- Integration: an ocpv query on `parsec-dev` emits `tool_start` / `tool_result` events *before*
  the answer text.
- CI: `scripts/extract_skill.py --check && git diff --exit-code`.
