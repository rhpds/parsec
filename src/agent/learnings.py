"""Post-conversation learning — analyze conversations and extract patterns."""

import json
import logging
import os
import re
from datetime import UTC, datetime

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


def is_admin_user(user: str | None) -> bool:
    """Check if a user is an admin for the learnings feature."""
    cfg = get_config()
    allow_anon = cfg.get("learnings", {}).get("allow_anonymous_admin", False)
    if not user and allow_anon:
        return True
    if not user:
        return False
    admins = _get_admin_users()
    if not admins:
        return False
    return user.lower() in admins


def _save_entries(entries: list[dict]) -> None:
    """Write entries to the learnings file as markdown."""
    os.makedirs(os.path.dirname(LEARNINGS_PATH), exist_ok=True)

    lines = ["## Learnings from Past Conversations\n"]
    lines.append(
        "_Auto-generated patterns from conversation analysis. "
        "Review and move useful entries to `config/agent_instructions.md`._\n"
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


async def analyze_and_learn(messages: list) -> None:
    """Analyze a completed conversation and extract learnings.

    Runs in the background — errors are logged but never propagated.
    """
    try:
        # Skip very short conversations (< 2 user messages)
        user_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        if len(user_msgs) < 2:
            return

        # Count tool calls
        tool_calls = []
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            }
                        )

        # Skip conversations with very few tool calls (nothing to learn)
        if len(tool_calls) < 3:
            return

        new_entries = await _ai_analyze(messages, tool_calls)
        if not new_entries:
            return

        # Merge with existing entries
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


def _summarize_conversation(messages: list) -> str:
    """Build a compact text summary of a conversation for analysis."""
    summary_parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_str = block.get("content", "")
                        if len(result_str) > 200:
                            result_str = result_str[:200] + "..."
                        summary_parts.append(f"[tool_result: {result_str}]")
                    elif isinstance(block, dict) and block.get("text"):
                        summary_parts.append(f"User: {block['text']}")
            else:
                summary_parts.append(f"User: {content}")
        elif role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        text = block["text"][:300]
                        summary_parts.append(f"Assistant: {text}")
                    elif block.get("type") == "tool_use":
                        summary_parts.append(
                            f"Tool call: {block.get('name')}("
                            f"{json.dumps(block.get('input', {}))[:150]})"
                        )
            elif isinstance(content, str):
                summary_parts.append(f"Assistant: {content[:300]}")

    result = "\n".join(summary_parts)
    if len(result) > 8000:
        result = result[:8000] + "\n... [truncated]"
    return result


async def _ai_analyze(messages: list, tool_calls: list[dict]) -> list[dict]:
    """Use Claude to analyze a conversation and extract learnings."""
    cfg = get_config()
    backend = cfg.anthropic.get("backend", "direct")
    model = cfg.anthropic.get("model", "claude-sonnet-4-20250514")

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
        if backend == "bedrock":
            return await _analyze_bedrock(cfg, model, analysis_prompt)
        elif backend == "vertex":
            return await _analyze_vertex(cfg, model, analysis_prompt)
        else:
            return await _analyze_direct(cfg, model, analysis_prompt)
    except Exception:
        logger.exception("AI analysis call failed")
        return []


async def _analyze_direct(cfg: dict, model: str, prompt: str) -> list[dict]:
    """Analyze using direct Anthropic API."""
    import anthropic

    api_key = cfg.anthropic.get("api_key", "")  # type: ignore[attr-defined]
    if not api_key:
        return []

    client = anthropic.AsyncAnthropic(api_key=api_key)
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_analysis_response(resp.content[0].text)


async def _analyze_vertex(cfg: dict, model: str, prompt: str) -> list[dict]:
    """Analyze using Vertex AI."""
    import anthropic

    project_id = cfg.anthropic.get("vertex_project_id", "")  # type: ignore[attr-defined]
    region = cfg.anthropic.get("vertex_region", "us-east5")  # type: ignore[attr-defined]
    if not project_id:
        return []

    client = anthropic.AsyncAnthropicVertex(project_id=project_id, region=region)
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_analysis_response(resp.content[0].text)


async def _analyze_bedrock(cfg: dict, model: str, prompt: str) -> list[dict]:
    """Analyze using AWS Bedrock."""
    import anthropic

    client = anthropic.AsyncAnthropicBedrock(
        aws_region=cfg.anthropic.get("bedrock_region", "us-east-1"),  # type: ignore[attr-defined]
    )
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_analysis_response(resp.content[0].text)


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


def _merge_entries(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    """Merge new entries into existing, combining similar ones."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    for new_entry in new_entries:
        new_text = new_entry["text"].lower().strip()
        merged = False

        for existing_entry in existing:
            existing_text = existing_entry["text"].lower().strip()
            # Simple similarity: if >60% of words overlap, merge
            new_words = set(new_text.split())
            existing_words = set(existing_text.split())
            if not new_words or not existing_words:
                continue
            overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
            if overlap > 0.6:
                existing_entry["count"] = existing_entry.get("count", 1) + 1
                existing_entry["last_seen"] = today
                # Keep the longer/more detailed version
                if len(new_entry["text"]) > len(existing_entry["text"]):
                    existing_entry["text"] = new_entry["text"]
                merged = True
                break

        if not merged:
            existing.append(new_entry)

    # Sort by count (most seen first), then by last_seen
    existing.sort(key=lambda e: (e.get("count", 1), e.get("last_seen", "")), reverse=True)

    # Cap at max entries
    return existing[:_MAX_ENTRIES]
