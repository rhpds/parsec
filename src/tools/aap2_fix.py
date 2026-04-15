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


def extract_catalog_item_path(extra_vars: dict, job_template_name: str | None = None) -> str | None:
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
                        f"https://github.com/{repo}/blob/master/" f"{catalog_path}/common.yaml"
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
            source_content = await _fetch_source_file(f"agnosticd/{collection_fqcn}", role_file)
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

    action = (extra_vars or {}).get("ACTION", "unknown")
    prompt = f"""You are diagnosing a failed Ansible task from an AAP2 job.
Analyze the failure and recommend a fix.

Job template: {job_template_name or "unknown"}
Action: {action}

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

            project_id = cfg.anthropic.get("vertex_project_id", "") or cfg.gcp.get("project_id", "")
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
            api_key = cfg.anthropic.get("api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
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
