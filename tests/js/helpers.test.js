/**
 * Tests for extracted helper functions in static/app.js.
 *
 * Focus: pure-logic functions and DOM helpers that were extracted
 * during the cognitive-complexity refactoring (S3776).
 */
import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadAppJs, fn } from './setup.js';

beforeAll(() => {
  loadAppJs();
});

// ────────────────────────────────────────────────────────────────
// Pure functions — no DOM dependencies
// ────────────────────────────────────────────────────────────────

describe('csvEscapeField', () => {
  const csvEscapeField = () => fn('csvEscapeField');

  it('returns empty string for null', () => {
    expect(csvEscapeField()(null)).toBe('');
  });

  it('returns empty string for undefined', () => {
    expect(csvEscapeField()(undefined)).toBe('');
  });

  it('returns plain string unchanged', () => {
    expect(csvEscapeField()('hello')).toBe('hello');
  });

  it('wraps string with comma in double quotes', () => {
    expect(csvEscapeField()('a,b')).toBe('"a,b"');
  });

  it('wraps string with double quote and escapes inner quotes', () => {
    expect(csvEscapeField()('say "hi"')).toBe('"say ""hi"""');
  });

  it('wraps string with newline in double quotes', () => {
    expect(csvEscapeField()('line1\nline2')).toBe('"line1\nline2"');
  });

  it('converts numeric values to string', () => {
    expect(csvEscapeField()(42)).toBe('42');
  });

  it('converts boolean values to string', () => {
    expect(csvEscapeField()(false)).toBe('false');
  });
});

describe('extractChoices', () => {
  const extractChoices = () => fn('extractChoices');

  it('returns null for text without choices block', () => {
    expect(extractChoices()('Hello world')).toBeNull();
  });

  it('extracts single-select choices', () => {
    const text = 'Pick one:\n{{choices}}\n- Option A\n- Option B\n- Option C\n{{/choices}}';
    const result = extractChoices()(text);
    expect(result).not.toBeNull();
    expect(result.multi).toBe(false);
    expect(result.options).toEqual(['Option A', 'Option B', 'Option C']);
    expect(result.cleanedText).toBe('Pick one:');
  });

  it('extracts multi-select choices', () => {
    const text = 'Select many:\n{{choices multi}}\n- X\n- Y\n{{/choices}}';
    const result = extractChoices()(text);
    expect(result).not.toBeNull();
    expect(result.multi).toBe(true);
    expect(result.options).toEqual(['X', 'Y']);
  });

  it('strips leading dash and whitespace from options', () => {
    const text = '{{choices}}\n-   Spaced  \n- Normal\n{{/choices}}';
    const result = extractChoices()(text);
    expect(result.options).toEqual(['Spaced', 'Normal']);
  });

  it('skips blank lines in choices block', () => {
    const text = '{{choices}}\n- A\n\n- B\n{{/choices}}';
    const result = extractChoices()(text);
    expect(result.options).toEqual(['A', 'B']);
  });

  it('returns null if choices block is empty', () => {
    const text = '{{choices}}\n\n{{/choices}}';
    const result = extractChoices()(text);
    expect(result).toBeNull();
  });
});

describe('_findTableTitle', () => {
  const _findTableTitle = () => fn('_findTableTitle');

  it('finds a markdown heading before the table', () => {
    const lines = ['', '## Cost Summary', '| Col1 | Col2 |'];
    expect(_findTableTitle()(lines, 2)).toBe('Cost Summary');
  });

  it('finds a plain-text title', () => {
    const lines = ['', 'My Table Title', '| A | B |'];
    expect(_findTableTitle()(lines, 2)).toBe('My Table Title');
  });

  it('returns null when no title found', () => {
    const lines = ['| A | B |', '| 1 | 2 |'];
    expect(_findTableTitle()(lines, 0)).toBeNull();
  });

  it('skips blank lines to find a title', () => {
    const lines = ['Title Here', '', '', '| A |'];
    expect(_findTableTitle()(lines, 3)).toBe('Title Here');
  });

  it('does not search more than 5 lines back', () => {
    const lines = ['Far Away Title', '', '', '', '', '', '| A |'];
    // tableStartIndex is 6, so max look-back is index 1 — title is at 0
    expect(_findTableTitle()(lines, 6)).toBeNull();
  });

  it('returns heading text without the hash prefix', () => {
    const lines = ['### Detailed Breakdown', '| X |'];
    expect(_findTableTitle()(lines, 1)).toBe('Detailed Breakdown');
  });
});

describe('_collectTableLines', () => {
  const _collectTableLines = () => fn('_collectTableLines');

  it('collects consecutive pipe-delimited lines', () => {
    const lines = ['| A | B |', '| --- | --- |', '| 1 | 2 |', 'Not a table'];
    const result = _collectTableLines()(lines, 0);
    expect(result.lines).toEqual(['| A | B |', '| --- | --- |', '| 1 | 2 |']);
    expect(result.nextIndex).toBe(3);
  });

  it('stops at non-table line', () => {
    const lines = ['| A |', '| B |', '', '| C |'];
    const result = _collectTableLines()(lines, 0);
    expect(result.lines).toEqual(['| A |', '| B |']);
    expect(result.nextIndex).toBe(2);
  });

  it('handles empty input', () => {
    const result = _collectTableLines()([], 0);
    expect(result.lines).toEqual([]);
    expect(result.nextIndex).toBe(0);
  });
});

describe('parseMarkdownTable', () => {
  const parseMarkdownTable = () => fn('parseMarkdownTable');

  it('parses a simple markdown table', () => {
    const table = '| Name | Cost |\n| --- | --- |\n| Alice | $100 |\n| Bob | $200 |';
    const result = parseMarkdownTable()(table);
    expect(result).toEqual([
      { Name: 'Alice', Cost: '$100' },
      { Name: 'Bob', Cost: '$200' },
    ]);
  });

  it('returns null for too few lines', () => {
    expect(parseMarkdownTable()('| A |\n| --- |')).toBeNull();
  });

  it('returns null for missing separator line', () => {
    expect(parseMarkdownTable()('| A |\n| B |\n| C |')).toBeNull();
  });

  it('skips rows with wrong column count', () => {
    const table = '| A | B |\n| --- | --- |\n| 1 |\n| 2 | 3 |';
    const result = parseMarkdownTable()(table);
    expect(result).toEqual([{ A: '2', B: '3' }]);
  });

  it('returns null if no valid rows', () => {
    const table = '| A | B |\n| --- | --- |\n| only one |';
    expect(parseMarkdownTable()(table)).toBeNull();
  });

  it('handles alignment markers in separator', () => {
    const table = '| A | B |\n| :--- | ---: |\n| 1 | 2 |';
    const result = parseMarkdownTable()(table);
    expect(result).toEqual([{ A: '1', B: '2' }]);
  });
});

describe('parseAllMarkdownTables', () => {
  const parseAllMarkdownTables = () => fn('parseAllMarkdownTables');

  it('returns empty array for non-string input', () => {
    expect(parseAllMarkdownTables()(null)).toEqual([]);
    expect(parseAllMarkdownTables()(123)).toEqual([]);
  });

  it('returns empty array for text with no tables', () => {
    expect(parseAllMarkdownTables()('Just some text\nNo tables here')).toEqual([]);
  });

  it('parses a single table', () => {
    const text = '## Summary\n| A | B |\n| --- | --- |\n| 1 | 2 |';
    const result = parseAllMarkdownTables()(text);
    expect(result).toHaveLength(1);
    expect(result[0].title).toBe('Summary');
    expect(result[0].rows).toEqual([{ A: '1', B: '2' }]);
  });

  it('parses multiple tables', () => {
    const text = [
      '## First',
      '| X |',
      '| --- |',
      '| a |',
      '',
      '## Second',
      '| Y |',
      '| --- |',
      '| b |',
    ].join('\n');
    const result = parseAllMarkdownTables()(text);
    expect(result).toHaveLength(2);
    expect(result[0].title).toBe('First');
    expect(result[1].title).toBe('Second');
  });

  it('uses fallback title when no heading found', () => {
    const text = '| A |\n| --- |\n| 1 |';
    const result = parseAllMarkdownTables()(text);
    expect(result).toHaveLength(1);
    expect(result[0].title).toBe('Table 1');
  });
});

describe('_collectUniqueHeaders', () => {
  const _collectUniqueHeaders = () => fn('_collectUniqueHeaders');

  it('collects unique headers in order', () => {
    const rows = [
      { a: 1, b: 2 },
      { b: 3, c: 4 },
      { a: 5, c: 6 },
    ];
    expect(_collectUniqueHeaders()(rows)).toEqual(['a', 'b', 'c']);
  });

  it('returns empty array for empty rows', () => {
    expect(_collectUniqueHeaders()([])).toEqual([]);
  });

  it('handles single row', () => {
    expect(_collectUniqueHeaders()([{ x: 1, y: 2 }])).toEqual(['x', 'y']);
  });
});

describe('_formatRowsAsCSV', () => {
  const _formatRowsAsCSV = () => fn('_formatRowsAsCSV');

  it('formats rows with title header', () => {
    const headers = ['Name', 'Value'];
    const rows = [{ Name: 'a', Value: '1' }];
    const result = _formatRowsAsCSV()('Test', headers, rows);
    const lines = result.split('\n');
    expect(lines[0]).toBe('# Test');
    expect(lines[1]).toBe('Name,Value');
    expect(lines[2]).toBe('a,1');
  });

  it('handles missing values in rows', () => {
    const headers = ['A', 'B'];
    const rows = [{ A: 'x' }]; // B is missing
    const result = _formatRowsAsCSV()('T', headers, rows);
    const lines = result.split('\n');
    expect(lines[2]).toBe('x,');
  });

  it('stringifies object values', () => {
    const headers = ['data'];
    const rows = [{ data: { nested: true } }];
    const result = _formatRowsAsCSV()('T', headers, rows);
    // csvEscapeField wraps the JSON string in quotes because it contains commas/quotes
    expect(result).toContain('nested');
    expect(result).toContain('true');
  });
});

describe('_getToolResultStatusText', () => {
  const _getToolResultStatusText = () => fn('_getToolResultStatusText');

  it('shows row count with bytes scanned', () => {
    const result = { bytes_scanned: 10485760, row_count: 42 };
    expect(_getToolResultStatusText()(result, false)).toBe('42 rows (10 MB scanned)');
  });

  it('shows just row count', () => {
    expect(_getToolResultStatusText()({ row_count: 5 }, false)).toBe('5 rows');
  });

  it('shows instance count', () => {
    expect(_getToolResultStatusText()({ instance_count: 3 }, false)).toBe('3 instances');
  });

  it('shows user count', () => {
    expect(_getToolResultStatusText()({ user_count: 10 }, false)).toBe('10 users');
  });

  it('shows agreement count', () => {
    expect(_getToolResultStatusText()({ agreement_count: 7 }, false)).toBe('7 agreements');
  });

  it('shows event count', () => {
    expect(_getToolResultStatusText()({ event_count: 99 }, false)).toBe('99 events');
  });

  it('shows total cost with dollar sign', () => {
    expect(_getToolResultStatusText()({ total_cost: 1234.56 }, false)).toContain('$');
    expect(_getToolResultStatusText()({ total_cost: 1234.56 }, false)).toContain('1,234.56');
  });

  it('shows filename', () => {
    expect(_getToolResultStatusText()({ filename: 'report.csv' }, false)).toBe('report.csv');
  });

  it('returns "done" when no recognized fields', () => {
    expect(_getToolResultStatusText()({}, false)).toBe('done');
  });

  it('returns "cached" when wasCached and no recognized fields', () => {
    expect(_getToolResultStatusText()({}, true)).toBe('cached');
  });

  it('prefixes with "cached: " when wasCached', () => {
    expect(_getToolResultStatusText()({ row_count: 5 }, true)).toBe('cached: 5 rows');
  });
});

describe('findTabularData', () => {
  const findTabularData = () => fn('findTabularData');

  it('returns array of objects directly', () => {
    const data = [{ a: 1 }, { a: 2 }];
    expect(findTabularData()(data)).toBe(data);
  });

  it('returns null for empty array', () => {
    expect(findTabularData()([])).toBeNull();
  });

  it('returns null for array of non-objects', () => {
    expect(findTabularData()([1, 2, 3])).toBeNull();
  });

  it('returns null for null input', () => {
    expect(findTabularData()(null)).toBeNull();
  });

  it('returns null for string input', () => {
    expect(findTabularData()('hello')).toBeNull();
  });

  it('finds array of objects in top-level field', () => {
    const data = { results: [{ x: 1 }], count: 1 };
    expect(findTabularData()(data)).toEqual([{ x: 1 }]);
  });

  it('skips non-array top-level fields', () => {
    const data = { name: 'test', items: [{ x: 1 }] };
    expect(findTabularData()(data)).toEqual([{ x: 1 }]);
  });

  it('parses markdown table from string fields', () => {
    const data = { content: '| A |\n| --- |\n| 1 |' };
    const result = findTabularData()(data);
    expect(result).toEqual([{ A: '1' }]);
  });

  it('returns null for object with no tabular data', () => {
    const data = { name: 'test', count: 5 };
    expect(findTabularData()(data)).toBeNull();
  });
});

describe('formatElapsed', () => {
  const formatElapsed = () => fn('formatElapsed');

  it('returns dash for falsy input', () => {
    expect(formatElapsed()(0)).toBe('—');
    expect(formatElapsed()(null)).toBe('—');
    expect(formatElapsed()(undefined)).toBe('—');
  });

  it('formats seconds', () => {
    expect(formatElapsed()(30)).toBe('30s');
    expect(formatElapsed()(59)).toBe('59s');
  });

  it('formats minutes', () => {
    expect(formatElapsed()(60)).toBe('1m');
    expect(formatElapsed()(90)).toBe('1m 30s');
    expect(formatElapsed()(120)).toBe('2m');
  });

  it('formats hours', () => {
    expect(formatElapsed()(3600)).toBe('1h');
    expect(formatElapsed()(5400)).toBe('1h 30m');
    expect(formatElapsed()(7200)).toBe('2h');
  });
});

describe('statusColor', () => {
  const statusColor = () => fn('statusColor');

  it('returns red for "failed"', () => {
    expect(statusColor()('failed')).toBe('red');
  });

  it('returns red for "error"', () => {
    expect(statusColor()('error')).toBe('red');
  });

  it('returns blue for other statuses', () => {
    expect(statusColor()('successful')).toBe('blue');
    expect(statusColor()('pending')).toBe('blue');
  });
});

describe('escHtml', () => {
  const escHtml = () => fn('escHtml');

  it('returns empty string for falsy input', () => {
    expect(escHtml()('')).toBe('');
    expect(escHtml()(null)).toBe('');
    expect(escHtml()(undefined)).toBe('');
  });

  it('escapes HTML special characters', () => {
    expect(escHtml()('<script>alert("xss")</script>')).toBe(
      '&lt;script&gt;alert("xss")&lt;/script&gt;'
    );
  });

  it('passes through safe strings', () => {
    expect(escHtml()('Hello World')).toBe('Hello World');
  });
});

// ────────────────────────────────────────────────────────────────
// Data processing — message/tool result functions
// ────────────────────────────────────────────────────────────────

describe('_buildToolResultMap', () => {
  const _buildToolResultMap = () => fn('_buildToolResultMap');

  it('builds map from user messages with tool_result blocks', () => {
    const messages = [
      { role: 'assistant', content: 'hello' },
      {
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'tool-1',
            content: '{"row_count": 5}',
          },
        ],
      },
    ];
    const map = _buildToolResultMap()(messages);
    expect(map['tool-1']).toEqual({ row_count: 5 });
  });

  it('stores raw content when JSON parsing fails', () => {
    const messages = [
      {
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'tool-2',
            content: 'not json',
          },
        ],
      },
    ];
    const map = _buildToolResultMap()(messages);
    expect(map['tool-2']).toBe('not json');
  });

  it('skips assistant messages', () => {
    const messages = [{ role: 'assistant', content: [{ type: 'tool_result', tool_use_id: 'x', content: '{}' }] }];
    const map = _buildToolResultMap()(messages);
    expect(map).toEqual({});
  });

  it('skips user messages without array content', () => {
    const messages = [{ role: 'user', content: 'plain text' }];
    const map = _buildToolResultMap()(messages);
    expect(map).toEqual({});
  });

  it('handles empty messages array', () => {
    expect(_buildToolResultMap()([])).toEqual({});
  });
});

describe('_tryCollapseToolChain', () => {
  const _tryCollapseToolChain = () => fn('_tryCollapseToolChain');

  it('collapses assistant with tools followed by user tool_result', () => {
    const messages = [
      {
        role: 'assistant',
        content: [
          { type: 'tool_use', id: 't1', name: 'query', input: {} },
          { type: 'text', text: 'analyzing...' },
        ],
      },
      {
        role: 'user',
        content: [{ type: 'tool_result', tool_use_id: 't1', content: '{}' }],
      },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'Final answer' }],
      },
    ];
    const result = _tryCollapseToolChain()(messages, 0);
    expect(result.collapsed).toBe(true);
    expect(result.nextIndex).toBe(2);
    // Should include tool_use and final text
    const toolUses = result.msg.content.filter((b) => b.type === 'tool_use');
    expect(toolUses).toHaveLength(1);
  });

  it('returns collapsed: false when message has no tools', () => {
    const messages = [
      { role: 'assistant', content: [{ type: 'text', text: 'just text' }] },
    ];
    const result = _tryCollapseToolChain()(messages, 0);
    expect(result.collapsed).toBe(false);
  });
});

describe('_collapseSubAgentMessages', () => {
  const _collapseSubAgentMessages = () => fn('_collapseSubAgentMessages');

  it('passes through messages without tool_use', () => {
    const messages = [
      { role: 'user', content: 'hello' },
      { role: 'assistant', content: 'hi' },
    ];
    const result = _collapseSubAgentMessages()(messages);
    expect(result).toHaveLength(2);
    expect(result[0].content).toBe('hello');
  });

  it('collapses tool chains', () => {
    const messages = [
      {
        role: 'assistant',
        content: [
          { type: 'tool_use', id: 't1', name: 'query', input: {} },
        ],
      },
      {
        role: 'user',
        content: [{ type: 'tool_result', tool_use_id: 't1', content: '{}' }],
      },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'done' }],
      },
    ];
    const result = _collapseSubAgentMessages()(messages);
    // Should have collapsed first two into one, then the third
    expect(result.length).toBeLessThanOrEqual(messages.length);
  });
});

// ────────────────────────────────────────────────────────────────
// DOM helper functions
// ────────────────────────────────────────────────────────────────

describe('_createConfidenceCallout', () => {
  const _createConfidenceCallout = () => fn('_createConfidenceCallout');

  beforeEach(() => {
    document.getElementById('messages').innerHTML = '';
  });

  it('creates a low-confidence callout', () => {
    const container = document.createElement('div');
    _createConfidenceCallout()(container, 'low', 'Sparse data');
    const callout = container.querySelector('.confidence-callout');
    expect(callout).not.toBeNull();
    expect(callout.classList.contains('low')).toBe(true);
    expect(callout.querySelector('.confidence-title').textContent).toContain('Low confidence');
    expect(callout.querySelector('li').textContent).toBe('Sparse data');
  });

  it('creates a medium-confidence callout', () => {
    const container = document.createElement('div');
    _createConfidenceCallout()(container, 'medium', 'Some uncertainty');
    const callout = container.querySelector('.confidence-callout');
    expect(callout.classList.contains('medium')).toBe(true);
    expect(callout.querySelector('.confidence-title').textContent).toContain('Medium confidence');
  });

  it('escapes HTML in reason', () => {
    const container = document.createElement('div');
    _createConfidenceCallout()(container, 'low', '<script>alert("xss")</script>');
    const li = container.querySelector('li');
    expect(li.innerHTML).not.toContain('<script>');
    expect(li.innerHTML).toContain('&lt;');
  });
});

describe('_mergeConfidenceMarker', () => {
  const _mergeConfidenceMarker = () => fn('_mergeConfidenceMarker');

  it('upgrades medium to low confidence', () => {
    const el = document.createElement('div');
    el.className = 'confidence-callout medium';
    el.innerHTML = '<div class="confidence-title">Medium</div><ul><li>Old reason</li></ul>';

    _mergeConfidenceMarker()(el, 'low', 'New reason');

    expect(el.classList.contains('low')).toBe(true);
    expect(el.classList.contains('medium')).toBe(false);
    expect(el.querySelector('.confidence-title').textContent).toContain('Low confidence');
  });

  it('appends new reason to existing list', () => {
    const el = document.createElement('div');
    el.className = 'confidence-callout low';
    el.innerHTML = '<div class="confidence-title">Low</div><ul><li>Reason 1</li></ul>';

    _mergeConfidenceMarker()(el, 'low', 'Reason 2');

    const items = el.querySelectorAll('li');
    expect(items).toHaveLength(2);
    expect(items[1].textContent).toBe('Reason 2');
  });

  it('does not downgrade from low to medium', () => {
    const el = document.createElement('div');
    el.className = 'confidence-callout low';
    el.innerHTML = '<div class="confidence-title">Low</div><ul></ul>';

    _mergeConfidenceMarker()(el, 'medium', 'Another reason');

    expect(el.classList.contains('low')).toBe(true);
    expect(el.classList.contains('medium')).toBe(false);
  });
});

describe('addMessage', () => {
  const addMessage = () => fn('addMessage');

  beforeEach(() => {
    document.getElementById('messages').innerHTML = '';
  });

  it('creates a user message element', () => {
    const el = addMessage()('user', 'Hello');
    expect(el.classList.contains('user')).toBe(true);
    expect(el.textContent).toBe('Hello');
  });

  it('creates an assistant message with .content div', () => {
    const el = addMessage()('assistant', '');
    expect(el.classList.contains('assistant')).toBe(true);
    expect(el.querySelector('.content')).not.toBeNull();
  });

  it('creates collapsible details for long user messages', () => {
    const longText = 'A'.repeat(400);
    const el = addMessage()('user', longText);
    expect(el.querySelector('details.user-query-details')).not.toBeNull();
    expect(el.querySelector('summary')).not.toBeNull();
    expect(el.querySelector('.user-query-full').textContent).toBe(longText);
  });

  it('creates collapsible details for multi-line user messages', () => {
    const multiLine = 'Line 1\nLine 2\nLine 3\nLine 4\nLine 5';
    const el = addMessage()('user', multiLine);
    expect(el.querySelector('details.user-query-details')).not.toBeNull();
  });

  it('appends the element to #messages', () => {
    addMessage()('user', 'test');
    expect(document.getElementById('messages').children.length).toBe(1);
  });
});

describe('createToolCall', () => {
  const createToolCall = () => fn('createToolCall');

  it('creates a details element with tool name and status', () => {
    const el = createToolCall()('query_provisions_db', { sql: 'SELECT 1' });
    expect(el.tagName).toBe('DETAILS');
    expect(el.classList.contains('tool-call')).toBe(true);
    expect(el.querySelector('.tool-name').textContent).toBe('query_provisions_db');
    expect(el.querySelector('.tool-status').textContent).toBe('running...');
  });

  it('shows SQL for query_provisions_db', () => {
    const el = createToolCall()('query_provisions_db', { sql: 'SELECT * FROM provisions' });
    expect(el.querySelector('.tool-body').textContent).toBe('SELECT * FROM provisions');
  });

  it('shows query for query_cloudtrail', () => {
    const el = createToolCall()('query_cloudtrail', { query: 'SELECT eventName FROM ...' });
    expect(el.querySelector('.tool-body').textContent).toBe('SELECT eventName FROM ...');
    expect(el.querySelector('.tool-status').textContent).toContain('CloudTrail');
  });

  it('shows account info for query_aws_account', () => {
    const el = createToolCall()('query_aws_account', { account_id: '123456', action: 'list_instances' });
    expect(el.querySelector('.tool-body').textContent).toContain('list_instances');
    expect(el.querySelector('.tool-body').textContent).toContain('123456');
  });

  it('shows report info for generate_report', () => {
    const el = createToolCall()('generate_report', { format: 'csv', title: 'Monthly' });
    expect(el.querySelector('.tool-body').textContent).toContain('csv');
    expect(el.querySelector('.tool-body').textContent).toContain('Monthly');
  });

  it('falls back to JSON stringify for unknown tools', () => {
    const input = { custom: 'data' };
    const el = createToolCall()('some_tool', input);
    expect(el.querySelector('.tool-body').textContent).toBe(JSON.stringify(input, null, 2));
  });
});

describe('addExpandCollapseToggle', () => {
  const addExpandCollapseToggle = () => fn('addExpandCollapseToggle');

  it('adds a toggle button to the summary element', () => {
    const summary = document.createElement('summary');
    const wrapper = document.createElement('details');
    const inner = document.createElement('div');
    inner.className = 'tool-calls-inner';
    wrapper.appendChild(inner);

    addExpandCollapseToggle()(summary, wrapper);

    const btn = summary.querySelector('.tool-toggle-all');
    expect(btn).not.toBeNull();
    expect(btn.textContent).toBe('Expand all');
  });
});

describe('renderChoices', () => {
  const renderChoices = () => fn('renderChoices');

  it('creates a container with choice buttons', () => {
    const el = renderChoices()(['A', 'B', 'C'], false);
    expect(el.classList.contains('choices-container')).toBe(true);
    const buttons = el.querySelectorAll('.choice-btn');
    expect(buttons).toHaveLength(3);
    expect(buttons[0].textContent).toBe('A');
    expect(buttons[1].textContent).toBe('B');
  });

  it('has active dataset', () => {
    const el = renderChoices()(['X'], false);
    expect(el.dataset.active).toBe('true');
  });

  it('adds submit button for multi-select', () => {
    const el = renderChoices()(['A', 'B'], true);
    const submitBtn = el.querySelector('.choices-submit');
    expect(submitBtn).not.toBeNull();
    expect(submitBtn.textContent).toBe('Submit');
  });

  it('does not add submit button for single-select', () => {
    const el = renderChoices()(['A', 'B'], false);
    expect(el.querySelector('.choices-submit')).toBeNull();
  });
});

describe('collapseChoices', () => {
  const collapseChoices = () => fn('collapseChoices');

  it('replaces container with summary', () => {
    const parent = document.createElement('div');
    const container = document.createElement('div');
    container.className = 'choices-container';
    container.dataset.active = 'true';
    parent.appendChild(container);

    collapseChoices()(container, 'Option A');

    const summary = parent.querySelector('.choices-summary');
    expect(summary).not.toBeNull();
    expect(summary.querySelector('.choices-selected-values').textContent).toBe('Option A');
    expect(parent.querySelector('.choices-container')).toBeNull();
  });
});

describe('collapseActiveChoices', () => {
  const collapseActiveChoices = () => fn('collapseActiveChoices');

  beforeEach(() => {
    document.getElementById('messages').innerHTML = '';
  });

  it('collapses all active choice containers', () => {
    const msg = document.getElementById('messages');
    const c1 = document.createElement('div');
    c1.className = 'choices-container';
    c1.dataset.active = 'true';
    const c2 = document.createElement('div');
    c2.className = 'choices-container';
    c2.dataset.active = 'true';
    msg.appendChild(c1);
    msg.appendChild(c2);

    collapseActiveChoices()('Skipped');

    expect(msg.querySelectorAll('.choices-container')).toHaveLength(0);
    expect(msg.querySelectorAll('.choices-summary')).toHaveLength(2);
    expect(msg.querySelectorAll('.choices-summary')[0].textContent).toBe('Skipped');
  });

  it('does not collapse inactive choice containers', () => {
    const msg = document.getElementById('messages');
    const c = document.createElement('div');
    c.className = 'choices-container';
    c.dataset.active = 'false';
    msg.appendChild(c);

    collapseActiveChoices()('Skipped');

    expect(msg.querySelectorAll('.choices-container')).toHaveLength(1);
  });
});

// ────────────────────────────────────────────────────────────────
// Stream event helpers
// ────────────────────────────────────────────────────────────────

describe('_ensureStreamStarted', () => {
  const _ensureStreamStarted = () => fn('_ensureStreamStarted');

  it('removes statusEl and sets streamStarted on first call', () => {
    const parent = document.createElement('div');
    const statusEl = document.createElement('div');
    parent.appendChild(statusEl);

    const state = { streamStarted: false, statusEl };
    _ensureStreamStarted()(state);

    expect(state.streamStarted).toBe(true);
    expect(statusEl.parentNode).toBeNull();
  });

  it('does nothing on subsequent calls', () => {
    const statusEl = document.createElement('div');
    const state = { streamStarted: true, statusEl };
    // Should not throw even though statusEl has no parent
    _ensureStreamStarted()(state);
    expect(state.streamStarted).toBe(true);
  });
});

describe('_cleanupStatusIndicators', () => {
  const _cleanupStatusIndicators = () => fn('_cleanupStatusIndicators');

  it('removes status indicator from content element', () => {
    const contentEl = document.createElement('div');
    const si = document.createElement('div');
    si.className = 'status-indicator';
    contentEl.appendChild(si);

    _cleanupStatusIndicators()(contentEl);
    expect(contentEl.querySelector('.status-indicator')).toBeNull();
  });

  it('does nothing when no status indicator exists', () => {
    const contentEl = document.createElement('div');
    _cleanupStatusIndicators()(contentEl);
    // Should not throw
    expect(contentEl.children.length).toBe(0);
  });
});

describe('_finalizeRunningToolStatuses', () => {
  const _finalizeRunningToolStatuses = () => fn('_finalizeRunningToolStatuses');

  it('changes all running statuses to done', () => {
    const contentEl = document.createElement('div');
    const s1 = document.createElement('span');
    s1.className = 'tool-status running';
    s1.textContent = 'running...';
    const s2 = document.createElement('span');
    s2.className = 'tool-status running';
    s2.textContent = 'running...';
    contentEl.appendChild(s1);
    contentEl.appendChild(s2);

    _finalizeRunningToolStatuses()(contentEl);

    expect(s1.className).toBe('tool-status done');
    expect(s1.textContent).toBe('done');
    expect(s2.className).toBe('tool-status done');
  });
});

describe('processStreamEvent', () => {
  const processStreamEvent = () => fn('processStreamEvent');

  function makeState() {
    const contentEl = document.createElement('div');
    const statusEl = document.createElement('div');
    contentEl.appendChild(statusEl);

    return {
      contentEl,
      statusEl,
      assistantEl: document.createElement('div'),
      streamStarted: false,
      fullText: '',
      currentToolEl: null,
      toolElements: {},
      textChunks: [],
      currentChunk: '',
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

  it('handles text event', () => {
    const state = makeState();
    processStreamEvent()('text', { content: 'Hello ' }, state);
    expect(state.fullText).toBe('Hello ');
    expect(state.currentChunk).toBe('Hello ');
    expect(state.streamStarted).toBe(true);
  });

  it('accumulates text across multiple events', () => {
    const state = makeState();
    processStreamEvent()('text', { content: 'Hello ' }, state);
    processStreamEvent()('text', { content: 'World' }, state);
    expect(state.fullText).toBe('Hello World');
    expect(state.currentChunk).toBe('Hello World');
  });

  it('handles error event', () => {
    const state = makeState();
    processStreamEvent()('error', { message: 'Something broke' }, state);
    const errEl = state.contentEl.querySelector('.error-message');
    expect(errEl).not.toBeNull();
    expect(errEl.textContent).toBe('Something broke');
  });

  it('handles status event', () => {
    const state = makeState();
    processStreamEvent()('status', { message: 'Querying database...' }, state);
    const si = state.contentEl.querySelector('.status-indicator');
    expect(si).not.toBeNull();
    expect(si.textContent).toContain('Querying database...');
  });

  it('handles agent_start event', () => {
    const state = makeState();
    processStreamEvent()('agent_start', { agent: 'cost', name: 'Cost Investigation' }, state);
    const banner = state.contentEl.querySelector('.agent-banner');
    expect(banner).not.toBeNull();
    expect(banner.dataset.agent).toBe('cost');
    expect(banner.classList.contains('agent-running')).toBe(true);
  });

  it('handles agent_done event', () => {
    const state = makeState();
    processStreamEvent()('agent_start', { agent: 'cost', name: 'Cost Investigation' }, state);
    // agent_done is dispatched with state (processStreamEvent extracts contentEl internally)
    processStreamEvent()('agent_done', { agent: 'cost' }, state);
    const banner = state.contentEl.querySelector('.agent-banner[data-agent="cost"]');
    expect(banner.classList.contains('agent-done')).toBe(true);
    expect(banner.classList.contains('agent-running')).toBe(false);
  });

  it('handles confidence event for medium level', () => {
    const state = makeState();
    processStreamEvent()('confidence', { level: 'medium', reasons: ['Sparse data'] }, state);
    const callout = state.contentEl.querySelector('.confidence-callout');
    expect(callout).not.toBeNull();
    expect(callout.classList.contains('medium')).toBe(true);
  });

  it('ignores confidence event for high level', () => {
    const state = makeState();
    processStreamEvent()('confidence', { level: 'high', reasons: [] }, state);
    expect(state.contentEl.querySelector('.confidence-callout')).toBeNull();
  });

  it('handles cache_hit event', () => {
    const state = makeState();
    // Set up a current tool element with a status span
    const toolEl = document.createElement('div');
    const statusSpan = document.createElement('span');
    statusSpan.className = 'tool-status running';
    toolEl.appendChild(statusSpan);
    state.currentToolEl = toolEl;

    processStreamEvent()('cache_hit', {}, state);
    expect(statusSpan.className).toBe('tool-status cached');
    expect(statusSpan.textContent).toBe('cached');
  });

  it('handles tool_start event', () => {
    const state = makeState();
    processStreamEvent()('tool_start', { tool: 'query_provisions_db', input: { sql: 'SELECT 1' } }, state);
    expect(state.liveToolCount).toBe(1);
    expect(state.currentToolEl).not.toBeNull();
    expect(state.liveWrapper).not.toBeNull();
    expect(state.liveSummary.textContent).toContain('1 query running');
  });

  it('increments tool count on multiple tool_start events', () => {
    const state = makeState();
    processStreamEvent()('tool_start', { tool: 'tool_a', input: {} }, state);
    processStreamEvent()('tool_start', { tool: 'tool_b', input: {} }, state);
    expect(state.liveToolCount).toBe(2);
    expect(state.liveSummary.textContent).toContain('2 queries running');
  });
});

// ────────────────────────────────────────────────────────────────
// Finalize tool call
// ────────────────────────────────────────────────────────────────

describe('finalizeToolCall', () => {
  const finalizeToolCall = () => fn('finalizeToolCall');
  const createToolCall = () => fn('createToolCall');

  it('marks tool as done with result info', () => {
    const el = createToolCall()('query_provisions_db', { sql: 'SELECT 1' });
    finalizeToolCall()(el, 'query_provisions_db', { row_count: 10 });
    const status = el.querySelector('.tool-status');
    expect(status.className).toBe('tool-status done');
    expect(status.textContent).toBe('10 rows');
  });

  it('marks tool as error when result has error', () => {
    const el = createToolCall()('some_tool', {});
    finalizeToolCall()(el, 'some_tool', { error: 'timeout' });
    const status = el.querySelector('.tool-status');
    expect(status.className).toBe('tool-status error');
    expect(status.textContent).toBe('error');
  });

  it('preserves cached class when finalizing', () => {
    const el = createToolCall()('query_provisions_db', { sql: 'SELECT 1' });
    // Simulate cache hit
    const status = el.querySelector('.tool-status');
    status.className = 'tool-status cached';
    finalizeToolCall()(el, 'query_provisions_db', { row_count: 5 });
    expect(status.className).toBe('tool-status cached');
    expect(status.textContent).toBe('cached: 5 rows');
  });

  it('appends result to tool body', () => {
    const el = createToolCall()('some_tool', { x: 1 });
    finalizeToolCall()(el, 'some_tool', { data: 'ok' });
    const body = el.querySelector('.tool-body');
    expect(body.textContent).toContain('--- Result ---');
    expect(body.textContent).toContain('"data": "ok"');
  });
});

// ────────────────────────────────────────────────────────────────
// renderChart (basic structure)
// ────────────────────────────────────────────────────────────────

describe('renderChart', () => {
  const renderChart = () => fn('renderChart');

  it('creates a chart container with canvas', () => {
    const data = {
      chart_type: 'bar',
      title: 'Test Chart',
      labels: ['A', 'B'],
      datasets: [{ label: 'Series 1', data: [10, 20] }],
    };
    const el = renderChart()(data);
    expect(el.classList.contains('chart-container')).toBe(true);
    expect(el.querySelector('canvas')).not.toBeNull();
  });

  it('adds export buttons', () => {
    const data = {
      chart_type: 'line',
      title: 'Chart',
      labels: ['X'],
      datasets: [{ label: 'S', data: [1] }],
    };
    const el = renderChart()(data);
    const exportBar = el.querySelector('.chart-export-bar');
    expect(exportBar).not.toBeNull();
    const buttons = exportBar.querySelectorAll('.chart-export-btn');
    expect(buttons).toHaveLength(2);
    expect(buttons[0].textContent).toBe('Export PNG');
    expect(buttons[1].textContent).toBe('Export CSV');
  });
});

// ────────────────────────────────────────────────────────────────
// Response export bar
// ────────────────────────────────────────────────────────────────

describe('createResponseExportBar', () => {
  const createResponseExportBar = () => fn('createResponseExportBar');

  it('creates export bar with MD, PDF, and Share buttons', () => {
    const messageEl = document.createElement('div');
    messageEl._exportToolResults = [];
    const bar = createResponseExportBar()(messageEl);
    expect(bar.classList.contains('response-export-bar')).toBe(true);
    const buttons = bar.querySelectorAll('.response-export-btn');
    const labels = Array.from(buttons).map((b) => b.textContent);
    expect(labels).toContain('Export MD');
    expect(labels).toContain('Export PDF');
    expect(labels).toContain('Share');
  });

  it('adds CSV and JSON buttons when toolResults are present', () => {
    const messageEl = document.createElement('div');
    messageEl._exportToolResults = [{ tool: 'test', result: {} }];
    const bar = createResponseExportBar()(messageEl);
    const labels = Array.from(bar.querySelectorAll('.response-export-btn')).map((b) => b.textContent);
    expect(labels).toContain('Export CSV');
    expect(labels).toContain('Export JSON');
  });
});

// ────────────────────────────────────────────────────────────────
// Debug helpers
// ────────────────────────────────────────────────────────────────

describe('_applyPDFPrintStyles', () => {
  const _applyPDFPrintStyles = () => fn('_applyPDFPrintStyles');

  it('sets white background on clone', () => {
    const clone = document.createElement('div');
    clone.innerHTML = '<h1>Title</h1><table><th>H</th><td>D</td></table><code>x</code><pre>y</pre><a href="#">z</a>';
    _applyPDFPrintStyles()(clone);
    // jsdom normalizes hex colors to rgb()
    expect(clone.style.background).toMatch(/white|#fff|rgb\(255,\s*255,\s*255\)/);
    expect(clone.style.color).toMatch(/#1a1a1a|rgb\(26,\s*26,\s*26\)/);
    expect(clone.style.padding).toBe('20px');
    expect(clone.style.width).toBe('550px');
  });

  it('styles heading, table, and code elements', () => {
    const clone = document.createElement('div');
    clone.innerHTML = '<h2>H</h2><th>TH</th><td>TD</td><code>C</code><pre>P</pre><a>L</a>';
    _applyPDFPrintStyles()(clone);
    // Verify it doesn't throw and applies styles to child elements
    expect(clone.querySelector('h2').style.color).toBeTruthy();
    expect(clone.querySelector('code').style.background).toBeTruthy();
    expect(clone.querySelector('a').style.color).toBeTruthy();
  });
});

describe('_appendThinkingText', () => {
  const _appendThinkingText = () => fn('_appendThinkingText');

  it('adds a thinking-text div with parsed markdown', () => {
    const container = document.createElement('div');
    _appendThinkingText()(container, 'Some thinking');
    const el = container.querySelector('.thinking-text');
    expect(el).not.toBeNull();
    // marked.parse just returns the string in our mock
    expect(el.textContent).toContain('Some thinking');
  });
});

describe('renderChart color handling', () => {
  const renderChart = () => fn('renderChart');

  it('applies distinct colors to pie chart segments', () => {
    const data = {
      chart_type: 'pie',
      title: 'Pie',
      labels: ['A', 'B', 'C'],
      datasets: [{ label: 'S', data: [10, 20, 30] }],
    };
    // Verifying chart creation doesn't throw — colors are applied internally
    const el = renderChart()(data);
    expect(el).not.toBeNull();
    expect(el.querySelector('canvas')).not.toBeNull();
  });

  it('handles log scale detection for wide-range data', () => {
    const data = {
      chart_type: 'bar',
      title: 'Wide Range',
      labels: ['Small', 'Large'],
      datasets: [{ label: 'S', data: [1, 10000] }],
    };
    const el = renderChart()(data);
    expect(el).not.toBeNull();
  });

  it('handles doughnut chart type', () => {
    const data = {
      chart_type: 'doughnut',
      title: 'Doughnut',
      labels: ['X', 'Y'],
      datasets: [{ label: 'D', data: [5, 15] }],
    };
    const el = renderChart()(data);
    expect(el).not.toBeNull();
  });
});
