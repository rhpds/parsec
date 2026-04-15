# CSV and JSON Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CSV and JSON download buttons to the response export bar so users can export structured tool results.

**Architecture:** Capture tool result objects during SSE streaming into a `_exportToolResults` array on each assistant message element. Two new functions (`exportResponseAsCSV`, `exportResponseAsJSON`) serialize this data. No backend changes.

**Tech Stack:** Vanilla JavaScript (no new dependencies)

**Spec:** `docs/superpowers/specs/2026-04-15-csv-json-export-design.md`

---

### Task 1: Capture tool results during streaming

**Files:**
- Modify: `static/app.js` (lines ~499, ~581-587, ~799-801)

- [ ] **Step 1: Add `toolResults` accumulator to streaming state**

After line 499 (`let liveToolCount = 0;`), add:

```js
let toolResults = [];    // Captured tool results for CSV/JSON export
```

- [ ] **Step 2: Track current tool name and input for pairing with results**

After line 503 (`let liveToolCount = 0;` block), add:

```js
let currentToolName = null;
let currentToolInput = null;
```

- [ ] **Step 3: Capture tool name/input on tool_start**

In the `tool_start` case, after line 574 (`currentToolEl = createToolCall(data.tool, data.input);`), add:

```js
currentToolName = data.tool;
currentToolInput = data.input;
```

- [ ] **Step 4: Push tool result on tool_result**

In the `tool_result` case, before `currentToolEl = null;` (line 584), add:

```js
toolResults.push({ tool: currentToolName || data.tool, input: currentToolInput || {}, result: data.result });
currentToolName = null;
currentToolInput = null;
```

- [ ] **Step 5: Store toolResults on message element at stream end**

In the `done` case, after line 801 (`assistantEl._exportCharts = chartCanvases;`), add:

```js
assistantEl._exportToolResults = toolResults;
```

- [ ] **Step 6: Update the export bar visibility condition**

Change the condition on line 802 from:

```js
if (fullText.trim() || currentChunk.trim() || chartCanvases.length > 0) {
```

to:

```js
if (fullText.trim() || currentChunk.trim() || chartCanvases.length > 0 || toolResults.length > 0) {
```

- [ ] **Step 7: Test manually**

Run: `scripts/local-server.sh restart`

Open Parsec, ask a question that triggers tool calls (e.g., "how many provisions today?"). Open browser devtools, select the assistant message element, and verify `$0._exportToolResults` is an array with entries containing `tool`, `input`, and `result`.

- [ ] **Step 8: Commit**

```bash
git add static/app.js
git commit -m "feat: capture tool results for CSV/JSON export"
```

---

### Task 2: Restore tool results for loaded conversations

**Files:**
- Modify: `static/app.js` (in `renderSharedMessages` function, lines ~1302-1513)

- [ ] **Step 1: Initialize restoredToolResults array**

In `renderSharedMessages`, inside the `msg.role === "assistant"` branch, after line 1334 (`var restoredCharts = [];`), add:

```js
var restoredToolResults = [];
```

- [ ] **Step 2: Capture tool results from toolResultMap during restoration**

Inside the `toolCalls.forEach` loop at line 1364, after the delegation check block (after the closing `}` of the `if (tc.name === "delegate_to_agent"` block at ~line 1371), add before the closing `});` of the forEach:

```js
// Capture for CSV/JSON export (skip delegations — their sub-results aren't directly exportable)
if (tc.name !== "delegate_to_agent" && result && typeof result === "object" && !result.error) {
    restoredToolResults.push({ tool: tc.name, input: tc.input || {}, result: result });
}
```

- [ ] **Step 3: Also capture delegate_to_agent sub-results**

Inside the `delegations.forEach` at ~line 1419, the `result` object from a delegation may contain aggregated findings but not individual tool results. We already capture direct tool calls above, so delegated sub-agent results get captured at the orchestrator level. No additional code needed here — delegated results are opaque summaries, not raw query data.

- [ ] **Step 4: Store restoredToolResults on the message element**

In the export bar section at line 1509-1513, change:

```js
// Add export bar to restored assistant messages
if (restoredText.trim()) {
    el._exportMarkdown = restoredText;
    el._exportCharts = restoredCharts;
    contentEl.appendChild(createResponseExportBar(el));
}
```

to:

```js
// Add export bar to restored assistant messages
if (restoredText.trim() || restoredToolResults.length > 0) {
    el._exportMarkdown = restoredText;
    el._exportCharts = restoredCharts;
    el._exportToolResults = restoredToolResults;
    contentEl.appendChild(createResponseExportBar(el));
}
```

- [ ] **Step 5: Test manually**

Restart local server. Ask a query that triggers tool calls. Reload the page (conversation restores from localStorage). Verify `_exportToolResults` is populated on restored messages via devtools.

- [ ] **Step 6: Commit**

```bash
git add static/app.js
git commit -m "feat: restore tool results for export in loaded conversations"
```

---

### Task 3: Implement JSON export function

**Files:**
- Modify: `static/app.js` (add new function after `exportResponseAsPDF`, ~line 1300)

- [ ] **Step 1: Add exportResponseAsJSON function**

After the `exportResponseAsPDF` function (after line 1300), add:

```js
function exportResponseAsJSON(messageEl) {
    var toolResults = messageEl._exportToolResults || [];
    if (toolResults.length === 0) return;

    var json = JSON.stringify(toolResults, null, 2);
    var blob = new Blob([json], { type: "application/json;charset=utf-8" });
    var link = document.createElement("a");
    var timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".json";
    link.href = URL.createObjectURL(blob);
    link.click();
}
```

- [ ] **Step 2: Test manually**

This function won't be wired up to the UI yet, but you can test it in devtools. After a query with tool calls, find the assistant message element and run:

```js
exportResponseAsJSON(document.querySelector('.message.assistant:last-of-type'))
```

Verify a `.json` file downloads with the expected structure.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat: add JSON export function for tool results"
```

---

### Task 4: Implement CSV export function

**Files:**
- Modify: `static/app.js` (add new function after `exportResponseAsJSON`)

- [ ] **Step 1: Add csvEscapeField helper**

After the `exportResponseAsJSON` function, add:

```js
function csvEscapeField(value) {
    if (value === null || value === undefined) return "";
    var str = String(value);
    if (str.indexOf(",") >= 0 || str.indexOf('"') >= 0 || str.indexOf("\n") >= 0) {
        return '"' + str.replace(/"/g, '""') + '"';
    }
    return str;
}
```

- [ ] **Step 2: Add findTabularData helper**

After `csvEscapeField`, add:

```js
function findTabularData(result) {
    // Look for arrays of objects in the result
    if (Array.isArray(result) && result.length > 0 && typeof result[0] === "object") {
        return result;
    }
    if (typeof result !== "object" || result === null) return null;
    // Search top-level fields for the first array of objects
    var keys = Object.keys(result);
    for (var i = 0; i < keys.length; i++) {
        var val = result[keys[i]];
        if (Array.isArray(val) && val.length > 0 && typeof val[0] === "object" && val[0] !== null) {
            return val;
        }
    }
    return null;
}
```

- [ ] **Step 3: Add exportResponseAsCSV function**

After `findTabularData`, add:

```js
function exportResponseAsCSV(messageEl) {
    var toolResults = messageEl._exportToolResults || [];
    if (toolResults.length === 0) return;

    var csvSections = [];

    toolResults.forEach(function(tr) {
        var rows = findTabularData(tr.result);
        if (!rows) return;

        // Collect all unique column headers across all rows
        var headers = [];
        var headerSet = {};
        rows.forEach(function(row) {
            Object.keys(row).forEach(function(key) {
                if (!headerSet[key]) {
                    headerSet[key] = true;
                    headers.push(key);
                }
            });
        });

        var lines = [];
        // Section header comment
        lines.push("# " + (tr.tool || "results"));
        // Column headers
        lines.push(headers.map(csvEscapeField).join(","));
        // Data rows
        rows.forEach(function(row) {
            var vals = headers.map(function(h) {
                var val = row[h];
                if (typeof val === "object" && val !== null) val = JSON.stringify(val);
                return csvEscapeField(val);
            });
            lines.push(vals.join(","));
        });

        csvSections.push(lines.join("\n"));
    });

    // Fallback: if no tabular data found, export as key-value pairs
    if (csvSections.length === 0) {
        toolResults.forEach(function(tr) {
            var lines = [];
            lines.push("# " + (tr.tool || "results"));
            lines.push("key,value");
            Object.keys(tr.result || {}).forEach(function(key) {
                var val = tr.result[key];
                if (typeof val === "object" && val !== null) val = JSON.stringify(val);
                lines.push(csvEscapeField(key) + "," + csvEscapeField(val));
            });
            csvSections.push(lines.join("\n"));
        });
    }

    var csv = csvSections.join("\n\n");
    var blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    var link = document.createElement("a");
    var timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".csv";
    link.href = URL.createObjectURL(blob);
    link.click();
}
```

- [ ] **Step 4: Test manually via devtools**

After a query with tool calls (e.g., "show me the top 10 external users by provisions this month"), run in devtools:

```js
exportResponseAsCSV(document.querySelector('.message.assistant:last-of-type'))
```

Verify a `.csv` file downloads. Open it in a spreadsheet app and confirm columns/rows look correct.

- [ ] **Step 5: Commit**

```bash
git add static/app.js
git commit -m "feat: add CSV export function for tool results"
```

---

### Task 5: Wire up buttons in the export bar

**Files:**
- Modify: `static/app.js` (in `createResponseExportBar` function, lines ~1141-1189)

- [ ] **Step 1: Add CSV and JSON buttons to createResponseExportBar**

In `createResponseExportBar`, after the `var bar` declaration (line 1142-1143), add the CSV and JSON buttons before the existing MD button:

```js
var toolResults = messageEl._exportToolResults || [];

if (toolResults.length > 0) {
    var csvBtn = document.createElement("button");
    csvBtn.className = "response-export-btn";
    csvBtn.textContent = "Export CSV";
    csvBtn.addEventListener("click", function() { exportResponseAsCSV(messageEl); });
    bar.appendChild(csvBtn);

    var jsonBtn = document.createElement("button");
    jsonBtn.className = "response-export-btn";
    jsonBtn.textContent = "Export JSON";
    jsonBtn.addEventListener("click", function() { exportResponseAsJSON(messageEl); });
    bar.appendChild(jsonBtn);
}
```

The existing `bar.appendChild(mdBtn)` / `pdfBtn` / `shareBtn` lines remain unchanged after this block.

- [ ] **Step 2: Test end-to-end**

Restart local server: `scripts/local-server.sh restart`

1. Ask a question that triggers tool calls (e.g., "who are the top GPU users this week?")
2. Verify "Export CSV" and "Export JSON" buttons appear in the export bar before the existing buttons
3. Click "Export CSV" — verify a `.csv` file downloads with tabular data
4. Click "Export JSON" — verify a `.json` file downloads with the full tool result array
5. Ask a text-only follow-up question (e.g., "thanks") — verify CSV/JSON buttons do NOT appear (only MD/PDF/Share)
6. Reload the page — verify CSV/JSON buttons appear on restored messages that had tool calls

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat: add CSV and JSON export buttons to response export bar"
```

---

### Task 6: Final verification

- [ ] **Step 1: Test CSV edge cases**

Ask queries that produce different data shapes:
- SQL query with tabular rows (e.g., "show me provisions for user@redhat.com")
- AWS cost query with nested service data
- A query with multiple tool calls — verify CSV has multiple sections separated by blank lines

- [ ] **Step 2: Test JSON edge cases**

- Verify JSON is valid and pretty-printed
- Open in a JSON viewer and confirm structure matches the spec

- [ ] **Step 3: Test restored conversations**

- Load a conversation from sidebar history
- Verify CSV/JSON buttons appear on messages that had tool calls
- Verify the exports work correctly on restored data

- [ ] **Step 4: Test shared sessions**

- Click Share, open the share link in another tab
- Click "Continue Investigation"
- Verify CSV/JSON export works on the continued conversation
