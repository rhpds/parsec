"""Tool: query_agnosticd_source — fetch source code from agnosticd GitHub repos."""

import base64
import logging

import httpx

logger = logging.getLogger(__name__)

# Known agnosticd repositories
_REPOS: dict[str, dict[str, str]] = {
    "agnosticd-v2": {
        "owner": "agnosticd",
        "repo": "agnosticd-v2",
        "default_ref": "main",
    },
    "agnosticd": {
        "owner": "redhat-cop",
        "repo": "agnosticd",
        "default_ref": "development",
    },
}

_GITHUB_API = "https://api.github.com"
_MAX_FILE_SIZE = 50_000  # Truncate files larger than this (chars)
_TIMEOUT = 15.0


def _resolve_repo(scm_url: str = "", repo: str = "") -> tuple[str, str, str]:
    """Resolve owner, repo name, and default ref from scm_url or repo key.

    Returns (owner, repo, default_ref).
    """
    if repo and repo in _REPOS:
        r = _REPOS[repo]
        return r["owner"], r["repo"], r["default_ref"]

    if scm_url:
        if "agnosticd-v2" in scm_url:
            r = _REPOS["agnosticd-v2"]
            return r["owner"], r["repo"], r["default_ref"]
        if "agnosticd" in scm_url:
            r = _REPOS["agnosticd"]
            return r["owner"], r["repo"], r["default_ref"]

    # Default to agnosticd-v2
    r = _REPOS["agnosticd-v2"]
    return r["owner"], r["repo"], r["default_ref"]


def _fallback_repo(owner: str, repo_name: str) -> tuple[str, str, str]:
    """Return the other agnosticd repo for fallback on 404."""
    r = _REPOS["agnosticd"] if repo_name == "agnosticd-v2" else _REPOS["agnosticd-v2"]
    return r["owner"], r["repo"], r["default_ref"]


async def _github_get(owner: str, repo: str, path: str, ref: str) -> dict:
    """Fetch a file or directory listing from the GitHub Contents API."""
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": ref}
    headers = {"Accept": "application/vnd.github.v3+json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _decode_file_content(data: dict) -> str:
    """Decode base64 file content from GitHub API response."""
    content = data.get("content", "")
    encoding = data.get("encoding", "")
    if encoding == "base64" and content:
        decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        if len(decoded) > _MAX_FILE_SIZE:
            return decoded[:_MAX_FILE_SIZE] + f"\n... [truncated at {_MAX_FILE_SIZE} chars]"
        return decoded
    return content


async def _get_role(owner: str, repo: str, ref: str, role: str, task_file: str) -> dict:
    """Fetch role task files from ansible/roles/{role}/tasks/."""
    tasks_path = f"ansible/roles/{role}/tasks"

    # First list the tasks directory to see what files exist
    try:
        listing = await _github_get(owner, repo, tasks_path, ref)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {
                "error": f"Role '{role}' not found at {tasks_path}",
                "repo": f"{owner}/{repo}",
                "ref": ref,
            }
        raise

    if not isinstance(listing, list):
        return {"error": f"Unexpected response for {tasks_path}", "repo": f"{owner}/{repo}"}

    task_files = [f["name"] for f in listing if isinstance(f, dict) and f.get("type") == "file"]

    # Determine which file(s) to fetch
    if task_file:
        # Normalize: add .yml/.yaml if not present
        candidates = [task_file]
        if not task_file.endswith((".yml", ".yaml")):
            candidates = [f"{task_file}.yml", f"{task_file}.yaml"]

        target = None
        for c in candidates:
            if c in task_files:
                target = c
                break
        if not target:
            return {
                "error": f"Task file '{task_file}' not found in role '{role}'",
                "available_files": task_files,
                "repo": f"{owner}/{repo}",
                "ref": ref,
            }
        targets = [target]
    else:
        # Fetch the most relevant files (main.yml + workload files)
        priority = [
            "main.yml",
            "main.yaml",
            "workload.yml",
            "workload.yaml",
            "remove_workload.yml",
            "remove_workload.yaml",
            "pre_workload.yml",
            "pre_workload.yaml",
            "post_workload.yml",
            "post_workload.yaml",
        ]
        targets = [f for f in priority if f in task_files]
        if not targets:
            # Fetch first 3 files if no standard names found
            targets = task_files[:3]

    # Fetch the target files
    files: dict[str, str] = {}
    for fname in targets:
        try:
            data = await _github_get(owner, repo, f"{tasks_path}/{fname}", ref)
            files[fname] = _decode_file_content(data)
        except Exception as e:
            files[fname] = f"[error fetching: {e}]"

    return {
        "role": role,
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "path": tasks_path,
        "available_files": task_files,
        "files": files,
    }


async def _get_config(owner: str, repo: str, ref: str, env_type: str, cloud_provider: str) -> dict:
    """Fetch config defaults from ansible/configs/{env_type}/."""
    base_path = f"ansible/configs/{env_type}"
    files: dict[str, str] = {}
    errors: list[str] = []

    # Fetch base default_vars
    for ext in ("yml", "yaml"):
        try:
            data = await _github_get(owner, repo, f"{base_path}/default_vars.{ext}", ref)
            files[f"default_vars.{ext}"] = _decode_file_content(data)
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                errors.append(f"default_vars.{ext}: {e.response.status_code}")
    else:
        errors.append(f"No default_vars found at {base_path}/")

    # Fetch cloud-specific defaults if requested
    if cloud_provider:
        cloud_path = f"{base_path}/{cloud_provider}"
        for ext in ("yml", "yaml"):
            try:
                data = await _github_get(owner, repo, f"{cloud_path}/default_vars.{ext}", ref)
                files[f"{cloud_provider}/default_vars.{ext}"] = _decode_file_content(data)
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    errors.append(f"{cloud_provider}/default_vars.{ext}: {e.response.status_code}")
        else:
            # Try legacy pattern: default_vars_{cloud_provider}.yml
            for ext in ("yml", "yaml"):
                try:
                    data = await _github_get(
                        owner,
                        repo,
                        f"{base_path}/default_vars_{cloud_provider}.{ext}",
                        ref,
                    )
                    files[f"default_vars_{cloud_provider}.{ext}"] = _decode_file_content(data)
                    break
                except httpx.HTTPStatusError:
                    pass

    if not files:
        return {
            "error": f"Config '{env_type}' not found in {owner}/{repo}",
            "ref": ref,
            "errors": errors,
        }

    return {
        "env_type": env_type,
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "path": base_path,
        "files": files,
        "errors": errors if errors else None,
    }


async def _get_file(owner: str, repo: str, ref: str, file_path: str) -> dict:
    """Fetch an arbitrary file or directory listing."""
    try:
        data = await _github_get(owner, repo, file_path, ref)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {
                "error": f"Path not found: {file_path}",
                "repo": f"{owner}/{repo}",
                "ref": ref,
            }
        raise

    # Directory listing
    if isinstance(data, list):
        entries = []
        for item in data:
            if isinstance(item, dict):
                entries.append(
                    {
                        "name": item.get("name", ""),
                        "type": item.get("type", ""),
                        "size": item.get("size", 0),
                    }
                )
        return {
            "repo": f"{owner}/{repo}",
            "ref": ref,
            "path": file_path,
            "type": "directory",
            "entries": entries,
        }

    # File content
    return {
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "path": file_path,
        "type": "file",
        "content": _decode_file_content(data),
    }


async def query_agnosticd_source(
    action: str,
    role: str = "",
    task_file: str = "",
    env_type: str = "",
    cloud_provider: str = "",
    file_path: str = "",
    scm_url: str = "",
    repo: str = "",
    ref: str = "",
) -> dict:
    """Main entry point for the query_agnosticd_source tool."""
    owner, repo_name, default_ref = _resolve_repo(scm_url, repo)
    effective_ref = ref or default_ref
    # Whether to try the other repo on 404 (only when no explicit repo was specified)
    should_fallback = not scm_url and not repo

    try:
        if action == "get_role":
            if not role:
                return {"error": "role is required for get_role"}
            result = await _get_role(owner, repo_name, effective_ref, role, task_file)
            if result.get("error") and should_fallback:
                fb_owner, fb_repo, fb_ref = _fallback_repo(owner, repo_name)
                result = await _get_role(fb_owner, fb_repo, fb_ref, role, task_file)
            return result

        elif action == "get_config":
            if not env_type:
                return {"error": "env_type is required for get_config"}
            result = await _get_config(owner, repo_name, effective_ref, env_type, cloud_provider)
            if result.get("error") and should_fallback:
                fb_owner, fb_repo, fb_ref = _fallback_repo(owner, repo_name)
                result = await _get_config(fb_owner, fb_repo, fb_ref, env_type, cloud_provider)
            return result

        elif action == "get_file":
            if not file_path:
                return {"error": "file_path is required for get_file"}
            return await _get_file(owner, repo_name, effective_ref, file_path)

        else:
            return {
                "error": f"Unknown action: '{action}'. " "Use get_role, get_config, or get_file."
            }

    except httpx.ConnectError as e:
        return {"error": f"Cannot reach GitHub API: {e}"}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return {"error": "GitHub API rate limit exceeded (60 req/hr unauthenticated)"}
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except Exception:
        logger.exception("Unexpected error in query_agnosticd_source")
        return {"error": "Internal error querying agnosticd source — check logs"}
