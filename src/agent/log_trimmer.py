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

_PRIMARY_ERROR_KEYS = {"msg", "stderr"}

MAX_LINE_CHARS = 1500
MAX_FATAL_LINE_CHARS = 20000
MAX_ERROR_FIELD_CHARS = 12000
MAX_SECONDARY_FIELD_CHARS = 1500

_NOISE_PREFIXES = ("skipping:", "changed:", "included:", "ASYNC ", "[WARNING]:")


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
            limit = (
                MAX_ERROR_FIELD_CHARS if key in _PRIMARY_ERROR_KEYS else MAX_SECONDARY_FIELD_CHARS
            )
            text = str(val)[:limit]
            parts.append(f'"{key}": {text!r}')

    if "cmd" in data:
        cmd = data["cmd"]
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        parts.append(f'"cmd": {str(cmd)[:300]!r}')

    return ", ".join(parts) if parts else None


def _extract_k8s_pod_status(line: str) -> str | None:
    """Extract container status summary from a K8s pod resource in a fatal line.

    Pod failure lines from k8s_info often contain the entire pod YAML including
    managedFields, network metadata, etc.  This extracts just the diagnostic
    parts: pod phase, conditions, and container statuses (waiting reasons,
    restart counts, termination messages).
    """
    start = line.find("=> {")
    if start == -1:
        return None

    json_str = line[start + 3 :].strip()
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None

    resources = data.get("resources")
    if not isinstance(resources, list) or not resources:
        return None

    pod = resources[0]
    if not isinstance(pod, dict) or pod.get("kind") != "Pod":
        return None

    status = pod.get("status", {})
    if not isinstance(status, dict):
        return None

    parts: list[str] = []

    phase = status.get("phase", "")
    if phase:
        parts.append(f"Pod Phase: {phase}")

    for cond in status.get("conditions", []):
        if isinstance(cond, dict) and cond.get("status") != "True":
            ctype = cond.get("type", "?")
            msg = str(cond.get("message", "N/A"))[:300]
            parts.append(f"  Condition {ctype}: {cond.get('status')} — {msg}")

    for label, key in [("Init", "initContainerStatuses"), ("Container", "containerStatuses")]:
        for cs in status.get(key, []):
            if not isinstance(cs, dict):
                continue
            name = cs.get("name", "?")
            ready = cs.get("ready", False)
            restarts = cs.get("restartCount", 0)
            state_info = ""
            for state_type, state_data in cs.get("state", {}).items():
                if isinstance(state_data, dict):
                    reason = state_data.get("reason", "")
                    message = str(state_data.get("message", ""))[:500]
                    state_info = state_type
                    if reason:
                        state_info += f"({reason})"
                    if message:
                        state_info += f": {message}"
            if not ready or restarts > 0:
                parts.append(
                    f"  {label} [{name}]: ready={ready}, restarts={restarts}, state={state_info}"
                )

    return "\n".join(parts) if parts else None


def _truncate_line(line: str, *, is_fatal: bool = False) -> str:
    """Truncate a long line, preserving extracted error fields.

    Fatal/error lines get a much larger budget so diagnostic content
    (e.g. pod YAML dumps, multi-line error messages) isn't lost.
    For K8s pod failures, extracts container status as a fallback.
    """
    max_chars = MAX_FATAL_LINE_CHARS if is_fatal else MAX_LINE_CHARS
    if len(line) <= max_chars:
        return line

    errors = _extract_json_errors(line)

    k8s_status = None
    if is_fatal and not errors:
        k8s_status = _extract_k8s_pod_status(line)

    prefix_len = min(2000, max_chars // 3) if is_fatal else 800
    prefix = line[:prefix_len]
    if errors:
        return f"{prefix}... [extracted: {errors}] [truncated from {len(line):,} chars]"
    if k8s_status:
        return f"{prefix}...\n[K8s Pod Status:\n{k8s_status}]\n[truncated from {len(line):,} chars]"
    return f"{prefix}... [truncated from {len(line):,} chars]"


def _find_fatal_context(lines: list[str]) -> set[int]:
    """Find line indices near fatal/failed lines (2 before, 3 after)."""
    total = len(lines)
    indices: set[int] = set()
    for i, line in enumerate(lines):
        if "fatal:" in line or "FAILED!" in line or line.lstrip().startswith("failed:"):
            for j in range(max(0, i - 2), min(total, i + 4)):
                indices.add(j)
    return indices


def _find_recap_start(lines: list[str]) -> int | None:
    """Find the PLAY RECAP / TASKS RECAP section start index."""
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("PLAY RECAP") or stripped.startswith("TASKS RECAP"):
            return i
    return None


def _is_noise_line(stripped: str) -> bool:
    """Return True for lines that should always be stripped."""
    if not stripped:
        return True
    if any(stripped.startswith(p) for p in _NOISE_PREFIXES):
        return True
    if "[WARNING]" in stripped or "[DEPRECATION WARNING]" in stripped:
        return True
    return bool(_TIMESTAMP_RE.match(stripped))


def _is_unconditional_keep(stripped: str) -> bool:
    """Return True for non-structural lines that should always be kept."""
    if stripped == "...ignoring":
        return True
    if "NO MORE HOSTS LEFT" in stripped:
        return True
    return stripped.startswith(("Vault password", "Pausing for"))


def _track_retry(prev_was_retry: bool, retry_count: int) -> tuple[bool, int]:
    """Track a retry occurrence. Returns updated (prev_was_retry, retry_count)."""
    if not prev_was_retry:
        return True, 1
    return True, retry_count + 1


def _format_trimmed_output(result: list[str], total_lines: int, original_size: int) -> str:
    """Build the final output with a metadata header."""
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


def trim_ansible_log(content: str) -> str:
    """Trim an Ansible job log, keeping only investigation-relevant lines.

    Returns the trimmed content with a metadata header showing compression.
    """
    lines = content.splitlines()
    original_size = len(content)

    fatal_indices = _find_fatal_context(lines)
    recap_start = _find_recap_start(lines)

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

        if stripped.startswith(("PLAY [", "TASK [")):
            result.append(line)
            prev_was_retry = False
            continue

        if i in fatal_indices:
            retry_match_in_fatal = _RETRY_RE.search(stripped)
            if retry_match_in_fatal:
                prev_was_retry, retry_count = _track_retry(prev_was_retry, retry_count)
                continue
            if prev_was_retry and retry_count > 0:
                result.append(f"  [... retried {retry_count} times before failing]")
                prev_was_retry = False
                retry_count = 0
            result.append(_truncate_line(line, is_fatal=True))
            continue

        if _is_unconditional_keep(stripped):
            result.append(line)
            continue

        retry_match = _RETRY_RE.search(stripped)
        if retry_match:
            retries_left = int(retry_match.group(1))
            prev_was_retry, retry_count = _track_retry(prev_was_retry, retry_count)
            if retries_left <= 1:
                result.append(f"  [... retried {retry_count} times before failing]")
                prev_was_retry = False
                retry_count = 0
            continue

        if prev_was_retry:
            prev_was_retry = False
            retry_count = 0

        if stripped.startswith("ok:"):
            if '"msg"' in stripped and len(stripped) < 500:
                result.append(line)
            continue

        if _is_noise_line(stripped):
            continue

    return _format_trimmed_output(result, len(lines), original_size)
