/* Parsec — Chat UI with SSE streaming */

const messagesEl = document.getElementById("messages");
const form = document.getElementById("query-form");
const input = document.getElementById("question");
const sendBtn = document.getElementById("send-btn");
const fileInput = document.getElementById("file-upload");
const uploadBtn = document.getElementById("upload-btn");
const attachmentIndicator = document.getElementById("attachment-indicator");
const attachmentNameEl = document.getElementById("attachment-name");
const attachmentRemoveBtn = document.getElementById("attachment-remove");

const MAX_UPLOAD_SIZE = 10 * 1024 * 1024; // 10 MB
let pendingAttachment = null; // { name, content }

let conversationHistory = [];
let currentConversationId = null;
const sessionId = crypto.randomUUID();

// Prompt history (up-arrow recall within session)
let promptHistory = [];
let promptHistoryIndex = -1;
let promptHistoryDraft = "";

// Auto-resize textarea to fit content (up to a max height)
function autoResizeInput() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
}
input.addEventListener("input", autoResizeInput);

// Enter submits, Shift+Enter inserts newline; ArrowUp/Down for prompt history
input.addEventListener("keydown", function(e) {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
    }
    if (e.key === "ArrowUp" && input.selectionStart === 0 && promptHistory.length > 0) {
        e.preventDefault();
        if (promptHistoryIndex === -1) {
            promptHistoryDraft = input.value;
            promptHistoryIndex = promptHistory.length - 1;
        } else if (promptHistoryIndex > 0) {
            promptHistoryIndex--;
        }
        input.value = promptHistory[promptHistoryIndex];
        autoResizeInput();
    }
    if (e.key === "ArrowDown" && promptHistoryIndex !== -1) {
        e.preventDefault();
        if (promptHistoryIndex < promptHistory.length - 1) {
            promptHistoryIndex++;
            input.value = promptHistory[promptHistoryIndex];
        } else {
            promptHistoryIndex = -1;
            input.value = promptHistoryDraft;
        }
        autoResizeInput();
    }
});

// Restore conversation history from localStorage (survives page refresh)
try {
    const saved = localStorage.getItem("parsec_history");
    if (saved) conversationHistory = JSON.parse(saved);
    currentConversationId = localStorage.getItem("parsec_conv_id") || null;
} catch {
    // Ignore corrupt localStorage data
}

// ─── File upload ───
uploadBtn.addEventListener("click", function() { fileInput.click(); });

fileInput.addEventListener("change", async function() {
    const file = fileInput.files[0];
    if (!file) return;
    if (file.size > MAX_UPLOAD_SIZE) {
        alert("File too large — maximum size is 10 MB.");
        fileInput.value = "";
        return;
    }
    const content = await file.text();
    pendingAttachment = { name: file.name, content: content };
    attachmentNameEl.textContent = file.name;
    attachmentIndicator.style.display = "flex";
    uploadBtn.classList.add("has-file");
});

attachmentRemoveBtn.addEventListener("click", function() {
    pendingAttachment = null;
    fileInput.value = "";
    attachmentIndicator.style.display = "none";
    uploadBtn.classList.remove("has-file");
});

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

const sidebarEl = document.getElementById("sidebar");
const sidebarListEl = document.getElementById("sidebar-list");
const sidebarHistoryTitle = sidebarEl.querySelector(".sidebar-title");
const sidebarExamplesEl = sidebarEl.querySelector(".sidebar-examples");
const tabHistory = document.getElementById("sidebar-tab-history");
const tabExamples = document.getElementById("sidebar-tab-examples");
const tabSkills = document.getElementById("sidebar-tab-skills");

// Sidebar sections — hide all by default, show based on which tab was clicked
sidebarListEl.style.display = "none";
sidebarHistoryTitle.style.display = "none";
sidebarExamplesEl.style.display = "none";

const learningsPanel = document.getElementById("learnings-panel");
const skillsPanel = document.getElementById("skills-panel");
let isAdmin = false;
let adminViewAllChats = false;

function showAdminChatToggle() {
    const existing = document.getElementById("admin-chat-toggle");
    if (existing) { existing.style.display = "flex"; return; }
    const toggle = document.createElement("div");
    toggle.id = "admin-chat-toggle";
    toggle.className = "admin-chat-toggle";
    const myBtn = document.createElement("button");
    myBtn.id = "admin-toggle-my";
    myBtn.className = "admin-toggle-btn" + (!adminViewAllChats ? " active" : "");
    myBtn.textContent = "My Chats";
    const allBtn = document.createElement("button");
    allBtn.id = "admin-toggle-all";
    allBtn.className = "admin-toggle-btn" + (adminViewAllChats ? " active" : "");
    allBtn.textContent = "All Users";
    myBtn.addEventListener("click", function() {
        adminViewAllChats = false;
        myBtn.classList.add("active");
        allBtn.classList.remove("active");
        loadConversationList();
    });
    allBtn.addEventListener("click", function() {
        adminViewAllChats = true;
        allBtn.classList.add("active");
        myBtn.classList.remove("active");
        loadConversationList();
    });
    const dlBtn = document.createElement("button");
    dlBtn.className = "admin-toggle-btn admin-download-btn";
    dlBtn.textContent = "⤓";
    dlBtn.title = "Download all conversations as JSON";
    dlBtn.addEventListener("click", function() {
        dlBtn.textContent = "…";
        fetch("/api/conversations/export").then(function(resp) {
            if (!resp.ok) throw new Error("Export failed");
            return resp.json();
        }).then(function(data) {
            const blob = new Blob([JSON.stringify(data.conversations, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = "parsec-conversations-" + new Date().toISOString().slice(0, 10) + ".json";
            a.click();
            URL.revokeObjectURL(url);
            dlBtn.textContent = "⤓";
        }).catch(function() { dlBtn.textContent = "⤓"; });
    });
    toggle.appendChild(myBtn);
    toggle.appendChild(allBtn);
    toggle.appendChild(dlBtn);
    sidebarListEl.parentNode.insertBefore(toggle, sidebarListEl);
}

function openSidebar(section) {
    // Default all togglable panels off, then turn on the one for this section.
    sidebarExamplesEl.style.display = "none";
    learningsPanel.style.display = "none";
    skillsPanel.style.display = "none";
    const adminToggle = document.getElementById("admin-chat-toggle");
    if (adminToggle) adminToggle.style.display = "none";
    sidebarHistoryTitle.style.display = "";

    if (section === "history") {
        sidebarHistoryTitle.textContent = "History";
        sidebarListEl.style.display = "";
        if (isAdmin) {
            learningsPanel.style.display = "block";
            showAdminChatToggle();
        }
        loadConversationList();
    } else if (section === "skills") {
        sidebarHistoryTitle.textContent = "Skills";
        sidebarListEl.style.display = "none";
        skillsPanel.style.display = "block";
        loadSkills();
    } else {
        sidebarHistoryTitle.textContent = "Examples";
        sidebarListEl.style.display = "none";
        sidebarExamplesEl.style.display = "";
    }
    sidebarEl.classList.add("open");
}

function closeSidebar() {
    sidebarEl.classList.remove("open");
}

document.getElementById("sidebar-close-btn").addEventListener("click", closeSidebar);
tabHistory.addEventListener("click", function() { showChatView(); openSidebar("history"); });
tabExamples.addEventListener("click", function() { showChatView(); openSidebar("examples"); });
tabSkills.addEventListener("click", function() { showChatView(); openSidebar("skills"); });

const tabDebug = document.getElementById("sidebar-tab-debug");
tabDebug.addEventListener("click", function() {
    closeSidebar();
    showDebugView();
});

// Example items click-to-fill
document.querySelectorAll(".sidebar-examples-list li").forEach(function(li) {
    li.addEventListener("click", function() {
        const input = document.getElementById("question");
        input.value = li.textContent;
        input.focus();
        input.dispatchEvent(new Event("input"));
        closeSidebar();
    });
});

// ─── Skills panel (read-only view of GET /api/skills) ───
// Nodes are built with textContent (never innerHTML) so skill metadata —
// which comes from arbitrary on-disk SKILL.md files — can't inject markup.

const skillsListEl = document.getElementById("skills-list");

function loadSkills() {
    skillsListEl.textContent = "Loading…";
    fetch("/api/skills").then(function(resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
    }).then(function(data) {
        renderSkills(data.skills || []);
    }).catch(function(err) {
        skillsListEl.textContent = "";
        const e = document.createElement("div");
        e.className = "skills-empty";
        e.textContent = "Could not load skills: " + err.message;
        skillsListEl.appendChild(e);
    });
}

function renderSkills(skills) {
    skillsListEl.textContent = "";
    if (!skills.length) {
        const empty = document.createElement("div");
        empty.className = "skills-empty";
        empty.textContent = "No skills discovered. Set skills.plugin_paths to mount a skill repo.";
        skillsListEl.appendChild(empty);
        return;
    }
    skills.forEach(function(s) {
        const card = document.createElement("div");
        card.className = "skill-card";

        const head = document.createElement("div");
        head.className = "skill-card-head";
        const name = document.createElement("span");
        name.className = "skill-name";
        name.textContent = s.name;
        head.appendChild(name);
        if (s.source) {
            const src = document.createElement("span");
            src.className = "skill-badge";
            src.textContent = s.source;
            head.appendChild(src);
        }
        if (s.is_parsec_native) {
            const nat = document.createElement("span");
            nat.className = "skill-badge skill-badge-native";
            nat.textContent = "parsec";
            head.appendChild(nat);
        }
        card.appendChild(head);

        const desc = document.createElement("div");
        desc.className = "skill-desc";
        desc.textContent = s.description || "";
        card.appendChild(desc);

        const bits = [];
        if (s.parsec?.version) bits.push("v" + s.parsec.version);
        if (s.parsec?.domain) bits.push(s.parsec.domain);
        if (s.allowed_tools?.length) bits.push(s.allowed_tools.length + " tools");
        if (bits.length) {
            const meta = document.createElement("div");
            meta.className = "skill-meta";
            meta.textContent = bits.join(" · ");
            card.appendChild(meta);
        }

        (s.warnings || []).forEach(function(w) {
            const warn = document.createElement("div");
            warn.className = "skill-warning";
            warn.textContent = "⚠ " + w;
            card.appendChild(warn);
        });

        if (s.skill_path) {
            const path = document.createElement("div");
            path.className = "skill-path";
            path.textContent = s.skill_path;
            path.title = "Discovered from " + s.skill_path;
            card.appendChild(path);
        }

        skillsListEl.appendChild(card);
    });
}

// ─── Learnings panel (admin only) ───

(function initLearnings() {
    fetch("/api/learnings/check").then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data || !data.is_admin) return;
        // Admin: show history tab and learnings
        isAdmin = true;
        tabHistory.style.display = "block";
        refreshLearningsCount();
    }).catch(function() { /* expected: non-critical fetch */ });
})();

function refreshLearningsCount() {
    fetch("/api/learnings").then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data) return;
        const countEl = document.getElementById("learnings-count");
        if (data.has_learnings) {
            const entries = (data.content.match(/^- /gm) || []).length;
            countEl.textContent = entries + " entries";
        } else {
            countEl.textContent = "empty";
        }
    }).catch(function() { /* expected: non-critical fetch */ });
}

document.getElementById("learnings-view-btn").addEventListener("click", function() {
    fetch("/api/learnings").then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data) return;
        const textarea = document.getElementById("learnings-text");
        textarea.value = data.content || "(no learnings yet)";
        document.getElementById("learnings-modal").style.display = "flex";
    }).catch(function() { /* expected: non-critical fetch */ });
});

document.getElementById("learnings-copy-btn").addEventListener("click", function() {
    const textarea = document.getElementById("learnings-text");
    navigator.clipboard.writeText(textarea.value).then(function() {
        const btn = document.getElementById("learnings-copy-btn");
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
    let url = "/api/conversations";
    if (isAdmin && adminViewAllChats) url += "?all_users=true";
    fetch(url).then(function(resp) {
        if (!resp.ok) return;
        return resp.json();
    }).then(function(data) {
        if (!data) return;
        renderConversationList(data.conversations || []);
    }).catch(function() { /* expected: non-critical fetch */ });
}

function renderConversationList(conversations) {
    sidebarListEl.textContent = "";
    if (conversations.length === 0) {
        const empty = document.createElement("div");
        empty.className = "sidebar-empty";
        empty.textContent = adminViewAllChats ? "No conversations found" : "No previous conversations";
        sidebarListEl.appendChild(empty);
        return;
    }
    conversations.forEach(function(conv) {
        const item = document.createElement("div");
        item.className = "sidebar-item";
        if (conv.id === currentConversationId) item.classList.add("active");

        const titleEl = document.createElement("div");
        titleEl.className = "sidebar-item-title";
        titleEl.textContent = conv.title;

        const metaEl = document.createElement("div");
        metaEl.className = "sidebar-item-meta";
        const date = new Date(conv.updated_at);
        let metaText = date.toLocaleDateString() + " \u00b7 " + conv.message_count + " msgs";
        if (adminViewAllChats && conv.owner) {
            metaText = conv.owner + " \u00b7 " + metaText;
        }
        metaEl.textContent = metaText;

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "sidebar-item-delete";
        deleteBtn.textContent = "\u00d7";
        deleteBtn.title = "Delete conversation";
        if (adminViewAllChats) deleteBtn.style.display = "none";
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
        } catch { /* expected: localStorage may be unavailable */ }
        window.location.href = window.location.pathname;
    }).catch(function(err) {
        alert("Failed to load conversation: " + err.message);
    });
}

function saveConversation() {
    if (conversationHistory.length === 0) return;
    const body = {
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
        try { localStorage.setItem("parsec_conv_id", data.id); } catch { /* expected: localStorage may be unavailable */ }
        loadConversationList();
        // Refresh learnings count after background analysis has time to complete
        setTimeout(refreshLearningsCount, 20000);
    }).catch(function() { /* expected: non-critical fetch */ });
}

// Share modal handlers
document.getElementById("share-copy-btn").addEventListener("click", function() {
    const input = document.getElementById("share-link-input");
    navigator.clipboard.writeText(input.value).then(function() {
        const btn = document.getElementById("share-copy-btn");
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
    const current = document.documentElement.dataset.theme;
    const next = current === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("parsec_theme", next);
});

// Open all markdown links in new tabs
const renderer = new marked.Renderer();
renderer.link = function(href, title, text) {
    // marked v12+ passes an object as first arg
    if (typeof href === "object") {
        text = href.text;
        title = href.title;
        href = href.href;
    }
    const titleAttr = title ? ' title="' + title + '"' : "";
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

    const welcomeShort = document.createElement("div");
    welcomeShort.className = "md-text welcome-short";
    welcomeShort.innerHTML = marked.parse(
        "**Hi, I'm Parsec** — a natural language investigation assistant for RHDP cloud costs and provisioning. " +
        "I can also look up automation source code in AgnosticD and catalog item configs in AgnosticV. " +
        "Ask me anything about costs, provisions, sandboxes, or usage. " +
        "Questions? Join [#forum-rhdp-parsec](https://redhat.enterprise.slack.com/archives/C0AN89G051T) on Slack."
    );
    contentEl.appendChild(welcomeShort);


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
            const shareResp = await fetch("/api/share/" + encodeURIComponent(shareId));
            if (shareResp.ok) {
                const shareData = await shareResp.json();
                document.getElementById("shared-banner").style.display = "flex";
                document.getElementById("query-form").style.display = "none";
                messagesEl.textContent = "";
                renderSharedMessages(shareData.messages);
                scrollToBottom();

                // Continue Investigation button
                document.getElementById("continue-btn").addEventListener("click", function() {
                    conversationHistory = shareData.messages;
                    try { localStorage.setItem("parsec_history", JSON.stringify(conversationHistory)); } catch { /* expected: localStorage may be unavailable */ }
                    window.location.href = window.location.pathname;
                });
                return;
            } else {
                const shareErr = await shareResp.json().catch(function() { return {}; });
                const errEl = document.createElement("div");
                errEl.className = "error-message";
                errEl.textContent = shareErr.detail || "Shared session not found";
                messagesEl.appendChild(errEl);
                return;
            }
        } catch (e) {
            const errEl2 = document.createElement("div");
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

// ─── Stream event helpers (extracted to reduce cognitive complexity — S3776) ───

function _ensureStreamStarted(state) {
    if (!state.streamStarted) {
        state.statusEl.remove();
        state.streamStarted = true;
    }
}

function _renderCurrentChunk(state) {
    let textEl = state.contentEl.querySelector(".md-text-live");
    if (!textEl) {
        textEl = document.createElement("div");
        textEl.className = "md-text-live";
        state.contentEl.appendChild(textEl);
    }
    textEl.innerHTML = marked.parse(state.currentChunk);
}

function _handleTextEvent(data, state) {
    _ensureStreamStarted(state);
    const si = state.contentEl.querySelector(".status-indicator");
    if (si) si.remove();
    state.fullText += data.content;
    state.currentChunk += data.content;
    _renderCurrentChunk(state);
    scrollToBottom();
}

function _createLiveToolWrapper(state) {
    state.liveWrapper = document.createElement("details");
    state.liveWrapper.className = "tool-calls-summary";
    state.liveWrapper.open = true;
    state.liveSummary = document.createElement("summary");
    state.liveWrapper.appendChild(state.liveSummary);
    state.liveInner = document.createElement("div");
    state.liveInner.className = "tool-calls-inner";
    state.liveWrapper.appendChild(state.liveInner);
    state.contentEl.appendChild(state.liveWrapper);
}

function _handleToolStartEvent(data, state) {
    _ensureStreamStarted(state);
    if (state.currentToolEl) {
        const prevStatus = state.currentToolEl.querySelector(".tool-status");
        if (prevStatus?.classList.contains("running")) {
            prevStatus.className = "tool-status done";
            prevStatus.textContent = "done";
        }
    }
    if (state.currentChunk.trim()) {
        state.textChunks.push(state.currentChunk);
    }
    state.currentChunk = "";
    const liveTextEl = state.contentEl.querySelector(".md-text-live");
    if (liveTextEl) liveTextEl.remove();
    if (!state.liveWrapper) {
        _createLiveToolWrapper(state);
    }
    state.liveToolCount++;
    state.liveSummary.textContent = state.liveToolCount === 1
        ? "1 query running..."
        : state.liveToolCount + " queries running...";
    state.currentToolEl = createToolCall(data.tool, data.input);
    state.currentToolName = data.tool;
    state.currentToolInput = data.input;
    state.toolElements[data.tool + "_" + Object.keys(state.toolElements).length] = state.currentToolEl;
    state.liveInner.appendChild(state.currentToolEl);
    scrollToBottom();
}

function _handleCacheHitEvent(state) {
    if (!state.currentToolEl) return;
    const cacheStatus = state.currentToolEl.querySelector(".tool-status");
    if (cacheStatus) {
        cacheStatus.className = "tool-status cached";
        cacheStatus.textContent = "cached";
    }
}

function _handleToolResultEvent(data, state) {
    if (!state.currentToolEl) return;
    finalizeToolCall(state.currentToolEl, data.tool, data.result);
    state.toolResults.push({
        tool: state.currentToolName || data.tool,
        input: state.currentToolInput || {},
        result: data.result
    });
    state.currentToolName = null;
    state.currentToolInput = null;
    state.currentToolEl = null;
    scrollToBottom();
}

function _handleChartEvent(data, state) {
    _ensureStreamStarted(state);
    const chartEl = renderChart(data);
    state.contentEl.appendChild(chartEl);
    const chartCanvas = chartEl.querySelector("canvas");
    if (chartCanvas) {
        state.chartCanvases.push({ title: data.title || "chart", canvas: chartCanvas });
    }
    scrollToBottom();
}

function _handleReportEvent(data, contentEl) {
    const link = document.createElement("a");
    link.className = "report-download";
    link.href = data.url;
    link.download = data.filename;
    link.textContent = "Download report: " + data.filename;
    contentEl.appendChild(link);
    scrollToBottom();
}

function _handleAgentStartEvent(data, state) {
    _ensureStreamStarted(state);
    const agentBanner = document.createElement("div");
    agentBanner.className = "agent-banner agent-running";
    agentBanner.dataset.agent = data.agent;
    // Same reason as the skill badge: under the SDK the agent label can come
    // from the model's own `subagent_type` when it names an agent the registry
    // does not know, so this is no longer a fixed string from a six-entry map.
    var aIcon = document.createElement("span");
    aIcon.className = "agent-icon";
    aIcon.textContent = "\u2699";
    var aLabel = document.createElement("span");
    aLabel.className = "agent-label";
    aLabel.textContent = data.name || data.agent || "";
    var aStatus = document.createElement("span");
    aStatus.className = "agent-status";
    aStatus.textContent = "investigating…";
    agentBanner.appendChild(aIcon);
    agentBanner.appendChild(document.createTextNode(" "));
    agentBanner.appendChild(aLabel);
    agentBanner.appendChild(document.createTextNode(" "));
    agentBanner.appendChild(aStatus);
    state.contentEl.appendChild(agentBanner);
    scrollToBottom();
}

function _handleSkillUsedEvent(data, state) {
    // A skill can shape an entire answer without ever producing a tool call —
    // preloaded skills are in the agent's context from turn one. Without this
    // badge there is no way to tell from the UI that one was in play.
    _ensureStreamStarted(state);
    var skill = data.skill || "";
    if (!skill) return;
    // Build with DOM APIs, not innerHTML. Skill names come from SKILL.md
    // directories, which include ones mounted from skills.plugin_paths — an
    // external repo. A name containing markup would otherwise be executed, and a
    // name containing a quote would break the selector below. Everything else in
    // this file uses textContent for the same reason.
    var badges = state.contentEl.querySelectorAll(".skill-badge");
    for (var i = 0; i < badges.length; i++) {
        if (badges[i].dataset.skill === skill) return;
    }
    var badge = document.createElement("div");
    badge.className = "skill-badge skill-" + (data.source === "preloaded" ? "preloaded" : "invoked");
    badge.dataset.skill = skill;

    var icon = document.createElement("span");
    icon.className = "skill-icon";
    icon.textContent = "\uD83D\uDCDA";

    var label = document.createElement("span");
    label.className = "skill-label";
    label.textContent = "skill: " + skill;

    var how = document.createElement("span");
    how.className = "skill-how";
    how.textContent = (data.source === "preloaded" ? "loaded" : "invoked") +
        (data.agent ? " by " + data.agent : "");

    badge.appendChild(icon);
    badge.appendChild(document.createTextNode(" "));
    badge.appendChild(label);
    badge.appendChild(document.createTextNode(" "));
    badge.appendChild(how);
    state.contentEl.appendChild(badge);
    scrollToBottom();
}

function _handleAgentDoneEvent(data, contentEl) {
    // Match on the dataset rather than building a selector from server data:
    // an agent name containing a quote would otherwise throw here.
    const banners = [].filter.call(
        contentEl.querySelectorAll(".agent-banner"),
        function (b) { return b.dataset.agent === data.agent; }
    );
    banners.forEach(function(b) {
        b.classList.remove("agent-running");
        b.classList.add("agent-done");
        const statusSpan = b.querySelector(".agent-status");
        if (statusSpan) statusSpan.textContent = "done";
    });
}

function _handleStatusEvent(data, state) {
    _ensureStreamStarted(state);
    const oldStatus = state.contentEl.querySelector(".status-indicator");
    if (oldStatus) oldStatus.remove();
    const si = document.createElement("div");
    si.className = "status-indicator";
    // data.message can carry model-derived text (e.g. "Using skill: X").
    si.textContent = "";
    var sp = document.createElement("div");
    sp.className = "spinner";
    si.appendChild(sp);
    si.appendChild(document.createTextNode(" " + (data.message || "")));
    state.contentEl.appendChild(si);
    scrollToBottom();
}

function _handleErrorEvent(data, state) {
    _ensureStreamStarted(state);
    const errEl = document.createElement("div");
    errEl.className = "error-message";
    errEl.textContent = data.message;
    state.contentEl.appendChild(errEl);
    scrollToBottom();
}

function _handleConfidenceEvent(data, state) {
    _ensureStreamStarted(state);
    if (data.level !== "medium" && data.level !== "low") return;
    const callout = document.createElement("div");
    callout.className = "confidence-callout " + data.level;
    const title = data.level === "low" ? "Low confidence" : "Medium confidence";
    const icon = data.level === "low" ? "⚠️" : "⚠";
    let html = '<div class="confidence-title">' + icon + " " + title + "</div>";
    const reasons = data.reasons || [];
    if (reasons.length > 0) {
        html += "<ul>";
        reasons.forEach(function(r) {
            html += "<li>" + r.replaceAll("<", "&lt;").replaceAll(">", "&gt;") + "</li>";
        });
        html += "</ul>";
    }
    callout.innerHTML = html;
    state.contentEl.appendChild(callout);
    scrollToBottom();
}

function _cleanupStatusIndicators(contentEl) {
    const remainingStatus = contentEl.querySelector(".status-indicator");
    if (remainingStatus) remainingStatus.remove();
}

function _finalizeRunningToolStatuses(contentEl) {
    contentEl.querySelectorAll(".tool-status.running").forEach(function(s) {
        s.className = "tool-status done";
        s.textContent = "done";
    });
}

function _appendThinkingText(container, text) {
    const thinkEl = document.createElement("div");
    thinkEl.className = "thinking-text";
    thinkEl.innerHTML = marked.parse(text);
    container.appendChild(thinkEl);
}

function _rebuildToolWrapperInner(state) {
    const toolEls = Array.from(state.liveInner.querySelectorAll(".tool-call"));
    state.liveInner.replaceChildren();
    let chunkIdx = 0;
    toolEls.forEach(function(tc) {
        if (chunkIdx < state.textChunks.length) {
            _appendThinkingText(state.liveInner, state.textChunks[chunkIdx]);
            chunkIdx++;
        }
        state.liveInner.appendChild(tc);
    });
    while (chunkIdx < state.textChunks.length) {
        _appendThinkingText(state.liveInner, state.textChunks[chunkIdx]);
        chunkIdx++;
    }
}

function _finalizeLiveToolWrapper(state) {
    const qCount = state.liveToolCount;
    state.liveSummary.textContent = qCount === 1
        ? "1 query executed"
        : qCount + " queries executed";
    if (qCount > 1) {
        addExpandCollapseToggle(state.liveSummary, state.liveWrapper);
    }
    _rebuildToolWrapperInner(state);
    state.liveWrapper.open = false;
    state.contentEl.insertBefore(state.liveWrapper, state.contentEl.firstChild);
}

function _mergeConfidenceMarker(existing, level, reason) {
    if (level === "low" && existing.classList.contains("medium")) {
        existing.classList.remove("medium");
        existing.classList.add("low");
        existing.querySelector(".confidence-title").innerHTML = '⚠️ Low confidence';
    }
    const ul = existing.querySelector("ul");
    if (ul) {
        const li = document.createElement("li");
        li.textContent = reason;
        ul.appendChild(li);
    }
}

function _createConfidenceCallout(contentEl, level, reason) {
    const mc = document.createElement("div");
    mc.className = "confidence-callout " + level;
    const mTitle = level === "low" ? "Low confidence" : "Medium confidence";
    mc.innerHTML = '<div class="confidence-title">⚠ ' + mTitle + "</div><ul><li>" + reason.replaceAll("<", "&lt;") + "</li></ul>";
    contentEl.appendChild(mc);
}

function _applyConfidenceMarker(contentEl, level, reason) {
    const existing = contentEl.querySelector(".confidence-callout");
    if (existing) {
        _mergeConfidenceMarker(existing, level, reason);
    } else {
        _createConfidenceCallout(contentEl, level, reason);
    }
}

function _processInlineConfidenceMarkers(contentEl) {
    const allTextEls = contentEl.querySelectorAll(".md-text, .md-text-live");
    allTextEls.forEach(function(el) {
        const html = el.innerHTML;
        const markerRegex = /\[confidence:\s*(medium|low)\s*\|\s*([^\]]+)\]/gi;
        let match;
        while ((match = markerRegex.exec(html)) !== null) {
            _applyConfidenceMarker(contentEl, match[1].toLowerCase(), match[2].trim());
        }
        el.innerHTML = html.replaceAll(/\[confidence:\s*(?:medium|low)\s*\|\s*[^\]]+\]/gi, "");
    });
}

function _renderFinalText(contentEl, currentChunk, fullText) {
    let finalText = currentChunk || fullText;
    const choicesResult = extractChoices(finalText);
    if (choicesResult) {
        finalText = choicesResult.cleanedText;
    }
    const liveEl = contentEl.querySelector(".md-text-live");
    if (liveEl) {
        if (choicesResult) {
            liveEl.innerHTML = marked.parse(finalText);
        }
        liveEl.className = "md-text";
    }
    if (choicesResult) {
        contentEl.appendChild(renderChoices(choicesResult.options, choicesResult.multi));
    }
}

function _addExportBar(state) {
    state.assistantEl._exportMarkdown = state.currentChunk || state.fullText;
    state.assistantEl._exportCharts = state.chartCanvases;
    state.assistantEl._exportToolResults = state.toolResults;
    if (state.fullText.trim() || state.currentChunk.trim() || state.chartCanvases.length > 0 || state.toolResults.length > 0) {
        state.contentEl.appendChild(createResponseExportBar(state.assistantEl));
    }
}

function _handleDoneEvent(state) {
    _cleanupStatusIndicators(state.contentEl);
    _finalizeRunningToolStatuses(state.contentEl);
    if (state.liveWrapper && state.liveSummary) {
        _finalizeLiveToolWrapper(state);
    }
    _processInlineConfidenceMarkers(state.contentEl);
    _renderFinalText(state.contentEl, state.currentChunk, state.fullText);
    _addExportBar(state);
    scrollToBottom();
}

function processStreamEvent(eventType, data, state) {
    switch (eventType) {
        case "text":
            _handleTextEvent(data, state);
            break;
        case "tool_start":
            _handleToolStartEvent(data, state);
            break;
        case "cache_hit":
            _handleCacheHitEvent(state);
            break;
        case "tool_result":
            _handleToolResultEvent(data, state);
            break;
        case "chart":
            _handleChartEvent(data, state);
            break;
        case "report":
            _handleReportEvent(data, state.contentEl);
            break;
        case "agent_start":
            _handleAgentStartEvent(data, state);
            break;
        case "agent_done":
            _handleAgentDoneEvent(data, state.contentEl);
            break;
        case "skill_used":
            _handleSkillUsedEvent(data, state);
            break;
        case "status":
            _handleStatusEvent(data, state);
            break;
        case "error":
            _handleErrorEvent(data, state);
            break;
        case "confidence":
            _handleConfidenceEvent(data, state);
            break;
        case "history":
            conversationHistory = data.messages;
            try { localStorage.setItem("parsec_history", JSON.stringify(conversationHistory)); } catch { /* expected: localStorage may be unavailable */ }
            saveConversation();
            break;
        case "done":
            _handleDoneEvent(state);
            break;
    }
}

// ─── renderSharedMessages helpers ───

function _buildToolResultMap(messages) {
    const map = {};
    messages.forEach(function(msg) {
        if (msg.role !== "user" || !Array.isArray(msg.content)) return;
        msg.content.forEach(function(block) {
            if (block.type === "tool_result" && block.tool_use_id) {
                try {
                    map[block.tool_use_id] = JSON.parse(block.content);
                } catch (e) {
                    map[block.tool_use_id] = block.content;
                }
            }
        });
    });
    return map;
}

function _tryCollapseToolChain(messages, startIdx) {
    const groupToolCalls = [];
    const groupToolUseIds = [];
    let finalText = [];
    let j = startIdx;
    while (j < messages.length) {
        const cur = messages[j];
        if (cur.role !== "assistant" || !Array.isArray(cur.content)) break;
        const curTools = cur.content.filter(function(b) { return b.type === "tool_use"; });
        const curText = cur.content.filter(function(b) { return b.type === "text" && b.text; });
        curTools.forEach(function(t) { groupToolCalls.push(t); groupToolUseIds.push(t.id); });
        if (curText.length > 0) finalText = curText;
        const next = messages[j + 1];
        if (next?.role === "user" && Array.isArray(next.content)) {
            const hasRealText = next.content.some(function(b) {
                return b.type !== "tool_result" && b.text?.trim();
            });
            if (!hasRealText) {
                j += 2;
                continue;
            }
        }
        break;
    }
    if (j <= startIdx) return { collapsed: false };
    const combinedContent = [];
    groupToolCalls.forEach(function(t) { combinedContent.push(t); });
    finalText.forEach(function(t) { combinedContent.push(t); });
    return {
        collapsed: true,
        msg: { role: "assistant", content: combinedContent, _collapsedToolIds: groupToolUseIds },
        nextIndex: j
    };
}

function _collapseSubAgentMessages(messages) {
    const collapsed = [];
    let i = 0;
    while (i < messages.length) {
        const msg = messages[i];
        if (msg.role === "assistant" && Array.isArray(msg.content)) {
            const hasToolUse = msg.content.some(function(b) { return b.type === "tool_use"; });
            if (hasToolUse) {
                const result = _tryCollapseToolChain(messages, i);
                if (result.collapsed) {
                    collapsed.push(result.msg);
                    i = result.nextIndex;
                    continue;
                }
            }
        }
        collapsed.push(messages[i]);
        i++;
    }
    return collapsed;
}

function _renderRestoredUserMessage(msg) {
    let text = msg.content;
    if (Array.isArray(text)) {
        const userParts = text.filter(function(b) { return b.type !== "tool_result"; });
        text = userParts.map(function(b) { return b.text || ""; }).join("");
    }
    if (text?.trim()) {
        addMessage("user", text);
    }
}

function _appendRestoredToolCall(inner, tc, toolResultMap) {
    const tcEl = document.createElement("details");
    tcEl.className = "tool-call";
    const tcSummary = document.createElement("summary");
    const nameSpan = document.createElement("span");
    nameSpan.className = "tool-name";
    nameSpan.textContent = tc.name || "tool";
    const statusSpan = document.createElement("span");
    statusSpan.className = "tool-status done";
    statusSpan.textContent = "done";
    tcSummary.appendChild(nameSpan);
    tcSummary.appendChild(statusSpan);
    tcEl.appendChild(tcSummary);
    const body = document.createElement("div");
    body.className = "tool-body";
    body.textContent = JSON.stringify(tc.input || {}, null, 2);
    const result = toolResultMap[tc.id];
    if (result && typeof result === "object") {
        body.textContent += "\n\n--- Result ---\n" + JSON.stringify(result, null, 2);
    }
    tcEl.appendChild(body);
    inner.appendChild(tcEl);
}

function _renderRestoredToolCalls(toolCalls, toolResultMap, contentEl) {
    const agentNames = {
        cost: "Cost Investigation", aap2: "AAP2 Investigation",
        babylon: "Babylon Investigation", security: "Security Investigation",
        ocpv: "OCPV Investigation", icinga: "Icinga Investigation"
    };
    const delegationTools = {
        investigate_costs: "cost", investigate_aap2_job: "aap2",
        investigate_babylon: "babylon", investigate_security: "security",
        investigate_ocpv: "ocpv", investigate_icinga: "icinga"
    };
    let totalQueries = 0;
    const delegations = [];
    const restoredToolResults = [];

    toolCalls.forEach(function(tc) {
        const result = toolResultMap[tc.id];
        const isDelegation = tc.name in delegationTools;
        if (isDelegation && result?.tool_calls) {
            totalQueries += result.tool_calls;
            delegations.push({ tc: tc, result: result, agentType: delegationTools[tc.name] });
        } else {
            totalQueries++;
        }
        if (!isDelegation && result && typeof result === "object" && !result.error) {
            restoredToolResults.push({ tool: tc.name, input: tc.input || {}, result: result });
        }
    });

    const wrapper = document.createElement("details");
    wrapper.className = "tool-calls-summary";
    const tcSummaryEl = document.createElement("summary");
    tcSummaryEl.textContent = totalQueries === 1
        ? "1 query executed"
        : totalQueries + " queries executed";
    if (toolCalls.length > 1) {
        addExpandCollapseToggle(tcSummaryEl, wrapper);
    }
    wrapper.appendChild(tcSummaryEl);
    const inner = document.createElement("div");
    inner.className = "tool-calls-inner";
    toolCalls.forEach(function(tc) {
        if (tc.name in delegationTools) return;
        _appendRestoredToolCall(inner, tc, toolResultMap);
    });
    wrapper.appendChild(inner);
    contentEl.appendChild(wrapper);

    return { delegations: delegations, restoredToolResults: restoredToolResults, agentNames: agentNames };
}

function _appendAgentBanner(contentEl, agentType, agentLabel) {
    const agentBanner = document.createElement("div");
    agentBanner.className = "agent-banner agent-done";
    agentBanner.dataset.agent = agentType;
    const iconSpan = document.createElement("span");
    iconSpan.className = "agent-icon";
    iconSpan.textContent = "⚙";
    const labelSpan = document.createElement("span");
    labelSpan.className = "agent-label";
    labelSpan.textContent = agentLabel;
    const statusSpan = document.createElement("span");
    statusSpan.className = "agent-status";
    statusSpan.textContent = "done";
    agentBanner.appendChild(iconSpan);
    agentBanner.appendChild(document.createTextNode(" "));
    agentBanner.appendChild(labelSpan);
    agentBanner.appendChild(document.createTextNode(" "));
    agentBanner.appendChild(statusSpan);
    contentEl.appendChild(agentBanner);
}

function _appendDelegationSummary(d, contentEl, textParts) {
    let summaryText = d.result.summary || "";
    if (!summaryText.trim()) {
        const findings = d.result.findings || [];
        summaryText = findings.filter(function(f) {
            return typeof f === "string" && !f.startsWith("[Tool:");
        }).join("\n\n");
    }
    if (summaryText.trim()) {
        const findingsDiv = document.createElement("div");
        findingsDiv.className = "md-text";
        findingsDiv.innerHTML = marked.parse(summaryText);
        contentEl.appendChild(findingsDiv);
        textParts.push(summaryText);
    }
}

function _renderRestoredDelegations(delegations, agentNames, contentEl, textParts) {
    delegations.forEach(function(d) {
        const agentType = d.agentType || d.result.agent || "unknown";
        const agentLabel = agentNames[agentType] || agentType;
        _appendAgentBanner(contentEl, agentType, agentLabel);
        _appendDelegationSummary(d, contentEl, textParts);
    });
}

function _reconstructReportsAndCharts(toolCalls, toolResultMap, contentEl) {
    const restoredCharts = [];
    toolCalls.forEach(function(tc) {
        const result = toolResultMap[tc.id];
        if (!result || typeof result !== "object" || result.error) return;
        if (tc.name === "generate_report" && result.filename) {
            const link = document.createElement("a");
            link.className = "report-download";
            link.href = "/api/reports/" + result.filename;
            link.download = result.filename;
            link.textContent = "Download report: " + result.filename;
            contentEl.appendChild(link);
        } else if (tc.name === "render_chart" && result.datasets) {
            try {
                const chartEl = renderChart(result);
                contentEl.appendChild(chartEl);
                const chartCanvas = chartEl.querySelector("canvas");
                if (chartCanvas) {
                    restoredCharts.push({ title: result.title || "chart", canvas: chartCanvas });
                }
            } catch (e) {
                console.warn("Failed to reconstruct chart:", e);
            }
        }
    });
    return restoredCharts;
}

function _renderRestoredTextWithChoices(text, contentEl, msgIdx, collapsed, interactive) {
    const sharedChoices = extractChoices(text);
    const renderText = sharedChoices ? sharedChoices.cleanedText : text;
    const textDiv = document.createElement("div");
    textDiv.className = "md-text";
    textDiv.innerHTML = marked.parse(renderText);
    contentEl.appendChild(textDiv);
    if (!sharedChoices) return;
    const isLastMsg = (msgIdx === collapsed.length - 1);
    if (interactive && isLastMsg) {
        contentEl.appendChild(renderChoices(sharedChoices.options, sharedChoices.multi));
    } else {
        const choicesSummary = document.createElement("div");
        choicesSummary.className = "choices-summary";
        choicesSummary.textContent = "Choices were presented";
        contentEl.appendChild(choicesSummary);
    }
}

function _renderRestoredArrayContent(content, toolResultMap, contentEl, msgIdx, collapsed, interactive) {
    const toolCalls = [];
    const textParts = [];
    let delegations = [];
    let restoredToolResults = [];
    let restoredCharts = [];

    content.forEach(function(block) {
        if (block.type === "text" && block.text) {
            textParts.push(block.text);
        } else if (block.type === "tool_use") {
            toolCalls.push(block);
        }
    });

    if (toolCalls.length > 0) {
        const tcResult = _renderRestoredToolCalls(toolCalls, toolResultMap, contentEl);
        delegations = tcResult.delegations;
        restoredToolResults = tcResult.restoredToolResults;
        _renderRestoredDelegations(delegations, tcResult.agentNames, contentEl, textParts);
        restoredCharts = _reconstructReportsAndCharts(toolCalls, toolResultMap, contentEl);
    }

    const restoredText = textParts.join("");
    if (restoredText.trim() && delegations.length === 0) {
        _renderRestoredTextWithChoices(restoredText, contentEl, msgIdx, collapsed, interactive);
    }

    return { restoredText: restoredText, restoredCharts: restoredCharts, restoredToolResults: restoredToolResults };
}

function _renderRestoredAssistantMessage(msg, msgIdx, collapsed, toolResultMap, interactive) {
    const el = addMessage("assistant", "");
    const contentEl = el.querySelector(".content");
    const content = msg.content;
    let restoredText = "";
    let restoredCharts = [];
    let restoredToolResults = [];

    if (typeof content === "string") {
        restoredText = content;
        const textDiv = document.createElement("div");
        textDiv.className = "md-text";
        textDiv.innerHTML = marked.parse(content);
        contentEl.appendChild(textDiv);
    } else if (Array.isArray(content)) {
        const result = _renderRestoredArrayContent(content, toolResultMap, contentEl, msgIdx, collapsed, interactive);
        restoredText = result.restoredText;
        restoredCharts = result.restoredCharts;
        restoredToolResults = result.restoredToolResults;
    }

    if (restoredText.trim() || restoredToolResults.length > 0) {
        el._exportMarkdown = restoredText;
        el._exportCharts = restoredCharts;
        el._exportToolResults = restoredToolResults;
        contentEl.appendChild(createResponseExportBar(el));
    }
}

// ─── parseAllMarkdownTables helpers ───

function _findTableTitle(lines, tableStartIndex) {
    for (let j = tableStartIndex - 1; j >= Math.max(0, tableStartIndex - 5); j--) {
        const prevLine = lines[j].trim();
        if (prevLine.match(/^#{1,6}\s+(.+)$/)) {
            return prevLine.replace(/^#{1,6}\s+/, "");
        }
        if (prevLine.length > 0 && !prevLine.includes("|")) {
            return prevLine;
        }
    }
    return null;
}

function _collectTableLines(lines, startIndex) {
    const tableLines = [];
    let i = startIndex;
    while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i]);
        i++;
    }
    return { lines: tableLines, nextIndex: i };
}

// ─── exportResponseAsCSV helpers ───

function _collectUniqueHeaders(rows) {
    const headers = [];
    const headerSet = {};
    rows.forEach(function(row) {
        Object.keys(row).forEach(function(key) {
            if (!headerSet[key]) {
                headerSet[key] = true;
                headers.push(key);
            }
        });
    });
    return headers;
}

function _formatRowsAsCSV(title, headers, rows) {
    const lines = [];
    lines.push("# " + title, headers.map(csvEscapeField).join(","));
    rows.forEach(function(row) {
        const vals = headers.map(function(h) {
            let val = row[h];
            if (typeof val === "object" && val !== null) val = JSON.stringify(val);
            return csvEscapeField(val);
        });
        lines.push(vals.join(","));
    });
    return lines.join("\n");
}

function _processToolResultForCSV(tr) {
    const sections = [];
    const rows = findTabularData(tr.result);

    if (!rows && tr.tool === "generate_report" && tr.input?.content) {
        const tables = parseAllMarkdownTables(tr.input.content);
        tables.forEach(function(table) {
            const headers = _collectUniqueHeaders(table.rows);
            sections.push(_formatRowsAsCSV(table.title, headers, table.rows));
        });
        return sections;
    }

    if (rows) {
        const headers = _collectUniqueHeaders(rows);
        sections.push(_formatRowsAsCSV(tr.tool || "results", headers, rows));
    }

    return sections;
}

// ─── exportResponseAsPDF helpers ───

function _applyPDFPrintStyles(clone) {
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
}

function _renderCanvasToMultiPagePDF(doc, canvas, margin, contentW, contentH) {
    const scale = canvas.width / contentW;
    const sliceH = Math.floor(contentH * scale);
    let yPx = 0;
    let pageNum = 0;
    while (yPx < canvas.height) {
        if (pageNum > 0) doc.addPage();
        const h = Math.min(sliceH, canvas.height - yPx);
        const pageCanvas = document.createElement("canvas");
        pageCanvas.width = canvas.width;
        pageCanvas.height = h;
        const ctx = pageCanvas.getContext("2d");
        ctx.drawImage(canvas, 0, yPx, canvas.width, h, 0, 0, canvas.width, h);
        const pageImg = pageCanvas.toDataURL("image/png");
        const drawH = h / scale;
        doc.addImage(pageImg, "PNG", margin, margin, contentW, drawH);
        yPx += sliceH;
        pageNum++;
    }
}

// ─── finalizeToolCall helper ───

function _getToolResultStatusText(result, wasCached) {
    const prefix = wasCached ? "cached: " : "";
    if (result.bytes_scanned !== undefined && result.row_count !== undefined) {
        const mb = (result.bytes_scanned / 1024 / 1024).toFixed(0);
        return prefix + result.row_count + " rows (" + mb + " MB scanned)";
    }
    if (result.row_count !== undefined) return prefix + result.row_count + " rows";
    if (result.instance_count !== undefined) return prefix + result.instance_count + " instances";
    if (result.user_count !== undefined) return prefix + result.user_count + " users";
    if (result.agreement_count !== undefined) return prefix + result.agreement_count + " agreements";
    if (result.event_count !== undefined) return prefix + result.event_count + " events";
    if (result.total_cost !== undefined) return prefix + "$" + result.total_cost.toLocaleString();
    if (result.filename) return prefix + result.filename;
    return wasCached ? "cached" : "done";
}

// ─── SSE form handler ───

form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (!question) return;

    promptHistory.push(question);
    promptHistoryIndex = -1;
    promptHistoryDraft = "";

    input.value = "";
    input.style.height = "auto";

    // Set up cancel button
    const abortController = new AbortController();
    sendBtn.textContent = "Cancel";
    sendBtn.classList.add("cancelling");
    sendBtn.type = "button";
    function onCancel() {
        abortController.abort();
        sendBtn.removeEventListener("click", onCancel);
    }
    sendBtn.addEventListener("click", onCancel);

    // Disable Share buttons on previous messages while streaming
    document.querySelectorAll(".response-export-btn").forEach(function(btn) {
        if (btn.textContent === "Share") {
            btn.disabled = true;
            btn.title = "Share is disabled while a query is running";
        }
    });

    // Collapse any active choice buttons from previous messages
    collapseActiveChoices("Skipped");

    const attachment = pendingAttachment;
    pendingAttachment = null;
    fileInput.value = "";
    attachmentIndicator.style.display = "none";
    uploadBtn.classList.remove("has-file");

    const displayText = attachment
        ? question + "\n\n\uD83D\uDCCE *" + attachment.name + "*"
        : question;
    addMessage("user", displayText);

    const assistantEl = addMessage("assistant", "");
    const contentEl = assistantEl.querySelector(".content");

    const statusEl = document.createElement("div");
    statusEl.className = "status";
    statusEl.innerHTML = '<div class="spinner"></div> Thinking...';
    contentEl.appendChild(statusEl);

    const state = {
        contentEl: contentEl,
        statusEl: statusEl,
        assistantEl: assistantEl,
        streamStarted: false,
        fullText: "",
        currentToolEl: null,
        toolElements: {},
        textChunks: [],
        currentChunk: "",
        chartCanvases: [],
        toolResults: [],
        currentToolName: null,
        currentToolInput: null,
        liveWrapper: null,
        liveInner: null,
        liveSummary: null,
        liveToolCount: 0,
    };

    function processEvent(eventType, data) {
        processStreamEvent(eventType, data, state);
    }

    try {
        let fullQuestion = question;
        if (attachment) {
            fullQuestion = question + "\n\n--- Attached file: " + attachment.name + " ---\n" + attachment.content;
        }
        if (!currentConversationId) {
            currentConversationId = crypto.randomUUID();
            try { localStorage.setItem("parsec_conv_id", currentConversationId); } catch { /* expected: localStorage may be unavailable */ }
        }
        const payload = {
            question: fullQuestion,
            conversation_history: conversationHistory.length > 0 ? conversationHistory : null,
            conversation_id: currentConversationId,
            session_id: sessionId,
        };
        const response = await fetch("/api/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            signal: abortController.signal,
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
        if (err.name === "AbortError") {
            // User cancelled — clean up gracefully
            if (!state.streamStarted) state.statusEl.remove();
            const cancelEl = document.createElement("div");
            cancelEl.className = "status-indicator cancelled";
            cancelEl.textContent = "Investigation cancelled.";
            contentEl.appendChild(cancelEl);
            // Remove any lingering spinners
            const oldStatus = contentEl.querySelector(".status-indicator:not(.cancelled)");
            if (oldStatus) oldStatus.remove();
            // Mark running agent banners as cancelled
            contentEl.querySelectorAll(".agent-banner.agent-running").forEach(function(b) {
                b.classList.remove("agent-running");
                b.classList.add("agent-cancelled");
                const statusSpan = b.querySelector(".agent-status");
                if (statusSpan) statusSpan.textContent = "cancelled";
            });
        } else {
            if (!state.streamStarted) state.statusEl.remove();
            const errorEl = document.createElement("div");
            errorEl.className = "error-message";
            errorEl.textContent = err.message;
            contentEl.appendChild(errorEl);
        }
    }

    // History is updated via the "history" SSE event from the server,
    // which includes the full message array with tool calls and results.

    // Re-enable Share buttons now that streaming is done
    document.querySelectorAll(".response-export-btn").forEach(function(btn) {
        if (btn.textContent === "Share") {
            btn.disabled = false;
            btn.title = "";
        }
    });

    // Restore Send button
    sendBtn.removeEventListener("click", onCancel);
    sendBtn.textContent = "Send";
    sendBtn.classList.remove("cancelling");
    sendBtn.type = "submit";
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

function addExpandCollapseToggle(summaryEl, wrapper) {
    const toggle = document.createElement("button");
    toggle.className = "tool-toggle-all";
    toggle.textContent = "Expand all";
    toggle.addEventListener("click", function(e) {
        e.stopPropagation();
        const inner = wrapper.querySelector(".tool-calls-inner");
        const details = inner.querySelectorAll("details.tool-call");
        const allOpen = Array.from(details).every(function(d) { return d.open; });
        details.forEach(function(d) { d.open = !allOpen; });
        toggle.textContent = allOpen ? "Expand all" : "Collapse all";
    });
    summaryEl.appendChild(toggle);
}

function finalizeToolCall(toolEl, toolName, result) {
    const statusSpan = toolEl.querySelector(".tool-status");
    const wasCached = statusSpan.classList.contains("cached");
    if (result.error) {
        statusSpan.className = "tool-status error";
        statusSpan.textContent = "error";
    } else {
        statusSpan.className = wasCached ? "tool-status cached" : "tool-status done";
        statusSpan.textContent = _getToolResultStatusText(result, wasCached);
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
        const colors = CHART_COLORS[i % CHART_COLORS.length];
        const config = {
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
    const allValues = datasets.flatMap(function(ds) { return ds.data; }).filter(function(v) { return v > 0; });
    let useLog = false;
    if (allValues.length >= 2) {
        const maxVal = Math.max.apply(null, allValues);
        const minVal = Math.min.apply(null, allValues);
        if (minVal > 0 && maxVal / minVal > 100) {
            useLog = true;
        }
    }

    /* eslint-disable-next-line no-new -- Chart.js renders via constructor side-effect */
    new Chart(canvas, {
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
    const exportBar = document.createElement("div");
    exportBar.className = "chart-export-bar";

    const pngBtn = document.createElement("button");
    pngBtn.className = "chart-export-btn";
    pngBtn.textContent = "Export PNG";
    pngBtn.addEventListener("click", function() {
        const link = document.createElement("a");
        link.download = (data.title || "chart").replaceAll(/[^a-z0-9]/gi, "_") + ".png";
        link.href = canvas.toDataURL("image/png");
        link.click();
    });

    const csvBtn = document.createElement("button");
    csvBtn.className = "chart-export-btn";
    csvBtn.textContent = "Export CSV";
    csvBtn.addEventListener("click", function() {
        const rows = ["Label," + data.datasets.map(function(ds) { return ds.label; }).join(",")];
        data.labels.forEach(function(label, i) {
            const vals = data.datasets.map(function(ds) { return ds.data[i]; });
            rows.push(label + "," + vals.join(","));
        });
        const blob = new Blob([rows.join("\n")], { type: "text/csv" });
        const link = document.createElement("a");
        link.download = (data.title || "chart").replaceAll(/[^a-z0-9]/gi, "_") + ".csv";
        link.href = URL.createObjectURL(blob);
        link.click();
    });

    exportBar.appendChild(pngBtn);
    exportBar.appendChild(csvBtn);
    wrapper.appendChild(exportBar);

    return wrapper;
}

function createResponseExportBar(messageEl) {
    const bar = document.createElement("div");
    bar.className = "response-export-bar";

    const toolResults = messageEl._exportToolResults || [];

    if (toolResults.length > 0) {
        const csvBtn = document.createElement("button");
        csvBtn.className = "response-export-btn";
        csvBtn.textContent = "Export CSV";
        csvBtn.addEventListener("click", function() { exportResponseAsCSV(messageEl); });
        bar.appendChild(csvBtn);

        const jsonBtn = document.createElement("button");
        jsonBtn.className = "response-export-btn";
        jsonBtn.textContent = "Export JSON";
        jsonBtn.addEventListener("click", function() { exportResponseAsJSON(messageEl); });
        bar.appendChild(jsonBtn);
    }

    const mdBtn = document.createElement("button");
    mdBtn.className = "response-export-btn";
    mdBtn.textContent = "Export MD";
    mdBtn.addEventListener("click", function() { exportResponseAsMarkdown(messageEl); });

    const pdfBtn = document.createElement("button");
    pdfBtn.className = "response-export-btn";
    pdfBtn.textContent = "Export PDF";
    pdfBtn.addEventListener("click", function() { exportResponseAsPDF(messageEl); });

    const shareBtn = document.createElement("button");
    shareBtn.className = "response-export-btn";
    shareBtn.textContent = "Share";
    shareBtn.addEventListener("click", async function() {
        if (conversationHistory.length === 0) return;
        shareBtn.disabled = true;
        shareBtn.textContent = "Sharing...";
        try {
            const resp = await fetch("/api/share", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ messages: conversationHistory }),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(function() { return {}; });
                alert(err.detail || "Failed to create share link");
                return;
            }
            const data = await resp.json();
            const shareUrl = window.location.origin + "/?share=" + data.id;
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
    let md = messageEl._exportMarkdown || "";
    const charts = messageEl._exportCharts || [];

    // Append chart images as base64 inline images
    charts.forEach(function(c) {
        const dataUrl = c.canvas.toDataURL("image/png");
        md += "\n\n![" + c.title + "](" + dataUrl + ")\n";
    });

    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const link = document.createElement("a");
    const timestamp = new Date().toISOString().slice(0, 19).replaceAll(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".md";
    link.href = URL.createObjectURL(blob);
    link.click();
}

function exportResponseAsPDF(messageEl) {
    const contentEl = messageEl.querySelector(".content");
    const clone = contentEl.cloneNode(true);

    // Remove export bars and tool summaries from clone
    clone.querySelectorAll(".response-export-bar, .chart-export-bar, .tool-calls-summary").forEach(function(el) {
        el.remove();
    });

    // Replace canvases with static images
    const origCanvases = contentEl.querySelectorAll("canvas");
    const cloneCanvases = clone.querySelectorAll("canvas");
    for (let i = 0; i < origCanvases.length; i++) {
        const img = document.createElement("img");
        img.src = origCanvases[i].toDataURL("image/png");
        img.style.maxWidth = "100%";
        cloneCanvases[i].replaceWith(img);
    }

    _applyPDFPrintStyles(clone);

    // Add clone to DOM visually hidden but still renderable by html2canvas
    clone.style.position = "fixed";
    clone.style.top = "0";
    clone.style.left = "0";
    clone.style.zIndex = "-1";
    document.body.appendChild(clone);

    html2canvas(clone, { scale: 2, useCORS: true }).then(function(canvas) {
        clone.remove();

        const jsPDF = window.jspdf.jsPDF;
        const pageW = 595.28;
        const pageH = 841.89;
        const margin = 20;
        const contentW = pageW - margin * 2;
        const contentH = pageH - margin * 2;

        const doc = new jsPDF({ orientation: "portrait", unit: "pt", format: "a4" });
        _renderCanvasToMultiPagePDF(doc, canvas, margin, contentW, contentH);

        const timestamp = new Date().toISOString().slice(0, 19).replaceAll(/[T:]/g, "-");
        doc.save("parsec-" + timestamp + ".pdf");
    });
}

function exportResponseAsJSON(messageEl) {
    const toolResults = messageEl._exportToolResults || [];
    if (toolResults.length === 0) return;

    const json = JSON.stringify(toolResults, null, 2);
    const blob = new Blob([json], { type: "application/json;charset=utf-8" });
    const link = document.createElement("a");
    const timestamp = new Date().toISOString().slice(0, 19).replaceAll(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".json";
    link.href = URL.createObjectURL(blob);
    link.click();
}

function csvEscapeField(value) {
    if (value === null || value === undefined) return "";
    const str = String(value);
    if (str.includes(",") || str.includes('"') || str.includes("\n")) {
        return '"' + str.replaceAll('"', '""') + '"';
    }
    return str;
}

function parseMarkdownTable(str) {
    // Parse a markdown table string into an array of objects
    const lines = str.split("\n").filter(function(l) { return l.trim().startsWith("|"); });
    if (lines.length < 3) return null; // need header + separator + at least one row
    // Check that the second line is a separator (| --- | --- |)
    if (!/^\|[\s\-:|]+\|$/.test(lines[1].trim())) return null;
    const headers = lines[0].split("|").slice(1, -1).map(function(h) { return h.trim(); });
    if (headers.length === 0) return null;
    const rows = [];
    for (let i = 2; i < lines.length; i++) {
        const cells = lines[i].split("|").slice(1, -1).map(function(c) { return c.trim(); });
        if (cells.length !== headers.length) continue;
        const obj = {};
        headers.forEach(function(h, idx) { obj[h] = cells[idx]; });
        rows.push(obj);
    }
    return rows.length > 0 ? rows : null;
}

function parseAllMarkdownTables(str) {
    if (typeof str !== "string") return [];
    const allLines = str.split("\n");
    const tables = [];
    let i = 0;
    while (i < allLines.length) {
        if (allLines[i].trim().startsWith("|")) {
            const title = _findTableTitle(allLines, i);
            const collected = _collectTableLines(allLines, i);
            i = collected.nextIndex;
            const parsed = parseMarkdownTable(collected.lines.join("\n"));
            if (parsed && parsed.length > 0) {
                tables.push({ title: title || "Table " + (tables.length + 1), rows: parsed });
            }
        } else {
            i++;
        }
    }
    return tables;
}

function findTabularData(result) {
    // Look for arrays of objects in the result
    if (Array.isArray(result) && result.length > 0 && typeof result[0] === "object") {
        return result;
    }
    if (typeof result !== "object" || result === null) return null;
    // Search top-level fields for the first array of objects
    const keys = Object.keys(result);
    for (const key of keys) {
        const val = result[key];
        if (Array.isArray(val) && val.length > 0 && typeof val[0] === "object" && val[0] !== null) {
            return val;
        }
    }
    // Fallback: try to parse markdown tables from string fields
    for (const key of keys) {
        const sval = result[key];
        if (typeof sval === "string" && sval.includes("|")) {
            const parsed = parseMarkdownTable(sval);
            if (parsed) return parsed;
        }
    }
    return null;
}

function exportResponseAsCSV(messageEl) {
    const toolResults = messageEl._exportToolResults || [];
    if (toolResults.length === 0) return;

    const csvSections = [];
    toolResults.forEach(function(tr) {
        const sections = _processToolResultForCSV(tr);
        sections.forEach(function(s) { csvSections.push(s); });
    });

    // Fallback: if no tabular data found, export as key-value pairs
    if (csvSections.length === 0) {
        toolResults.forEach(function(tr) {
            const headers = Object.keys(tr.result || {});
            const lines = ["# " + (tr.tool || "results"), "key,value"];
            headers.forEach(function(key) {
                let val = tr.result[key];
                if (typeof val === "object" && val !== null) val = JSON.stringify(val);
                lines.push(csvEscapeField(key) + "," + csvEscapeField(val));
            });
            csvSections.push(lines.join("\n"));
        });
    }

    const csv = csvSections.join("\n\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    const timestamp = new Date().toISOString().slice(0, 19).replaceAll(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".csv";
    link.href = URL.createObjectURL(blob);
    link.click();
}

function renderSharedMessages(messages, interactive) {
    const toolResultMap = _buildToolResultMap(messages);
    const collapsed = _collapseSubAgentMessages(messages);
    collapsed.forEach(function(msg, msgIdx) {
        if (msg.role === "user") {
            _renderRestoredUserMessage(msg);
        } else if (msg.role === "assistant") {
            _renderRestoredAssistantMessage(msg, msgIdx, collapsed, toolResultMap, interactive);
        }
    });
}


function scrollToBottom() {
    const chat = document.getElementById("chat");
    chat.scrollTop = chat.scrollHeight;
}

// ─── Debug Automation ───

const debugViewEl = document.getElementById("debug-view");
const chatEl = document.getElementById("chat");
const footerEl = document.querySelector("footer");

function showDebugView() {
    chatEl.style.display = "none";
    footerEl.style.display = "none";
    debugViewEl.style.display = "flex";
    debugViewEl.style.flexDirection = "column";
}

function showChatView() {
    debugViewEl.style.display = "none";
    chatEl.style.display = "";
    footerEl.style.display = "";
}

// Debug state
let debugResult = null;
let debugCorrelation = null;
let debugEEInfo = null;
let debugActiveTab = "triage";
let debugUrl = "";

const debugUrlInput = document.getElementById("debug-url");
const debugDiagnoseBtn = document.getElementById("debug-diagnose-btn");
const debugErrorEl = document.getElementById("debug-error");
const debugLoadingEl = document.getElementById("debug-loading");
const debugResultEl = document.getElementById("debug-result");
const debugSummaryEl = document.getElementById("debug-summary");
const debugTabContentEl = document.getElementById("debug-tab-content");
const debugFixPreviewEl = document.getElementById("debug-fix-preview");

debugDiagnoseBtn.addEventListener("click", runDiagnosis);
debugUrlInput.addEventListener("keydown", function(e) {
    if (e.key === "Enter") runDiagnosis();
});

function runDiagnosis() {
    const url = debugUrlInput.value.trim();
    if (!url) return;

    debugUrl = url;
    debugResult = null;
    debugCorrelation = null;
    debugEEInfo = null;
    debugActiveTab = "triage";

    debugErrorEl.style.display = "none";
    debugResultEl.style.display = "none";
    debugLoadingEl.style.display = "flex";
    debugDiagnoseBtn.disabled = true;

    fetch("/api/debug/diagnose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
    })
    .then(function(resp) {
        if (!resp.ok) return resp.json().then(function(d) { throw new Error(d.detail || resp.statusText); });
        return resp.json();
    })
    .then(function(data) {
        debugResult = data;
        renderDebugResult();
    })
    .catch(function(err) {
        debugErrorEl.textContent = err.message;
        debugErrorEl.style.display = "block";
    })
    .finally(function() {
        debugLoadingEl.style.display = "none";
        debugDiagnoseBtn.disabled = false;
    });
}

function formatElapsed(seconds) {
    if (!seconds) return "\u2014";
    if (seconds < 60) return Math.round(seconds) + "s";
    if (seconds < 3600) {
        const mins = Math.floor(seconds / 60);
        const secs = Math.round(seconds % 60);
        return secs > 0 ? mins + "m " + secs + "s" : mins + "m";
    }
    const hours = Math.floor(seconds / 3600);
    const mins2 = Math.floor((seconds % 3600) / 60);
    return mins2 > 0 ? hours + "h " + mins2 + "m" : hours + "h";
}

function statusColor(status) {
    return (status === "failed" || status === "error") ? "red" : "blue";
}

function escHtml(s) {
    if (!s) return "";
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
}

function renderDebugResult() {
    if (!debugResult) return;
    const m = debugResult.metadata;

    debugSummaryEl.innerHTML =
        '<span class="debug-status-label ' + statusColor(m.status) + '">' + escHtml(m.status) + '</span>' +
        '<strong>Job ' + m.id + '</strong>' +
        '<span>' + escHtml(m.action) + '</span>' +
        '<span>' + formatElapsed(m.elapsed) + '</span>';

    debugResultEl.style.display = "block";
    const tabs = debugResultEl.querySelectorAll(".debug-tab");
    tabs.forEach(function(tab) {
        tab.onclick = function() {
            debugActiveTab = tab.dataset.tab;
            tabs.forEach(function(t) { t.classList.remove("active"); });
            tab.classList.add("active");
            renderDebugTab();
        };
        if (tab.dataset.tab === debugActiveTab) {
            tab.classList.add("active");
        } else {
            tab.classList.remove("active");
        }
    });

    renderDebugTab();
    renderFixPreview();
}

function renderDebugTab() {
    if (debugActiveTab === "triage") renderTriageTab();
    else if (debugActiveTab === "failing-task") renderFailingTaskTab();
    else if (debugActiveTab === "fix") renderFixTab();
    else if (debugActiveTab === "correlation") renderCorrelationTab();
    else if (debugActiveTab === "ee-info") renderEEInfoTab();
    renderFixPreview();
}

function renderTriageTab() {
    const m = debugResult.metadata;
    let html = '<dl class="debug-dl">';
    html += '<dt>Status</dt><dd>' + escHtml(m.status) + '</dd>';
    html += '<dt>Action</dt><dd>' + escHtml(m.action) + '</dd>';
    html += '<dt>Started</dt><dd>' + (m.started ? new Date(m.started).toLocaleString() : "\u2014") + '</dd>';
    html += '<dt>Elapsed</dt><dd>' + formatElapsed(m.elapsed) + '</dd>';
    if (m.jobExplanation) {
        html += '<dt>Job Explanation</dt><dd>' + escHtml(m.jobExplanation) + '</dd>';
    }
    if (m.resultTraceback) {
        html += '<dt>Result Traceback</dt><dd><div class="debug-code">' + escHtml(m.resultTraceback) + '</div></dd>';
    }
    html += '</dl>';
    debugTabContentEl.innerHTML = html;
}

function renderFailingTaskTab() {
    const ft = debugResult.failingTask;
    if (!ft) {
        debugTabContentEl.innerHTML = '<p class="debug-empty">No failing task information available.</p>';
        return;
    }
    let html = '<dl class="debug-dl">';
    html += '<dt>Task</dt><dd>' + escHtml(ft.taskName) + '</dd>';
    if (ft.roleFqcn) html += '<dt>Role</dt><dd>' + escHtml(ft.roleFqcn) + '</dd>';
    if (ft.hostPattern) html += '<dt>Host</dt><dd>' + escHtml(ft.hostPattern) + '</dd>';
    if (ft.filePath) html += '<dt>File</dt><dd><code>' + escHtml(ft.filePath) + '</code></dd>';

    const pi = debugResult.projectInfo;
    if (pi) {
        const ref = pi.scmBranch || pi.scmRevision || "\u2014";
        let link = "";
        if (pi.scmUrl) {
            const repoMatch = /github\.com[:/]([^/]+\/[^/.]+)/.exec(pi.scmUrl);
            if (repoMatch) {
                const ghRef = pi.scmBranch || pi.scmRevision || "main";
                link = ' <a href="https://github.com/' + repoMatch[1] + '/tree/' + ghRef + '" target="_blank" rel="noopener" class="debug-link">View in GitHub</a>';
            }
        }
        html += '<dt>SCM Ref</dt><dd><code>' + escHtml(ref) + '</code>' + link + '</dd>';
    }
    html += '</dl>';
    let errorText = ft.errorMessage;
    try {
        const parsed = JSON.parse(errorText);
        errorText = JSON.stringify(parsed, null, 2);
    } catch { /* expected: errorText may not be valid JSON */ }
    html += '<div class="debug-code">' + escHtml(errorText) + '</div>';
    debugTabContentEl.innerHTML = html;
}

function renderFixTab() {
    const fix = debugResult.fix;
    if (!fix) {
        debugTabContentEl.innerHTML = '<p class="debug-empty">No fix recommendation available.</p>';
        return;
    }
    const labelColor = fix.source === "pattern" ? "green" : "blue";
    const labelText = fix.source === "pattern" ? "Pattern Match" : "AI Generated";

    let html = '<div style="margin-bottom:16px"><span class="debug-status-label ' + labelColor + '">' + labelText + '</span></div>';
    html += '<dl class="debug-dl">';
    html += '<dt>Repository</dt><dd>' + escHtml(fix.repo) + '</dd>';
    html += '<dt>File</dt><dd>' + escHtml(fix.file) + '</dd>';
    if (fix.line != null) html += '<dt>Line</dt><dd>' + fix.line + '</dd>';
    if (fix.lintWarning) {
        html += '<dt>Lint Warning</dt><dd style="color:var(--warning)">' + escHtml(fix.lintWarning) + '</dd>';
    }
    html += '</dl>';

    // Format explanation as paragraphs — split on double newlines, or sentences
    const rawExpl = fix.explanation || "";
    const paragraphs = rawExpl.includes("\n\n")
        ? rawExpl.split(/\n\n+/)
        : rawExpl.split(/(?<=\.)\s+(?=[A-Z])/);
    html += '<div class="debug-section-title">Explanation</div>';
    html += '<div class="debug-explanation">';
    paragraphs.forEach(function(p) {
        const trimmed = p.trim();
        if (trimmed) html += '<p>' + escHtml(trimmed) + '</p>';
    });
    html += '</div>';

    if (fix.before) {
        html += '<div class="debug-section-title">Before' + (fix.line != null ? ' (line ' + fix.line + ')' : '') + '</div>';
        html += '<div class="debug-code">' + escHtml(fix.before) + '</div>';
    }
    if (fix.after) {
        html += '<div class="debug-section-title">After</div>';
        html += '<div class="debug-code">' + escHtml(fix.after) + '</div>';
    }
    if (fix.githubUrl) {
        html += '<div style="margin-top:16px"><a href="' + escHtml(fix.githubUrl) + '" target="_blank" rel="noopener" class="debug-link">View on GitHub</a></div>';
    }
    debugTabContentEl.innerHTML = html;
}

function renderCorrelationTab() {
    if (debugCorrelation) {
        renderCorrelationData();
        return;
    }
    debugTabContentEl.innerHTML = '<div class="debug-loading" style="display:flex"><div class="debug-spinner"></div><span>Loading correlation data...</span></div>';

    fetch("/api/debug/correlation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: debugUrl, job_id: debugResult.metadata.id, job_template: debugResult.metadata.jobTemplate }),
    })
    .then(function(resp) {
        if (!resp.ok) return resp.json().then(function(d) { throw new Error(d.detail || resp.statusText); });
        return resp.json();
    })
    .then(function(data) {
        debugCorrelation = data;
        renderCorrelationData();
    })
    .catch(function(err) {
        debugTabContentEl.innerHTML = '<p class="debug-empty">Failed to load correlation data: ' + escHtml(err.message) + '</p>';
    });
}

function renderCorrelationData() {
    const c = debugCorrelation;
    let html = '<div class="debug-section-title">' + c.totalFailures + ' other failures of this job template</div>';

    if (c.byError && c.byError.length > 0) {
        html += '<div class="debug-section-title">By Error</div>';
        c.byError.slice(0, 5).forEach(function(entry) {
            html += '<div class="debug-card"><div class="debug-card-header"><span class="debug-card-mono">' + escHtml(entry.error || "(empty explanation)") + '</span><span class="debug-card-count">' + entry.count + ' jobs</span></div>';
            html += '<div class="debug-card-jobids">Job IDs: ' + entry.jobIds.slice(0, 10).join(", ") + (entry.jobIds.length > 10 ? " (+" + (entry.jobIds.length - 10) + " more)" : "") + '</div></div>';
        });
    }

    if (c.byEE && c.byEE.length > 0) {
        html += '<div class="debug-section-title">By Execution Environment</div>';
        c.byEE.slice(0, 5).forEach(function(entry) {
            html += '<div class="debug-card"><div class="debug-card-header"><span>EE #' + escHtml(entry.image) + '</span><span class="debug-card-count">' + entry.count + ' failures</span></div></div>';
        });
    }

    if (c.byInstanceGroup && c.byInstanceGroup.length > 0) {
        html += '<div class="debug-section-title">By Instance Group</div>';
        c.byInstanceGroup.forEach(function(entry) {
            html += '<div class="debug-card"><div class="debug-card-header"><span>Group #' + escHtml(entry.group) + '</span><span class="debug-card-count">' + entry.count + ' failures</span></div></div>';
        });
    }

    debugTabContentEl.innerHTML = html;
}

function renderEEInfoTab() {
    if (debugResult.eeInfo) {
        debugEEInfo = debugResult.eeInfo;
    }

    if (debugEEInfo) {
        renderEEInfoData();
        return;
    }

    if (!debugResult.metadata.executionEnvironment) {
        debugTabContentEl.innerHTML = '<p class="debug-empty">No execution environment available for this job.</p>';
        return;
    }

    debugTabContentEl.innerHTML = '<div class="debug-loading" style="display:flex"><div class="debug-spinner"></div><span>Loading EE info...</span></div>';

    fetch("/api/debug/ee", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: debugUrl, job_id: debugResult.metadata.id, ee_id: debugResult.metadata.executionEnvironment }),
    })
    .then(function(resp) {
        if (!resp.ok) return resp.json().then(function(d) { throw new Error(d.detail || resp.statusText); });
        return resp.json();
    })
    .then(function(data) {
        debugEEInfo = data;
        renderEEInfoData();
    })
    .catch(function(err) {
        debugTabContentEl.innerHTML = '<p class="debug-empty">Failed to load EE info: ' + escHtml(err.message) + '</p>';
    });
}

function renderEEInfoData() {
    const ee = debugEEInfo;
    let html = '<dl class="debug-dl">';
    html += '<dt>Image</dt><dd><code>' + escHtml(ee.image) + '</code></dd>';
    if (ee.sourceRepo) {
        const srcLink = 'https://github.com/' + ee.sourceRepo + '/tree/main/' + (ee.sourceDir || '');
        html += '<dt>Source</dt><dd><a href="' + srcLink + '" target="_blank" rel="noopener" class="debug-link">' + escHtml(ee.sourceRepo + '/' + (ee.sourceDir || '')) + '</a></dd>';
    }
    html += '</dl>';

    if (ee.sourceFiles && ee.sourceFiles.length > 0) {
        html += '<div class="debug-section-title">EE Definition Files</div>';
        ee.sourceFiles.forEach(function(file) {
            const fileId = "ee-file-" + file.name.replaceAll(/\W/g, "-");
            html += '<div class="debug-card">';
            html += '<button class="debug-ee-file-btn" onclick="toggleEEFile(\'' + fileId + '\', this)">\u25b6 ' + escHtml(file.name) + '</button>';
            html += '<div id="' + fileId + '" style="display:none;margin-top:8px"><div class="debug-code">' + escHtml(file.content) + '</div></div>';
            html += '</div>';
        });
    }

    debugTabContentEl.innerHTML = html;
}

window.toggleEEFile = function(id, btn) {
    const el = document.getElementById(id);
    if (el.style.display === "none") {
        el.style.display = "block";
        btn.innerHTML = "\u25bc " + btn.textContent.trim().slice(2);
    } else {
        el.style.display = "none";
        btn.innerHTML = "\u25b6 " + btn.textContent.trim().slice(2);
    }
};

function renderFixPreview() {
    if (!debugResult || !debugResult.fix || debugActiveTab === "fix") {
        debugFixPreviewEl.style.display = "none";
        return;
    }
    const fix = debugResult.fix;
    const labelColor = fix.source === "pattern" ? "green" : "blue";
    const labelText = fix.source === "pattern" ? "Pattern Match" : "AI Generated";

    debugFixPreviewEl.innerHTML =
        '<div style="flex:1">' +
            '<h4>Fix Recommendation Available</h4>' +
            '<span class="debug-status-label ' + labelColor + '">' + labelText + '</span>' +
        '</div>' +
        '<button onclick="document.querySelector(\'[data-tab=fix]\').click()">View Fix</button>';
    debugFixPreviewEl.style.display = "flex";
}

// ─── Choice buttons ───

function extractChoices(text) {
    // Match {{choices}} or {{choices multi}} ... {{/choices}}
    const match = /\{\{choices(\s+multi)?\}\}\s*\n([\s\S]*?)\{\{\/choices\}\}/.exec(text);
    if (!match) return null;

    const multi = !!match[1];
    const block = match[2];
    const options = [];
    block.split("\n").forEach(function(line) {
        const trimmed = line.replace(/^\s*-\s*/, "").trim();
        if (trimmed) options.push(trimmed);
    });

    if (options.length === 0) return null;

    const cleanedText = text.replace(/\{\{choices(\s+multi)?\}\}\s*\n[\s\S]*?\{\{\/choices\}\}/, "").trim();
    return { options: options, multi: multi, cleanedText: cleanedText };
}

function renderChoices(options, multi) {
    const container = document.createElement("div");
    container.className = "choices-container";
    container.dataset.active = "true";

    options.forEach(function(opt) {
        const btn = document.createElement("button");
        btn.className = "choice-btn";
        btn.textContent = opt;
        btn.addEventListener("click", function() {
            if (container.dataset.active !== "true") return;
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
        const submitBtn = document.createElement("button");
        submitBtn.className = "choices-submit";
        submitBtn.textContent = "Submit";
        submitBtn.addEventListener("click", function() {
            if (container.dataset.active !== "true") return;
            const selected = [];
            container.querySelectorAll(".choice-btn.selected").forEach(function(b) {
                selected.push(b.textContent);
            });
            if (selected.length === 0) return;
            const text = selected.join(", ");
            collapseChoices(container, text);
            input.value = text;
            form.requestSubmit();
        });
        container.appendChild(submitBtn);
    }

    return container;
}

function collapseChoices(container, selectedText) {
    container.dataset.active = "false";
    const summary = document.createElement("div");
    summary.className = "choices-summary";
    summary.innerHTML = 'Selected: <span class="choices-selected-values"></span>';
    summary.querySelector(".choices-selected-values").textContent = selectedText;
    container.replaceWith(summary);
}

function collapseActiveChoices(label) {
    document.querySelectorAll('.choices-container[data-active="true"]').forEach(function(c) {
        const summary = document.createElement("div");
        summary.className = "choices-summary";
        summary.textContent = label;
        c.replaceWith(summary);
    });
}
