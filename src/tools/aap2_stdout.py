"""Ansible stdout parser for extracting failing tasks."""

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_failing_task(stdout: str) -> dict | None:
    """Extract the first failing task from Ansible stdout.

    Handles multiple failure formats:
      - fatal: [host]: FAILED! => {...}
      - failed: [host] (item=...) => {...}
      - [ERROR]: Task failed: ...
      - ERROR! ...
    """
    lines = stdout.split("\n")

    # Try fatal: and failed: blocks
    for i, line in enumerate(lines):
        fail_match = re.match(r"^(fatal|failed):\s*\[([^\]]+)\].*?=>\s*(\{.*\})", line)
        if not fail_match:
            continue

        host_pattern = fail_match.group(2).strip()

        # Extract error message from JSON blob
        error_message = "Task failed"
        try:
            error_data = json.loads(fail_match.group(3))
            error_message = (
                error_data.get("msg") or error_data.get("message") or json.dumps(error_data)
            )
        except Exception:
            error_message = fail_match.group(3)

        # Look backwards for TASK line to get task name and role
        task_name = "Unknown task"
        role_fqcn = None
        file_path = None

        for j in range(i - 1, -1, -1):
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

        return {
            "taskName": task_name,
            "roleFqcn": role_fqcn,
            "module": None,
            "errorMessage": error_message,
            "hostPattern": host_pattern,
            "filePath": file_path,
        }

    # Try [ERROR]: lines
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

    # Try ERROR! lines
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
