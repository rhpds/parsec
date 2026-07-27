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
} catch (e) {
    // Ignore corrupt data
}

// ─── File upload ───
uploadBtn.addEventListener("click", function() { fileInput.click(); });

fileInput.addEventListener("change", function() {
    const file = fileInput.files[0];
    if (!file) return;
    if (file.size > MAX_UPLOAD_SIZE) {
        alert("File too large — maximum size is 10 MB.");
        fileInput.value = "";
        return;
    }
    const reader = new FileReader();
    reader.onload = function() {
        pendingAttachment = { name: file.name, content: reader.result };
        attachmentNameEl.textContent = file.name;
        attachmentIndicator.style.display = "flex";
        uploadBtn.classList.add("has-file");
    };
    reader.readAsText(file);
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
    }).catch(function() {});
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
    }).catch(function() {});
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
    }).catch(function() {});
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
    }).catch(function() {});
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
        } catch (e) {}
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
        try { localStorage.setItem("parsec_conv_id", data.id); } catch (e) {}
        loadConversationList();
        // Refresh learnings count after background analysis has time to complete
        setTimeout(refreshLearningsCount, 20000);
    }).catch(function() {});
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
                    try { localStorage.setItem("parsec_history", JSON.stringify(conversationHistory)); } catch (e) {}
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

    let fullText = "";
    let currentToolEl = null;
    let toolElements = {};   // Map tool name to element for finalization
    let streamStarted = false;
    let textChunks = [];     // Array of text segments between tool calls
    let currentChunk = "";   // Current text being accumulated
    let chartCanvases = [];  // Track chart canvases for export
    let toolResults = [];    // Captured tool results for CSV/JSON export
    let currentToolName = null;
    let currentToolInput = null;
    let liveWrapper = null;  // Live tool-calls-summary wrapper
    let liveInner = null;    // Inner container for tool calls
    let liveSummary = null;  // Summary element (updated with count)
    let liveToolCount = 0;   // Running count of tool calls

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
                    const prevStatus = currentToolEl.querySelector(".tool-status");
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
                // Remove the live text element — it'll be collapsed into wrapper later
                const liveTextEl = contentEl.querySelector(".md-text-live");
                if (liveTextEl) liveTextEl.remove();

                // Create the live wrapper on first tool call
                if (!liveWrapper) {
                    liveWrapper = document.createElement("details");
                    liveWrapper.className = "tool-calls-summary";
                    liveWrapper.open = true;
                    liveSummary = document.createElement("summary");
                    liveWrapper.appendChild(liveSummary);
                    liveInner = document.createElement("div");
                    liveInner.className = "tool-calls-inner";
                    liveWrapper.appendChild(liveInner);
                    contentEl.appendChild(liveWrapper);
                }

                liveToolCount++;
                liveSummary.textContent = liveToolCount === 1
                    ? "1 query running..."
                    : liveToolCount + " queries running...";

                currentToolEl = createToolCall(data.tool, data.input);
                currentToolName = data.tool;
                currentToolInput = data.input;
                toolElements[data.tool + "_" + Object.keys(toolElements).length] = currentToolEl;
                liveInner.appendChild(currentToolEl);
                scrollToBottom();
                break;
            }

            case "cache_hit": {
                if (currentToolEl) {
                    const cacheStatus = currentToolEl.querySelector(".tool-status");
                    if (cacheStatus) {
                        cacheStatus.className = "tool-status cached";
                        cacheStatus.textContent = "cached";
                    }
                }
                break;
            }

            case "tool_result": {
                if (currentToolEl) {
                    finalizeToolCall(currentToolEl, data.tool, data.result);
                    toolResults.push({ tool: currentToolName || data.tool, input: currentToolInput || {}, result: data.result });
                    currentToolName = null;
                    currentToolInput = null;
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

            case "agent_start": {
                ensureStreamStarted();
                const agentBanner = document.createElement("div");
                agentBanner.className = "agent-banner agent-running";
                agentBanner.dataset.agent = data.agent;
                agentBanner.innerHTML = '<span class="agent-icon">&#9881;</span> ' +
                    '<span class="agent-label">' + (data.name || data.agent) + '</span>' +
                    ' <span class="agent-status">investigating\u2026</span>';
                contentEl.appendChild(agentBanner);
                scrollToBottom();
                break;
            }

            case "agent_done": {
                const banners = contentEl.querySelectorAll('.agent-banner[data-agent="' + data.agent + '"]');
                banners.forEach(function(b) {
                    b.classList.remove("agent-running");
                    b.classList.add("agent-done");
                    const statusSpan = b.querySelector(".agent-status");
                    if (statusSpan) statusSpan.textContent = "done";
                });
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

            case "confidence": {
                ensureStreamStarted();
                const level = data.level;
                const reasons = data.reasons || [];
                if (level === "medium" || level === "low") {
                    const callout = document.createElement("div");
                    callout.className = "confidence-callout " + level;
                    const title = level === "low" ? "Low confidence" : "Medium confidence";
                    const icon = level === "low" ? "\u26A0\uFE0F" : "\u26A0";
                    let html = '<div class="confidence-title">' + icon + " " + title + "</div>";
                    if (reasons.length > 0) {
                        html += "<ul>";
                        reasons.forEach(function(r) {
                            html += "<li>" + r.replace(/</g, "&lt;").replace(/>/g, "&gt;") + "</li>";
                        });
                        html += "</ul>";
                    }
                    callout.innerHTML = html;
                    contentEl.appendChild(callout);
                    scrollToBottom();
                }
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

                // Finalize the live tool-calls wrapper
                if (liveWrapper && liveSummary) {
                    const qCount = liveToolCount;
                    liveSummary.textContent = qCount === 1
                        ? "1 query executed"
                        : qCount + " queries executed";
                    if (qCount > 1) {
                        addExpandCollapseToggle(liveSummary, liveWrapper);
                    }

                    // Rebuild inner with interleaved thinking text + tool calls
                    const toolEls = Array.from(liveInner.querySelectorAll(".tool-call"));
                    liveInner.replaceChildren();
                    let chunkIdx = 0;
                    toolEls.forEach(function(tc) {
                        if (chunkIdx < textChunks.length) {
                            const thinkEl = document.createElement("div");
                            thinkEl.className = "thinking-text";
                            thinkEl.innerHTML = marked.parse(textChunks[chunkIdx]); // safe: server-generated
                            liveInner.appendChild(thinkEl);
                            chunkIdx++;
                        }
                        liveInner.appendChild(tc);
                    });
                    while (chunkIdx < textChunks.length) {
                        const thinkEl2 = document.createElement("div");
                        thinkEl2.className = "thinking-text";
                        thinkEl2.innerHTML = marked.parse(textChunks[chunkIdx]); // safe: server-generated
                        liveInner.appendChild(thinkEl2);
                        chunkIdx++;
                    }

                    // Collapse the wrapper now that all queries are done
                    liveWrapper.open = false;
                    // Move wrapper to top of content
                    contentEl.insertBefore(liveWrapper, contentEl.firstChild);
                }

                // Scan final text for [confidence: ...] markers from the agent
                const allTextEls = contentEl.querySelectorAll(".md-text, .md-text-live");
                allTextEls.forEach(function(el) {
                    const html = el.innerHTML;
                    const markerRegex = /\[confidence:\s*(medium|low)\s*\|\s*([^\]]+)\]/gi;
                    let match;
                    while ((match = markerRegex.exec(html)) !== null) {
                        const markerLevel = match[1].toLowerCase();
                        const markerReason = match[2].trim();
                        // Create callout if not already present from SSE event
                        const existing = contentEl.querySelector(".confidence-callout");
                        if (!existing) {
                            const mc = document.createElement("div");
                            mc.className = "confidence-callout " + markerLevel;
                            const mTitle = markerLevel === "low" ? "Low confidence" : "Medium confidence";
                            mc.innerHTML = '<div class="confidence-title">\u26A0 ' + mTitle + "</div><ul><li>" + markerReason.replace(/</g, "&lt;") + "</li></ul>";
                            contentEl.appendChild(mc);
                        } else {
                            // Merge: downgrade level if needed, add reason
                            if (markerLevel === "low" && existing.classList.contains("medium")) {
                                existing.classList.remove("medium");
                                existing.classList.add("low");
                                existing.querySelector(".confidence-title").innerHTML = '\u26A0\uFE0F Low confidence';
                            }
                            const ul = existing.querySelector("ul");
                            if (ul) {
                                const li = document.createElement("li");
                                li.textContent = markerReason;
                                ul.appendChild(li);
                            }
                        }
                    }
                    // Strip markers from displayed text
                    el.innerHTML = html.replace(/\[confidence:\s*(?:medium|low)\s*\|\s*[^\]]+\]/gi, "");
                });

                // Extract choice buttons from {{choices}} blocks before rendering final text
                let finalText = currentChunk || fullText;
                const choicesResult = extractChoices(finalText);
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
                assistantEl._exportToolResults = toolResults;
                if (fullText.trim() || currentChunk.trim() || chartCanvases.length > 0 || toolResults.length > 0) {
                    contentEl.appendChild(createResponseExportBar(assistantEl));
                }

                scrollToBottom();
                break;
            }
        }
    }

    try {
        let fullQuestion = question;
        if (attachment) {
            fullQuestion = question + "\n\n--- Attached file: " + attachment.name + " ---\n" + attachment.content;
        }
        if (!currentConversationId) {
            currentConversationId = crypto.randomUUID();
            try { localStorage.setItem("parsec_conv_id", currentConversationId); } catch (e) {}
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
            if (!streamStarted) statusEl.remove();
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
            if (!streamStarted) statusEl.remove();
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
        const prefix = wasCached ? "cached: " : "";
        if (result.bytes_scanned !== undefined && result.row_count !== undefined) {
            const mb = (result.bytes_scanned / 1024 / 1024).toFixed(0);
            statusSpan.textContent = prefix + result.row_count + " rows (" + mb + " MB scanned)";
        } else if (result.row_count !== undefined) {
            statusSpan.textContent = prefix + result.row_count + " rows";
        } else if (result.instance_count !== undefined) {
            statusSpan.textContent = prefix + result.instance_count + " instances";
        } else if (result.user_count !== undefined) {
            statusSpan.textContent = prefix + result.user_count + " users";
        } else if (result.agreement_count !== undefined) {
            statusSpan.textContent = prefix + result.agreement_count + " agreements";
        } else if (result.event_count !== undefined) {
            statusSpan.textContent = prefix + result.event_count + " events";
        } else if (result.total_cost !== undefined) {
            statusSpan.textContent = prefix + "$" + result.total_cost.toLocaleString();
        } else if (result.filename) {
            statusSpan.textContent = prefix + result.filename;
        } else {
            statusSpan.textContent = wasCached ? "cached" : "done";
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

    const chartInstance = new Chart(canvas, {
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
        link.download = (data.title || "chart").replace(/[^a-z0-9]/gi, "_") + ".png";
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
    const timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
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

        const jsPDF = window.jspdf.jsPDF;

        // A4 dimensions in pt
        const pageW = 595.28;
        const pageH = 841.89;
        const margin = 20;
        const contentW = pageW - margin * 2;
        const contentH = pageH - margin * 2;

        // How many source pixels correspond to one page of content
        const scale = canvas.width / contentW;
        const sliceH = Math.floor(contentH * scale);

        const doc = new jsPDF({ orientation: "portrait", unit: "pt", format: "a4" });
        let yPx = 0;
        let pageNum = 0;

        while (yPx < canvas.height) {
            if (pageNum > 0) doc.addPage();
            const h = Math.min(sliceH, canvas.height - yPx);

            // Crop this page's slice from the full canvas
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

        const timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
        doc.save("parsec-" + timestamp + ".pdf");
    });
}

function exportResponseAsJSON(messageEl) {
    const toolResults = messageEl._exportToolResults || [];
    if (toolResults.length === 0) return;

    const json = JSON.stringify(toolResults, null, 2);
    const blob = new Blob([json], { type: "application/json;charset=utf-8" });
    const link = document.createElement("a");
    const timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".json";
    link.href = URL.createObjectURL(blob);
    link.click();
}

function csvEscapeField(value) {
    if (value === null || value === undefined) return "";
    const str = String(value);
    if (str.indexOf(",") >= 0 || str.indexOf('"') >= 0 || str.indexOf("\n") >= 0) {
        return '"' + str.replace(/"/g, '""') + '"';
    }
    return str;
}

function parseMarkdownTable(str) {
    // Parse a markdown table string into an array of objects
    const lines = str.split("\n").filter(function(l) { return l.trim().indexOf("|") === 0; });
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
    // Parse ALL markdown tables from a string and return them with context
    // Returns an array of {title: string, rows: array} objects
    if (typeof str !== "string") return [];

    const allLines = str.split("\n");
    const tables = [];
    let i = 0;

    while (i < allLines.length) {
        const line = allLines[i].trim();

        // Look for table start (line with |)
        if (line.indexOf("|") === 0) {
            // Try to find preceding header (lines starting with ##)
            let title = null;
            for (let j = i - 1; j >= Math.max(0, i - 5); j--) {
                const prevLine = allLines[j].trim();
                if (prevLine.match(/^#{1,6}\s+(.+)$/)) {
                    title = prevLine.replace(/^#{1,6}\s+/, "");
                    break;
                }
                if (prevLine.length > 0 && prevLine.indexOf("|") < 0) {
                    // Use first non-empty, non-table line as title
                    if (!title) title = prevLine;
                    break;
                }
            }

            // Collect all consecutive table lines
            const tableLines = [];
            while (i < allLines.length && allLines[i].trim().indexOf("|") === 0) {
                tableLines.push(allLines[i]);
                i++;
            }

            // Parse this table
            const tableStr = tableLines.join("\n");
            const parsed = parseMarkdownTable(tableStr);
            if (parsed && parsed.length > 0) {
                tables.push({
                    title: title || "Table " + (tables.length + 1),
                    rows: parsed
                });
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
    for (let i = 0; i < keys.length; i++) {
        const val = result[keys[i]];
        if (Array.isArray(val) && val.length > 0 && typeof val[0] === "object" && val[0] !== null) {
            return val;
        }
    }
    // Fallback: try to parse markdown tables from string fields
    for (let j = 0; j < keys.length; j++) {
        const sval = result[keys[j]];
        if (typeof sval === "string" && sval.indexOf("|") >= 0) {
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
        // First try to find tabular data in the result
        const rows = findTabularData(tr.result);

        // For report generation tools, also check input.content for markdown tables
        if (!rows && tr.tool === "generate_report" && tr.input && tr.input.content) {
            const tables = parseAllMarkdownTables(tr.input.content);
            if (tables.length > 0) {
                // Export each table as a separate CSV section
                tables.forEach(function(table) {
                    const headers = [];
                    const headerSet = {};
                    table.rows.forEach(function(row) {
                        Object.keys(row).forEach(function(key) {
                            if (!headerSet[key]) {
                                headerSet[key] = true;
                                headers.push(key);
                            }
                        });
                    });

                    const lines = [];
                    lines.push("# " + table.title);
                    lines.push(headers.map(csvEscapeField).join(","));
                    table.rows.forEach(function(row) {
                        const vals = headers.map(function(h) {
                            let val = row[h];
                            if (typeof val === "object" && val !== null) val = JSON.stringify(val);
                            return csvEscapeField(val);
                        });
                        lines.push(vals.join(","));
                    });

                    csvSections.push(lines.join("\n"));
                });
                return; // Skip the rest of the processing for this tool result
            }
        }

        // If we found rows in the result, export them
        if (rows) {
            // Collect all unique column headers across all rows
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

            const lines = [];
            // Section header comment
            lines.push("# " + (tr.tool || "results"));
            // Column headers
            lines.push(headers.map(csvEscapeField).join(","));
            // Data rows
            rows.forEach(function(row) {
                const vals = headers.map(function(h) {
                    let val = row[h];
                    if (typeof val === "object" && val !== null) val = JSON.stringify(val);
                    return csvEscapeField(val);
                });
                lines.push(vals.join(","));
            });

            csvSections.push(lines.join("\n"));
        }
    });

    // Fallback: if no tabular data found, export as key-value pairs
    if (csvSections.length === 0) {
        toolResults.forEach(function(tr) {
            const lines = [];
            lines.push("# " + (tr.tool || "results"));
            lines.push("key,value");
            Object.keys(tr.result || {}).forEach(function(key) {
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
    const timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
    link.download = "parsec-" + timestamp + ".csv";
    link.href = URL.createObjectURL(blob);
    link.click();
}

function renderSharedMessages(messages, interactive) {
    // Build tool_use_id → tool_result map for reconst ructing reports/charts
    const toolResultMap = {};
    messages.forEach(function(msg) {
        if (msg.role !== "user" || !Array.isArray(msg.content)) return;
        msg.content.forEach(function(block) {
            if (block.type === "tool_result" && block.tool_use_id) {
                try {
                    toolResultMap[block.tool_use_id] = JSON.parse(block.content);
                } catch (e) {
                    toolResultMap[block.tool_use_id] = block.content;
                }
            }
        });
    });

    // Pre-process: collapse fast-path sub-agent intermediate messages.
    // Pattern: assistant(tool_use) → user(tool_result only) → assistant(tool_use) → ...
    // Collapse into one combined message, keeping only the final assistant text.
    const collapsed = [];
    let i = 0;
    while (i < messages.length) {
        const msg = messages[i];
        if (msg.role === "assistant" && Array.isArray(msg.content)) {
            const hasToolUse = msg.content.some(function(b) { return b.type === "tool_use"; });
            if (hasToolUse) {
                // Look ahead: is the next message a tool_result-only user message?
                const groupToolCalls = [];
                const groupToolUseIds = [];
                let finalText = [];
                let j = i;
                while (j < messages.length) {
                    const cur = messages[j];
                    if (cur.role === "assistant" && Array.isArray(cur.content)) {
                        const curTools = cur.content.filter(function(b) { return b.type === "tool_use"; });
                        const curText = cur.content.filter(function(b) { return b.type === "text" && b.text; });
                        curTools.forEach(function(t) { groupToolCalls.push(t); groupToolUseIds.push(t.id); });
                        if (curText.length > 0) finalText = curText;
                        // Check if next is tool_result-only user message
                        const next = messages[j + 1];
                        if (next && next.role === "user" && Array.isArray(next.content)) {
                            const hasRealText = next.content.some(function(b) {
                                return b.type !== "tool_result" && b.text && b.text.trim();
                            });
                            if (!hasRealText) {
                                j += 2; // skip tool_result user msg + continue to next assistant
                                continue;
                            }
                        }
                    }
                    break;
                }
                if (j > i) {
                    // We collapsed multiple messages — create a combined one
                    const combinedContent = [];
                    groupToolCalls.forEach(function(t) { combinedContent.push(t); });
                    finalText.forEach(function(t) { combinedContent.push(t); });
                    collapsed.push({ role: "assistant", content: combinedContent, _collapsedToolIds: groupToolUseIds });
                    i = j;
                    continue;
                }
            }
        }
        collapsed.push(messages[i]);
        i++;
    }

    collapsed.forEach(function(msg, msgIdx) {
        if (msg.role === "user") {
            let text = msg.content;
            if (Array.isArray(text)) {
                const userParts = text.filter(function(b) { return b.type !== "tool_result"; });
                text = userParts.map(function(b) { return b.text || ""; }).join("");
            }
            if (!text.trim()) return;
            addMessage("user", text);
        } else if (msg.role === "assistant") {
            const el = addMessage("assistant", "");
            const contentEl = el.querySelector(".content");
            const content = msg.content;
            let restoredText = "";
            const restoredCharts = [];
            const restoredToolResults = [];

            if (typeof content === "string") {
                restoredText = content;
                const textDiv = document.createElement("div");
                textDiv.className = "md-text";
                textDiv.innerHTML = marked.parse(content);
                contentEl.appendChild(textDiv);
            } else if (Array.isArray(content)) {
                const toolCalls = [];
                const textParts = [];
                let delegations = [];

                content.forEach(function(block) {
                    if (block.type === "text" && block.text) {
                        textParts.push(block.text);
                    } else if (block.type === "tool_use") {
                        toolCalls.push(block);
                    }
                });

                // Show tool calls as collapsed summary
                if (toolCalls.length > 0) {
                    // Count total queries including sub-agent tool calls
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
                    delegations = [];
                    toolCalls.forEach(function(tc) {
                        const result = toolResultMap[tc.id];
                        const isDelegation = tc.name in delegationTools;
                        if (isDelegation && result && result.tool_calls) {
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

                        // Append tool result if available
                        const result = toolResultMap[tc.id];
                        if (result && typeof result === "object") {
                            body.textContent += "\n\n--- Result ---\n" + JSON.stringify(result, null, 2);
                        }

                        tcEl.appendChild(body);
                        inner.appendChild(tcEl);
                    });
                    wrapper.appendChild(inner);
                    contentEl.appendChild(wrapper);

                    // Render sub-agent banners and findings for delegations
                    delegations.forEach(function(d) {
                        const agentType = d.agentType || d.result.agent || "unknown";
                        const agentLabel = agentNames[agentType] || agentType;

                        const agentBanner = document.createElement("div");
                        agentBanner.className = "agent-banner agent-done";
                        agentBanner.dataset.agent = agentType;
                        const iconSpan = document.createElement("span");
                        iconSpan.className = "agent-icon";
                        iconSpan.textContent = "\u2699";
                        const labelSpan = document.createElement("span");
                        labelSpan.className = "agent-label";
                        labelSpan.textContent = agentLabel;
                        const statusSpan2 = document.createElement("span");
                        statusSpan2.className = "agent-status";
                        statusSpan2.textContent = "done";
                        agentBanner.appendChild(iconSpan);
                        agentBanner.appendChild(document.createTextNode(" "));
                        agentBanner.appendChild(labelSpan);
                        agentBanner.appendChild(document.createTextNode(" "));
                        agentBanner.appendChild(statusSpan2);
                        contentEl.appendChild(agentBanner);

                        // Render sub-agent summary as markdown (server-generated, not user input)
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
                            findingsDiv.innerHTML = marked.parse(summaryText); // safe: server-generated markdown
                            contentEl.appendChild(findingsDiv);
                            textParts.push(summaryText);
                        }
                    });

                    // Reconst ruct report download links and charts
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
                                console.warn("Failed to reconst ruct chart:", e);
                            }
                        }
                    });
                }

                // Render text content (skip if delegations already rendered summaries)
                restoredText = textParts.join("");
                if (restoredText.trim() && delegations.length === 0) {
                    const sharedChoices = extractChoices(restoredText);
                    const renderText = sharedChoices ? sharedChoices.cleanedText : restoredText;
                    const textDiv2 = document.createElement("div");
                    textDiv2.className = "md-text";
                    textDiv2.innerHTML = marked.parse(renderText);  // safe: server-generated markdown
                    contentEl.appendChild(textDiv2);
                    if (sharedChoices) {
                        const isLastMsg = (msgIdx === messages.length - 1);
                        if (interactive && isLastMsg) {
                            contentEl.appendChild(renderChoices(sharedChoices.options, sharedChoices.multi));
                        } else {
                            const choicesSummary = document.createElement("div");
                            choicesSummary.className = "choices-summary";
                            choicesSummary.textContent = "Choices were presented";
                            contentEl.appendChild(choicesSummary);
                        }
                    }
                }
            }

            // Add export bar to restored assistant messages
            if (restoredText.trim() || restoredToolResults.length > 0) {
                el._exportMarkdown = restoredText;
                el._exportCharts = restoredCharts;
                el._exportToolResults = restoredToolResults;
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
            const repoMatch = pi.scmUrl.match(/github\.com[:/]([^/]+\/[^/.]+)/);
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
    } catch (e) {}
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
    const paragraphs = rawExpl.indexOf("\n\n") >= 0
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
            const fileId = "ee-file-" + file.name.replace(/\W/g, "-");
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
    const match = text.match(/\{\{choices(\s+multi)?\}\}\s*\n([\s\S]*?)\{\{\/choices\}\}/);
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
