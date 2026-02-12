/* Parsec — Chat UI with SSE streaming */

const messagesEl = document.getElementById("messages");
const form = document.getElementById("query-form");
const input = document.getElementById("question");
const sendBtn = document.getElementById("send-btn");

let conversationHistory = [];

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
    let accText = "";
    let currentToolEl = null;
    let streamStarted = false;

    function ensureStreamStarted() {
        if (!streamStarted) {
            statusEl.remove();
            streamStarted = true;
        }
    }

    function renderText() {
        let textEl = contentEl.querySelector(".md-text");
        if (!textEl) {
            textEl = document.createElement("div");
            textEl.className = "md-text";
            contentEl.appendChild(textEl);
        }
        textEl.innerHTML = marked.parse(accText);
    }

    function processEvent(eventType, data) {
        switch (eventType) {
            case "text":
                ensureStreamStarted();
                fullText += data.content;
                accText += data.content;
                renderText();
                scrollToBottom();
                break;

            case "tool_start": {
                ensureStreamStarted();
                currentToolEl = createToolCall(data.tool, data.input);
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

            case "error": {
                ensureStreamStarted();
                const errEl = document.createElement("div");
                errEl.className = "error-message";
                errEl.textContent = data.message;
                contentEl.appendChild(errEl);
                scrollToBottom();
                break;
            }

            case "done":
                break;
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

    conversationHistory.push({ role: "user", content: question });
    if (fullText) {
        conversationHistory.push({ role: "assistant", content: fullText });
    }

    sendBtn.disabled = false;
    input.focus();
});

function addMessage(role, text) {
    const el = document.createElement("div");
    el.className = "message " + role;

    if (role === "user") {
        el.textContent = text;
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
        if (result.row_count !== undefined) {
            statusSpan.textContent = result.row_count + " rows";
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

function scrollToBottom() {
    const chat = document.getElementById("chat");
    chat.scrollTop = chat.scrollHeight;
}
