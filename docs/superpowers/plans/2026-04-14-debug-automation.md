# Debug Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port AAP2 debug automation from demolition into parsec as a dedicated non-chat page accessible via a "Debug Automation" sidebar tab.

**Architecture:** Three new backend modules (`aap2_debug.py`, `aap2_stdout.py`, `aap2_fix.py`) ported from demolition's Python services, wired to parsec's existing AAP2 connection layer. One new FastAPI router (`routes/debug.py`). Frontend is vanilla HTML/CSS/JS added to parsec's existing static files — no build step, no framework.

**Tech Stack:** Python/FastAPI (backend), vanilla HTML/CSS/JS (frontend), httpx via `src/connections/aap2.py` (AAP2 API), Anthropic SDK (AI fix fallback)

**Security note:** All frontend rendering uses an `escHtml()` helper that escapes via `textContent` before insertion into `innerHTML`. This matches the existing pattern in parsec's `app.js`. No user-controlled data is inserted without escaping.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/tools/aap2_stdout.py` | Create | Parse Ansible stdout to extract failing task |
| `src/tools/aap2_fix.py` | Create | Pattern-match fixes + AI fallback |
| `src/tools/aap2_debug.py` | Create | Debug orchestrator (metadata, stdout, project, correlation, EE) |
| `src/routes/debug.py` | Create | FastAPI router with 3 POST endpoints |
| `src/app.py` | Modify | Register debug router |
| `static/index.html` | Modify | Add debug sidebar tab + debug view container |
| `static/style.css` | Modify | Add debug view styles |
| `static/app.js` | Modify | Add debug view logic |
| `tests/test_aap2_stdout.py` | Create | Unit tests for stdout parser |
| `tests/test_aap2_fix.py` | Create | Unit tests for pattern matching |
| `tests/test_aap2_debug.py` | Create | Unit tests for URL parsing and controller resolution |

---

### Task 1: Ansible stdout parser (`aap2_stdout.py`)

**Files:**
- Create: `src/tools/aap2_stdout.py`
- Create: `tests/test_aap2_stdout.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aap2_stdout.py`:

```python
"""Tests for Ansible stdout parser."""

from src.tools.aap2_stdout import extract_failing_task


def test_fatal_failed_with_json():
    stdout = (
        'TASK [agnosticd.osp_on_ocp.ocp4_setup : Create namespace] ***\n'
        'fatal: [bastion.abc12.sandbox1234.opentlc.com]: FAILED! => '
        '{"msg": "Failed to create namespace", "rc": 1}\n'
    )
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Create namespace"
    assert result["roleFqcn"] == "agnosticd.osp_on_ocp.ocp4_setup"
    assert result["hostPattern"] == "bastion.abc12.sandbox1234.opentlc.com"
    assert "Failed to create namespace" in result["errorMessage"]


def test_fatal_without_role():
    stdout = (
        'TASK [Install packages] ***\n'
        'fatal: [host1]: FAILED! => {"msg": "No package matching found"}\n'
    )
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Install packages"
    assert result["roleFqcn"] is None
    assert result["hostPattern"] == "host1"


def test_error_bracket():
    stdout = '[ERROR]: Task failed: cannot find role "missing_role"\n'
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Ansible error"
    assert "missing_role" in result["errorMessage"]


def test_error_bang():
    stdout = 'ERROR! No inventory was parsed\n'
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Ansible parse error"
    assert "No inventory was parsed" in result["errorMessage"]


def test_no_failure():
    stdout = 'TASK [Do something] ***\nok: [host1]\nPLAY RECAP ***\n'
    result = extract_failing_task(stdout)
    assert result is None


def test_empty_stdout():
    assert extract_failing_task("") is None


def test_failed_loop_item():
    stdout = (
        'TASK [agnosticd.core.setup : Verify DNS] ***\n'
        'failed: [host1] (item=api.cluster.example.com) => '
        '{"msg": "DNS lookup failed", "item": "api.cluster.example.com"}\n'
    )
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Verify DNS"
    assert result["roleFqcn"] == "agnosticd.core.setup"
    assert "DNS lookup failed" in result["errorMessage"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/test_aap2_stdout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.tools.aap2_stdout'`

- [ ] **Step 3: Implement the stdout parser**

Create `src/tools/aap2_stdout.py`:

```python
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
                error_data.get("msg")
                or error_data.get("message")
                or json.dumps(error_data)
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
                    task_name = task_content[colon_index + 3:].strip()
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
            error_message = line[line.find("ERROR!") + 6:].strip()
            return {
                "taskName": "Ansible parse error",
                "roleFqcn": None,
                "module": None,
                "errorMessage": error_message,
                "hostPattern": None,
                "filePath": None,
            }

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/test_aap2_stdout.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/parsec
git add src/tools/aap2_stdout.py tests/test_aap2_stdout.py
git commit -m "feat: add Ansible stdout parser for debug automation"
```

---

### Task 2: Fix recommendation engine (`aap2_fix.py`)

**Files:**
- Create: `src/tools/aap2_fix.py`
- Create: `tests/test_aap2_fix.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aap2_fix.py`:

```python
"""Tests for AAP2 fix recommendation engine (pattern matching only)."""

from src.tools.aap2_fix import match_pattern


def test_match_invalid_client_token():
    result = match_pattern("InvalidClientTokenId: The security token included in the request is invalid")
    assert result is not None
    assert result["source"] == "pattern"
    assert "credential" in result["explanation"].lower()


def test_match_private_data_dir():
    result = match_pattern("unrecognized arguments: --private-data-dir /runner/artifacts")
    assert result is not None
    assert result["source"] == "pattern"
    assert "entrypoint" in result["explanation"].lower()


def test_match_json_format():
    result = match_pattern("configuration string is not in JSON format")
    assert result is not None
    assert result["source"] == "pattern"


def test_match_role_not_found():
    result = match_pattern("ERROR! the role 'agnosticd.missing_role' was not found")
    assert result is not None
    assert result["source"] == "pattern"
    assert "collection" in result["explanation"].lower()


def test_match_worker_stream():
    result = match_pattern("Failed to JSON parse a line from worker stream")
    assert result is not None
    assert result["source"] == "pattern"


def test_no_match():
    result = match_pattern("Some random unrecognized error message")
    assert result is None


def test_catalog_item_substitution():
    result = match_pattern(
        "InvalidClientTokenId",
        extra_vars={"catalog_item": "ocp4-cluster", "account": "agd-v2"},
        job_template_name="RHPDS agd-v2.ocp4-cluster.prod-abc12-1-provision x",
    )
    assert result is not None
    assert "agd-v2/ocp4-cluster" in result["file"]


def test_extract_catalog_item_from_template_name():
    from src.tools.aap2_fix import extract_catalog_item_path

    path = extract_catalog_item_path({}, "RHPDS agd-v2.sovereign-cloud.prod-abc12-1-provision x")
    assert path == "agd_v2/sovereign-cloud"


def test_extract_catalog_item_from_extra_vars():
    from src.tools.aap2_fix import extract_catalog_item_path

    path = extract_catalog_item_path({"catalog_item": "ocp4-cluster", "account": "openshift_cnv"})
    assert path == "openshift_cnv/ocp4-cluster"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/test_aap2_fix.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.tools.aap2_fix'`

- [ ] **Step 3: Implement the fix recommendation engine**

Create `src/tools/aap2_fix.py`:

```python
"""Fix recommendation engine for AAP2 job failures."""

import json
import logging
import os
import re

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)

ANSIBLE_BEST_PRACTICES = """
CRITICAL: All modules in before/after code MUST use FQCN (Fully Qualified Collection Name).
  CORRECT: ansible.builtin.uri, ansible.builtin.raw, kubernetes.core.k8s
  WRONG: uri, raw, k8s
- Use bracket notation for variable access (item['key'] not item.key)
- Always quote template expressions: "{{ variable }}"
- Use | default() filter for optional variables
- Tasks should be idempotent
- Use block/rescue/always for error handling, not ignore_errors
""".strip()


class PatternMatch:
    """Pattern matching configuration for known errors."""

    def __init__(
        self,
        pattern: re.Pattern,
        explanation: str,
        repo: str,
        file: str,
        before: str | None = None,
        after: str | None = None,
    ):
        self.pattern = pattern
        self.explanation = explanation
        self.repo = repo
        self.file = file
        self.before = before
        self.after = after


KNOWN_PATTERNS: list[PatternMatch] = [
    PatternMatch(
        pattern=re.compile(
            r"InvalidClientTokenId|security token included in the request is invalid",
            re.IGNORECASE,
        ),
        explanation=(
            "Expired or invalid AWS credential. Check the credential configured in "
            "AAP (Settings > Credentials) or the sandbox configuration in the agnosticv "
            "catalog item (common.yaml or env overlay). If using sandbox-api, verify "
            "the sandbox allocation succeeded."
        ),
        repo="rhpds/agnosticv",
        file="<catalog_item>/common.yaml",
    ),
    PatternMatch(
        pattern=re.compile(r"unrecognized arguments:.*--private-data-dir", re.IGNORECASE),
        explanation=(
            "EE entrypoint bug — the chained entrypoint does not detect AAP container "
            "group mode (ansible-runner worker). It falls into the wrong code path and "
            "passes runner args to ansible-playbook. Fix: detect KUBERNETES_SERVICE_HOST "
            "env var in entrypoint.sh and install the ansible-playbook wrapper."
        ),
        repo="agnosticd/agnosticd-v2",
        file="tools/execution_environments/ee-multicloud-public/entrypoint.sh",
    ),
    PatternMatch(
        pattern=re.compile(r"configuration string is not in JSON format", re.IGNORECASE),
        explanation=(
            "Python dict literal string piped through to_json filter. In ansible-core "
            "2.19+, this produces a quoted string instead of a JSON object. Convert the "
            "Python dict literal to a proper YAML dict."
        ),
        repo="agnosticd/cloud_provider_openshift_cnv",
        file="<role>/tasks/<task>.yaml",
        before='some_var: "{{ python_dict_string | to_json }}"',
        after='some_var: "{{ proper_yaml_dict | to_json }}"',
    ),
    PatternMatch(
        pattern=re.compile(r"role '.*' was not found", re.IGNORECASE),
        explanation=(
            "Ansible collection role not found. Either the collection was not installed "
            "(check dynamic dependency installation in stdout) or the collections path is "
            "misconfigured. Verify ansible.cfg collections_path includes "
            "/runner/requirements_collections."
        ),
        repo="agnosticd/agnosticd-v2",
        file="ansible.cfg",
    ),
    PatternMatch(
        pattern=re.compile(r"Failed to JSON parse a line from worker stream", re.IGNORECASE),
        explanation=(
            "EE/runner infrastructure failure. The ansible-runner worker process produced "
            "invalid JSON on its communication stream. Check job_explanation for the "
            "specific invalid line. Common causes: entrypoint crash, image pull failure, "
            "receptor communication issue."
        ),
        repo="agnosticd/agnosticd-v2",
        file="tools/execution_environments/ee-multicloud-public/entrypoint.sh",
    ),
]


def extract_catalog_item_path(
    extra_vars: dict, job_template_name: str | None = None
) -> str | None:
    """Extract catalog item path from extra_vars or job template name."""
    catalog_item = extra_vars.get("catalog_item")
    account = extra_vars.get("account")
    if catalog_item and account:
        return f"{account}/{catalog_item}"

    if job_template_name:
        m = re.match(r"^RHPDS\s+([^.]+)\.([^.]+)\.", job_template_name)
        if m:
            acct = m.group(1).replace("-", "_")
            item = m.group(2)
            return f"{acct}/{item}"

    return None


def match_pattern(
    error_message: str,
    extra_vars: dict | None = None,
    job_template_name: str | None = None,
) -> dict | None:
    """Match error message against known patterns."""
    for pm in KNOWN_PATTERNS:
        if pm.pattern.search(error_message):
            file = pm.file
            repo = pm.repo
            github_url = f"https://github.com/{repo}"

            if extra_vars and "<catalog_item>" in file:
                catalog_path = extract_catalog_item_path(extra_vars, job_template_name)
                if catalog_path:
                    file = file.replace("<catalog_item>", catalog_path)
                    github_url = (
                        f"https://github.com/{repo}/blob/master/"
                        f"{catalog_path}/common.yaml"
                    )

            return {
                "source": "pattern",
                "file": file,
                "repo": repo,
                "line": None,
                "before": pm.before,
                "after": pm.after,
                "explanation": pm.explanation,
                "githubUrl": github_url,
                "lintWarning": None,
            }
    return None


async def _fetch_source_file(repo: str, path: str, ref: str = "main") -> str | None:
    """Fetch a source file from GitHub for AI context."""
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                lines = resp.text.split("\n")
                numbered = "\n".join(f"{i + 1:4d}: {line}" for i, line in enumerate(lines))
                return numbered
    except Exception as e:
        logger.debug("Failed to fetch %s/%s: %s", repo, path, e)
    return None


async def ai_analyze_fix(
    failing_task: dict,
    extra_vars: dict | None = None,
    job_template_name: str | None = None,
) -> dict | None:
    """Use AI to analyze a failing task and recommend a fix.

    Uses parsec's Anthropic config (supports direct API, Vertex AI, Bedrock).
    """
    cfg = get_config()
    backend = cfg.anthropic.get("backend", "api")
    model = cfg.anthropic.get("model", "claude-sonnet-4-6")

    # Filter sensitive vars
    safe_vars = {}
    if extra_vars:
        for k, v in extra_vars.items():
            if any(s in k for s in ("password", "secret", "token", "key")):
                continue
            if isinstance(v, str) and len(v) > 200:
                continue
            safe_vars[k] = v

    # Derive repo + file path from role FQCN and fetch source
    role_repo_hint = ""
    source_section = ""
    role_fqcn = failing_task.get("roleFqcn") or ""
    if role_fqcn:
        parts = role_fqcn.split(".")
        if len(parts) >= 3 and parts[0] == "agnosticd":
            collection_fqcn = parts[1]
            collection_hyphen = collection_fqcn.replace("_", "-")
            role_name = ".".join(parts[2:])
            role_file = f"roles/{role_name}/tasks/main.yml"

            repo_name = collection_fqcn
            source_content = await _fetch_source_file(
                f"agnosticd/{collection_fqcn}", role_file
            )
            if not source_content and collection_hyphen != collection_fqcn:
                source_content = await _fetch_source_file(
                    f"agnosticd/{collection_hyphen}", role_file
                )
                if source_content:
                    repo_name = collection_hyphen

            role_repo_hint = (
                f'\nIMPORTANT: The role "{role_fqcn}" is in '
                f'GitHub repo "agnosticd/{repo_name}".\n'
                f'The file path is "{role_file}".\n'
                f'Use repo "agnosticd/{repo_name}" in your response, '
                f'NOT "redhat-cop/agnosticd".\n'
            )
            if source_content:
                source_section = (
                    f"\nSource file "
                    f"(agnosticd/{repo_name}/{role_file}):\n"
                    f"```yaml\n{source_content[:3000]}\n```\n"
                    f"Reference line numbers from this source "
                    f"in your response.\n"
                )
        elif len(parts) >= 2:
            ns = parts[0]
            collection = parts[1]
            role_name = ".".join(parts[2:]) if len(parts) > 2 else ""
            role_repo_hint = (
                f'\nThe role "{role_fqcn}" is in namespace "{ns}", '
                f'collection "{collection}".\n'
                f'The file path is likely "roles/{role_name}/tasks/main.yml".\n'
                f'Try repo "{ns}/{collection}" on GitHub.\n'
            )

    prompt = f"""You are diagnosing a failed Ansible task from an AAP2 job.
Analyze the failure and recommend a fix.

Job template: {job_template_name or "unknown"}
Action: {(extra_vars or {{}}).get("ACTION", "unknown")}

Failing task:
  Name: {failing_task.get("taskName", "unknown")}
  Role: {failing_task.get("roleFqcn") or "none"}
  Module: {failing_task.get("module") or "unknown"}
  Host: {failing_task.get("hostPattern") or "unknown"}
  Error: {(failing_task.get("errorMessage") or "")[:1000]}
{role_repo_hint}{source_section}
Relevant extra_vars:
{json.dumps(safe_vars, indent=2)[:1500]}

Ansible best practices:
{ANSIBLE_BEST_PRACTICES}

Respond in this exact JSON format (no markdown, no explanation outside the JSON):
{{{{
  "file": "path/to/file/to/fix.yml",
  "repo": "org/repo-name",
  "line": null or the line number where the problematic code starts (integer),
  "explanation": "Clear explanation of the root cause and what to change",
  "before": "the ACTUAL problematic code as it exists today (1-3 lines, or null if not a code fix)",
  "after": "the corrected code with the fix applied, use FQCN for all modules (1-3 lines, or null)"
}}}}

If the issue is environmental (infrastructure, timing, resource limits),
set file to "N/A" and explain in the explanation field.
The "before" field must show real code from the source, not a summary.
The "after" field must show the corrected version with FQCN."""

    try:
        import anthropic

        if backend == "vertex":
            from anthropic import AnthropicVertex
            from google.oauth2 import service_account

            project_id = cfg.anthropic.get("vertex_project_id", "") or cfg.gcp.get(
                "project_id", ""
            )
            region = cfg.anthropic.get("vertex_region", "us-east5")
            kwargs: dict = {"project_id": project_id, "region": region}
            creds_path = cfg.anthropic.get("vertex_credentials_path", "")
            if creds_path and os.path.isfile(creds_path):
                credentials = service_account.Credentials.from_service_account_file(
                    creds_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                kwargs["credentials"] = credentials
            client = AnthropicVertex(**kwargs)
        elif backend == "bedrock":
            from anthropic import AnthropicBedrock

            region = cfg.anthropic.get("bedrock_region", "us-east-1")
            client = AnthropicBedrock(aws_region=region)
        else:
            api_key = cfg.anthropic.get("api_key", "") or os.environ.get(
                "ANTHROPIC_API_KEY", ""
            )
            if not api_key:
                logger.info("AI not available: no API key configured")
                return None
            client = anthropic.Anthropic(api_key=api_key)

        logger.info("Running AI fix analysis (backend=%s)...", backend)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = "".join(b.text for b in response.content if hasattr(b, "text"))

        # Strip markdown code fences if present
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*$", "", text)

        json_match = re.search(r"\{[\s\S]*\}", text)
        if not json_match:
            logger.warning("AI response did not contain JSON: %s", text[:200])
            return {
                "source": "ai",
                "file": "N/A",
                "repo": "unknown",
                "line": None,
                "before": None,
                "after": None,
                "explanation": f"AI analysis returned non-JSON response: {text[:500]}",
                "githubUrl": "",
                "lintWarning": None,
            }

        parsed = json.loads(json_match.group(0))
        logger.info("AI fix: %s", parsed.get("explanation", "")[:100])

        line_num = parsed.get("line")
        line_suffix = f"#L{line_num}" if isinstance(line_num, int) else ""
        file_val = parsed.get("file", "N/A")
        repo_val = parsed.get("repo", "unknown")

        return {
            "source": "ai",
            "file": file_val,
            "repo": repo_val,
            "line": line_num if isinstance(line_num, int) else None,
            "before": parsed.get("before"),
            "after": parsed.get("after"),
            "explanation": parsed.get("explanation", ""),
            "githubUrl": (
                f"https://github.com/{repo_val}/blob/main/{file_val}{line_suffix}"
                if file_val and file_val != "N/A"
                else f"https://github.com/{repo_val}"
            ),
            "lintWarning": None,
        }
    except Exception as e:
        logger.warning("AI fix analysis failed: %s", e)
        return None


async def recommend_fix(
    failing_task: dict,
    extra_vars: dict | None = None,
    job_template_name: str | None = None,
) -> dict | None:
    """Recommend a fix: pattern match first, AI fallback second."""
    fix = match_pattern(
        failing_task.get("errorMessage", ""),
        extra_vars=extra_vars,
        job_template_name=job_template_name,
    )
    if fix:
        return fix

    return await ai_analyze_fix(failing_task, extra_vars, job_template_name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/test_aap2_fix.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/parsec
git add src/tools/aap2_fix.py tests/test_aap2_fix.py
git commit -m "feat: add fix recommendation engine for debug automation"
```

---

### Task 3: Debug orchestrator (`aap2_debug.py`)

**Files:**
- Create: `src/tools/aap2_debug.py`
- Create: `tests/test_aap2_debug.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aap2_debug.py`:

```python
"""Tests for AAP2 debug orchestrator (URL parsing and controller resolution)."""

import pytest

from src.tools.aap2_debug import parse_job_url


def test_parse_hash_fragment_url():
    controller, job_id = parse_job_url(
        "https://aap2-prod.example.com/#/jobs/playbook/12345/output"
    )
    assert controller == "https://aap2-prod.example.com"
    assert job_id == 12345


def test_parse_api_url():
    controller, job_id = parse_job_url(
        "https://aap2-prod.example.com/api/v2/jobs/67890/"
    )
    assert controller == "https://aap2-prod.example.com"
    assert job_id == 67890


def test_parse_command_job():
    controller, job_id = parse_job_url(
        "https://controller.example.com/#/jobs/command/999"
    )
    assert controller == "https://controller.example.com"
    assert job_id == 999


def test_parse_with_query_params():
    controller, job_id = parse_job_url(
        "https://controller.example.com/#/jobs/playbook/555?tab=output"
    )
    assert controller == "https://controller.example.com"
    assert job_id == 555


def test_parse_invalid_url():
    with pytest.raises(ValueError, match="Could not extract job ID"):
        parse_job_url("https://example.com/not-a-job-url")


def test_parse_garbage():
    with pytest.raises(ValueError):
        parse_job_url("not a url at all")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/test_aap2_debug.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.tools.aap2_debug'`

- [ ] **Step 3: Implement the debug orchestrator**

Create `src/tools/aap2_debug.py`:

```python
"""AAP2 debug orchestrator — fetches job metadata, traces failures, recommends fixes."""

import json
import logging
import re
from urllib.parse import urlparse

import httpx

from src.connections.aap2 import api_get, api_get_text, resolve_controller

logger = logging.getLogger(__name__)


def parse_job_url(url: str) -> tuple[str, int]:
    """Parse AAP2 job URL and extract controller base URL and job ID.

    Supported formats:
    - https://controller/#/jobs/playbook/12345
    - https://controller/api/v2/jobs/12345/
    - https://controller/#/jobs/command/12345?tab=output
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid AAP2 job URL: {e}") from e

    controller_url = f"{parsed.scheme}://{parsed.netloc}"

    # Try hash fragment format: /#/jobs/playbook/12345
    if parsed.fragment:
        hash_match = re.search(
            r"/jobs/(?:playbook|command|inventory|project)/(\d+)",
            parsed.fragment,
        )
        if hash_match:
            return controller_url, int(hash_match.group(1))

    # Try API format: /api/v2/jobs/12345/
    path_match = re.search(r"/api/v2/jobs/(\d+)", parsed.path)
    if path_match:
        return controller_url, int(path_match.group(1))

    raise ValueError(f"Could not extract job ID from URL: {url}")


def find_controller_for_url(url: str) -> str:
    """Match a controller URL against parsec's configured controllers.

    Extracts the hostname and delegates to resolve_controller().
    Returns the cluster name.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    return resolve_controller(hostname)


async def fetch_job_metadata(cluster_name: str, job_id: int) -> dict:
    """Fetch job metadata from AAP2 controller."""
    data = await api_get(cluster_name, f"/api/v2/jobs/{job_id}/")

    extra_vars: dict = {}
    raw_ev = data.get("extra_vars")
    if isinstance(raw_ev, str) and raw_ev:
        try:
            extra_vars = json.loads(raw_ev)
        except (json.JSONDecodeError, TypeError):
            pass
    elif isinstance(raw_ev, dict):
        extra_vars = raw_ev

    action = (
        extra_vars.get("ACTION", "unknown")
        if isinstance(extra_vars.get("ACTION"), str)
        else "unknown"
    )

    ee_val = data.get("execution_environment")
    return {
        "id": job_id,
        "status": data.get("status", "pending"),
        "action": action,
        "executionEnvironment": ee_val if isinstance(ee_val, int) else None,
        "instanceGroup": (
            str(data["instance_group"]) if isinstance(data.get("instance_group"), int) else None
        ),
        "executionNode": data.get("execution_node") or None,
        "jobExplanation": data.get("job_explanation", "") or "",
        "resultTraceback": data.get("result_traceback", "") or "",
        "launchType": data.get("launch_type", "") or "",
        "jobTemplate": (
            data["job_template"] if isinstance(data.get("job_template"), int) else None
        ),
        "jobTemplateName": (
            (data.get("summary_fields") or {}).get("job_template", {}).get("name")
        ),
        "projectId": (
            data["project"] if isinstance(data.get("project"), int) else None
        ),
        "started": data.get("started") or None,
        "finished": data.get("finished") or None,
        "elapsed": data.get("elapsed") if isinstance(data.get("elapsed"), (int, float)) else 0,
        "extraVars": extra_vars,
    }


async def fetch_job_stdout(cluster_name: str, job_id: int) -> str:
    """Fetch job stdout as plain text."""
    try:
        return await api_get_text(
            cluster_name,
            f"/api/v2/jobs/{job_id}/stdout/",
            {"format": "txt"},
        )
    except Exception as e:
        logger.warning("Failed to fetch stdout for job %d: %s", job_id, e)
        return ""


async def fetch_project_info(cluster_name: str, project_id: int) -> dict:
    """Fetch project SCM details."""
    data = await api_get(cluster_name, f"/api/v2/projects/{project_id}/")
    return {
        "scmUrl": data.get("scm_url", "") or "",
        "scmBranch": data.get("scm_branch", "") or "",
        "scmRevision": data.get("scm_revision", "") or "",
    }


async def fetch_correlation(cluster_name: str, job_id: int) -> dict:
    """Fetch correlation data — recent failures grouped by error, EE, instance group."""
    data = await api_get(
        cluster_name,
        "/api/v2/jobs/",
        {"status__in": "error,failed", "order_by": "-finished", "page_size": "50"},
    )

    failures = [
        job
        for job in data.get("results", [])
        if isinstance(job.get("id"), int) and job["id"] != job_id
    ]

    by_error: dict[str, list[int]] = {}
    by_ee: dict[int, list[int]] = {}
    by_ig: dict[str, list[int]] = {}

    for job in failures:
        jid = job.get("id")
        if not isinstance(jid, int):
            continue

        explanation = (job.get("job_explanation") or "")[:100]
        by_error.setdefault(explanation, []).append(jid)

        ee = job.get("execution_environment")
        if isinstance(ee, int):
            by_ee.setdefault(ee, []).append(jid)

        ig = job.get("instance_group")
        if isinstance(ig, int):
            by_ig.setdefault(str(ig), []).append(jid)

    return {
        "totalFailures": len(failures),
        "byError": [
            {"error": e, "count": len(ids), "jobIds": ids}
            for e, ids in by_error.items()
        ],
        "byEE": [
            {"image": str(ee_id), "count": len(ids), "jobIds": ids}
            for ee_id, ids in by_ee.items()
        ],
        "byInstanceGroup": [
            {"group": g, "count": len(ids), "jobIds": ids}
            for g, ids in by_ig.items()
        ],
    }


# Known EE image-name to source-directory mappings
_EE_NAME_MAP = {
    "ee-multicloud": "ee-multicloud-public",
    "ee-multicloud-public": "ee-multicloud-public",
    "ee-multicloud-private": "ee-multicloud-private",
    "ee-ansible-workshop": "ee-ansible-workshop",
}

_EE_SOURCE_FILES = [
    "Containerfile",
    "entrypoint.sh",
    "requirements.txt",
    "requirements.yml",
]


async def fetch_ee_info(cluster_name: str, ee_id: int) -> dict:
    """Fetch EE metadata and source definition files from GitHub."""
    data = await api_get(cluster_name, f"/api/v2/execution_environments/{ee_id}/")
    image = data.get("image", "") or ""

    result: dict = {
        "id": ee_id,
        "image": image,
        "sourceRepo": None,
        "sourceDir": None,
        "sourceFiles": [],
    }

    if not image:
        return result

    # Extract EE directory name from image URL
    name_tag = image.split("/")[-1]
    name = name_tag.split(":")[0]
    ee_dir = _EE_NAME_MAP.get(name, name)

    source_files = []
    for filename in _EE_SOURCE_FILES:
        path = f"tools/execution_environments/{ee_dir}/{filename}"
        url = f"https://raw.githubusercontent.com/agnosticd/agnosticd-v2/main/{path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    source_files.append({"name": filename, "content": resp.text[:5000]})
        except Exception:
            pass

    if source_files:
        result["sourceRepo"] = "agnosticd/agnosticd-v2"
        result["sourceDir"] = f"tools/execution_environments/{ee_dir}"
        result["sourceFiles"] = source_files

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/test_aap2_debug.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Run all tests together**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/test_aap2_stdout.py tests/test_aap2_fix.py tests/test_aap2_debug.py -v`
Expected: All 22 tests PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/parsec
git add src/tools/aap2_debug.py tests/test_aap2_debug.py
git commit -m "feat: add debug orchestrator for AAP2 job diagnosis"
```

---

### Task 4: FastAPI debug router (`routes/debug.py`)

**Files:**
- Create: `src/routes/debug.py`
- Modify: `src/app.py:21-99`

- [ ] **Step 1: Create the debug router**

Create `src/routes/debug.py`:

```python
"""AAP2 debug API endpoints."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.tools.aap2_debug import (
    fetch_correlation,
    fetch_ee_info,
    fetch_job_metadata,
    fetch_job_stdout,
    fetch_project_info,
    find_controller_for_url,
    parse_job_url,
)
from src.tools.aap2_fix import match_pattern, recommend_fix
from src.tools.aap2_stdout import extract_failing_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])


class DiagnoseRequest(BaseModel):
    url: str


class CorrelationRequest(BaseModel):
    url: str
    job_id: int


class EERequest(BaseModel):
    url: str
    job_id: int
    ee_id: int


@router.post("/diagnose")
async def diagnose(body: DiagnoseRequest):
    """Diagnose an AAP2 job failure (Phases 1-3 + fix).

    Auto-triggers Phase 5 for status=error.
    """
    try:
        controller_url, job_id = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)

        logger.info("Diagnosing job %d on controller %s", job_id, cluster_name)

        # Phase 1: Fetch job metadata
        metadata = await fetch_job_metadata(cluster_name, job_id)

        result: dict = {
            "metadata": metadata,
            "failingTask": None,
            "projectInfo": None,
            "fix": None,
            "eeInfo": None,
        }

        # Fetch project info for SCM ref
        if metadata.get("projectId"):
            try:
                result["projectInfo"] = await fetch_project_info(
                    cluster_name, metadata["projectId"]
                )
            except Exception as e:
                logger.warning("Failed to fetch project info: %s", e)

        # Phase 2: If failed, fetch stdout and extract failing task
        if metadata["status"] == "failed":
            stdout = await fetch_job_stdout(cluster_name, job_id)
            if stdout:
                failing_task = extract_failing_task(stdout)
                if failing_task:
                    result["failingTask"] = failing_task

                    # Phase 3: Recommend fix
                    fix = await recommend_fix(
                        failing_task,
                        extra_vars=metadata["extraVars"],
                        job_template_name=metadata.get("jobTemplateName"),
                    )
                    if fix:
                        result["fix"] = fix

        # Auto Phase 5 for status=error
        if metadata["status"] == "error":
            # Try pattern match on job_explanation
            if metadata["jobExplanation"]:
                fix = match_pattern(metadata["jobExplanation"])
                if fix:
                    result["fix"] = fix

            # Auto-fetch EE info
            if metadata["executionEnvironment"]:
                try:
                    result["eeInfo"] = await fetch_ee_info(
                        cluster_name, metadata["executionEnvironment"]
                    )
                except Exception as e:
                    logger.warning("EE inspection failed: %s", e)

        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Diagnosis failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/correlation")
async def correlation(body: CorrelationRequest):
    """Fetch correlation data for a job (Phase 4)."""
    try:
        controller_url, _ = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)
        return await fetch_correlation(cluster_name, body.job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("Correlation fetch failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/ee")
async def ee_info(body: EERequest):
    """Fetch execution environment info (Phase 5)."""
    try:
        controller_url, _ = parse_job_url(body.url)
        cluster_name = find_controller_for_url(controller_url)
        return await fetch_ee_info(cluster_name, body.ee_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("EE fetch failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
```

- [ ] **Step 2: Register the router in app.py**

In `src/app.py`, add the import alongside the existing router imports (after line 28, near the other `from src.routes...` imports):

```python
from src.routes.debug import router as debug_router
```

Add `app.include_router(debug_router)` after the existing `include_router` calls (after line 99):

```python
app.include_router(debug_router)
```

- [ ] **Step 3: Verify the app starts and the route is registered**

Run: `cd /Users/prutledg/parsec && python3 -c "from src.routes.debug import router; print('Routes:', [r.path for r in router.routes])"`
Expected: `Routes: ['/diagnose', '/correlation', '/ee']`

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/parsec
git add src/routes/debug.py src/app.py
git commit -m "feat: add debug API router with diagnose/correlation/ee endpoints"
```

---

### Task 5: Frontend — HTML structure and CSS

**Files:**
- Modify: `static/index.html:61-65`
- Modify: `static/style.css` (append)

- [ ] **Step 1: Add the debug sidebar tab and debug view container to index.html**

In `static/index.html`, add the debug tab button after the examples tab (around line 63):

```html
<button id="sidebar-tab-debug" class="sidebar-tab">Debug Automation &raquo;</button>
```

Add the debug view container inside `<div id="app">`, after `<footer>` and before the closing `</div>` of `#app` (around line 101). It's hidden by default:

```html
<div id="debug-view" class="debug-view" style="display: none;">
    <div class="debug-header">
        <h1>Debug Automation</h1>
        <p>Paste an AAP2 job URL to diagnose failures. Traces failures to source code and recommends fixes.</p>
    </div>
    <div class="debug-input-row">
        <input type="text" id="debug-url" placeholder="https://aap2-prod.example.com/#/jobs/playbook/12345/output" autocomplete="off">
        <button id="debug-diagnose-btn">Diagnose</button>
    </div>
    <div id="debug-error" class="debug-error" style="display: none;"></div>
    <div id="debug-loading" class="debug-loading" style="display: none;">
        <div class="debug-spinner"></div>
        <span>Diagnosing...</span>
    </div>
    <div id="debug-result" style="display: none;">
        <div id="debug-summary" class="debug-summary"></div>
        <div class="debug-tabs">
            <button class="debug-tab active" data-tab="triage">Triage</button>
            <button class="debug-tab" data-tab="failing-task">Failing Task</button>
            <button class="debug-tab" data-tab="fix">Recommended Fix</button>
            <button class="debug-tab" data-tab="correlation">Correlation</button>
            <button class="debug-tab" data-tab="ee-info">EE Info</button>
        </div>
        <div id="debug-tab-content" class="debug-tab-content"></div>
        <div id="debug-fix-preview" class="debug-fix-preview" style="display: none;"></div>
    </div>
</div>
```

- [ ] **Step 2: Add debug view CSS to style.css**

Append to `static/style.css`. See the complete CSS block in the spec at `docs/superpowers/specs/2026-04-14-debug-automation-design.md`, Frontend > Styling section. The CSS uses parsec's existing variables (`--bg`, `--bg-elevated`, `--bg-surface`, `--text`, `--text-bright`, `--text-muted`, `--accent`, `--accent-bright`, `--accent-dim`, `--accent-glow`, `--border`, `--border-subtle`, `--success`, `--error`, `--warning`, `--font-mono`, `--font-sans`, `--radius-sm`, `--radius-md`, `--transition`).

Key selectors to add:
- `.debug-view` — flex column, padding, max-width 960px
- `.debug-header h1`, `.debug-header p` — title and subtitle
- `.debug-input-row`, `.debug-input-row input`, `.debug-input-row button` — URL input and diagnose button
- `.debug-error` — red error alert
- `.debug-loading`, `.debug-spinner`, `@keyframes debug-spin` — loading state
- `.debug-summary`, `.debug-status-label`, `.debug-status-label.red/blue/green` — result summary bar
- `.debug-tabs`, `.debug-tab`, `.debug-tab.active` — tab navigation
- `.debug-tab-content` — tab content area
- `.debug-dl`, `.debug-dl dt`, `.debug-dl dd` — description lists
- `.debug-code` — code blocks
- `.debug-empty` — empty state text
- `.debug-fix-preview` — fix preview card
- `.debug-card`, `.debug-card-header`, `.debug-card-mono`, `.debug-card-count`, `.debug-card-jobids` — correlation cards
- `.debug-section-title` — section headers
- `.debug-ee-file-btn` — expandable EE file buttons
- `.debug-link` — GitHub links

- [ ] **Step 3: Verify the HTML is valid**

Run: `cd /Users/prutledg/parsec && python3 -c "from html.parser import HTMLParser; p = HTMLParser(); p.feed(open('static/index.html').read()); print('HTML parsed OK')"`
Expected: `HTML parsed OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/parsec
git add static/index.html static/style.css
git commit -m "feat: add debug automation HTML structure and CSS"
```

---

### Task 6: Frontend — JavaScript logic

**Files:**
- Modify: `static/app.js` (around line 87-122 for tab handlers, append for debug logic)

- [ ] **Step 1: Add the debug sidebar tab handler**

In `static/app.js`, after the line `tabExamples.addEventListener("click", function() { openSidebar("examples"); });` (around line 122), add:

```javascript
var tabDebug = document.getElementById("sidebar-tab-debug");
tabDebug.addEventListener("click", function() {
    closeSidebar();
    showDebugView();
});
```

- [ ] **Step 2: Add the view-switching functions**

Modify the existing tab event listeners to also call `showChatView()`. Replace:

```javascript
tabHistory.addEventListener("click", function() { openSidebar("history"); });
tabExamples.addEventListener("click", function() { openSidebar("examples"); });
```

With:

```javascript
tabHistory.addEventListener("click", function() { showChatView(); openSidebar("history"); });
tabExamples.addEventListener("click", function() { showChatView(); openSidebar("examples"); });
```

Then add the view-switching functions:

```javascript
// --- Debug Automation ---

var debugViewEl = document.getElementById("debug-view");
var chatEl = document.getElementById("chat");
var footerEl = document.querySelector("footer");

function showDebugView() {
    chatEl.style.display = "none";
    footerEl.style.display = "none";
    debugViewEl.style.display = "flex";
    debugViewEl.style.flexDirection = "column";
}

function showChatView() {
    debugViewEl.style.display = "none";
    chatEl.style.display = "";
    footerEl.style.display = "";
}
```

- [ ] **Step 3: Add the debug view state, API calls, and rendering logic**

Append the complete debug JS logic to `static/app.js`. This includes:

1. **State variables:** `debugResult`, `debugCorrelation`, `debugEEInfo`, `debugActiveTab`, `debugUrl`
2. **DOM references:** `debugUrlInput`, `debugDiagnoseBtn`, `debugErrorEl`, `debugLoadingEl`, `debugResultEl`, `debugSummaryEl`, `debugTabContentEl`, `debugFixPreviewEl`
3. **Event handlers:** click on Diagnose button, Enter key on URL input
4. **`runDiagnosis()`** — POST to `/api/debug/diagnose`, handle response/error, call `renderDebugResult()`
5. **Helper functions:** `formatElapsed(seconds)`, `statusColor(status)`, `escHtml(s)` (uses `document.createElement("div").textContent = s` to safely escape, then reads `innerHTML`)
6. **`renderDebugResult()`** — renders summary bar, wires up tab clicks, calls `renderDebugTab()`
7. **Tab renderers:**
   - `renderTriageTab()` — description list with metadata
   - `renderFailingTaskTab()` — task details + error code block
   - `renderFixTab()` — fix source label, details, before/after code, GitHub link
   - `renderCorrelationTab()` — lazy-loads via POST to `/api/debug/correlation`, then `renderCorrelationData()`
   - `renderEEInfoTab()` — lazy-loads via POST to `/api/debug/ee`, then `renderEEInfoData()`
8. **`renderFixPreview()`** — persistent fix card below tabs (hidden on Fix tab)
9. **`window.toggleEEFile(id, btn)`** — expand/collapse EE file content

**Security note:** All user-controlled strings (job status, error messages, file paths, etc.) pass through `escHtml()` before insertion. `escHtml()` creates a temporary DOM element, sets `textContent` (which escapes HTML entities), then reads back `innerHTML`. This is the same safe-escaping pattern used throughout parsec's existing `app.js`.

The complete JS code for each function is provided in the spec's code blocks above in this plan (see the first write attempt of this plan for the full JavaScript source). Copy those code blocks verbatim.

- [ ] **Step 4: Start the dev server and verify the debug view renders**

Run: `cd /Users/prutledg/parsec && source .venv/bin/activate && scripts/local-server.sh restart`

Open `http://localhost:8000` in a browser. Verify:
- "Debug Automation" tab appears in the sidebar tabs
- Clicking it hides the chat and shows the debug view with title, URL input, and Diagnose button
- Clicking "History" or "Examples" restores the chat view
- Pasting a URL and clicking Diagnose calls the API (may fail without AAP2 access — verify the network request fires in browser devtools)

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/parsec
git add static/app.js
git commit -m "feat: add debug automation frontend logic"
```

---

### Task 7: Quality gates and final verification

**Files:** (none new — validates existing)

- [ ] **Step 1: Run all backend tests**

Run: `cd /Users/prutledg/parsec && python3 -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run linting**

Run: `cd /Users/prutledg/parsec && source .venv/bin/activate && ruff check src/tools/aap2_debug.py src/tools/aap2_fix.py src/tools/aap2_stdout.py src/routes/debug.py`
Expected: No errors

- [ ] **Step 3: Run type checking**

Run: `cd /Users/prutledg/parsec && source .venv/bin/activate && mypy src/tools/aap2_debug.py src/tools/aap2_fix.py src/tools/aap2_stdout.py src/routes/debug.py --ignore-missing-imports`
Expected: No errors (or only pre-existing issues)

- [ ] **Step 4: Run black formatter**

Run: `cd /Users/prutledg/parsec && source .venv/bin/activate && black src/tools/aap2_debug.py src/tools/aap2_fix.py src/tools/aap2_stdout.py src/routes/debug.py tests/test_aap2_*.py`
Expected: Files reformatted or unchanged

- [ ] **Step 5: Manual test with a real AAP2 job URL**

Prerequisites: port-forward to parsec's AAP2 controller config (or run against the dev deployment).

Test cases:
1. Paste a **failed** job URL — verify Triage, Failing Task, and Fix tabs populate
2. Paste an **error** job URL — verify EE Info auto-loads and fix preview appears
3. Paste a **successful** job URL — verify Triage shows metadata, other tabs show empty state
4. Click **Correlation** tab — verify lazy load works
5. Click **EE Info** tab — verify EE files are expandable
6. Paste an invalid URL — verify error message appears
7. Switch back to chat — verify chat works normally

- [ ] **Step 6: Commit any formatting fixes**

```bash
cd /Users/prutledg/parsec
git add -A
git status  # review: only formatting changes expected
git commit -m "style: apply formatting to debug automation modules"
```
