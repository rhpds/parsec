# Debug Automation — Move from Demolition to Parsec

**Date:** 2026-04-14
**Status:** Draft

## Summary

Move the AAP2 debug automation feature from demolition's coordinator UI into parsec as a dedicated, non-chat page. Users paste an AAP2 job URL and get structured diagnostic output: job metadata, failing task extraction, source tracing, pattern-matched fix recommendations (with AI fallback), correlation analysis, and execution environment inspection.

This is a full port of demolition's debug feature — not a wrapper or proxy. Parsec takes ownership of the debug logic and uses its own AAP2 connection layer.

## Motivation

Debug automation was recently added to demolition's coordinator, but demolition is a lab runner — its primary audience is load testing, not job debugging. Parsec is the natural home for this feature:

- Parsec already has AAP2 controller connections with pre-configured credentials
- Parsec already has an AAP2 investigation sub-agent with deep domain knowledge
- Parsec's users (RHDP ops/admins) are the people who debug AAP2 job failures
- Keeping debug automation in demolition forces users to navigate to a different tool

## Approach

Port demolition's Python backend services into new parsec modules. Build a vanilla HTML/CSS/JS debug view in parsec's `static/` directory, accessible via a "Debug Automation" sidebar tab. No new frameworks, no build step — consistent with parsec's existing stack.

## Backend

### New Files

#### `src/tools/aap2_debug.py` — Debug orchestrator

Main entry point for the debug pipeline. Uses parsec's existing `src/connections/aap2.py` connection layer (`api_get`, `api_get_text`, `resolve_controller`) instead of demolition's standalone HTTP client.

Functions:

- **`parse_job_url(url: str) -> tuple[str, int]`** — Extract controller base URL and job ID from AAP2 job URLs. Supports hash fragment format (`/#/jobs/playbook/12345`) and API format (`/api/v2/jobs/12345/`).

- **`find_controller_for_url(url: str) -> str`** — Match a controller URL against parsec's configured controllers. Extracts the hostname from the URL and passes it to `resolve_controller()`, which already supports hostname-contains matching. Raises `ValueError` if no match.

- **`fetch_job_metadata(cluster_name: str, job_id: int) -> dict`** — Fetch job metadata via `api_get`. Parses `extra_vars` JSON, extracts ACTION, execution environment ID, project ID, job explanation, result traceback, timing info.

- **`fetch_job_stdout(cluster_name: str, job_id: int) -> str`** — Fetch job stdout as plain text via `api_get_text(cluster_name, path, {"format": "txt"})`.

- **`fetch_project_info(cluster_name: str, project_id: int) -> dict`** — Fetch project SCM details (URL, branch, revision) via `api_get`.

- **`fetch_correlation(cluster_name: str, job_id: int) -> dict`** — Fetch recent failed/error jobs from the same controller, group by error message (first 100 chars), execution environment, and instance group. Returns counts and job ID lists.

- **`fetch_ee_info(cluster_name: str, ee_id: int) -> dict`** — Fetch EE metadata via `api_get`, then fetch EE definition files (Containerfile, entrypoint.sh, requirements.txt, requirements.yml) from GitHub raw content.

#### `src/tools/aap2_stdout.py` — Ansible stdout parser

Direct port of demolition's `coordinator/backend/app/services/aap_stdout.py`. Pure parsing logic, no external dependencies.

- **`extract_failing_task(stdout: str) -> dict | None`** — Parse Ansible stdout to find the first failing task. Handles `fatal:`, `failed:`, `[ERROR]:`, and `ERROR!` formats. Looks backwards from the failure line to find the `TASK [role : name]` header. Returns task name, role FQCN, host pattern, error message, and file path.

#### `src/tools/aap2_fix.py` — Fix recommendation engine

Port of demolition's `coordinator/backend/app/services/aap_fix.py`.

- **`KNOWN_PATTERNS`** — List of `PatternMatch` objects mapping error regexes to fix explanations with repo/file/before/after context. Ported from demolition's existing patterns (InvalidClientTokenId, unrecognized arguments --private-data-dir, configuration string not in JSON format, role not found, Failed to JSON parse worker stream).

- **`match_pattern(error_message, extra_vars, job_template_name) -> dict | None`** — Match error against known patterns. Resolves `<catalog_item>` placeholders using extra_vars.

- **`ai_analyze_fix(failing_task, extra_vars, job_template_name) -> dict | None`** — AI fallback using parsec's existing Anthropic config (from `src/config.py`). Same prompt as demolition. Fetches role source from GitHub for context. Returns structured fix with file, repo, line, explanation, before/after code.

- **`recommend_fix(failing_task, extra_vars, job_template_name) -> dict | None`** — Pattern match first, AI fallback second.

#### `src/routes/debug.py` — FastAPI router

Three POST endpoints:

- **`POST /api/debug/diagnose`** — Body: `{"url": "..."}`. Runs phases 1-3:
  1. Parse URL → resolve controller
  2. Fetch job metadata
  3. If failed: fetch stdout, extract failing task, recommend fix
  4. If error: auto-trigger EE inspection, try pattern match on job_explanation
  Returns: `{metadata, failingTask, projectInfo, fix, eeInfo}`

- **`POST /api/debug/correlation`** — Body: `{"url": "...", "job_id": N}`. Runs phase 4 (correlation analysis). Returns: `{totalFailures, byError, byEE, byInstanceGroup}`

- **`POST /api/debug/ee`** — Body: `{"url": "...", "job_id": N, "ee_id": N}`. Runs phase 5 (EE inspection). Returns: `{id, image, sourceRepo, sourceDir, sourceFiles}`

### Registration

In `src/app.py`:
- Import `from src.routes.debug import router as debug_router`
- Add `app.include_router(debug_router)` alongside existing routers

### Connection Layer

All AAP2 API calls go through parsec's existing `src/connections/aap2.py`:
- `api_get(cluster_name, path)` for JSON responses
- `api_get_text(cluster_name, path, params)` for stdout
- `resolve_controller(hostname)` for URL-to-cluster resolution

No new connection code, no credential handling in the debug modules.

### AI Configuration

Demolition uses Vertex AI env vars (`VERTEX_AI_PROJECT`, etc.). Parsec uses its own Anthropic config in `config.yaml` (supports direct API, Vertex AI, and Bedrock). The AI fix fallback will use parsec's existing client configuration. The prompt content stays the same.

## Frontend

### Navigation

Add "Debug Automation" as a third sidebar tab alongside "History" and "Examples".

Current sidebar tab HTML:
```html
<div class="sidebar-tabs">
    <button id="sidebar-tab-history" class="sidebar-tab">History</button>
    <button id="sidebar-tab-examples" class="sidebar-tab">Examples</button>
</div>
```

Becomes:
```html
<div class="sidebar-tabs">
    <button id="sidebar-tab-history" class="sidebar-tab">History</button>
    <button id="sidebar-tab-examples" class="sidebar-tab">Examples</button>
    <button id="sidebar-tab-debug" class="sidebar-tab">Debug Automation</button>
</div>
```

Clicking "Debug Automation" swaps `<main id="chat">` content to the debug view and hides the chat input footer. Clicking "History" or "Examples" restores the chat view.

### Debug View

Injected into `<main>` as a single container `<div id="debug-view">`:

1. **Header** — "Debug Automation" title + subtitle
2. **URL input row** — text input + "Diagnose" button
3. **Result summary bar** — status label (colored), job ID, action, elapsed time
4. **Tabbed content** — 5 tabs implemented with vanilla JS:

   **Tab 1: Triage** (default)
   - Description list: status, action, started, elapsed, job explanation, result traceback

   **Tab 2: Failing Task**
   - Description list: task name, role FQCN, host, file path, SCM ref (with GitHub link)
   - Code block: error message
   - "No failing task information available" when empty

   **Tab 3: Recommended Fix**
   - Label: "Pattern Match" (green) or "AI Generated" (blue)
   - Description list: repo, file, line number, explanation
   - Before/after code blocks (when available)
   - "View on GitHub" link
   - "No fix recommendation available" when empty

   **Tab 4: Correlation** (lazy-loaded on tab click)
   - "N other failures in the last 24 hours"
   - Grouped cards: by error (monospace, with job count), by EE, by instance group
   - Loading spinner while fetching

   **Tab 5: EE Info** (lazy-loaded on tab click)
   - Description list: image, source repo link
   - Expandable file cards for EE definition files (Containerfile, entrypoint.sh, etc.)
   - Loading spinner while fetching

5. **Fix preview card** — persistent card below tabs (visible on all tabs except Fix tab) showing fix summary with "View Fix" button

### Styling

All new CSS uses parsec's existing CSS variables:
- `var(--bg-primary)`, `var(--bg-secondary)`, `var(--text-primary)`, etc.
- Same fonts (DM Sans for UI, JetBrains Mono for code)
- Same card patterns, input styles, button styles as the chat UI
- Tab styling inspired by the sidebar tab pattern already in use
- Status colors: red for failed/error, blue for other statuses
- Fix source colors: green for pattern match, blue for AI

### State Management

All state in vanilla JS module-level variables:
- `debugResult` — latest diagnosis response
- `debugCorrelation` — lazy-loaded correlation data
- `debugEEInfo` — lazy-loaded EE info
- `debugActiveTab` — current tab key
- `debugLoading` / `debugLoadingCorrelation` / `debugLoadingEE` — loading states

No localStorage persistence for debug state. Each page load starts fresh.

### Files Modified

- `static/index.html` — add debug sidebar tab button, add `<div id="debug-view">` container (hidden by default)
- `static/app.js` — add debug view logic (tab switching, API calls, DOM rendering, sidebar tab handler)
- `static/style.css` — add debug view styles (tabs, cards, description lists, code blocks, status labels)

## Error Handling

| Scenario | User-facing behavior |
|----------|---------------------|
| Invalid URL format | Alert: "Invalid AAP2 job URL — expected format: https://controller/#/jobs/playbook/12345" |
| No matching controller | Alert: "No configured controller matches this URL" |
| Job not found (404) | Alert: "Job 12345 not found on controller east" |
| Auth failure (401) | Alert: "Authentication failed — check controller credentials in config" |
| Network error | Alert: "Cannot reach AAP2 controller" |
| Non-failed job | Triage tab shows metadata; Failing Task/Fix show "Job did not fail" |
| AI fix unavailable | Fix tab shows "No fix recommendation available" (pattern match already tried) |

## What Does NOT Change

- **Parsec chat** — the existing AAP2 sub-agent is unaffected. The debug page and chat agent are independent features.
- **Demolition** — nothing removed from demolition. The debug page stays in the coordinator until parsec's version is validated. Cleanup is a separate PR.
- **Demolition CLI** — the TypeScript `src/aap-*.ts` CLI debug modules are unrelated to the coordinator and remain as-is.

## Testing

- Manual testing: paste known failed/error/successful job URLs, verify all tabs render correctly
- Backend: unit tests for `aap2_stdout.py` (stdout parsing) and `aap2_fix.py` (pattern matching) — these are pure logic
- Integration: verify controller resolution works with parsec's configured controllers
- Edge cases: job with no stdout, job with status=error, job with no EE, very long error messages
