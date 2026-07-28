"""Post-conversation learning — analyze conversations and extract patterns."""

import json
import logging
import os
import re
from datetime import UTC, datetime

from anthropic.types import TextBlock

from src.agent.client_factory import build_async_client, resolve_model, strip_thinking_tokens
from src.config import get_config

logger = logging.getLogger(__name__)

LEARNINGS_PATH = os.path.join("data", "agent_learnings.md")
_MAX_ENTRIES = 50


def get_learnings() -> str:
    """Read the learnings file, returning empty string if missing."""
    try:
        with open(LEARNINGS_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def clear_learnings() -> None:
    """Delete the learnings file."""
    try:
        os.remove(LEARNINGS_PATH)
        logger.info("Cleared learnings file")
    except FileNotFoundError:
        pass


def _get_admin_users() -> set[str]:
    """Get the set of admin users who can see/manage learnings."""
    cfg = get_config()
    admin_str = cfg.get("learnings", {}).get("admin_users", "")
    if not admin_str:
        return set()
    return {u.strip().lower() for u in admin_str.split(",") if u.strip()}


def _get_admin_groups() -> set[str]:
    """Get the set of OpenShift groups that grant admin access."""
    cfg = get_config()
    groups_str = cfg.get("learnings", {}).get("admin_groups", "")
    if not groups_str:
        return set()
    return {g.strip().lower() for g in groups_str.split(",") if g.strip()}


def is_admin_user(user: str | None) -> bool:
    """Check if a user is an admin (email-based only, sync).

    For group-based admin checks, use ``is_admin_user_async`` instead.
    """
    cfg = get_config()
    allow_anon = cfg.get("learnings", {}).get("allow_anonymous_admin", False)
    if not user and allow_anon:
        return True
    if not user:
        return False
    admins = _get_admin_users()
    return bool(admins and user.lower() in admins)


async def is_admin_user_async(user: str | None) -> bool:
    """Check if a user is an admin via email list or OpenShift group membership."""
    if is_admin_user(user):
        return True
    if not user:
        return False
    admin_groups = _get_admin_groups()
    if not admin_groups:
        return False
    from src.routes.query import _get_user_groups

    user_groups = await _get_user_groups(user)
    return bool(admin_groups & user_groups)


def _save_entries(entries: list[dict]) -> None:
    """Write entries to the learnings file as markdown."""
    os.makedirs(os.path.dirname(LEARNINGS_PATH), exist_ok=True)

    lines = ["## Learnings from Past Conversations\n"]
    lines.append(
        "_Auto-generated patterns from conversation analysis. "
        "Review and move useful entries to the prompt files in `config/prompts/`._\n"
    )

    for entry in entries[:_MAX_ENTRIES]:
        lines.append(f"- {entry['text']}")
        lines.append(
            f"  _(seen {entry.get('count', 1)}x, last: {entry.get('last_seen', 'unknown')})_\n"
        )

    with open(LEARNINGS_PATH, "w") as f:
        f.write("\n".join(lines))


def _load_entries() -> list[dict]:
    """Parse existing entries from the learnings file."""
    content = get_learnings()
    if not content:
        return []

    entries = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("- ") and not line.startswith("_"):
            text = line[2:].strip()
            count = 1
            last_seen = "unknown"
            # Check next line for metadata
            if i + 1 < len(lines):
                meta_line = lines[i + 1].strip()
                meta_match = re.search(r"seen (\d+)x, last: ([^)]+)", meta_line)
                if meta_match:
                    count = int(meta_match.group(1))
                    last_seen = meta_match.group(2)
                    i += 1
            entries.append({"text": text, "count": count, "last_seen": last_seen})
        i += 1

    return entries


def _extract_tool_calls(messages: list) -> list[dict]:
    """Extract tool call summaries from assistant messages."""
    tool_calls: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_calls.append({"name": block.get("name", ""), "input": block.get("input", {})})
    return tool_calls


async def analyze_and_learn(messages: list) -> None:
    """Analyze a completed conversation and extract learnings.

    Runs in the background — errors are logged but never propagated.
    """
    try:
        user_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        if len(user_msgs) < 2:
            return

        tool_calls = _extract_tool_calls(messages)
        if len(tool_calls) < 3:
            return

        new_entries = await _ai_analyze(messages, tool_calls)
        if not new_entries:
            return

        existing = _load_entries()
        merged = _merge_entries(existing, new_entries)
        _save_entries(merged)
        logger.info(
            "Learnings updated: %d existing + %d new → %d merged",
            len(existing),
            len(new_entries),
            len(merged),
        )

    except Exception:
        logger.exception("Background learning analysis failed (non-fatal)")


def _summarize_user_content(content: object) -> list[str]:
    """Extract summary lines from a user message's content."""
    if not isinstance(content, list):
        return [f"User: {content}"]
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            result_str = block.get("content", "")
            if len(result_str) > 200:
                result_str = result_str[:200] + "..."
            parts.append(f"[tool_result: {result_str}]")
        elif block.get("text"):
            parts.append(f"User: {block['text']}")
    return parts


def _summarize_assistant_content(content: object) -> list[str]:
    """Extract summary lines from an assistant message's content."""
    if isinstance(content, str):
        return [f"Assistant: {content[:300]}"]
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append(f"Assistant: {block['text'][:300]}")
        elif block.get("type") == "tool_use":
            parts.append(
                f"Tool call: {block.get('name')}({json.dumps(block.get('input', {}))[:150]})"
            )
    return parts


def _summarize_conversation(messages: list) -> str:
    """Build a compact text summary of a conversation for analysis."""
    summary_parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            summary_parts.extend(_summarize_user_content(content))
        elif role == "assistant":
            summary_parts.extend(_summarize_assistant_content(content))

    result = "\n".join(summary_parts)
    if len(result) > 8000:
        result = result[:8000] + "\n... [truncated]"
    return result


async def _ai_analyze(messages: list, _tool_calls: list[dict]) -> list[dict]:
    """Use Claude to analyze a conversation and extract learnings."""
    cfg = get_config()

    conversation_summary = _summarize_conversation(messages)

    analysis_prompt = f"""Analyze this Parsec investigation conversation and extract 1-3 actionable learnings
that would help the agent handle similar questions better in the future.

Focus on:
- Tool calls that were wasted (duplicates, empty results, unnecessary)
- Where the agent could have concluded sooner
- Effective tool sequences that worked well
- Resolution patterns (what fixed the issue)
- Common mistakes to avoid

Each learning should be a single concise sentence that can be added as a rule.
Do NOT include learnings about general best practices — only specific patterns
observed in this conversation.

If the conversation was already efficient, return an empty list.

Conversation:
{conversation_summary}

Respond with ONLY a JSON array of strings, each being one learning. Example:
["For destroy-failed AnarchySubjects, the root cause is always in the AAP2 destroy job events — don't check the provision job.", "When bookbag role fails with connection timeout, the target cluster is offline — suggest manual AnarchySubject cleanup."]

If no useful learnings, respond with: []"""

    try:
        client = build_async_client(cfg, "learnings")
        model = resolve_model(cfg, "learnings")
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        block = resp.content[0]
        if not isinstance(block, TextBlock):
            return []
        return _parse_analysis_response(strip_thinking_tokens(block.text))
    except ValueError:
        logger.info("AI not available for learnings extraction")
        return []
    except Exception:
        logger.exception("AI analysis call failed")
        return []


def _parse_analysis_response(text: str) -> list[dict]:
    """Parse the AI response into learning entries."""
    text = text.strip()
    # Extract JSON array from response (may have markdown wrapping)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []

    try:
        items = json.loads(match.group())
    except json.JSONDecodeError:
        return []

    if not isinstance(items, list):
        return []

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return [
        {"text": str(item).strip(), "count": 1, "last_seen": today}
        for item in items
        if isinstance(item, str) and item.strip()
    ]


def _find_similar_entry(existing: list[dict], new_text_lower: str) -> dict | None:
    """Find an existing entry with >60% word overlap, or None."""
    new_words = set(new_text_lower.split())
    if not new_words:
        return None
    for entry in existing:
        existing_words = set(entry["text"].lower().strip().split())
        if not existing_words:
            continue
        overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
        if overlap > 0.6:
            return entry
    return None


def _merge_entries(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    """Merge new entries into existing, combining similar ones."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    for new_entry in new_entries:
        match = _find_similar_entry(existing, new_entry["text"].lower().strip())
        if match:
            match["count"] = match.get("count", 1) + 1
            match["last_seen"] = today
            if len(new_entry["text"]) > len(match["text"]):
                match["text"] = new_entry["text"]
        else:
            existing.append(new_entry)

    existing.sort(key=lambda e: (e.get("count", 1), e.get("last_seen", "")), reverse=True)
    return existing[:_MAX_ENTRIES]
