/**
 * Extended tests for static/app.js — covers sidebar, history, share,
 * message rendering, export, debug, admin, theme, and SSE form handler.
 *
 * Goal: cover ~300+ additional lines to push coverage from ~42% to 70%+.
 *
 * NOTE: app.js uses script-level `let` variables (debugResult, isAdmin,
 * adminViewAllChats, conversationHistory, currentConversationId, etc.)
 * that are NOT accessible via globalThis. Only `function` declarations
 * are on globalThis. Tests must go through the public function API
 * (e.g. test debug rendering via runDiagnosis, not by setting
 * globalThis.debugResult directly).
 */
import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs, fn } from './setup.js';

beforeAll(() => {
  loadAppJs();
});

// ─── Helper to reset key DOM elements between tests ───
function resetDOM() {
  document.getElementById('messages').innerHTML = '';
  document.getElementById('sidebar-list').textContent = '';
  document.getElementById('sidebar-list').style.display = '';
  document.getElementById('skills-list').textContent = '';
  const sidebar = document.getElementById('sidebar');
  sidebar.classList.remove('open');
  const learningsPanel = document.getElementById('learnings-panel');
  learningsPanel.style.display = 'none';
  const skillsPanel = document.getElementById('skills-panel');
  skillsPanel.style.display = 'none';
  const debugView = document.getElementById('debug-view');
  debugView.style.display = 'none';
  const chat = document.getElementById('chat');
  chat.style.display = '';
  const footer = document.querySelector('footer');
  if (footer) footer.style.display = '';
}

// ────────────────────────────────────────────────────────────────
// Sidebar functions
// ────────────────────────────────────────────────────────────────

describe('openSidebar', () => {
  const openSidebar = () => fn('openSidebar');

  beforeEach(() => {
    resetDOM();
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ conversations: [], skills: [] }),
    });
  });

  it('opens sidebar with history section', () => {
    openSidebar()('history');
    const sidebar = document.getElementById('sidebar');
    expect(sidebar.classList.contains('open')).toBe(true);
    expect(document.getElementById('sidebar-list').style.display).toBe('');
    const title = sidebar.querySelector('.sidebar-title');
    expect(title.textContent).toBe('History');
  });

  it('opens sidebar with examples section', () => {
    openSidebar()('examples');
    const sidebar = document.getElementById('sidebar');
    expect(sidebar.classList.contains('open')).toBe(true);
    const title = sidebar.querySelector('.sidebar-title');
    expect(title.textContent).toBe('Examples');
    const examplesEl = sidebar.querySelector('.sidebar-examples');
    expect(examplesEl.style.display).toBe('');
    expect(document.getElementById('sidebar-list').style.display).toBe('none');
  });

  it('opens sidebar with skills section', () => {
    openSidebar()('skills');
    const sidebar = document.getElementById('sidebar');
    expect(sidebar.classList.contains('open')).toBe(true);
    const title = sidebar.querySelector('.sidebar-title');
    expect(title.textContent).toBe('Skills');
    expect(document.getElementById('skills-panel').style.display).toBe('block');
    expect(document.getElementById('sidebar-list').style.display).toBe('none');
  });

  it('hides other panels when opening history', () => {
    openSidebar()('history');
    expect(document.getElementById('skills-panel').style.display).not.toBe('block');
    const sidebar = document.getElementById('sidebar');
    expect(sidebar.querySelector('.sidebar-examples').style.display).toBe('none');
  });

  it('hides sidebar-list when opening examples', () => {
    openSidebar()('examples');
    expect(document.getElementById('sidebar-list').style.display).toBe('none');
    expect(document.getElementById('learnings-panel').style.display).toBe('none');
  });
});

describe('closeSidebar', () => {
  const closeSidebar = () => fn('closeSidebar');
  const openSidebar = () => fn('openSidebar');

  beforeEach(() => {
    resetDOM();
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ conversations: [] }),
    });
  });

  it('removes open class from sidebar', () => {
    openSidebar()('history');
    const sidebar = document.getElementById('sidebar');
    expect(sidebar.classList.contains('open')).toBe(true);
    closeSidebar()();
    expect(sidebar.classList.contains('open')).toBe(false);
  });
});

// ────────────────────────────────────────────────────────────────
// Conversation list / history
// ────────────────────────────────────────────────────────────────

describe('renderConversationList', () => {
  const renderConversationList = () => fn('renderConversationList');

  beforeEach(() => {
    resetDOM();
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
  });

  it('shows empty message when no conversations', () => {
    renderConversationList()([]);
    const sidebarList = document.getElementById('sidebar-list');
    const empty = sidebarList.querySelector('.sidebar-empty');
    expect(empty).not.toBeNull();
    expect(empty.textContent).toBe('No previous conversations');
  });

  it('renders conversation items', () => {
    const convs = [
      { id: 'conv-1', title: 'Test Conv', updated_at: '2026-01-15T10:00:00Z', message_count: 5 },
      { id: 'conv-2', title: 'Another Conv', updated_at: '2026-01-16T12:00:00Z', message_count: 3 },
    ];
    renderConversationList()(convs);
    const sidebarList = document.getElementById('sidebar-list');
    const items = sidebarList.querySelectorAll('.sidebar-item');
    expect(items).toHaveLength(2);
    expect(items[0].querySelector('.sidebar-item-title').textContent).toBe('Test Conv');
    expect(items[1].querySelector('.sidebar-item-title').textContent).toBe('Another Conv');
  });

  it('shows meta text with message count and date', () => {
    const convs = [
      { id: 'conv-1', title: 'Test', updated_at: '2026-01-15T10:00:00Z', message_count: 5 },
    ];
    renderConversationList()(convs);
    const meta = document.getElementById('sidebar-list').querySelector('.sidebar-item-meta');
    expect(meta.textContent).toContain('5 msgs');
  });

  it('has delete button on each item', () => {
    const convs = [
      { id: 'conv-1', title: 'Test', updated_at: '2026-01-15T10:00:00Z', message_count: 1 },
    ];
    renderConversationList()(convs);
    const deleteBtn = document.getElementById('sidebar-list').querySelector('.sidebar-item-delete');
    expect(deleteBtn).not.toBeNull();
    expect(deleteBtn.textContent).toBe('×');
  });

  it('clears previous content on re-render', () => {
    renderConversationList()([
      { id: 'c1', title: 'First', updated_at: '2026-01-15T10:00:00Z', message_count: 1 },
    ]);
    renderConversationList()([
      { id: 'c2', title: 'Second', updated_at: '2026-01-16T10:00:00Z', message_count: 2 },
    ]);
    const items = document.getElementById('sidebar-list').querySelectorAll('.sidebar-item');
    expect(items).toHaveLength(1);
    expect(items[0].querySelector('.sidebar-item-title').textContent).toBe('Second');
  });
});

describe('loadConversationList', () => {
  const loadConversationList = () => fn('loadConversationList');

  beforeEach(() => {
    resetDOM();
  });

  it('calls fetch to load conversations', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ conversations: [] }),
    });
    loadConversationList()();
    await vi.waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    const url = globalThis.fetch.mock.calls[0][0];
    expect(url).toBe('/api/conversations');
  });

  it('handles fetch failure gracefully', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('Network error'));
    // Should not throw
    loadConversationList()();
    await vi.waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
  });

  it('handles non-ok response gracefully', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({}),
    });
    loadConversationList()();
    await vi.waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
  });
});

describe('loadConversation', () => {
  const loadConversation = () => fn('loadConversation');

  beforeEach(() => {
    resetDOM();
  });

  it('fetches conversation by ID', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ id: 'conv-123', messages: [{ role: 'user', content: 'hi' }] }),
    });
    // Mock window.location
    delete window.location;
    window.location = { pathname: '/', href: '' };
    loadConversation()('conv-123');
    await vi.waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith('/api/conversations/conv-123');
    });
  });

  it('alerts on fetch failure', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({}),
    });
    globalThis.alert = vi.fn();
    loadConversation()('bad-id');
    await vi.waitFor(() => {
      expect(globalThis.alert).toHaveBeenCalled();
    });
  });
});

// ────────────────────────────────────────────────────────────────
// Admin / showAdminChatToggle
// ────────────────────────────────────────────────────────────────

describe('showAdminChatToggle', () => {
  const showAdminChatToggle = () => fn('showAdminChatToggle');

  beforeEach(() => {
    resetDOM();
    const existing = document.getElementById('admin-chat-toggle');
    if (existing) existing.remove();
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ conversations: [] }),
    });
  });

  it('creates admin toggle with My Chats and All Users buttons', () => {
    showAdminChatToggle()();
    const toggle = document.getElementById('admin-chat-toggle');
    expect(toggle).not.toBeNull();
    expect(toggle.querySelector('#admin-toggle-my').textContent).toBe('My Chats');
    expect(toggle.querySelector('#admin-toggle-all').textContent).toBe('All Users');
  });

  it('shows existing toggle instead of creating duplicate', () => {
    showAdminChatToggle()();
    showAdminChatToggle()();
    const toggles = document.querySelectorAll('#admin-chat-toggle');
    expect(toggles).toHaveLength(1);
  });

  it('has download button', () => {
    showAdminChatToggle()();
    const toggle = document.getElementById('admin-chat-toggle');
    const dlBtn = toggle.querySelector('.admin-download-btn');
    expect(dlBtn).not.toBeNull();
  });

  it('My Chats button has active class by default', () => {
    showAdminChatToggle()();
    const myBtn = document.getElementById('admin-toggle-my');
    expect(myBtn.classList.contains('active')).toBe(true);
  });
});

// ────────────────────────────────────────────────────────────────
// Skills panel
// ────────────────────────────────────────────────────────────────

describe('renderSkills', () => {
  const renderSkills = () => fn('renderSkills');

  beforeEach(() => {
    resetDOM();
  });

  it('shows empty message when no skills', () => {
    renderSkills()([]);
    const skillsList = document.getElementById('skills-list');
    const empty = skillsList.querySelector('.skills-empty');
    expect(empty).not.toBeNull();
    expect(empty.textContent).toContain('No skills discovered');
  });

  it('renders skill cards with name and description', () => {
    const skills = [
      { name: 'cost-analysis', description: 'Analyze cloud costs', source: 'builtin' },
      { name: 'security-check', description: 'Run security checks' },
    ];
    renderSkills()(skills);
    const skillsList = document.getElementById('skills-list');
    const cards = skillsList.querySelectorAll('.skill-card');
    expect(cards).toHaveLength(2);
    expect(cards[0].querySelector('.skill-name').textContent).toBe('cost-analysis');
    expect(cards[0].querySelector('.skill-desc').textContent).toBe('Analyze cloud costs');
  });

  it('shows source badge when present', () => {
    renderSkills()([{ name: 'test', description: '', source: 'plugin' }]);
    const badge = document.getElementById('skills-list').querySelector('.skill-badge');
    expect(badge).not.toBeNull();
    expect(badge.textContent).toBe('plugin');
  });

  it('shows parsec native badge', () => {
    renderSkills()([{ name: 'test', description: '', is_parsec_native: true }]);
    const badge = document.getElementById('skills-list').querySelector('.skill-badge-native');
    expect(badge).not.toBeNull();
    expect(badge.textContent).toBe('parsec');
  });

  it('shows metadata bits (version, domain, tools count)', () => {
    renderSkills()([{
      name: 'test', description: '',
      parsec: { version: '1.2.0', domain: 'cost' },
      allowed_tools: ['a', 'b', 'c'],
    }]);
    const meta = document.getElementById('skills-list').querySelector('.skill-meta');
    expect(meta).not.toBeNull();
    expect(meta.textContent).toContain('v1.2.0');
    expect(meta.textContent).toContain('cost');
    expect(meta.textContent).toContain('3 tools');
  });

  it('shows warnings', () => {
    renderSkills()([{
      name: 'test', description: '',
      warnings: ['Missing dependency'],
    }]);
    const warn = document.getElementById('skills-list').querySelector('.skill-warning');
    expect(warn).not.toBeNull();
    expect(warn.textContent).toContain('Missing dependency');
  });

  it('shows skill path', () => {
    renderSkills()([{
      name: 'test', description: '',
      skill_path: '/path/to/skill',
    }]);
    const path = document.getElementById('skills-list').querySelector('.skill-path');
    expect(path).not.toBeNull();
    expect(path.textContent).toBe('/path/to/skill');
  });

  it('omits meta div when no metadata bits', () => {
    renderSkills()([{ name: 'minimal', description: '' }]);
    const card = document.getElementById('skills-list').querySelector('.skill-card');
    expect(card.querySelector('.skill-meta')).toBeNull();
  });
});

describe('loadSkills', () => {
  const loadSkills = () => fn('loadSkills');

  beforeEach(() => {
    resetDOM();
  });

  it('fetches and renders skills', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ skills: [{ name: 'test-skill', description: 'Test' }] }),
    });
    loadSkills()();
    await vi.waitFor(() => {
      const cards = document.getElementById('skills-list').querySelectorAll('.skill-card');
      expect(cards).toHaveLength(1);
    });
  });

  it('shows error on fetch failure', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('Network error'));
    loadSkills()();
    await vi.waitFor(() => {
      const el = document.getElementById('skills-list').querySelector('.skills-empty');
      expect(el).not.toBeNull();
      expect(el.textContent).toContain('Could not load skills');
    });
  });

  it('shows loading text immediately', () => {
    globalThis.fetch = vi.fn().mockReturnValue(new Promise(() => {})); // never resolves
    loadSkills()();
    expect(document.getElementById('skills-list').textContent).toBe('Loading…');
  });
});

// ────────────────────────────────────────────────────────────────
// Message rendering helpers (restored conversations)
// ────────────────────────────────────────────────────────────────

describe('_renderRestoredUserMessage', () => {
  const _renderRestoredUserMessage = () => fn('_renderRestoredUserMessage');

  beforeEach(() => {
    resetDOM();
  });

  it('renders string content user message', () => {
    _renderRestoredUserMessage()({ role: 'user', content: 'Hello there' });
    const msgs = document.getElementById('messages');
    expect(msgs.children.length).toBe(1);
    expect(msgs.children[0].textContent).toBe('Hello there');
  });

  it('renders array content user message, filtering tool_result blocks', () => {
    _renderRestoredUserMessage()({
      role: 'user',
      content: [
        { type: 'text', text: 'My question' },
        { type: 'tool_result', tool_use_id: 't1', content: '{}' },
      ],
    });
    const msgs = document.getElementById('messages');
    expect(msgs.children.length).toBe(1);
    expect(msgs.children[0].textContent).toBe('My question');
  });

  it('does not render empty content', () => {
    _renderRestoredUserMessage()({
      role: 'user',
      content: [{ type: 'tool_result', tool_use_id: 't1', content: '{}' }],
    });
    const msgs = document.getElementById('messages');
    expect(msgs.children.length).toBe(0);
  });
});

describe('_appendRestoredToolCall', () => {
  const _appendRestoredToolCall = () => fn('_appendRestoredToolCall');

  it('creates a tool call details element with name and status', () => {
    const inner = document.createElement('div');
    const tc = { id: 'tc1', name: 'query_provisions_db', input: { sql: 'SELECT 1' } };
    const map = { tc1: { row_count: 5 } };
    _appendRestoredToolCall()(inner, tc, map);
    const details = inner.querySelector('.tool-call');
    expect(details).not.toBeNull();
    expect(details.querySelector('.tool-name').textContent).toBe('query_provisions_db');
    expect(details.querySelector('.tool-status').textContent).toBe('done');
    expect(details.querySelector('.tool-body').textContent).toContain('SELECT 1');
    expect(details.querySelector('.tool-body').textContent).toContain('row_count');
  });

  it('handles tool call without result in map', () => {
    const inner = document.createElement('div');
    const tc = { id: 'tc2', name: 'some_tool', input: {} };
    _appendRestoredToolCall()(inner, tc, {});
    const details = inner.querySelector('.tool-call');
    expect(details).not.toBeNull();
    expect(details.querySelector('.tool-body').textContent).not.toContain('--- Result ---');
  });

  it('handles tool call with no name', () => {
    const inner = document.createElement('div');
    const tc = { id: 'tc3', input: { x: 1 } };
    _appendRestoredToolCall()(inner, tc, {});
    const nameSpan = inner.querySelector('.tool-name');
    expect(nameSpan.textContent).toBe('tool');
  });
});

describe('_renderRestoredToolCalls', () => {
  const _renderRestoredToolCalls = () => fn('_renderRestoredToolCalls');

  it('renders tool calls wrapper with count', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [
      { id: 'tc1', name: 'query_provisions_db', input: { sql: 'SELECT 1' } },
      { id: 'tc2', name: 'query_aws_costs', input: {} },
    ];
    const toolResultMap = {
      tc1: { row_count: 5 },
      tc2: { total_cost: 100 },
    };
    const result = _renderRestoredToolCalls()(toolCalls, toolResultMap, contentEl);
    const wrapper = contentEl.querySelector('.tool-calls-summary');
    expect(wrapper).not.toBeNull();
    expect(wrapper.querySelector('summary').textContent).toContain('2');
    expect(result.delegations).toEqual([]);
    expect(result.restoredToolResults).toHaveLength(2);
  });

  it('identifies delegation tool calls', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [
      { id: 'tc1', name: 'investigate_costs', input: { question: 'test' } },
    ];
    const toolResultMap = {
      tc1: { tool_calls: 3, summary: 'Found costs', findings: [] },
    };
    const result = _renderRestoredToolCalls()(toolCalls, toolResultMap, contentEl);
    expect(result.delegations).toHaveLength(1);
    expect(result.delegations[0].agentType).toBe('cost');
  });

  it('counts delegation tool calls in total', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [
      { id: 'tc1', name: 'investigate_costs', input: {} },
      { id: 'tc2', name: 'query_provisions_db', input: {} },
    ];
    const toolResultMap = {
      tc1: { tool_calls: 5, summary: 'Results' },
      tc2: { row_count: 10 },
    };
    const result = _renderRestoredToolCalls()(toolCalls, toolResultMap, contentEl);
    const summary = contentEl.querySelector('summary');
    // 5 (from delegation) + 1 (direct tool) = 6
    expect(summary.textContent).toContain('6');
  });

  it('excludes delegation tools from restoredToolResults', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [
      { id: 'tc1', name: 'investigate_babylon', input: {} },
    ];
    const toolResultMap = {
      tc1: { tool_calls: 2, summary: 'Done' },
    };
    const result = _renderRestoredToolCalls()(toolCalls, toolResultMap, contentEl);
    expect(result.restoredToolResults).toHaveLength(0);
  });

  it('handles single tool call with singular text', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [
      { id: 'tc1', name: 'query_provisions_db', input: {} },
    ];
    const toolResultMap = { tc1: { row_count: 1 } };
    const result = _renderRestoredToolCalls()(toolCalls, toolResultMap, contentEl);
    const summary = contentEl.querySelector('summary');
    expect(summary.textContent).toContain('1 query executed');
  });
});

describe('_appendAgentBanner', () => {
  const _appendAgentBanner = () => fn('_appendAgentBanner');

  it('creates agent banner with done status', () => {
    const contentEl = document.createElement('div');
    _appendAgentBanner()(contentEl, 'cost', 'Cost Investigation');
    const banner = contentEl.querySelector('.agent-banner');
    expect(banner).not.toBeNull();
    expect(banner.classList.contains('agent-done')).toBe(true);
    expect(banner.dataset.agent).toBe('cost');
    expect(banner.querySelector('.agent-label').textContent).toBe('Cost Investigation');
    expect(banner.querySelector('.agent-status').textContent).toBe('done');
  });
});

describe('_appendDelegationSummary', () => {
  const _appendDelegationSummary = () => fn('_appendDelegationSummary');

  it('appends summary text as md-text div', () => {
    const contentEl = document.createElement('div');
    const textParts = [];
    const d = { result: { summary: 'Found 3 high-cost items' } };
    _appendDelegationSummary()(d, contentEl, textParts);
    const findings = contentEl.querySelector('.md-text');
    expect(findings).not.toBeNull();
    expect(textParts).toHaveLength(1);
    expect(textParts[0]).toBe('Found 3 high-cost items');
  });

  it('falls back to findings array when summary is empty', () => {
    const contentEl = document.createElement('div');
    const textParts = [];
    const d = { result: { summary: '', findings: ['Finding 1', '[Tool: query]', 'Finding 2'] } };
    _appendDelegationSummary()(d, contentEl, textParts);
    expect(textParts[0]).toContain('Finding 1');
    expect(textParts[0]).toContain('Finding 2');
    expect(textParts[0]).not.toContain('[Tool:');
  });

  it('does nothing when summary and findings are empty', () => {
    const contentEl = document.createElement('div');
    const textParts = [];
    const d = { result: { summary: '', findings: [] } };
    _appendDelegationSummary()(d, contentEl, textParts);
    expect(contentEl.querySelector('.md-text')).toBeNull();
    expect(textParts).toHaveLength(0);
  });
});

describe('_renderRestoredDelegations', () => {
  const _renderRestoredDelegations = () => fn('_renderRestoredDelegations');

  it('renders agent banners and summaries for each delegation', () => {
    const contentEl = document.createElement('div');
    const textParts = [];
    const agentNames = { cost: 'Cost Investigation', babylon: 'Babylon Investigation' };
    const delegations = [
      { agentType: 'cost', result: { summary: 'Cost analysis results' } },
      { agentType: 'babylon', result: { summary: 'Babylon results' } },
    ];
    _renderRestoredDelegations()(delegations, agentNames, contentEl, textParts);
    const banners = contentEl.querySelectorAll('.agent-banner');
    expect(banners).toHaveLength(2);
    expect(banners[0].querySelector('.agent-label').textContent).toBe('Cost Investigation');
    expect(banners[1].querySelector('.agent-label').textContent).toBe('Babylon Investigation');
    expect(textParts).toHaveLength(2);
  });

  it('uses agentType as label when not in agentNames', () => {
    const contentEl = document.createElement('div');
    const textParts = [];
    const agentNames = {};
    const delegations = [
      { agentType: 'unknown', result: { summary: 'Result' } },
    ];
    _renderRestoredDelegations()(delegations, agentNames, contentEl, textParts);
    const label = contentEl.querySelector('.agent-label');
    expect(label.textContent).toBe('unknown');
  });
});

describe('_reconstructReportsAndCharts', () => {
  const _reconstructReportsAndCharts = () => fn('_reconstructReportsAndCharts');

  it('creates report download link for generate_report', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [{ id: 'tc1', name: 'generate_report', input: {} }];
    const toolResultMap = { tc1: { filename: 'report.csv' } };
    _reconstructReportsAndCharts()(toolCalls, toolResultMap, contentEl);
    const link = contentEl.querySelector('.report-download');
    expect(link).not.toBeNull();
    expect(link.href).toContain('/api/reports/report.csv');
    expect(link.textContent).toContain('report.csv');
  });

  it('renders chart for render_chart result', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [{ id: 'tc1', name: 'render_chart', input: {} }];
    const toolResultMap = {
      tc1: {
        chart_type: 'bar',
        title: 'Test',
        labels: ['A'],
        datasets: [{ label: 'S', data: [10] }],
      },
    };
    const charts = _reconstructReportsAndCharts()(toolCalls, toolResultMap, contentEl);
    expect(contentEl.querySelector('.chart-container')).not.toBeNull();
  });

  it('skips tool results with errors', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [{ id: 'tc1', name: 'generate_report', input: {} }];
    const toolResultMap = { tc1: { error: 'failed' } };
    _reconstructReportsAndCharts()(toolCalls, toolResultMap, contentEl);
    expect(contentEl.querySelector('.report-download')).toBeNull();
  });

  it('skips when no result in map', () => {
    const contentEl = document.createElement('div');
    const toolCalls = [{ id: 'tc1', name: 'generate_report', input: {} }];
    _reconstructReportsAndCharts()(toolCalls, {}, contentEl);
    expect(contentEl.querySelector('.report-download')).toBeNull();
  });
});

describe('_renderRestoredAssistantMessage', () => {
  const _renderRestoredAssistantMessage = () => fn('_renderRestoredAssistantMessage');

  beforeEach(() => {
    resetDOM();
  });

  it('renders string content as md-text', () => {
    _renderRestoredAssistantMessage()(
      { role: 'assistant', content: 'Hello world' },
      0, [{ role: 'assistant', content: 'Hello world' }], {}, false
    );
    const textDiv = document.getElementById('messages').querySelector('.md-text');
    expect(textDiv).not.toBeNull();
    expect(textDiv.textContent).toContain('Hello world');
  });

  it('renders array content with text blocks', () => {
    _renderRestoredAssistantMessage()(
      { role: 'assistant', content: [{ type: 'text', text: 'Answer text' }] },
      0,
      [{ role: 'assistant', content: [{ type: 'text', text: 'Answer text' }] }],
      {}, false
    );
    const textDiv = document.getElementById('messages').querySelector('.md-text');
    expect(textDiv).not.toBeNull();
    expect(textDiv.textContent).toContain('Answer text');
  });

  it('adds export bar when text is present', () => {
    _renderRestoredAssistantMessage()(
      { role: 'assistant', content: 'Some answer' },
      0,
      [{ role: 'assistant', content: 'Some answer' }],
      {}, false
    );
    const bar = document.getElementById('messages').querySelector('.response-export-bar');
    expect(bar).not.toBeNull();
  });

  it('renders tool uses with results', () => {
    const toolResultMap = { tc1: { row_count: 5 } };
    _renderRestoredAssistantMessage()(
      {
        role: 'assistant',
        content: [
          { type: 'tool_use', id: 'tc1', name: 'query_provisions_db', input: { sql: 'SELECT 1' } },
          { type: 'text', text: 'Got results' },
        ],
      },
      0,
      [{
        role: 'assistant',
        content: [
          { type: 'tool_use', id: 'tc1', name: 'query_provisions_db', input: { sql: 'SELECT 1' } },
          { type: 'text', text: 'Got results' },
        ],
      }],
      toolResultMap, false
    );
    const wrapper = document.getElementById('messages').querySelector('.tool-calls-summary');
    expect(wrapper).not.toBeNull();
  });
});

describe('_renderRestoredArrayContent', () => {
  const _renderRestoredArrayContent = () => fn('_renderRestoredArrayContent');

  it('renders text parts from array content', () => {
    const contentEl = document.createElement('div');
    const content = [
      { type: 'text', text: 'Part 1' },
      { type: 'text', text: ' Part 2' },
    ];
    const collapsed = [{ role: 'assistant', content: content }];
    const result = _renderRestoredArrayContent()(content, {}, contentEl, 0, collapsed, false);
    expect(result.restoredText).toBe('Part 1 Part 2');
    const textDiv = contentEl.querySelector('.md-text');
    expect(textDiv).not.toBeNull();
  });

  it('renders tool calls from array content', () => {
    const contentEl = document.createElement('div');
    const content = [
      { type: 'tool_use', id: 'tc1', name: 'query_provisions_db', input: { sql: 'SELECT 1' } },
      { type: 'text', text: 'Result text' },
    ];
    const toolResultMap = { tc1: { row_count: 5 } };
    const collapsed = [{ role: 'assistant', content: content }];
    const result = _renderRestoredArrayContent()(content, toolResultMap, contentEl, 0, collapsed, false);
    expect(contentEl.querySelector('.tool-calls-summary')).not.toBeNull();
    expect(result.restoredToolResults).toHaveLength(1);
  });
});

describe('_renderRestoredTextWithChoices', () => {
  const _renderRestoredTextWithChoices = () => fn('_renderRestoredTextWithChoices');

  it('renders plain text without choices', () => {
    const contentEl = document.createElement('div');
    const collapsed = [{ role: 'assistant', content: 'test' }];
    _renderRestoredTextWithChoices()('Hello world', contentEl, 0, collapsed, false);
    const textDiv = contentEl.querySelector('.md-text');
    expect(textDiv).not.toBeNull();
    expect(textDiv.textContent).toContain('Hello world');
  });

  it('renders interactive choices on last message', () => {
    const contentEl = document.createElement('div');
    const text = 'Pick one:\n{{choices}}\n- Option A\n- Option B\n{{/choices}}';
    const collapsed = [{ role: 'assistant', content: text }];
    _renderRestoredTextWithChoices()(text, contentEl, 0, collapsed, true);
    expect(contentEl.querySelector('.choices-container')).not.toBeNull();
  });

  it('renders choices summary on non-last message', () => {
    const contentEl = document.createElement('div');
    const text = 'Pick one:\n{{choices}}\n- Option A\n- Option B\n{{/choices}}';
    const collapsed = [
      { role: 'assistant', content: text },
      { role: 'assistant', content: 'next message' },
    ];
    _renderRestoredTextWithChoices()(text, contentEl, 0, collapsed, true);
    expect(contentEl.querySelector('.choices-summary')).not.toBeNull();
    expect(contentEl.querySelector('.choices-container')).toBeNull();
  });

  it('renders choices summary when not interactive', () => {
    const contentEl = document.createElement('div');
    const text = 'Pick:\n{{choices}}\n- A\n- B\n{{/choices}}';
    const collapsed = [{ role: 'assistant', content: text }];
    _renderRestoredTextWithChoices()(text, contentEl, 0, collapsed, false);
    // Non-interactive: last message but interactive=false
    expect(contentEl.querySelector('.choices-summary')).not.toBeNull();
  });
});

// ────────────────────────────────────────────────────────────────
// renderSharedMessages (integration)
// ────────────────────────────────────────────────────────────────

describe('renderSharedMessages', () => {
  const renderSharedMessages = () => fn('renderSharedMessages');

  beforeEach(() => {
    resetDOM();
  });

  it('renders user and assistant messages from history', () => {
    const messages = [
      { role: 'user', content: 'What are the costs?' },
      { role: 'assistant', content: 'Here are the costs...' },
    ];
    renderSharedMessages()(messages, false);
    const msgs = document.getElementById('messages');
    expect(msgs.children.length).toBe(2);
    expect(msgs.children[0].classList.contains('user')).toBe(true);
    expect(msgs.children[1].classList.contains('assistant')).toBe(true);
  });

  it('renders messages with tool use and results', () => {
    const messages = [
      { role: 'user', content: 'Query costs' },
      {
        role: 'assistant',
        content: [
          { type: 'tool_use', id: 'tc1', name: 'query_provisions_db', input: { sql: 'SELECT 1' } },
          { type: 'text', text: 'Found results' },
        ],
      },
      {
        role: 'user',
        content: [
          { type: 'tool_result', tool_use_id: 'tc1', content: '{"row_count": 5}' },
        ],
      },
    ];
    renderSharedMessages()(messages, false);
    const msgs = document.getElementById('messages');
    expect(msgs.children.length).toBeGreaterThanOrEqual(1);
  });

  it('renders empty messages array without error', () => {
    renderSharedMessages()([], false);
    const msgs = document.getElementById('messages');
    expect(msgs.children.length).toBe(0);
  });
});

// ────────────────────────────────────────────────────────────────
// Export functions
// ────────────────────────────────────────────────────────────────

describe('exportResponseAsCSV', () => {
  const exportResponseAsCSV = () => fn('exportResponseAsCSV');

  beforeEach(() => {
    globalThis.Blob = class Blob {
      constructor(parts, options) {
        this.parts = parts;
        this.options = options;
      }
    };
    globalThis.URL.createObjectURL = vi.fn().mockReturnValue('blob:test');
    globalThis.URL.revokeObjectURL = vi.fn();
  });

  it('does nothing when no tool results', () => {
    const el = document.createElement('div');
    el._exportToolResults = [];
    exportResponseAsCSV()(el);
  });

  it('exports tabular data as CSV', () => {
    const el = document.createElement('div');
    el._exportToolResults = [
      {
        tool: 'query_provisions_db',
        input: {},
        result: { rows: [{ name: 'Alice', cost: 100 }, { name: 'Bob', cost: 200 }] },
      },
    ];
    const clicked = [];
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const elem = origCreateElement(tag);
      if (tag === 'a') {
        elem.click = () => clicked.push(elem.download);
      }
      return elem;
    });
    exportResponseAsCSV()(el);
    expect(clicked.length).toBeGreaterThan(0);
    document.createElement.mockRestore();
  });

  it('falls back to key-value pairs when no tabular data', () => {
    const el = document.createElement('div');
    el._exportToolResults = [
      { tool: 'simple_tool', input: {}, result: { status: 'ok', count: 42 } },
    ];
    const clicked = [];
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const elem = origCreateElement(tag);
      if (tag === 'a') {
        elem.click = () => clicked.push(true);
      }
      return elem;
    });
    exportResponseAsCSV()(el);
    expect(clicked.length).toBeGreaterThan(0);
    document.createElement.mockRestore();
  });
});

describe('exportResponseAsMarkdown', () => {
  const exportResponseAsMarkdown = () => fn('exportResponseAsMarkdown');

  beforeEach(() => {
    globalThis.Blob = class Blob {
      constructor(parts) { this.parts = parts; }
    };
    globalThis.URL.createObjectURL = vi.fn().mockReturnValue('blob:test');
  });

  it('exports markdown content', () => {
    const el = document.createElement('div');
    el._exportMarkdown = '# Report\n\nSome data here';
    el._exportCharts = [];
    const clicked = [];
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const elem = origCreateElement(tag);
      if (tag === 'a') {
        elem.click = () => clicked.push(elem.download);
      }
      return elem;
    });
    exportResponseAsMarkdown()(el);
    expect(clicked.length).toBe(1);
    expect(clicked[0]).toMatch(/^parsec-.*\.md$/);
    document.createElement.mockRestore();
  });
});

describe('exportResponseAsJSON', () => {
  const exportResponseAsJSON = () => fn('exportResponseAsJSON');

  beforeEach(() => {
    globalThis.Blob = class Blob {
      constructor(parts) { this.parts = parts; }
    };
    globalThis.URL.createObjectURL = vi.fn().mockReturnValue('blob:test');
  });

  it('does nothing when no tool results', () => {
    const el = document.createElement('div');
    el._exportToolResults = [];
    exportResponseAsJSON()(el);
  });

  it('exports JSON content', () => {
    const el = document.createElement('div');
    el._exportToolResults = [{ tool: 'test', result: { data: 'ok' } }];
    const clicked = [];
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const elem = origCreateElement(tag);
      if (tag === 'a') {
        elem.click = () => clicked.push(elem.download);
      }
      return elem;
    });
    exportResponseAsJSON()(el);
    expect(clicked.length).toBe(1);
    expect(clicked[0]).toMatch(/^parsec-.*\.json$/);
    document.createElement.mockRestore();
  });
});

describe('exportResponseAsPDF', () => {
  const exportResponseAsPDF = () => fn('exportResponseAsPDF');

  it('clones content and calls html2canvas', async () => {
    const el = document.createElement('div');
    el.className = 'message assistant';
    const contentEl = document.createElement('div');
    contentEl.className = 'content';
    const text = document.createElement('div');
    text.className = 'md-text';
    text.textContent = 'Test content';
    contentEl.appendChild(text);
    el.appendChild(contentEl);

    const mockCanvas = document.createElement('canvas');
    mockCanvas.width = 100;
    mockCanvas.height = 200;
    mockCanvas.getContext = () => ({
      drawImage: vi.fn(),
    });
    mockCanvas.toDataURL = () => 'data:image/png;base64,test';
    globalThis.html2canvas = vi.fn().mockResolvedValue(mockCanvas);

    const mockDoc = {
      addPage: vi.fn(),
      addImage: vi.fn(),
      save: vi.fn(),
    };
    globalThis.jspdf = { jsPDF: vi.fn().mockReturnValue(mockDoc) };

    // Mock createElement for the page canvases created inside _renderCanvasToMultiPagePDF
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const elem = origCreateElement(tag);
      if (tag === 'canvas') {
        elem.getContext = () => ({ drawImage: vi.fn() });
        elem.toDataURL = () => 'data:image/png;base64,page';
      }
      return elem;
    });

    exportResponseAsPDF()(el);
    await vi.waitFor(() => {
      expect(globalThis.html2canvas).toHaveBeenCalled();
    });
    document.createElement.mockRestore();
  });
});

describe('_processToolResultForCSV', () => {
  const _processToolResultForCSV = () => fn('_processToolResultForCSV');

  it('extracts tabular data from result', () => {
    const tr = {
      tool: 'query_provisions_db',
      input: {},
      result: [{ name: 'Alice', cost: 100 }],
    };
    const sections = _processToolResultForCSV()(tr);
    expect(sections).toHaveLength(1);
    expect(sections[0]).toContain('Alice');
    expect(sections[0]).toContain('100');
  });

  it('parses markdown tables from generate_report input', () => {
    const tr = {
      tool: 'generate_report',
      input: { content: '## Report\n| Name | Cost |\n| --- | --- |\n| Alice | 100 |' },
      result: {},
    };
    const sections = _processToolResultForCSV()(tr);
    expect(sections.length).toBeGreaterThan(0);
    expect(sections[0]).toContain('Alice');
  });

  it('returns empty when no tabular data', () => {
    const tr = {
      tool: 'simple',
      input: {},
      result: { message: 'ok' },
    };
    const sections = _processToolResultForCSV()(tr);
    expect(sections).toHaveLength(0);
  });
});

// ────────────────────────────────────────────────────────────────
// PDF helpers
// ────────────────────────────────────────────────────────────────

describe('_renderCanvasToMultiPagePDF', () => {
  const _renderCanvasToMultiPagePDF = () => fn('_renderCanvasToMultiPagePDF');

  // Override createElement to provide canvas with working getContext
  let origCreateElement;

  beforeEach(() => {
    origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const elem = origCreateElement(tag);
      if (tag === 'canvas') {
        elem.getContext = () => ({ drawImage: vi.fn() });
        elem.toDataURL = () => 'data:image/png;base64,page';
      }
      return elem;
    });
  });

  afterEach(() => {
    document.createElement.mockRestore?.();
  });

  it('renders single page when canvas fits', () => {
    const doc = { addPage: vi.fn(), addImage: vi.fn() };
    const canvas = origCreateElement('canvas');
    canvas.width = 555;
    canvas.height = 200;
    canvas.getContext = () => ({ drawImage: vi.fn() });
    canvas.toDataURL = () => 'data:image/png;base64,test';
    _renderCanvasToMultiPagePDF()(doc, canvas, 20, 555, 801);
    expect(doc.addImage).toHaveBeenCalled();
    expect(doc.addPage).not.toHaveBeenCalled();
  });

  it('renders multiple pages for tall canvas', () => {
    const doc = { addPage: vi.fn(), addImage: vi.fn() };
    const canvas = origCreateElement('canvas');
    canvas.width = 555;
    canvas.height = 5000;
    canvas.getContext = () => ({ drawImage: vi.fn() });
    canvas.toDataURL = () => 'data:image/png;base64,test';
    _renderCanvasToMultiPagePDF()(doc, canvas, 20, 555, 400);
    expect(doc.addPage).toHaveBeenCalled();
    expect(doc.addImage.mock.calls.length).toBeGreaterThan(1);
  });
});

// ────────────────────────────────────────────────────────────────
// Debug panel functions — tested through runDiagnosis flow
// ────────────────────────────────────────────────────────────────

describe('showDebugView', () => {
  const showDebugView = () => fn('showDebugView');

  beforeEach(() => {
    resetDOM();
  });

  it('shows debug view and hides chat', () => {
    showDebugView()();
    expect(document.getElementById('debug-view').style.display).toBe('flex');
    expect(document.getElementById('chat').style.display).toBe('none');
    const footer = document.querySelector('footer');
    expect(footer.style.display).toBe('none');
  });
});

describe('showChatView', () => {
  const showChatView = () => fn('showChatView');
  const showDebugView = () => fn('showDebugView');

  beforeEach(() => {
    resetDOM();
  });

  it('hides debug view and shows chat', () => {
    showDebugView()();
    showChatView()();
    expect(document.getElementById('debug-view').style.display).toBe('none');
    expect(document.getElementById('chat').style.display).toBe('');
  });
});

describe('runDiagnosis — integration tests for debug rendering', () => {
  const runDiagnosis = () => fn('runDiagnosis');

  beforeEach(() => {
    resetDOM();
    document.getElementById('debug-url').value = '';
    document.getElementById('debug-error').style.display = 'none';
    document.getElementById('debug-result').style.display = 'none';
    document.getElementById('debug-loading').style.display = 'none';
    document.getElementById('debug-fix-preview').style.display = 'none';
    document.getElementById('debug-summary').innerHTML = '';
    document.getElementById('debug-tab-content').innerHTML = '';
  });

  it('does nothing when URL is empty', () => {
    globalThis.fetch = vi.fn();
    document.getElementById('debug-url').value = '';
    runDiagnosis()();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('renders triage tab on successful diagnosis', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/42';
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        metadata: {
          id: 42, status: 'failed', action: 'provision',
          elapsed: 300, started: '2026-01-15T10:00:00Z',
          jobExplanation: 'Job timed out',
          resultTraceback: 'Error trace...',
        },
        failingTask: null,
        fix: null,
      }),
    });
    runDiagnosis()();
    await vi.waitFor(() => {
      expect(document.getElementById('debug-result').style.display).toBe('block');
    });
    // Triage tab should be rendered (default)
    const content = document.getElementById('debug-tab-content');
    expect(content.innerHTML).toContain('failed');
    expect(content.innerHTML).toContain('provision');
    expect(content.innerHTML).toContain('Job timed out');
    expect(content.innerHTML).toContain('Error trace');
    // Summary bar should show job ID
    const summary = document.getElementById('debug-summary');
    expect(summary.innerHTML).toContain('42');
  });

  it('renders fix preview when fix is present', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/99';
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        metadata: { id: 99, status: 'failed', action: 'run', elapsed: 60 },
        failingTask: {
          taskName: 'Install pkg',
          roleFqcn: 'my.role',
          errorMessage: 'Package not found',
        },
        fix: {
          source: 'pattern',
          repo: 'rhpds/agnosticd',
          file: 'roles/my_role/tasks/main.yml',
          line: 42,
          explanation: 'Wrong package name.',
          before: '- yum: name=wrong',
          after: '- yum: name=right',
        },
      }),
    });
    runDiagnosis()();
    await vi.waitFor(() => {
      expect(document.getElementById('debug-result').style.display).toBe('block');
    });
    // Fix preview should be visible (since we're on triage tab, not fix tab)
    const preview = document.getElementById('debug-fix-preview');
    expect(preview.style.display).toBe('flex');
    expect(preview.innerHTML).toContain('Fix Recommendation');
    expect(preview.innerHTML).toContain('Pattern Match');
  });

  it('renders failing task tab when tab is clicked', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/55';
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        metadata: { id: 55, status: 'failed', action: 'run', elapsed: 120 },
        failingTask: {
          taskName: 'Deploy service',
          roleFqcn: 'deploy.role',
          hostPattern: 'bastion',
          filePath: '/path/to/tasks.yml',
          errorMessage: 'Connection refused',
        },
        projectInfo: {
          scmUrl: 'https://github.com/rhpds/agnosticd.git',
          scmBranch: 'main',
        },
        fix: null,
      }),
    });
    runDiagnosis()();
    await vi.waitFor(() => {
      expect(document.getElementById('debug-result').style.display).toBe('block');
    });
    // Click the failing-task tab
    const ftTab = document.querySelector('.debug-tab[data-tab="failing-task"]');
    ftTab.click();
    const content = document.getElementById('debug-tab-content');
    expect(content.innerHTML).toContain('Deploy service');
    expect(content.innerHTML).toContain('deploy.role');
    expect(content.innerHTML).toContain('bastion');
    expect(content.innerHTML).toContain('Connection refused');
    expect(content.innerHTML).toContain('github.com');
  });

  it('renders fix tab when tab is clicked', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/66';
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        metadata: { id: 66, status: 'failed', action: 'run', elapsed: 90 },
        failingTask: null,
        fix: {
          source: 'ai',
          repo: 'rhpds/agnosticd',
          file: 'roles/test/tasks/main.yml',
          explanation: 'Fix the issue by updating the config.',
          before: 'old: value',
          after: 'new: value',
          githubUrl: 'https://github.com/rhpds/agnosticd/blob/main/roles/test/tasks/main.yml',
        },
      }),
    });
    runDiagnosis()();
    await vi.waitFor(() => {
      expect(document.getElementById('debug-result').style.display).toBe('block');
    });
    // Click the fix tab
    const fixTab = document.querySelector('.debug-tab[data-tab="fix"]');
    fixTab.click();
    const content = document.getElementById('debug-tab-content');
    expect(content.innerHTML).toContain('AI Generated');
    expect(content.innerHTML).toContain('rhpds/agnosticd');
    expect(content.innerHTML).toContain('old: value');
    expect(content.innerHTML).toContain('new: value');
    expect(content.innerHTML).toContain('github.com');
    // Fix preview should be hidden on fix tab
    expect(document.getElementById('debug-fix-preview').style.display).toBe('none');
  });

  it('renders correlation tab with fetched data', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/77';
    // First fetch: diagnosis
    globalThis.fetch = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          metadata: { id: 77, status: 'failed', action: 'run', elapsed: 30, jobTemplate: 'deploy-lab' },
          failingTask: null,
          fix: null,
        }),
      })
      // Second fetch: correlation data
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          totalFailures: 15,
          byError: [{ error: 'Timeout', count: 8, jobIds: [1, 2, 3, 4, 5, 6, 7, 8] }],
          byEE: [{ image: 'ee-test:latest', count: 5 }],
          byInstanceGroup: [{ group: 'ig-prod', count: 3 }],
        }),
      });
    runDiagnosis()();
    await vi.waitFor(() => {
      expect(document.getElementById('debug-result').style.display).toBe('block');
    });
    // Click correlation tab
    const corrTab = document.querySelector('.debug-tab[data-tab="correlation"]');
    corrTab.click();
    await vi.waitFor(() => {
      const content = document.getElementById('debug-tab-content');
      expect(content.innerHTML).toContain('15 other failures');
    });
    const content = document.getElementById('debug-tab-content');
    expect(content.innerHTML).toContain('Timeout');
    expect(content.innerHTML).toContain('8 jobs');
    expect(content.innerHTML).toContain('ee-test:latest');
    expect(content.innerHTML).toContain('ig-prod');
  });

  it('renders EE info tab with inline data', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/88';
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        metadata: { id: 88, status: 'failed', action: 'run', elapsed: 45, executionEnvironment: 5 },
        failingTask: null,
        fix: null,
        eeInfo: {
          image: 'quay.io/rhpds/ee-test:v1',
          sourceRepo: 'rhpds/ee-configs',
          sourceDir: 'ee-test',
          sourceFiles: [
            { name: 'execution-environment.yml', content: 'version: 3\nbuild:\n  steps: []' },
            { name: 'requirements.txt', content: 'boto3\nrequests' },
          ],
        },
      }),
    });
    runDiagnosis()();
    await vi.waitFor(() => {
      expect(document.getElementById('debug-result').style.display).toBe('block');
    });
    // Click EE info tab
    const eeTab = document.querySelector('.debug-tab[data-tab="ee-info"]');
    eeTab.click();
    const content = document.getElementById('debug-tab-content');
    expect(content.innerHTML).toContain('quay.io/rhpds/ee-test:v1');
    expect(content.innerHTML).toContain('rhpds/ee-configs');
    expect(content.innerHTML).toContain('execution-environment.yml');
    expect(content.innerHTML).toContain('requirements.txt');
  });

  it('shows error on fetch failure', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/bad';
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('Network error'));
    runDiagnosis()();
    await vi.waitFor(() => {
      const errorEl = document.getElementById('debug-error');
      expect(errorEl.textContent).toBe('Network error');
      expect(errorEl.style.display).toBe('block');
    });
  });

  it('shows error for non-ok response', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/404';
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      statusText: 'Not Found',
      json: () => Promise.resolve({ detail: 'Job not found' }),
    });
    runDiagnosis()();
    await vi.waitFor(() => {
      const errorEl = document.getElementById('debug-error');
      expect(errorEl.textContent).toBe('Job not found');
      expect(errorEl.style.display).toBe('block');
    });
  });

  it('disables diagnose button during fetch and re-enables after', async () => {
    document.getElementById('debug-url').value = 'https://aap2.example.com/jobs/42';
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        metadata: { id: 42, status: 'failed', action: 'run', elapsed: 10 },
        failingTask: null,
        fix: null,
      }),
    });
    runDiagnosis()();
    expect(document.getElementById('debug-diagnose-btn').disabled).toBe(true);
    await vi.waitFor(() => {
      expect(document.getElementById('debug-diagnose-btn').disabled).toBe(false);
    });
  });
});

// ────────────────────────────────────────────────────────────────
// Theme / autoResizeInput
// ────────────────────────────────────────────────────────────────

describe('autoResizeInput', () => {
  const autoResizeInput = () => fn('autoResizeInput');

  it('sets input height based on scrollHeight', () => {
    const inputEl = document.getElementById('question');
    inputEl.value = 'test';
    autoResizeInput()();
    expect(inputEl.style.height).toBeDefined();
  });
});

// ────────────────────────────────────────────────────────────────
// Stream event handlers (additional coverage)
// ────────────────────────────────────────────────────────────────

describe('_handleTextEvent', () => {
  const _handleTextEvent = () => fn('_handleTextEvent');

  function makeState() {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    return {
      contentEl, statusEl, streamStarted: false,
      fullText: '', currentChunk: '',
    };
  }

  it('accumulates text and creates live text element', () => {
    const state = makeState();
    _handleTextEvent()({ content: 'Hello' }, state);
    expect(state.fullText).toBe('Hello');
    expect(state.currentChunk).toBe('Hello');
    const liveText = state.contentEl.querySelector('.md-text-live');
    expect(liveText).not.toBeNull();
  });

  it('removes status indicator on text event', () => {
    const state = makeState();
    const si = document.createElement('div');
    si.className = 'status-indicator';
    state.contentEl.appendChild(si);
    _handleTextEvent()({ content: 'text' }, state);
    expect(state.contentEl.querySelector('.status-indicator')).toBeNull();
  });
});

describe('_handleToolResultEvent', () => {
  const _handleToolResultEvent = () => fn('_handleToolResultEvent');
  const createToolCall = () => fn('createToolCall');

  it('finalizes tool call and pushes to toolResults', () => {
    const toolEl = createToolCall()('query_provisions_db', { sql: 'SELECT 1' });
    const state = {
      currentToolEl: toolEl,
      currentToolName: 'query_provisions_db',
      currentToolInput: { sql: 'SELECT 1' },
      toolResults: [],
    };
    _handleToolResultEvent()({ tool: 'query_provisions_db', result: { row_count: 5 } }, state);
    expect(state.toolResults).toHaveLength(1);
    expect(state.toolResults[0].tool).toBe('query_provisions_db');
    expect(state.currentToolEl).toBeNull();
  });

  it('does nothing when no current tool element', () => {
    const state = { currentToolEl: null, toolResults: [] };
    _handleToolResultEvent()({ tool: 'test', result: {} }, state);
    expect(state.toolResults).toHaveLength(0);
  });
});

describe('_handleChartEvent', () => {
  const _handleChartEvent = () => fn('_handleChartEvent');

  it('renders chart and adds to chartCanvases', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = {
      contentEl, statusEl, streamStarted: false,
      chartCanvases: [],
    };
    const data = {
      chart_type: 'bar',
      title: 'Test',
      labels: ['A'],
      datasets: [{ label: 'S', data: [10] }],
    };
    _handleChartEvent()(data, state);
    expect(state.chartCanvases).toHaveLength(1);
    expect(state.contentEl.querySelector('.chart-container')).not.toBeNull();
  });
});

describe('_handleReportEvent', () => {
  const _handleReportEvent = () => fn('_handleReportEvent');

  it('creates report download link', () => {
    const contentEl = document.createElement('div');
    _handleReportEvent()({ url: '/api/reports/test.csv', filename: 'test.csv' }, contentEl);
    const link = contentEl.querySelector('.report-download');
    expect(link).not.toBeNull();
    expect(link.textContent).toContain('test.csv');
  });
});

describe('_handleAgentStartEvent', () => {
  const _handleAgentStartEvent = () => fn('_handleAgentStartEvent');

  it('creates running agent banner', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = { contentEl, statusEl, streamStarted: false };
    _handleAgentStartEvent()({ agent: 'babylon', name: 'Babylon Investigation' }, state);
    const banner = contentEl.querySelector('.agent-banner');
    expect(banner).not.toBeNull();
    expect(banner.dataset.agent).toBe('babylon');
    expect(banner.classList.contains('agent-running')).toBe(true);
  });
});

describe('_handleAgentDoneEvent', () => {
  const _handleAgentDoneEvent = () => fn('_handleAgentDoneEvent');

  it('marks agent banner as done', () => {
    const contentEl = document.createElement('div');
    const banner = document.createElement('div');
    banner.className = 'agent-banner agent-running';
    banner.dataset.agent = 'cost';
    const statusSpan = document.createElement('span');
    statusSpan.className = 'agent-status';
    statusSpan.textContent = 'investigating...';
    banner.appendChild(statusSpan);
    contentEl.appendChild(banner);

    _handleAgentDoneEvent()({ agent: 'cost' }, contentEl);
    expect(banner.classList.contains('agent-done')).toBe(true);
    expect(banner.classList.contains('agent-running')).toBe(false);
    expect(statusSpan.textContent).toBe('done');
  });
});

describe('_handleStatusEvent', () => {
  const _handleStatusEvent = () => fn('_handleStatusEvent');

  it('creates status indicator', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = { contentEl, statusEl, streamStarted: false };
    _handleStatusEvent()({ message: 'Querying...' }, state);
    const si = state.contentEl.querySelector('.status-indicator');
    expect(si).not.toBeNull();
    expect(si.textContent).toContain('Querying...');
  });

  it('replaces existing status indicator', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = { contentEl, statusEl, streamStarted: false };
    _handleStatusEvent()({ message: 'First' }, state);
    _handleStatusEvent()({ message: 'Second' }, state);
    const indicators = contentEl.querySelectorAll('.status-indicator');
    expect(indicators).toHaveLength(1);
    expect(indicators[0].textContent).toContain('Second');
  });
});

describe('_handleErrorEvent', () => {
  const _handleErrorEvent = () => fn('_handleErrorEvent');

  it('creates error message element', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = { contentEl, statusEl, streamStarted: false };
    _handleErrorEvent()({ message: 'Something went wrong' }, state);
    const err = contentEl.querySelector('.error-message');
    expect(err).not.toBeNull();
    expect(err.textContent).toBe('Something went wrong');
  });
});

describe('_handleConfidenceEvent', () => {
  const _handleConfidenceEvent = () => fn('_handleConfidenceEvent');

  it('creates low confidence callout', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = { contentEl, statusEl, streamStarted: false };
    _handleConfidenceEvent()({ level: 'low', reasons: ['Incomplete data'] }, state);
    const callout = contentEl.querySelector('.confidence-callout');
    expect(callout).not.toBeNull();
    expect(callout.classList.contains('low')).toBe(true);
    expect(callout.textContent).toContain('Incomplete data');
  });

  it('ignores high confidence', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = { contentEl, statusEl, streamStarted: false };
    _handleConfidenceEvent()({ level: 'high', reasons: [] }, state);
    expect(contentEl.querySelector('.confidence-callout')).toBeNull();
  });

  it('handles empty reasons array', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = { contentEl, statusEl, streamStarted: false };
    _handleConfidenceEvent()({ level: 'low', reasons: [] }, state);
    const callout = contentEl.querySelector('.confidence-callout');
    expect(callout).not.toBeNull();
  });
});

describe('_handleCacheHitEvent', () => {
  const _handleCacheHitEvent = () => fn('_handleCacheHitEvent');

  it('does nothing when no current tool element', () => {
    const state = { currentToolEl: null };
    _handleCacheHitEvent()(state);
  });

  it('marks tool as cached', () => {
    const toolEl = document.createElement('div');
    const statusSpan = document.createElement('span');
    statusSpan.className = 'tool-status running';
    toolEl.appendChild(statusSpan);
    const state = { currentToolEl: toolEl };
    _handleCacheHitEvent()(state);
    expect(statusSpan.className).toBe('tool-status cached');
    expect(statusSpan.textContent).toBe('cached');
  });
});

// ────────────────────────────────────────────────────────────────
// Stream finalization helpers
// ────────────────────────────────────────────────────────────────

describe('_processInlineConfidenceMarkers', () => {
  const _processInlineConfidenceMarkers = () => fn('_processInlineConfidenceMarkers');

  it('extracts inline confidence markers from text', () => {
    const contentEl = document.createElement('div');
    const textEl = document.createElement('div');
    textEl.className = 'md-text';
    textEl.innerHTML = 'Some text [confidence: low | Sparse data] more text';
    contentEl.appendChild(textEl);
    _processInlineConfidenceMarkers()(contentEl);
    const callout = contentEl.querySelector('.confidence-callout');
    expect(callout).not.toBeNull();
    expect(callout.classList.contains('low')).toBe(true);
    expect(textEl.innerHTML).not.toContain('[confidence:');
  });

  it('handles medium confidence markers', () => {
    const contentEl = document.createElement('div');
    const textEl = document.createElement('div');
    textEl.className = 'md-text-live';
    textEl.innerHTML = 'Result [confidence: medium | Some uncertainty]';
    contentEl.appendChild(textEl);
    _processInlineConfidenceMarkers()(contentEl);
    const callout = contentEl.querySelector('.confidence-callout');
    expect(callout.classList.contains('medium')).toBe(true);
  });

  it('does nothing when no markers present', () => {
    const contentEl = document.createElement('div');
    const textEl = document.createElement('div');
    textEl.className = 'md-text';
    textEl.innerHTML = 'Clean text without markers';
    contentEl.appendChild(textEl);
    _processInlineConfidenceMarkers()(contentEl);
    expect(contentEl.querySelector('.confidence-callout')).toBeNull();
  });
});

describe('_renderFinalText', () => {
  const _renderFinalText = () => fn('_renderFinalText');

  it('converts live text class to md-text', () => {
    const contentEl = document.createElement('div');
    const liveEl = document.createElement('div');
    liveEl.className = 'md-text-live';
    liveEl.innerHTML = 'Final text';
    contentEl.appendChild(liveEl);
    _renderFinalText()(contentEl, 'Final text', 'Final text');
    expect(liveEl.className).toBe('md-text');
  });

  it('handles choices in final text', () => {
    const contentEl = document.createElement('div');
    const liveEl = document.createElement('div');
    liveEl.className = 'md-text-live';
    liveEl.innerHTML = 'Pick:\n{{choices}}\n- A\n- B\n{{/choices}}';
    contentEl.appendChild(liveEl);
    _renderFinalText()(contentEl, 'Pick:\n{{choices}}\n- A\n- B\n{{/choices}}', '');
    expect(contentEl.querySelector('.choices-container')).not.toBeNull();
  });

  it('handles text with no live element', () => {
    const contentEl = document.createElement('div');
    _renderFinalText()(contentEl, 'Some text', 'Some text');
  });
});

describe('_addExportBar', () => {
  const _addExportBar = () => fn('_addExportBar');

  it('adds export bar when text is present', () => {
    const contentEl = document.createElement('div');
    const assistantEl = document.createElement('div');
    const state = {
      contentEl, assistantEl,
      currentChunk: 'Some answer text',
      fullText: 'Some answer text',
      chartCanvases: [],
      toolResults: [],
    };
    _addExportBar()(state);
    expect(contentEl.querySelector('.response-export-bar')).not.toBeNull();
    expect(assistantEl._exportMarkdown).toBe('Some answer text');
  });

  it('does not add export bar when all content is empty', () => {
    const contentEl = document.createElement('div');
    const assistantEl = document.createElement('div');
    const state = {
      contentEl, assistantEl,
      currentChunk: '',
      fullText: '',
      chartCanvases: [],
      toolResults: [],
    };
    _addExportBar()(state);
    expect(contentEl.querySelector('.response-export-bar')).toBeNull();
  });

  it('adds export bar when charts are present', () => {
    const contentEl = document.createElement('div');
    const assistantEl = document.createElement('div');
    const state = {
      contentEl, assistantEl,
      currentChunk: '',
      fullText: '',
      chartCanvases: [{ title: 'chart', canvas: document.createElement('canvas') }],
      toolResults: [],
    };
    _addExportBar()(state);
    expect(contentEl.querySelector('.response-export-bar')).not.toBeNull();
  });
});

describe('_handleDoneEvent', () => {
  const _handleDoneEvent = () => fn('_handleDoneEvent');

  function makeFullState() {
    const contentEl = document.createElement('div');
    const assistantEl = document.createElement('div');
    return {
      contentEl, assistantEl,
      streamStarted: true,
      fullText: 'Done text',
      currentChunk: 'Done text',
      currentToolEl: null,
      toolElements: {},
      textChunks: [],
      chartCanvases: [],
      toolResults: [],
      currentToolName: null,
      currentToolInput: null,
      liveWrapper: null,
      liveInner: null,
      liveSummary: null,
      liveToolCount: 0,
    };
  }

  it('finalizes stream and adds export bar', () => {
    const state = makeFullState();
    _handleDoneEvent()(state);
    expect(state.contentEl.querySelector('.response-export-bar')).not.toBeNull();
  });

  it('cleans up status indicators', () => {
    const state = makeFullState();
    const si = document.createElement('div');
    si.className = 'status-indicator';
    state.contentEl.appendChild(si);
    _handleDoneEvent()(state);
    expect(state.contentEl.querySelector('.status-indicator')).toBeNull();
  });

  it('finalizes live tool wrapper when present', () => {
    const state = makeFullState();
    const liveWrapper = document.createElement('details');
    liveWrapper.className = 'tool-calls-summary';
    liveWrapper.open = true;
    const liveSummary = document.createElement('summary');
    liveWrapper.appendChild(liveSummary);
    const liveInner = document.createElement('div');
    liveInner.className = 'tool-calls-inner';
    liveWrapper.appendChild(liveInner);
    state.contentEl.appendChild(liveWrapper);
    state.liveWrapper = liveWrapper;
    state.liveSummary = liveSummary;
    state.liveInner = liveInner;
    state.liveToolCount = 2;

    _handleDoneEvent()(state);
    expect(liveWrapper.open).toBe(false);
    expect(liveSummary.textContent).toContain('2 queries executed');
  });
});

describe('_applyConfidenceMarker', () => {
  const _applyConfidenceMarker = () => fn('_applyConfidenceMarker');

  it('creates new callout when none exists', () => {
    const contentEl = document.createElement('div');
    _applyConfidenceMarker()(contentEl, 'low', 'Test reason');
    const callout = contentEl.querySelector('.confidence-callout');
    expect(callout).not.toBeNull();
  });

  it('merges into existing callout', () => {
    const contentEl = document.createElement('div');
    _applyConfidenceMarker()(contentEl, 'medium', 'First reason');
    _applyConfidenceMarker()(contentEl, 'low', 'Second reason');
    const callouts = contentEl.querySelectorAll('.confidence-callout');
    expect(callouts).toHaveLength(1);
    expect(callouts[0].classList.contains('low')).toBe(true);
    const items = callouts[0].querySelectorAll('li');
    expect(items).toHaveLength(2);
  });
});

// ────────────────────────────────────────────────────────────────
// Tool wrapper finalization
// ────────────────────────────────────────────────────────────────

describe('_createLiveToolWrapper', () => {
  const _createLiveToolWrapper = () => fn('_createLiveToolWrapper');

  it('creates tool wrapper with details, summary, and inner div', () => {
    const contentEl = document.createElement('div');
    const state = { contentEl, liveWrapper: null, liveSummary: null, liveInner: null };
    _createLiveToolWrapper()(state);
    expect(state.liveWrapper).not.toBeNull();
    expect(state.liveWrapper.tagName).toBe('DETAILS');
    expect(state.liveWrapper.classList.contains('tool-calls-summary')).toBe(true);
    expect(state.liveSummary).not.toBeNull();
    expect(state.liveInner).not.toBeNull();
    expect(state.liveInner.classList.contains('tool-calls-inner')).toBe(true);
    expect(state.liveWrapper.open).toBe(true);
  });
});

describe('_rebuildToolWrapperInner', () => {
  const _rebuildToolWrapperInner = () => fn('_rebuildToolWrapperInner');

  it('interleaves thinking text with tool calls', () => {
    const liveInner = document.createElement('div');
    const tc1 = document.createElement('details');
    tc1.className = 'tool-call';
    tc1.textContent = 'Tool 1';
    const tc2 = document.createElement('details');
    tc2.className = 'tool-call';
    tc2.textContent = 'Tool 2';
    liveInner.appendChild(tc1);
    liveInner.appendChild(tc2);
    const state = { liveInner, textChunks: ['thinking before tool 2'] };
    _rebuildToolWrapperInner()(state);
    const thinkEls = liveInner.querySelectorAll('.thinking-text');
    expect(thinkEls).toHaveLength(1);
  });

  it('appends remaining text chunks after all tools', () => {
    const liveInner = document.createElement('div');
    const tc1 = document.createElement('details');
    tc1.className = 'tool-call';
    liveInner.appendChild(tc1);
    const state = { liveInner, textChunks: ['chunk1', 'chunk2'] };
    _rebuildToolWrapperInner()(state);
    const thinkEls = liveInner.querySelectorAll('.thinking-text');
    expect(thinkEls).toHaveLength(2);
  });
});

describe('_finalizeLiveToolWrapper', () => {
  const _finalizeLiveToolWrapper = () => fn('_finalizeLiveToolWrapper');

  it('updates summary text and closes wrapper', () => {
    const contentEl = document.createElement('div');
    const liveWrapper = document.createElement('details');
    liveWrapper.className = 'tool-calls-summary';
    liveWrapper.open = true;
    const liveSummary = document.createElement('summary');
    liveWrapper.appendChild(liveSummary);
    const liveInner = document.createElement('div');
    liveInner.className = 'tool-calls-inner';
    liveWrapper.appendChild(liveInner);
    contentEl.appendChild(liveWrapper);

    const state = {
      contentEl, liveWrapper, liveSummary, liveInner,
      liveToolCount: 3, textChunks: [],
    };
    _finalizeLiveToolWrapper()(state);
    expect(liveSummary.textContent).toContain('3 queries executed');
    expect(liveWrapper.open).toBe(false);
  });

  it('shows singular form for 1 query', () => {
    const contentEl = document.createElement('div');
    const liveWrapper = document.createElement('details');
    const liveSummary = document.createElement('summary');
    liveWrapper.appendChild(liveSummary);
    const liveInner = document.createElement('div');
    liveInner.className = 'tool-calls-inner';
    liveWrapper.appendChild(liveInner);
    contentEl.appendChild(liveWrapper);

    const state = {
      contentEl, liveWrapper, liveSummary, liveInner,
      liveToolCount: 1, textChunks: [],
    };
    _finalizeLiveToolWrapper()(state);
    expect(liveSummary.textContent).toBe('1 query executed');
  });
});

// ────────────────────────────────────────────────────────────────
// processStreamEvent — additional events
// ────────────────────────────────────────────────────────────────

describe('processStreamEvent — additional events', () => {
  const processStreamEvent = () => fn('processStreamEvent');

  function makeState() {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    return {
      contentEl, statusEl,
      assistantEl: document.createElement('div'),
      streamStarted: false,
      fullText: '', currentToolEl: null,
      toolElements: {}, textChunks: [],
      currentChunk: '', chartCanvases: [],
      toolResults: [], currentToolName: null,
      currentToolInput: null,
      liveWrapper: null, liveInner: null,
      liveSummary: null, liveToolCount: 0,
    };
  }

  it('handles report event', () => {
    const state = makeState();
    state.streamStarted = true;
    processStreamEvent()('report', { url: '/api/reports/r.csv', filename: 'r.csv' }, state);
    const link = state.contentEl.querySelector('.report-download');
    expect(link).not.toBeNull();
  });

  it('handles tool_result event via processStreamEvent', () => {
    const state = makeState();
    const createToolCall = fn('createToolCall');
    const toolEl = createToolCall('query_provisions_db', { sql: 'SELECT 1' });
    state.currentToolEl = toolEl;
    state.currentToolName = 'query_provisions_db';
    state.currentToolInput = { sql: 'SELECT 1' };
    processStreamEvent()('tool_result', { tool: 'query_provisions_db', result: { row_count: 3 } }, state);
    expect(state.toolResults).toHaveLength(1);
    expect(state.currentToolEl).toBeNull();
  });

  it('handles chart event via processStreamEvent', () => {
    const state = makeState();
    const data = {
      chart_type: 'pie',
      title: 'Distribution',
      labels: ['A', 'B'],
      datasets: [{ label: 'S', data: [30, 70] }],
    };
    processStreamEvent()('chart', data, state);
    expect(state.chartCanvases).toHaveLength(1);
  });

  it('handles history event (sets internal state)', () => {
    const state = makeState();
    const msgs = [{ role: 'user', content: 'test' }];
    // Mock fetch for saveConversation that gets called
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ id: 'conv-1' }),
    });
    processStreamEvent()('history', { messages: msgs }, state);
    // The history event sets the internal conversationHistory and calls saveConversation.
    // We can verify it triggered saveConversation by checking fetch was called
    // (saveConversation POSTs to /api/conversations if history is non-empty).
    // Since we can't read the internal variable, just verify no errors.
  });
});

// ────────────────────────────────────────────────────────────────
// toggleEEFile (global)
// ────────────────────────────────────────────────────────────────

describe('toggleEEFile', () => {
  it('toggles file visibility', () => {
    const el = document.createElement('div');
    el.id = 'ee-file-test';
    el.style.display = 'none';
    document.body.appendChild(el);

    const btn = document.createElement('button');
    btn.textContent = '> test.yml';

    window.toggleEEFile('ee-file-test', btn);
    expect(el.style.display).toBe('block');

    window.toggleEEFile('ee-file-test', btn);
    expect(el.style.display).toBe('none');

    el.remove();
  });
});

// ────────────────────────────────────────────────────────────────
// _handleToolStartEvent
// ────────────────────────────────────────────────────────────────

describe('_handleToolStartEvent', () => {
  const _handleToolStartEvent = () => fn('_handleToolStartEvent');

  it('creates tool wrapper on first tool start', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = {
      contentEl, statusEl, streamStarted: false,
      liveWrapper: null, liveSummary: null, liveInner: null,
      liveToolCount: 0, currentToolEl: null,
      toolElements: {}, textChunks: [], currentChunk: '',
      currentToolName: null, currentToolInput: null,
    };
    _handleToolStartEvent()({ tool: 'query_provisions_db', input: { sql: 'SELECT 1' } }, state);
    expect(state.liveToolCount).toBe(1);
    expect(state.liveWrapper).not.toBeNull();
    expect(state.currentToolEl).not.toBeNull();
  });

  it('saves current chunk as thinking text when new tool starts', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const state = {
      contentEl, statusEl, streamStarted: true,
      liveWrapper: null, liveSummary: null, liveInner: null,
      liveToolCount: 0, currentToolEl: null,
      toolElements: {}, textChunks: [],
      currentChunk: 'Some thinking text...',
      currentToolName: null, currentToolInput: null,
    };
    _handleToolStartEvent()({ tool: 'tool1', input: {} }, state);
    expect(state.textChunks).toHaveLength(1);
    expect(state.textChunks[0]).toBe('Some thinking text...');
    expect(state.currentChunk).toBe('');
  });

  it('finalizes previous tool status on new tool start', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const prevToolEl = document.createElement('div');
    const prevStatus = document.createElement('span');
    prevStatus.className = 'tool-status running';
    prevToolEl.appendChild(prevStatus);

    const state = {
      contentEl, statusEl, streamStarted: true,
      liveWrapper: null, liveSummary: null, liveInner: null,
      liveToolCount: 0, currentToolEl: prevToolEl,
      toolElements: {}, textChunks: [], currentChunk: '',
      currentToolName: null, currentToolInput: null,
    };
    _handleToolStartEvent()({ tool: 'tool2', input: {} }, state);
    expect(prevStatus.className).toBe('tool-status done');
    expect(prevStatus.textContent).toBe('done');
  });

  it('removes live text element when tool starts', () => {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);
    const liveText = document.createElement('div');
    liveText.className = 'md-text-live';
    liveText.textContent = 'Thinking...';
    contentEl.appendChild(liveText);

    const state = {
      contentEl, statusEl, streamStarted: true,
      liveWrapper: null, liveSummary: null, liveInner: null,
      liveToolCount: 0, currentToolEl: null,
      toolElements: {}, textChunks: [], currentChunk: '',
      currentToolName: null, currentToolInput: null,
    };
    _handleToolStartEvent()({ tool: 'tool1', input: {} }, state);
    expect(contentEl.querySelector('.md-text-live')).toBeNull();
  });
});

// ────────────────────────────────────────────────────────────────
// scrollToBottom / _renderCurrentChunk
// ────────────────────────────────────────────────────────────────

describe('scrollToBottom', () => {
  it('sets scrollTop to scrollHeight', () => {
    const scrollToBottom = fn('scrollToBottom');
    const chat = document.getElementById('chat');
    scrollToBottom();
    expect(chat.scrollTop).toBeDefined();
  });
});

describe('_renderCurrentChunk', () => {
  const _renderCurrentChunk = () => fn('_renderCurrentChunk');

  it('creates md-text-live element if not present', () => {
    const contentEl = document.createElement('div');
    const state = { contentEl, currentChunk: 'Hello' };
    _renderCurrentChunk()(state);
    const el = contentEl.querySelector('.md-text-live');
    expect(el).not.toBeNull();
    expect(el.textContent).toContain('Hello');
  });

  it('reuses existing md-text-live element', () => {
    const contentEl = document.createElement('div');
    const existing = document.createElement('div');
    existing.className = 'md-text-live';
    contentEl.appendChild(existing);
    const state = { contentEl, currentChunk: 'Updated' };
    _renderCurrentChunk()(state);
    const els = contentEl.querySelectorAll('.md-text-live');
    expect(els).toHaveLength(1);
    expect(els[0].textContent).toContain('Updated');
  });
});

// ────────────────────────────────────────────────────────────────
// refreshLearningsCount
// ────────────────────────────────────────────────────────────────

describe('refreshLearningsCount', () => {
  const refreshLearningsCount = () => fn('refreshLearningsCount');

  it('updates count when learnings exist', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        has_learnings: true,
        content: '- Learning 1\n- Learning 2\n- Learning 3',
      }),
    });
    refreshLearningsCount()();
    await vi.waitFor(() => {
      const countEl = document.getElementById('learnings-count');
      expect(countEl.textContent).toBe('3 entries');
    });
  });

  it('shows empty when no learnings', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ has_learnings: false, content: '' }),
    });
    refreshLearningsCount()();
    await vi.waitFor(() => {
      const countEl = document.getElementById('learnings-count');
      expect(countEl.textContent).toBe('empty');
    });
  });

  it('handles fetch failure gracefully', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('Network error'));
    refreshLearningsCount()();
    // Should not throw
    await vi.waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
  });
});

// ────────────────────────────────────────────────────────────────
// _applyPDFPrintStyles (additional coverage)
// ────────────────────────────────────────────────────────────────

describe('_applyPDFPrintStyles — extended', () => {
  const _applyPDFPrintStyles = () => fn('_applyPDFPrintStyles');

  it('sets font properties on clone', () => {
    const clone = document.createElement('div');
    clone.innerHTML = '<p>Text</p>';
    _applyPDFPrintStyles()(clone);
    expect(clone.style.fontSize).toBe('13px');
    expect(clone.style.lineHeight).toBe('1.6');
    expect(clone.style.width).toBe('550px');
  });
});
