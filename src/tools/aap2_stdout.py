"""Ansible stdout parser for extracting failing tasks."""

import json
import logging
import re

logger = logging.getLogger(__name__)


def _parse_error_json(json_str: str) -> str:
    """Parse error message from JSON blob."""
    try:
        error_data = json.loads(json_str)
        return error_data.get("msg") or error_data.get("message") or json.dumps(error_data)
    except Exception:
        return json_str


def _find_task_context(lines: list[str], fail_index: int) -> tuple[str, str | None, str | None]:
    """Look backwards from a failure line to find TASK name and role.

    Returns (task_name, role_fqcn, file_path).
    """
    task_name = "Unknown task"
    role_fqcn = None
    file_path = None

    for j in range(fail_index - 1, -1, -1):
        prev_line = lines[j]

        task_match = re.search(r"TASK\s*\[([^\]]+)\]", prev_line)
        if task_match:
            task_content = task_match.group(1)
            colon_index = task_content.find(" : ")
            if colon_index != -1:
                role_fqcn = task_content[:colon_index].strip()
                task_name = task_content[colon_index + 3 :].strip()
            else:
                task_name = task_content.strip()
            break

        path_match = re.search(r"task path:\s*(.+?)(?::\d+)?$", prev_line)
        if path_match:
            file_path = path_match.group(1).strip()

    return task_name, role_fqcn, file_path


def _extract_fatal_failed_task(lines: list[str]) -> dict | None:
    """Extract failing task from fatal/failed lines."""
    for i, line in enumerate(lines):
        fail_match = re.match(r"^(fatal|failed):\s*\[([^\]]+)\].*?=>\s*(\{.*\})", line)
        if not fail_match:
            continue

        host_pattern = fail_match.group(2).strip()
        error_message = _parse_error_json(fail_match.group(3))
        task_name, role_fqcn, file_path = _find_task_context(lines, i)

        return {
            "taskName": task_name,
            "roleFqcn": role_fqcn,
            "module": None,
            "errorMessage": error_message,
            "hostPattern": host_pattern,
            "filePath": file_path,
        }
    return None


def _extract_error_bracket(lines: list[str]) -> dict | None:
    """Extract error from [ERROR]: lines."""
    for line in lines:
        error_bracket = re.search(r"\[ERROR\]:\s*(.+)", line)
        if error_bracket:
            return {
                "taskName": "Ansible error",
                "roleFqcn": None,
                "module": None,
                "errorMessage": error_bracket.group(1).strip(),
                "hostPattern": None,
                "filePath": None,
            }
    return None


def _extract_error_bang(lines: list[str]) -> dict | None:
    """Extract error from ERROR! lines."""
    for line in lines:
        if line.strip().startswith("ERROR!"):
            error_message = line[line.find("ERROR!") + 6 :].strip()
            return {
                "taskName": "Ansible parse error",
                "roleFqcn": None,
                "module": None,
                "errorMessage": error_message,
                "hostPattern": None,
                "filePath": None,
            }
    return None


def extract_failing_task(stdout: str) -> dict | None:
    """Extract the first failing task from Ansible stdout.

    Handles multiple failure formats:
      - fatal: [host]: FAILED! => {...}
      - failed: [host] (item=...) => {...}
      - [ERROR]: Task failed: ...
      - ERROR! ...
    """
    lines = stdout.split("\n")
    return (
        _extract_fatal_failed_task(lines)
        or _extract_error_bracket(lines)
        or _extract_error_bang(lines)
    )
