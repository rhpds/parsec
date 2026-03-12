# Chat Choice Buttons — Design Spec

## Summary

Add interactive choice buttons to the Parsec chat UI that Claude can present when
asking clarifying questions. Users click pill-shaped buttons to select their answer(s),
which auto-populates and submits the chat input. Supports both single-select (pick one)
and multi-select (toggle multiple, then submit).

## Architecture

Entirely frontend — no backend changes. Claude writes a `{{choices}}` markdown syntax
in its text response. The frontend detects this pattern during rendering, strips it from
the markdown, and renders interactive pill buttons below the message text. When the user
makes a selection, it becomes a normal user message in the conversation.

## Syntax

Claude uses this in its markdown text responses:

**Single-select** (click one, auto-submits):
```
Which cloud provider should I focus on?

{{choices}}
- AWS
- Azure
- GCP
{{/choices}}
```

**Multi-select** (toggle on/off, submit button):
```
Which areas should I investigate?

{{choices multi}}
- Cost anomalies
- GPU usage
- IAM activity
- Marketplace purchases
{{/choices}}
```

## User Interaction Flow

### Single-select
1. Claude's response renders with text above and pill buttons below
2. User clicks a button (e.g., "AWS")
3. Buttons collapse into a summary: "Selected: AWS"
4. The text "AWS" is placed in the chat input and the form auto-submits
5. Claude receives "AWS" as a normal user message

### Multi-select
1. Claude's response renders with text above and pill buttons below
2. User clicks buttons to toggle them on/off (highlighted border when selected)
3. User clicks "Submit" button
4. Buttons collapse into summary: "Selected: Cost anomalies, GPU usage"
5. The text "Cost anomalies, GPU usage" is placed in the chat input and auto-submits
6. Claude receives the comma-separated list as a normal user message

### Edge cases
- If the user types in the chat input instead of clicking buttons, the buttons
  collapse to "Skipped" (dimmed) when the next message is sent
- Buttons from previous messages are always shown in collapsed/summary state
  (never interactive) — only the most recent set of buttons is interactive
- During SSE streaming, the `{{choices}}` block is parsed at the `done` event,
  not during live text streaming (avoids partial parse issues)

## File Changes

### `static/app.js`

Add a function `extractAndRenderChoices(contentEl, text)` that:
1. Uses regex to find `{{choices}}` or `{{choices multi}}` blocks in the text
2. Extracts the list items (lines starting with `- `)
3. Strips the `{{choices}}...{{/choices}}` block from the text
4. Returns the cleaned text and renders a `.choices-container` div with buttons

Called in the `done` event handler, after the final text is known but before
rendering the final markdown.

Button click handlers:
- **Single-select**: on click, collapse buttons to summary, populate input, submit form
- **Multi-select**: on click, toggle `.selected` class on button. A "Submit" button
  at the end collects all selected values, collapses to summary, populates input, submits.

Collapse logic: replace the `.choices-container` div with a `.choices-summary` span
showing "Selected: X, Y, Z".

On form submit, check for any active (non-collapsed) `.choices-container` and collapse
it to "Skipped".

### `static/style.css`

New classes:

```css
.choices-container       /* flex-wrap container for buttons */
.choice-btn              /* pill button — rounded, border, hover effect */
.choice-btn.selected     /* highlighted border + text color when toggled on */
.choice-btn:hover        /* subtle highlight */
.choices-submit          /* "Submit" button for multi-select */
.choices-summary         /* collapsed state — small italic text */
```

Theme-aware: uses existing CSS custom properties from the dark/light theme system.

### `config/agent_instructions.md`

Add a section documenting the `{{choices}}` syntax so Claude knows when and how
to use it. Key guidance:
- Use `{{choices}}` for simple A/B/C questions with short labels
- Use `{{choices multi}}` when the user should pick multiple items
- Keep option labels short (1-5 words)
- Always include a text question above the choices block
- Don't use choices for open-ended questions — only for discrete options

## Visual Design

Pill buttons matching the Parsec dark theme:
- Background: `var(--bg-secondary)` (surface color)
- Border: `1px solid var(--border)` (default), `2px solid var(--accent)` (selected)
- Text: `var(--accent)` color (blue-ish)
- Border-radius: 16px
- Padding: 6px 16px
- Font size: 12px
- Hover: slightly lighter background
- Selected: accent border + accent text color
- Gap: 8px between buttons, flex-wrap for overflow
- Submit button (multi-select only): solid accent background, dark text, 8px radius

Collapsed summary:
- Small text, italic, muted color
- Format: "Selected: X, Y, Z" or "Skipped"

## Testing

### Manual Testing

Start Parsec locally, then test these scenarios:

1. Ask "what cloud providers do you support?" — Claude should use `{{choices}}`
   (after agent instructions are updated)
2. Click a single-select button — verify it collapses and submits
3. Ask a multi-select question — toggle multiple, submit, verify
4. Type a response manually instead of clicking — verify buttons collapse to "Skipped"
5. Scroll up to a previous message with choices — verify they show as summaries
6. Test in both dark and light theme
7. Test with long option labels that wrap to multiple lines
8. Test shared session restore — choices should render as collapsed summaries
