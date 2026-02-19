/* Parsec — Chat UI with SSE streaming */

const messagesEl = document.getElementById("messages");
const form = document.getElementById("query-form");
const input = document.getElementById("question");
const sendBtn = document.getElementById("send-btn");

let conversationHistory = [];

// Open all markdown links in new tabs
var renderer = new marked.Renderer();
renderer.link = function(href, title, text) {
    // marked v12+ passes an object as first arg
    if (typeof href === "object") {
        text = href.text;
        title = href.title;
        href = href.href;
    }
    var titleAttr = title ? ' title="' + title + '"' : "";
    return '<a href="' + href + '"' + titleAttr + ' target="_blank" rel="noopener">' + text + "</a>";
};
marked.setOptions({ renderer: renderer });

// Auth check and welcome message on load
(async function checkAuthAndShowWelcome() {
    try {
        const resp = await fetch("/api/auth/check");
        if (resp.status === 403) {
            // User is authenticated but not authorized
            const err = await resp.json().catch(() => ({}));
            document.getElementById("query-form").style.display = "none";
            const el = document.createElement("div");
            el.className = "access-denied";
            el.innerHTML =
                "<h2>Access Denied</h2>" +
                "<p>" + (err.detail || "You are not authorized to use Parsec.") + "</p>" +
                "<p>If you believe this is an error, contact an RHDP administrator " +
                "to be added to an authorized group.</p>";
            messagesEl.appendChild(el);
            return;
        }
    } catch (e) {
        // Network error or no proxy (local dev) — proceed normally
    }

    // Authorized (or local dev with no proxy) — show welcome
    const el = addMessage("assistant", "");
    const contentEl = el.querySelector(".content");
    const textEl = document.createElement("div");
    textEl.className = "md-text";
    textEl.innerHTML = marked.parse(
        "**Hi, I'm Parsec** — a natural language investigation assistant for RHDP cloud costs.\n\n" +
        "I can help you with things like:\n" +
        "- \"Who are the top GPU users this week?\"\n" +
        "- \"How much did we spend on AWS yesterday?\"\n" +
        "- \"Show me external users with 50+ provisions since December\"\n" +
        "- \"How much does a g4dn.xlarge cost?\"\n" +
        "- \"Chart the top 10 AWS services by cost this month\"\n" +
        "- \"Generate a report of suspicious activity\"\n\n" +
        "I'm still learning! My instructions are in " +
        "[`config/agent_instructions.md`](https://github.com/rhpds/parsec/blob/main/config/agent_instructions.md) " +
        "in the [parsec repo](https://github.com/rhpds/parsec) " +
        "— PRs welcome \uD83D\uDE01"
    );
    contentEl.appendChild(textEl);

    // Auto-submit if ?q= URL parameter is present (e.g. from Slack alert links)
    const urlParams = new URLSearchParams(window.location.search);
    const injectedQuery = urlParams.get("q");
    if (injectedQuery) {
        // Clear the URL parameter so refreshes don't re-submit
        window.history.replaceState({}, "", window.location.pathname);
        input.value = injectedQuery;
        // Trigger form submit after a brief delay to let the UI render
        setTimeout(function() { form.requestSubmit(); }, 300);
    }
})();

form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (!question) return;

    input.value = "";
    sendBtn.disabled = true;

    addMessage("user", question);

    const assistantEl = addMessage("assistant", "");
    const contentEl = assistantEl.querySelector(".content");

    const statusEl = document.createElement("div");
    statusEl.className = "status";
    statusEl.innerHTML = '<div class="spinner"></div> Thinking...';
    contentEl.appendChild(statusEl);

    let fullText = "";
    let currentToolEl = null;
    let toolElements = {};   // Map tool name to element for finalization
    let streamStarted = false;
    let textChunks = [];     // Array of text segments between tool calls
    let currentChunk = "";   // Current text being accumulated
    let chartCanvases = [];  // Track chart canvases for export

    function ensureStreamStarted() {
        if (!streamStarted) {
            statusEl.remove();
            streamStarted = true;
        }
    }

    function renderCurrentText() {
        // Render the current chunk into a text element
        let textEl = contentEl.querySelector(".md-text-live");
        if (!textEl) {
            textEl = document.createElement("div");
            textEl.className = "md-text-live";
            contentEl.appendChild(textEl);
        }
        textEl.innerHTML = marked.parse(currentChunk);
    }

    function processEvent(eventType, data) {
        switch (eventType) {
            case "text": {
                ensureStreamStarted();
                // Remove status indicator when real text arrives
                const si = contentEl.querySelector(".status-indicator");
                if (si) si.remove();
                fullText += data.content;
                currentChunk += data.content;
                renderCurrentText();
                scrollToBottom();
                break;
            }

            case "tool_start": {
                ensureStreamStarted();
                // Finalize any previous tool that didn't get a result
                if (currentToolEl) {
                    var prevStatus = currentToolEl.querySelector(".tool-status");
                    if (prevStatus && prevStatus.classList.contains("running")) {
                        prevStatus.className = "tool-status done";
                        prevStatus.textContent = "done";
                    }
                }
                // Save current text chunk as intermediate thinking
                if (currentChunk.trim()) {
                    textChunks.push(currentChunk);
                }
                currentChunk = "";
                // Remove the live text element — it'll be collapsed later
                const liveEl = contentEl.querySelector(".md-text-live");
                if (liveEl) liveEl.remove();

                currentToolEl = createToolCall(data.tool, data.input);
                toolElements[data.tool + "_" + Object.keys(toolElements).length] = currentToolEl;
                contentEl.appendChild(currentToolEl);
                scrollToBottom();
                break;
            }

            case "tool_result": {
                if (currentToolEl) {
                    finalizeToolCall(currentToolEl, data.tool, data.result);
                    currentToolEl = null;
                }
                scrollToBottom();
                break;
            }

            case "chart": {
                ensureStreamStarted();
                const chartEl = renderChart(data);
                contentEl.appendChild(chartEl);
                const chartCanvas = chartEl.querySelector("canvas");
                if (chartCanvas) {
                    chartCanvases.push({ title: data.title || "chart", canvas: chartCanvas });
                }
                scrollToBottom();
                break;
            }

            case "report": {
                const link = document.createElement("a");
                link.className = "report-download";
                link.href = data.url;
                link.download = data.filename;
                link.textContent = "Download report: " + data.filename;
                contentEl.appendChild(link);
                scrollToBottom();
                break;
            }

            case "status": {
                ensureStreamStarted();
                // Remove previous status indicator if any
                const oldStatus = contentEl.querySelector(".status-indicator");
                if (oldStatus) oldStatus.remove();
                const si = document.createElement("div");
                si.className = "status-indicator";
                si.innerHTML = '<div class="spinner"></div> ' + data.message;
                contentEl.appendChild(si);
                scrollToBottom();
                break;
            }

            case "error": {
                ensureStreamStarted();
                const errEl = document.createElement("div");
                errEl.className = "error-message";
                errEl.textContent = data.message;
                contentEl.appendChild(errEl);
                scrollToBottom();
                break;
            }

            case "history":
                // Store full message history (includes tool calls/results)
                conversationHistory = data.messages;
                break;

            case "done": {
                // Finalize any tools still showing "running"
                contentEl.querySelectorAll(".tool-status.running").forEach(function(s) {
                    s.className = "tool-status done";
                    s.textContent = "done";
                });

                // Collect tool calls and intermediate thinking text
                const toolCalls = contentEl.querySelectorAll(".tool-call");
                if (toolCalls.length > 0 || textChunks.length > 0) {
                    const wrapper = document.createElement("details");
                    wrapper.className = "tool-calls-summary";
                    const summary = document.createElement("summary");
                    const qCount = toolCalls.length;
                    const label = qCount === 1 ? "1 query executed" : qCount + " queries executed";
                    summary.textContent = label;
                    wrapper.appendChild(summary);
                    const inner = document.createElement("div");
                    inner.className = "tool-calls-inner";

                    // Interleave thinking text and tool calls
                    // The thinking chunks were saved before each tool_start
                    let chunkIdx = 0;
                    const allToolCalls = Array.from(toolCalls);
                    allToolCalls.forEach(tc => {
                        if (chunkIdx < textChunks.length) {
                            const thinkEl = document.createElement("div");
                            thinkEl.className = "thinking-text";
                            thinkEl.innerHTML = marked.parse(textChunks[chunkIdx]);
                            inner.appendChild(thinkEl);
                            chunkIdx++;
                        }
                        inner.appendChild(tc);
                    });
                    // Any remaining thinking chunks
                    while (chunkIdx < textChunks.length) {
                        const thinkEl = document.createElement("div");
                        thinkEl.className = "thinking-text";
                        thinkEl.innerHTML = marked.parse(textChunks[chunkIdx]);
                        inner.appendChild(thinkEl);
                        chunkIdx++;
                    }

                    wrapper.appendChild(inner);
                    // Insert at the top of content
                    contentEl.insertBefore(wrapper, contentEl.firstChild);
                }

                // Render the final answer (currentChunk is the text after the last tool call)
                const liveEl = contentEl.querySelector(".md-text-live");
                if (liveEl) {
                    liveEl.className = "md-text";
                }

                // Store export data and add export buttons
                assistantEl._exportMarkdown = currentChunk || fullText;
                assistantEl._exportCharts = chartCanvases;
                if (fullText.trim() || currentChunk.trim() || chartCanvases.length > 0) {
                    contentEl.appendChild(createResponseExportBar(assistantEl));
                }

                scrollToBottom();
                break;
            }
        }
    }

    try {
        const response = await fetch("/api/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                question,
                conversation_history: conversationHistory.length > 0 ? conversationHistory : null,
            }),
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(err.detail || "HTTP " + response.status);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const { done, value } = reader.readSync ? reader.readSync() : await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop();

            let eventType = null;
            for (const line of lines) {
                if (line.startsWith("event: ")) {
                    eventType = line.slice(7).trim();
                } else if (line.startsWith("data: ") && eventType) {
                    try {
                        const data = JSON.parse(line.slice(6));
                        processEvent(eventType, data);
                    } catch (parseErr) {
                        console.warn("Failed to parse SSE data:", line);
                    }
                    eventType = null;
                }
            }
        }
    } catch (err) {
        if (!streamStarted) statusEl.remove();
        const errorEl = document.createElement("div");
        errorEl.className = "error-message";
        errorEl.textContent = err.message;
        contentEl.appendChild(errorEl);
    }

    // History is updated via the "history" SSE event from the server,
    // which includes the full message array with tool calls and results.

    sendBtn.disabled = false;
    input.focus();
});

function addMessage(role, text) {
    const el = document.createElement("div");
    el.className = "message " + role;

    if (role === "user") {
        const lines = text.split("\n");
        const isLong = text.length > 300 || lines.length > 4;
        if (isLong) {
            const preview = lines.slice(0, 3).join("\n").substring(0, 200);
            const details = document.createElement("details");
            details.className = "user-query-details";
            const summary = document.createElement("summary");
            summary.textContent = preview + (preview.length < text.length ? "…" : "");
            details.appendChild(summary);
            const full = document.createElement("div");
            full.className = "user-query-full";
            full.textContent = text;
            details.appendChild(full);
            el.appendChild(details);
        } else {
            el.textContent = text;
        }
    } else {
        const contentEl = document.createElement("div");
        contentEl.className = "content";
        el.appendChild(contentEl);
    }

    messagesEl.appendChild(el);
    scrollToBottom();
    return el;
}

function createToolCall(toolName, toolInput) {
    const details = document.createElement("details");
    details.className = "tool-call";

    const summary = document.createElement("summary");
    const nameSpan = document.createElement("span");
    nameSpan.className = "tool-name";
    nameSpan.textContent = toolName;

    const statusSpan = document.createElement("span");
    statusSpan.className = "tool-status running";
    statusSpan.textContent = "running...";

    summary.appendChild(nameSpan);
    summary.appendChild(statusSpan);
    details.appendChild(summary);

    const body = document.createElement("div");
    body.className = "tool-body";

    if (toolName === "query_provisions_db" && toolInput.sql) {
        body.textContent = toolInput.sql;
    } else if (toolName === "query_cloudtrail" && toolInput.query) {
        statusSpan.textContent = "scanning CloudTrail Lake...";
        body.textContent = toolInput.query;
    } else if (toolName === "query_aws_account") {
        statusSpan.textContent = "querying account " + (toolInput.account_id || "") + "...";
        body.textContent = (toolInput.action || "") + " in " + (toolInput.account_id || "");
    } else if (toolName === "generate_report") {
        body.textContent = "Generating " + (toolInput.format || "markdown") + " report: " + (toolInput.title || "");
    } else {
        body.textContent = JSON.stringify(toolInput, null, 2);
    }

    details.appendChild(body);
    return details;
}

function finalizeToolCall(toolEl, toolName, result) {
    const statusSpan = toolEl.querySelector(".tool-status");
    if (result.error) {
        statusSpan.className = "tool-status error";
        statusSpan.textContent = "error";
    } else {
        statusSpan.className = "tool-status done";
        if (result.bytes_scanned !== undefined && result.row_count !== undefined) {
            var mb = (result.bytes_scanned / 1024 / 1024).toFixed(0);
            statusSpan.textContent = result.row_count + " rows (" + mb + " MB scanned)";
        } else if (result.row_count !== undefined) {
            statusSpan.textContent = result.row_count + " rows";
        } else if (result.instance_count !== undefined) {
            statusSpan.textContent = result.instance_count + " instances";
        } else if (result.user_count !== undefined) {
            statusSpan.textContent = result.user_count + " users";
        } else if (result.agreement_count !== undefined) {
            statusSpan.textContent = result.agreement_count + " agreements";
        } else if (result.event_count !== undefined) {
            statusSpan.textContent = result.event_count + " events";
        } else if (result.total_cost !== undefined) {
            statusSpan.textContent = "$" + result.total_cost.toLocaleString();
        } else if (result.filename) {
            statusSpan.textContent = result.filename;
        } else {
            statusSpan.textContent = "done";
        }
    }

    const body = toolEl.querySelector(".tool-body");
    body.textContent += "\n\n--- Result ---\n" + JSON.stringify(result, null, 2);
}

const CHART_COLORS = [
    "#7aa2f7", "#9ece6a", "#e0af68", "#f7768e", "#bb9af7",
    "#7dcfff", "#73daca", "#ff9e64", "#c0caf5", "#a9b1d6",
];

function renderChart(data) {
    const wrapper = document.createElement("div");
    wrapper.className = "chart-container";
    const canvas = document.createElement("canvas");
    wrapper.appendChild(canvas);

    const datasets = (data.datasets || []).map(function(ds, i) {
        var colors = CHART_COLORS[i % CHART_COLORS.length];
        var config = {
            label: ds.label,
            data: ds.data,
        };

        if (data.chart_type === "pie" || data.chart_type === "doughnut") {
            config.backgroundColor = ds.data.map(function(_, j) {
                return CHART_COLORS[j % CHART_COLORS.length];
            });
            config.borderColor = "#1a1b26";
            config.borderWidth = 2;
        } else {
            config.backgroundColor = colors + "99";
            config.borderColor = colors;
            config.borderWidth = 2;
        }

        return config;
    });

    // Auto-detect if values span multiple orders of magnitude → use log scale
    var allValues = datasets.flatMap(function(ds) { return ds.data; }).filter(function(v) { return v > 0; });
    var useLog = false;
    if (allValues.length >= 2) {
        var maxVal = Math.max.apply(null, allValues);
        var minVal = Math.min.apply(null, allValues);
        if (minVal > 0 && maxVal / minVal > 100) {
            useLog = true;
        }
    }

    var chartInstance = new Chart(canvas, {
        type: data.chart_type,
        data: {
            labels: data.labels,
            datasets: datasets,
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                title: {
                    display: true,
                    text: data.title + (useLog ? " (log scale)" : ""),
                    color: "#c0caf5",
                    font: { size: 14 },
                },
                legend: {
                    labels: { color: "#a9b1d6", font: { size: 11 } },
                },
            },
            scales: (data.chart_type === "pie" || data.chart_type === "doughnut") ? {} : {
                x: {
                    ticks: { color: "#565f89", font: { size: 11 } },
                    grid: { color: "#3b4261" },
                },
                y: {
                    type: useLog ? "logarithmic" : "linear",
                    ticks: { color: "#565f89", font: { size: 11 } },
                    grid: { color: "#3b4261" },
                },
            },
        },
    });

    // Export buttons
    var exportBar = document.createElement("div");
    exportBar.className = "chart-export-bar";

    var pngBtn = document.createElement("button");
    pngBtn.className = "chart-export-btn";
    pngBtn.textContent = "Export PNG";
    pngBtn.addEventListener("click", function() {
        var link = document.createElement("a");
        link.download = (data.title || "chart").replace(/[^a-z0-9]/gi, "_") + ".png";
        link.href = canvas.toDataURL("image/png");
        link.click();
    });

    var csvBtn = document.createElement("button");
    csvBtn.className = "chart-export-btn";
    csvBtn.textContent = "Export CSV";
    csvBtn.addEventListener("click", function() {
        var rows = ["Label," + data.datasets.map(function(ds) { return ds.label; }).join(",")];
        data.labels.forEach(function(label, i) {
            var vals = data.datasets.map(function(ds) { return ds.data[i]; });
            rows.push(label + "," + vals.join(","));
        });
        var blob = new Blob([rows.join("\n")], { type: "text/csv" });
        var link = document.createElement("a");
        link.download = (data.title || "chart").replace(/[^a-z0-9]/gi, "_") + ".csv";
        link.href = URL.createObjectURL(blob);
        link.click();
    });

    exportBar.appendChild(pngBtn);
    exportBar.appendChild(csvBtn);
    wrapper.appendChild(exportBar);

    return wrapper;
}

function createResponseExportBar(messageEl) {
    var bar = document.createElement("div");
    bar.className = "response-export-bar";

    var mdBtn = document.createElement("button");
    mdBtn.className = "response-export-btn";
    mdBtn.textContent = "Export MD";
    mdBtn.addEventListener("click", function() { exportResponseAsMarkdown(messageEl); });

    var pdfBtn = document.createElement("button");
    pdfBtn.className = "response-export-btn";
    pdfBtn.textContent = "Export PDF";
    pdfBtn.addEventListener("click", function() { exportResponseAsPDF(messageEl); });

    bar.appendChild(mdBtn);
    bar.appendChild(pdfBtn);
    return bar;
}

function exportResponseAsMarkdown(messageEl) {
    var md = messageEl._exportMarkdown || "";
    var charts = messageEl._exportCharts || [];

    // Append chart images as base64 inline images
    charts.forEach(function(c) {
        var dataUrl = c.canvas.toDataURL("image/png");
        md += "\n\n![" + c.title + "](" + dataUrl + ")\n";
    });

    var blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    var link = document.createElement("a");
    var timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".md";
    link.href = URL.createObjectURL(blob);
    link.click();
}

function exportResponseAsPDF(messageEl) {
    var contentEl = messageEl.querySelector(".content");
    var clone = contentEl.cloneNode(true);

    // Remove export bars and tool summaries from clone
    clone.querySelectorAll(".response-export-bar, .chart-export-bar, .tool-calls-summary").forEach(function(el) {
        el.remove();
    });

    // Replace canvases with static images
    var origCanvases = contentEl.querySelectorAll("canvas");
    var cloneCanvases = clone.querySelectorAll("canvas");
    for (var i = 0; i < origCanvases.length; i++) {
        var img = document.createElement("img");
        img.src = origCanvases[i].toDataURL("image/png");
        img.style.maxWidth = "100%";
        cloneCanvases[i].parentNode.replaceChild(img, cloneCanvases[i]);
    }

    // Apply light theme inline styles for readable PDF
    clone.style.background = "#ffffff";
    clone.style.color = "#1a1a1a";
    clone.style.fontFamily = "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif";
    clone.style.fontSize = "13px";
    clone.style.lineHeight = "1.6";
    clone.style.padding = "20px";
    clone.style.width = "550px";

    clone.querySelectorAll("h1, h2, h3").forEach(function(el) { el.style.color = "#1a1a1a"; });
    clone.querySelectorAll("th").forEach(function(el) {
        el.style.background = "#f0f0f0";
        el.style.color = "#1a1a1a";
        el.style.borderColor = "#ccc";
    });
    clone.querySelectorAll("td").forEach(function(el) { el.style.borderColor = "#ccc"; });
    clone.querySelectorAll("code").forEach(function(el) {
        el.style.background = "#f0f0f0";
        el.style.color = "#1a1a1a";
    });
    clone.querySelectorAll("pre").forEach(function(el) { el.style.background = "#f0f0f0"; });
    clone.querySelectorAll("a").forEach(function(el) { el.style.color = "#2563eb"; });

    // Add clone to DOM visually hidden but still renderable by html2canvas
    clone.style.position = "fixed";
    clone.style.top = "0";
    clone.style.left = "0";
    clone.style.zIndex = "-1";
    document.body.appendChild(clone);

    html2canvas(clone, { scale: 2, useCORS: true }).then(function(canvas) {
        document.body.removeChild(clone);

        var jsPDF = window.jspdf.jsPDF;

        // A4 dimensions in pt
        var pageW = 595.28;
        var pageH = 841.89;
        var margin = 20;
        var contentW = pageW - margin * 2;
        var contentH = pageH - margin * 2;

        // How many source pixels correspond to one page of content
        var scale = canvas.width / contentW;
        var sliceH = Math.floor(contentH * scale);

        var doc = new jsPDF({ orientation: "portrait", unit: "pt", format: "a4" });
        var yPx = 0;
        var pageNum = 0;

        while (yPx < canvas.height) {
            if (pageNum > 0) doc.addPage();
            var h = Math.min(sliceH, canvas.height - yPx);

            // Crop this page's slice from the full canvas
            var pageCanvas = document.createElement("canvas");
            pageCanvas.width = canvas.width;
            pageCanvas.height = h;
            var ctx = pageCanvas.getContext("2d");
            ctx.drawImage(canvas, 0, yPx, canvas.width, h, 0, 0, canvas.width, h);

            var pageImg = pageCanvas.toDataURL("image/png");
            var drawH = h / scale;
            doc.addImage(pageImg, "PNG", margin, margin, contentW, drawH);

            yPx += sliceH;
            pageNum++;
        }

        var timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
        doc.save("parsec-" + timestamp + ".pdf");
    });
}

function scrollToBottom() {
    const chat = document.getElementById("chat");
    chat.scrollTop = chat.scrollHeight;
}
