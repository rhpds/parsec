/**
 * Test setup — loads static/app.js into the jsdom global scope.
 *
 * app.js is a vanilla browser script (no modules, no build step).
 * It declares functions with `function` keyword at global scope.
 * We mock the browser APIs it depends on during load, then expose
 * the functions for testing.
 *
 * Uses vm.Script with the original filename so v8 coverage is
 * attributed to static/app.js (eval() loses this association).
 */
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import vm from 'vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const APP_JS_PATH = resolve(__dirname, '../../static/app.js');

/**
 * Set up the minimal DOM and browser API mocks required by app.js,
 * then evaluate it so all `function` declarations land on `globalThis`.
 *
 * Call this once in a `beforeAll` block.
 */
export function loadAppJs() {
  // ── Mock browser APIs that app.js touches during initial load ──

  // marked (markdown renderer)
  globalThis.marked = {
    parse: (text) => text || '',
    setOptions: () => {},
    Renderer: class {
      link() { return ''; }
    },
  };

  // Chart.js
  globalThis.Chart = class Chart { constructor() {} };

  // html2canvas / jspdf (used by PDF export)
  globalThis.html2canvas = () => Promise.resolve(document.createElement('canvas'));
  globalThis.jspdf = { jsPDF: class { addPage() {} addImage() {} save() {} } };

  // crypto.randomUUID
  if (!globalThis.crypto) {
    globalThis.crypto = {};
  }
  if (!globalThis.crypto.randomUUID) {
    globalThis.crypto.randomUUID = () =>
      'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
      });
  }

  // localStorage
  const store = {};
  globalThis.localStorage = {
    getItem(key) { return store[key] ?? null; },
    setItem(key, val) { store[key] = String(val); },
    removeItem(key) { delete store[key]; },
    clear() { Object.keys(store).forEach((k) => delete store[k]); },
  };

  // ── Build minimal DOM that app.js queries on load ──
  document.body.innerHTML = `
    <div id="app">
      <div id="sidebar" class="sidebar">
        <span class="sidebar-title">History</span>
        <div class="sidebar-examples" style="display:none">
          <ul class="sidebar-examples-list"></ul>
        </div>
      </div>
      <div id="sidebar-list"></div>
      <button id="sidebar-close-btn"></button>
      <button id="sidebar-tab-history"></button>
      <button id="sidebar-tab-examples"></button>
      <button id="sidebar-tab-skills"></button>
      <button id="sidebar-tab-debug"></button>
      <div id="learnings-panel" style="display:none"></div>
      <div id="skills-panel" style="display:none"></div>
      <div id="skills-list"></div>
      <div id="learnings-count"></div>
      <button id="learnings-view-btn"></button>
      <button id="learnings-copy-btn"></button>
      <button id="learnings-modal-close"></button>
      <div id="learnings-modal" style="display:none">
        <textarea id="learnings-text"></textarea>
      </div>
      <button id="learnings-clear-btn"></button>
      <div id="chat">
        <div id="messages"></div>
      </div>
      <footer>
        <form id="query-form">
          <textarea id="question"></textarea>
          <button id="send-btn" type="submit">Send</button>
          <input type="file" id="file-upload" />
          <button id="upload-btn"></button>
          <div id="attachment-indicator" style="display:none">
            <span id="attachment-name"></span>
            <button id="attachment-remove"></button>
          </div>
        </form>
      </footer>
      <button id="new-chat-btn"></button>
      <button id="theme-toggle-btn"></button>
      <div id="share-modal" style="display:none">
        <input id="share-link-input" />
        <button id="share-copy-btn"></button>
        <button id="share-close-btn"></button>
      </div>
      <div id="shared-banner" style="display:none">
        <button id="continue-btn"></button>
      </div>
      <div id="debug-view" style="display:none">
        <input id="debug-url" />
        <button id="debug-diagnose-btn"></button>
        <div id="debug-error" style="display:none"></div>
        <div id="debug-loading" style="display:none"></div>
        <div id="debug-result" style="display:none">
          <div class="debug-tab" data-tab="triage"></div>
          <div class="debug-tab" data-tab="failing-task"></div>
          <div class="debug-tab" data-tab="fix"></div>
          <div class="debug-tab" data-tab="correlation"></div>
          <div class="debug-tab" data-tab="ee-info"></div>
        </div>
        <div id="debug-summary"></div>
        <div id="debug-tab-content"></div>
        <div id="debug-fix-preview" style="display:none"></div>
      </div>
    </div>
  `;

  // Mock fetch globally (app.js fires fetch calls on load for auth check, etc.)
  globalThis.fetch = () =>
    Promise.resolve({
      ok: false,
      status: 500,
      json: () => Promise.resolve({}),
    });

  // ── Load app.js via vm.Script so v8 coverage tracks the original file ──
  const appCode = readFileSync(APP_JS_PATH, 'utf-8');
  const script = new vm.Script(appCode, {
    filename: APP_JS_PATH,
  });
  // runInThisContext executes in the current global scope,
  // making function declarations available on globalThis
  script.runInThisContext();
}

/**
 * Get a function from the global scope (populated by loadAppJs).
 */
export function fn(name) {
  const f = globalThis[name];
  if (typeof f !== 'function') {
    throw new Error(`Global function "${name}" not found — was loadAppJs() called?`);
  }
  return f;
}
