/* Parsec — Chat UI with SSE streaming */

const messagesEl = document.getElementById("messages");
const form = document.getElementById("query-form");
const input = document.getElementById("question");
const sendBtn = document.getElementById("send-btn");

let conversationHistory = [];
let currentConversationId = null;

// Auto-resize textarea to fit content (up to a max height)
function autoResizeInput() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
}
input.addEventListener("input", autoResizeInput);

// Enter submits, Shift+Enter inserts newline
input.addEventListener("keydown", function(e) {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
    }
});

// Restore conversation history from localStorage (survives page refresh)
try {
    const saved = localStorage.getItem("parsec_history");
    if (saved) conversationHistory = JSON.parse(saved);
    currentConversationId = localStorage.getItem("parsec_conv_id") || null;
} catch (e) {
    // Ignore corrupt data
}

// New Chat button — saves current, clears, and reloads
document.getElementById("new-chat-btn").addEventListener("click", function() {
    conversationHistory = [];
    currentConversationId = null;
    localStorage.removeItem("parsec_history");
    localStorage.removeItem("parsec_conv_id");
    messagesEl.textContent = "";
    window.location.reload();
});

// ─── Sidebar ───

var sidebarEl = document.getElementById("sidebar");
var sidebarListEl = document.getElementById("sidebar-list");
var sidebarTab = document.getElementById("sidebar-tab");

document.getElementById("sidebar-close-btn").addEventListener("click", function() {
    sidebarEl.classList.remove("open");
    sidebarTab.style.display = "block";
});

sidebarTab.addEventListener("click", function() {
    sidebarEl.classList.add("open");
    sidebarTab.style.display = "none";
    loadConversationList();
});

// Load conversation list on page load (sidebar starts open)
loadConversationList();

// ─── Learnings panel (admin only) ───

(function initLearnings() {
    fetch("/api/learnings/check").then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data || !data.is_admin) return;
        var panel = document.getElementById("learnings-panel");
        panel.style.display = "block";
        refreshLearningsCount();
    }).catch(function() {});
})();

function refreshLearningsCount() {
    fetch("/api/learnings").then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data) return;
        var countEl = document.getElementById("learnings-count");
        if (data.has_learnings) {
            var entries = (data.content.match(/^- /gm) || []).length;
            countEl.textContent = entries + " entries";
        } else {
            countEl.textContent = "empty";
        }
    }).catch(function() {});
}

document.getElementById("learnings-view-btn").addEventListener("click", function() {
    fetch("/api/learnings").then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data) return;
        var textarea = document.getElementById("learnings-text");
        textarea.value = data.content || "(no learnings yet)";
        document.getElementById("learnings-modal").style.display = "flex";
    }).catch(function() {});
});

document.getElementById("learnings-copy-btn").addEventListener("click", function() {
    var textarea = document.getElementById("learnings-text");
    navigator.clipboard.writeText(textarea.value).then(function() {
        var btn = document.getElementById("learnings-copy-btn");
        btn.textContent = "Copied!";
        setTimeout(function() { btn.textContent = "Copy All"; }, 2000);
    });
});

document.getElementById("learnings-modal-close").addEventListener("click", function() {
    document.getElementById("learnings-modal").style.display = "none";
});

document.getElementById("learnings-modal").addEventListener("click", function(e) {
    if (e.target === this) this.style.display = "none";
});

document.getElementById("learnings-clear-btn").addEventListener("click", function() {
    if (!confirm("Delete all learnings? (Make sure you copied what you need first)")) return;
    fetch("/api/learnings", { method: "DELETE" }).then(function(resp) {
        if (resp.ok) refreshLearningsCount();
    });
});

function loadConversationList() {
    fetch("/api/conversations").then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data) return;
        renderConversationList(data.conversations || []);
    }).catch(function() {});
}

function renderConversationList(conversations) {
    sidebarListEl.textContent = "";
    if (conversations.length === 0) {
        var empty = document.createElement("div");
        empty.className = "sidebar-empty";
        empty.textContent = "No previous conversations";
        sidebarListEl.appendChild(empty);
        return;
    }
    conversations.forEach(function(conv) {
        var item = document.createElement("div");
        item.className = "sidebar-item";
        if (conv.id === currentConversationId) item.classList.add("active");

        var titleEl = document.createElement("div");
        titleEl.className = "sidebar-item-title";
        titleEl.textContent = conv.title;

        var metaEl = document.createElement("div");
        metaEl.className = "sidebar-item-meta";
        var date = new Date(conv.updated_at);
        metaEl.textContent = date.toLocaleDateString() + " \u00b7 " + conv.message_count + " msgs";

        var deleteBtn = document.createElement("button");
        deleteBtn.className = "sidebar-item-delete";
        deleteBtn.textContent = "\u00d7";
        deleteBtn.title = "Delete conversation";
        deleteBtn.addEventListener("click", function(e) {
            e.stopPropagation();
            if (!confirm("Delete this conversation?")) return;
            fetch("/api/conversations/" + conv.id, { method: "DELETE" }).then(function(resp) {
                if (resp.ok) {
                    item.remove();
                    if (conv.id === currentConversationId) {
                        currentConversationId = null;
                        localStorage.removeItem("parsec_conv_id");
                    }
                }
            });
        });

        item.appendChild(deleteBtn);
        item.appendChild(titleEl);
        item.appendChild(metaEl);

        item.addEventListener("click", function() {
            loadConversation(conv.id);
        });

        sidebarListEl.appendChild(item);
    });
}

function loadConversation(convId) {
    fetch("/api/conversations/" + convId).then(function(resp) {
        if (!resp.ok) throw new Error("Failed to load");
        return resp.json();
    }).then(function(data) {
        conversationHistory = data.messages || [];
        currentConversationId = data.id;
        try {
            localStorage.setItem("parsec_history", JSON.stringify(conversationHistory));
            localStorage.setItem("parsec_conv_id", currentConversationId);
        } catch (e) {}
        window.location.href = window.location.pathname;
    }).catch(function(err) {
        alert("Failed to load conversation: " + err.message);
    });
}

function saveConversation() {
    if (conversationHistory.length === 0) return;
    var body = {
        messages: conversationHistory,
    };
    if (currentConversationId) body.id = currentConversationId;
    fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    }).then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data) return;
        currentConversationId = data.id;
        try { localStorage.setItem("parsec_conv_id", data.id); } catch (e) {}
        loadConversationList();
        // Refresh learnings count after background analysis has time to complete
        setTimeout(refreshLearningsCount, 20000);
    }).catch(function() {});
}

// Share modal handlers
document.getElementById("share-copy-btn").addEventListener("click", function() {
    var input = document.getElementById("share-link-input");
    navigator.clipboard.writeText(input.value).then(function() {
        var btn = document.getElementById("share-copy-btn");
        btn.textContent = "Copied!";
        setTimeout(function() { btn.textContent = "Copy"; }, 2000);
    });
});

document.getElementById("share-close-btn").addEventListener("click", function() {
    document.getElementById("share-modal").style.display = "none";
});

document.getElementById("share-modal").addEventListener("click", function(e) {
    if (e.target === this) this.style.display = "none";
});

// Theme toggle — preference is applied in <head> to prevent flash
document.getElementById("theme-toggle-btn").addEventListener("click", function() {
    var current = document.documentElement.getAttribute("data-theme");
    var next = current === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("parsec_theme", next);
});

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
    el.id = "welcome-message";
    const contentEl = el.querySelector(".content");

    var welcomeShort = document.createElement("div");
    welcomeShort.className = "md-text welcome-short";
    welcomeShort.innerHTML = marked.parse(
        "**Hi, I'm Parsec** — a natural language investigation assistant for RHDP cloud costs and provisioning. " +
        "Ask me anything about costs, provisions, sandboxes, or usage."
    );
    contentEl.appendChild(welcomeShort);

    var welcomeFull = document.createElement("div");
    welcomeFull.className = "md-text welcome-full";
    welcomeFull.innerHTML = marked.parse(
        "I can help you with things like:\n" +
        "- \"What services does user@redhat.com have?\"\n" +
        "- \"What instances should clusterplatform.ocp4-aws.prod be running?\"\n" +
        "- \"What's deployed on sandbox1234?\"\n" +
        "- \"Who are the top GPU users this week?\"\n" +
        "- \"How much did we spend on AWS yesterday?\"\n" +
        "- \"Show me external users with 50+ provisions since December\"\n" +
        "- \"What workshops are running in user-user-redhat-com?\"\n" +
        "- \"Show me the active workshops on the east Babylon cluster\"\n" +
        "- \"Chart the top 10 AWS services by cost this month\"\n" +
        "- \"Generate a report of suspicious activity\"\n\n" +
        "I'm still learning! My instructions are in " +
        "[`config/agent_instructions.md`](https://github.com/rhpds/parsec/blob/main/config/agent_instructions.md) " +
        "in the [parsec repo](https://github.com/rhpds/parsec) " +
        "— PRs welcome \uD83D\uDE01"
    );
    contentEl.appendChild(welcomeFull);

    var welcomeToggle = document.createElement("button");
    welcomeToggle.className = "welcome-toggle";
    welcomeToggle.textContent = "Show examples";
    welcomeToggle.addEventListener("click", function() {
        var isExpanded = el.classList.toggle("welcome-expanded");
        welcomeToggle.textContent = isExpanded ? "Hide examples" : "Show examples";
    });
    contentEl.appendChild(welcomeToggle);

    // Restore previous conversation from localStorage (e.g. after "Continue Investigation")
    if (conversationHistory.length > 0) {
        renderSharedMessages(conversationHistory, true);
        scrollToBottom();
    }

    // Check for shared session link
    const urlParams = new URLSearchParams(window.location.search);
    const shareId = urlParams.get("share");
    if (shareId) {
        try {
            var shareResp = await fetch("/api/share/" + encodeURIComponent(shareId));
            if (shareResp.ok) {
                var shareData = await shareResp.json();
                document.getElementById("shared-banner").style.display = "flex";
                document.getElementById("query-form").style.display = "none";
                renderSharedMessages(shareData.messages);

                // Continue Investigation button
                document.getElementById("continue-btn").addEventListener("click", function() {
                    conversationHistory = shareData.messages;
                    try { localStorage.setItem("parsec_history", JSON.stringify(conversationHistory)); } catch (e) {}
                    window.location.href = window.location.pathname;
                });
                return;
            } else {
                var shareErr = await shareResp.json().catch(function() { return {}; });
                var errEl = document.createElement("div");
                errEl.className = "error-message";
                errEl.textContent = shareErr.detail || "Shared session not found";
                messagesEl.appendChild(errEl);
                return;
            }
        } catch (e) {
            var errEl2 = document.createElement("div");
            errEl2.className = "error-message";
            errEl2.textContent = "Failed to load shared session: " + e.message;
            messagesEl.appendChild(errEl2);
            return;
        }
    }

    // Auto-submit if ?q= URL parameter is present (e.g. from Slack alert links)
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
    input.style.height = "auto";
    sendBtn.disabled = true;

    // Collapse welcome message on first send
    var welcomeEl = document.getElementById("welcome-message");
    if (welcomeEl && welcomeEl.classList.contains("welcome-expanded")) {
        welcomeEl.classList.remove("welcome-expanded");
        var toggle = welcomeEl.querySelector(".welcome-toggle");
        if (toggle) toggle.textContent = "Show examples";
    }

    // Collapse any active choice buttons from previous messages
    collapseActiveChoices("Skipped");

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
                try { localStorage.setItem("parsec_history", JSON.stringify(conversationHistory)); } catch (e) {}
                // Auto-save conversation to server
                saveConversation();
                break;

            case "done": {
                // Clean up any remaining status indicator
                const remainingStatus = contentEl.querySelector(".status-indicator");
                if (remainingStatus) remainingStatus.remove();

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

                // Extract choice buttons from {{choices}} blocks before rendering final text
                var finalText = currentChunk || fullText;
                var choicesResult = extractChoices(finalText);
                if (choicesResult) {
                    finalText = choicesResult.cleanedText;
                }

                // Render the final answer
                const liveEl = contentEl.querySelector(".md-text-live");
                if (liveEl) {
                    // Re-render with cleaned text if choices were extracted
                    if (choicesResult) {
                        liveEl.innerHTML = marked.parse(finalText);
                    }
                    liveEl.className = "md-text";
                }

                // Append choice buttons after the text
                if (choicesResult) {
                    contentEl.appendChild(renderChoices(choicesResult.options, choicesResult.multi));
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
        const payload = {
            question,
            conversation_history: conversationHistory.length > 0 ? conversationHistory : null,
        };
        const response = await fetch("/api/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
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

    var shareBtn = document.createElement("button");
    shareBtn.className = "response-export-btn";
    shareBtn.textContent = "Share";
    shareBtn.addEventListener("click", async function() {
        if (conversationHistory.length === 0) return;
        shareBtn.disabled = true;
        shareBtn.textContent = "Sharing...";
        try {
            var resp = await fetch("/api/share", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ messages: conversationHistory }),
            });
            if (!resp.ok) {
                var err = await resp.json().catch(function() { return {}; });
                alert(err.detail || "Failed to create share link");
                return;
            }
            var data = await resp.json();
            var shareUrl = window.location.origin + "/?share=" + data.id;
            document.getElementById("share-link-input").value = shareUrl;
            document.getElementById("share-modal").style.display = "flex";
        } catch (e) {
            alert("Failed to create share link: " + e.message);
        } finally {
            shareBtn.disabled = false;
            shareBtn.textContent = "Share";
        }
    });

    bar.appendChild(mdBtn);
    bar.appendChild(pdfBtn);
    bar.appendChild(shareBtn);
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

function renderSharedMessages(messages, interactive) {
    messages.forEach(function(msg, msgIdx) {
        if (msg.role === "user") {
            var text = msg.content;
            if (Array.isArray(text)) {
                text = text.map(function(b) { return b.text || ""; }).join("");
            }
            addMessage("user", text);
        } else if (msg.role === "assistant") {
            var el = addMessage("assistant", "");
            var contentEl = el.querySelector(".content");
            var content = msg.content;
            var restoredText = "";

            if (typeof content === "string") {
                restoredText = content;
                var textDiv = document.createElement("div");
                textDiv.className = "md-text";
                textDiv.innerHTML = marked.parse(content);
                contentEl.appendChild(textDiv);
            } else if (Array.isArray(content)) {
                var toolCalls = [];
                var textParts = [];

                content.forEach(function(block) {
                    if (block.type === "text" && block.text) {
                        textParts.push(block.text);
                    } else if (block.type === "tool_use") {
                        toolCalls.push(block);
                    }
                });

                // Show tool calls as collapsed summary
                if (toolCalls.length > 0) {
                    var wrapper = document.createElement("details");
                    wrapper.className = "tool-calls-summary";
                    var tcSummaryEl = document.createElement("summary");
                    tcSummaryEl.textContent = toolCalls.length === 1
                        ? "1 query executed"
                        : toolCalls.length + " queries executed";
                    wrapper.appendChild(tcSummaryEl);
                    var inner = document.createElement("div");
                    inner.className = "tool-calls-inner";
                    toolCalls.forEach(function(tc) {
                        var tcEl = document.createElement("details");
                        tcEl.className = "tool-call";
                        var tcSummary = document.createElement("summary");
                        var nameSpan = document.createElement("span");
                        nameSpan.className = "tool-name";
                        nameSpan.textContent = tc.name || "tool";
                        var statusSpan = document.createElement("span");
                        statusSpan.className = "tool-status done";
                        statusSpan.textContent = "done";
                        tcSummary.appendChild(nameSpan);
                        tcSummary.appendChild(statusSpan);
                        tcEl.appendChild(tcSummary);
                        var body = document.createElement("div");
                        body.className = "tool-body";
                        body.textContent = JSON.stringify(tc.input || {}, null, 2);
                        tcEl.appendChild(body);
                        inner.appendChild(tcEl);
                    });
                    wrapper.appendChild(inner);
                    contentEl.appendChild(wrapper);
                }

                // Render text content
                restoredText = textParts.join("");
                if (restoredText.trim()) {
                    var sharedChoices = extractChoices(restoredText);
                    var renderText = sharedChoices ? sharedChoices.cleanedText : restoredText;
                    var textDiv2 = document.createElement("div");
                    textDiv2.className = "md-text";
                    textDiv2.innerHTML = marked.parse(renderText);  // safe: server-generated markdown
                    contentEl.appendChild(textDiv2);
                    if (sharedChoices) {
                        var isLastMsg = (msgIdx === messages.length - 1);
                        if (interactive && isLastMsg) {
                            contentEl.appendChild(renderChoices(sharedChoices.options, sharedChoices.multi));
                        } else {
                            var choicesSummary = document.createElement("div");
                            choicesSummary.className = "choices-summary";
                            choicesSummary.textContent = "Choices were presented";
                            contentEl.appendChild(choicesSummary);
                        }
                    }
                }
            }

            // Add export bar to restored assistant messages
            if (restoredText.trim()) {
                el._exportMarkdown = restoredText;
                el._exportCharts = [];
                contentEl.appendChild(createResponseExportBar(el));
            }
        }
        // Skip tool_result messages — internal
    });
}

function scrollToBottom() {
    const chat = document.getElementById("chat");
    chat.scrollTop = chat.scrollHeight;
}

// ─── Choice buttons ───

function extractChoices(text) {
    // Match {{choices}} or {{choices multi}} ... {{/choices}}
    var match = text.match(/\{\{choices(\s+multi)?\}\}\s*\n([\s\S]*?)\{\{\/choices\}\}/);
    if (!match) return null;

    var multi = !!match[1];
    var block = match[2];
    var options = [];
    block.split("\n").forEach(function(line) {
        var trimmed = line.replace(/^\s*-\s*/, "").trim();
        if (trimmed) options.push(trimmed);
    });

    if (options.length === 0) return null;

    var cleanedText = text.replace(/\{\{choices(\s+multi)?\}\}\s*\n[\s\S]*?\{\{\/choices\}\}/, "").trim();
    return { options: options, multi: multi, cleanedText: cleanedText };
}

function renderChoices(options, multi) {
    var container = document.createElement("div");
    container.className = "choices-container";
    container.setAttribute("data-active", "true");

    options.forEach(function(opt) {
        var btn = document.createElement("button");
        btn.className = "choice-btn";
        btn.textContent = opt;
        btn.addEventListener("click", function() {
            if (container.getAttribute("data-active") !== "true") return;
            if (multi) {
                btn.classList.toggle("selected");
            } else {
                // Single-select: submit immediately
                collapseChoices(container, opt);
                input.value = opt;
                form.requestSubmit();
            }
        });
        container.appendChild(btn);
    });

    if (multi) {
        var submitBtn = document.createElement("button");
        submitBtn.className = "choices-submit";
        submitBtn.textContent = "Submit";
        submitBtn.addEventListener("click", function() {
            if (container.getAttribute("data-active") !== "true") return;
            var selected = [];
            container.querySelectorAll(".choice-btn.selected").forEach(function(b) {
                selected.push(b.textContent);
            });
            if (selected.length === 0) return;
            var text = selected.join(", ");
            collapseChoices(container, text);
            input.value = text;
            form.requestSubmit();
        });
        container.appendChild(submitBtn);
    }

    return container;
}

function collapseChoices(container, selectedText) {
    container.setAttribute("data-active", "false");
    var summary = document.createElement("div");
    summary.className = "choices-summary";
    summary.innerHTML = 'Selected: <span class="choices-selected-values"></span>';
    summary.querySelector(".choices-selected-values").textContent = selectedText;
    container.replaceWith(summary);
}

function collapseActiveChoices(label) {
    document.querySelectorAll('.choices-container[data-active="true"]').forEach(function(c) {
        var summary = document.createElement("div");
        summary.className = "choices-summary";
        summary.textContent = label;
        c.replaceWith(summary);
    });
}
