# CSV and JSON Export for Query Results

**Date:** 2026-04-15
**Request:** Billy Bethell (Slack #forum-rhdp-parsec)
**Scope:** Frontend only — no backend changes, no new dependencies

## Problem

Users want to download Parsec query results as CSV and JSON files for further analysis in spreadsheets, scripts, or other tools. The current export options (Markdown, PDF, Share) capture the narrative response but not the structured data from tool calls.

## Design

### Data Capture

During SSE streaming, tool results are already parsed in `processEvent("tool_result", ...)`. We add a `_exportToolResults` array on each assistant message element (same pattern as `_exportMarkdown` and `_exportCharts`):

```js
assistantEl._exportToolResults = [];
// Each tool_result event pushes:
// { tool: "query_provisions_db", input: { sql: "..." }, result: { rows: [...], row_count: 5 } }
```

For restored conversations (`renderSharedMessages`), the same array is reconstructed from the existing `toolResultMap`.

### JSON Export

Serialize `_exportToolResults` as pretty-printed JSON:

```json
[
  {
    "tool": "query_provisions_db",
    "input": { "sql": "SELECT ..." },
    "result": { "rows": [...], "row_count": 5 }
  },
  {
    "tool": "query_aws_costs",
    "input": { "start_date": "2026-04-01", "end_date": "2026-04-15" },
    "result": { "total_cost": 1234.56, "services": [...] }
  }
]
```

Download filename: `parsec-YYYY-MM-DD-HHmmss.json`

### CSV Export

Heuristic to extract tabular data from tool results:

1. For each tool result, scan for fields that are arrays of objects (e.g., `result.rows`, `result.data`, `result.instances`, `result.services`, `result.agreements`, or the result itself if it's an array)
2. Extract column headers from the union of all object keys in the first array found
3. If multiple tool calls produced tabular data, separate sections with a blank row and a header comment row (`# tool_name`)
4. Properly escape values per RFC 4180 (quote fields containing commas, quotes, or newlines; double-escape internal quotes)
5. If no tabular data is found in any tool result, fall back to a two-column `key,value` layout of the top-level result fields

Download filename: `parsec-YYYY-MM-DD-HHmmss.csv`

### UI Changes

Add "Export CSV" and "Export JSON" buttons to `createResponseExportBar()`:

```
[Export CSV] [Export JSON] [Export MD] [Export PDF] [Share]
```

The CSV and JSON buttons only render if `_exportToolResults` has entries. Text-only responses (no tool calls) continue to show only MD/PDF/Share.

Button styling reuses the existing `.response-export-btn` class — no new CSS needed.

### Files Modified

- `static/app.js` — data capture in streaming loop, two new export functions, button creation in `createResponseExportBar()`, data reconstruction in `renderSharedMessages()`

### No New Dependencies

CSV generation uses manual string building — same pattern as the existing chart CSV export at line ~1122 of `app.js`. JSON uses native `JSON.stringify`. No PapaParse, SheetJS, or npm packages needed.

## Out of Scope

- Per-tool-call export buttons (could add later if needed)
- Server-side export endpoints
- Excel (.xlsx) format
