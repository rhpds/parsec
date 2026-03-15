"""Trim Ansible job logs to keep only investigation-relevant content.

AAP2 job logs are typically 100KB–3MB but contain vast amounts of noise
(ok/skipping/changed lines, timestamps, JSON blobs from K8s resources).
This module strips the noise and keeps the diagnostically useful parts:
PLAY/TASK headers, failures with context, PLAY RECAP, and timing summaries.
"""

from __future__ import annotations

import json
import re

_TIMESTAMP_RE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+\d+")

_RETRY_RE = re.compile(r"FAILED - RETRYING:.*\((\d+) retries left\)")

_JSON_ERROR_KEYS = ("msg", "stderr", "message", "error", "reason")

MAX_LINE_CHARS = 1500


def is_ansible_log(content: str) -> bool:
    """Return True if content looks like an Ansible job log."""
    sample = content[:8000]
    markers = sum(
        1
        for m in ("PLAY [", "TASK [", "PLAY RECAP", "TASKS RECAP")
        if m in sample or m in content[-4000:]
    )
    return markers >= 2


def _extract_json_errors(line: str) -> str | None:
    """Pull error-relevant fields from a JSON blob in a fatal line."""
    start = line.find("=> {")
    if start == -1:
        start = line.find("=> (")
    if start == -1:
        return None

    json_str = line[start + 3 :].strip()
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None

    parts: list[str] = []
    for key in _JSON_ERROR_KEYS:
        val = data.get(key)
        if val:
            text = str(val)[:500]
            parts.append(f'"{key}": {text!r}')

    if "cmd" in data:
        cmd = data["cmd"]
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        parts.append(f'"cmd": {str(cmd)[:300]!r}')

    return ", ".join(parts) if parts else None


def _truncate_line(line: str) -> str:
    """Truncate a long line, preserving extracted error fields."""
    if len(line) <= MAX_LINE_CHARS:
        return line

    errors = _extract_json_errors(line)
    prefix = line[:800]
    if errors:
        return f"{prefix}... " f"[extracted: {errors}] " f"[truncated from {len(line):,} chars]"
    return f"{prefix}... [truncated from {len(line):,} chars]"


def trim_ansible_log(content: str) -> str:
    """Trim an Ansible job log, keeping only investigation-relevant lines.

    Returns the trimmed content with a metadata header showing compression.
    """
    lines = content.splitlines()
    total_lines = len(lines)
    original_size = len(content)

    fatal_indices: set[int] = set()
    for i, line in enumerate(lines):
        if "fatal:" in line or "FAILED!" in line or line.lstrip().startswith("failed:"):
            for j in range(max(0, i - 2), min(total_lines, i + 4)):
                fatal_indices.add(j)

    recap_start: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("PLAY RECAP") or stripped.startswith("TASKS RECAP"):
            recap_start = i
            break

    result: list[str] = []
    prev_was_retry = False
    retry_count = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        if i < 3:
            result.append(line)
            continue

        if recap_start is not None and i >= recap_start:
            result.append(_truncate_line(line))
            continue

        if stripped.startswith("PLAY ["):
            result.append(line)
            prev_was_retry = False
            continue

        if stripped.startswith("TASK ["):
            result.append(line)
            prev_was_retry = False
            continue

        if i in fatal_indices:
            retry_match_in_fatal = _RETRY_RE.search(stripped)
            if retry_match_in_fatal:
                if not prev_was_retry:
                    retry_count = 1
                    prev_was_retry = True
                else:
                    retry_count += 1
                continue
            if prev_was_retry and retry_count > 0:
                result.append(f"  [... retried {retry_count} times before failing]")
                prev_was_retry = False
                retry_count = 0
            result.append(_truncate_line(line))
            continue

        if "NO MORE HOSTS LEFT" in stripped:
            result.append(line)
            continue

        if stripped == "...ignoring":
            result.append(line)
            continue

        retry_match = _RETRY_RE.search(stripped)
        if retry_match:
            retries_left = int(retry_match.group(1))
            if not prev_was_retry:
                retry_count = 1
                prev_was_retry = True
            else:
                retry_count += 1
            if retries_left <= 1:
                result.append(f"  [... retried {retry_count} times before failing]")
                prev_was_retry = False
                retry_count = 0
            continue

        if prev_was_retry:
            prev_was_retry = False
            retry_count = 0

        if stripped.startswith("skipping:"):
            continue
        if stripped.startswith("ok:"):
            if '"msg"' in stripped and len(stripped) < 500:
                result.append(line)
            continue
        if stripped.startswith("changed:"):
            continue
        if stripped.startswith("included:"):
            continue
        if "[WARNING]" in stripped or "[DEPRECATION WARNING]" in stripped:
            continue
        if stripped.startswith("ASYNC "):
            continue
        if _TIMESTAMP_RE.match(stripped):
            continue
        if stripped.startswith("Vault password"):
            result.append(line)
            continue
        if stripped.startswith("Pausing for"):
            result.append(line)
            continue
        if stripped.startswith("[WARNING]:"):
            continue

        if not stripped:
            continue

    trimmed_text = "\n".join(result)
    trimmed_size = len(trimmed_text)
    kept_lines = len(result)

    ratio = (
        f"{original_size / 1024:.0f}KB → {trimmed_size / 1024:.0f}KB"
        if original_size > 1024
        else f"{original_size} → {trimmed_size} chars"
    )

    header = (
        f"[Trimmed Ansible log: {total_lines} → {kept_lines} lines, {ratio}. "
        f"Kept: PLAY/TASK headers, failures with context, PLAY RECAP, timing summary. "
        f"Stripped: ok/skipping/changed lines, timestamps, retries, JSON blobs.]"
    )

    return f"{header}\n\n{trimmed_text}"
