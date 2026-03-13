"""Tool: query_agnosticv_repo — fetch catalog item config from AgnosticV GitHub repos."""

import base64
import logging

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_MAX_FILE_SIZE = 50_000
_TIMEOUT = 15.0

# Map agnosticvRepo names (from AgnosticVComponent spec) to GitHub repos.
# Most repos are under the rhpds org. The main repo has a name mismatch:
# AgnosticVComponent uses "rhpds-agnosticv" but the GitHub repo is "agnosticv".
_REPO_NAME_MAP: dict[str, str] = {
    "rhpds-agnosticv": "agnosticv",
}


def _get_github_token() -> str:
    """Get GitHub token from config."""
    return get_config().get("github.token", "")


def _resolve_github_repo(agnosticv_repo: str) -> str:
    """Map agnosticvRepo value to GitHub repo name.

    Most repos have the same name in GitHub (e.g. zt-ansiblebu-agnosticv).
    Only 'rhpds-agnosticv' needs mapping to 'agnosticv'.
    """
    return _REPO_NAME_MAP.get(agnosticv_repo, agnosticv_repo)


async def _github_get(owner: str, repo: str, path: str, ref: str, token: str) -> dict:
    """Fetch a file or directory listing from the GitHub Contents API."""
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": ref}
    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

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


async def query_agnosticv_repo(
    action: str,
    agnosticv_repo: str = "",
    path: str = "",
    ref: str = "",
) -> dict:
    """Main entry point for the query_agnosticv_repo tool.

    Args:
        action: get_config (fetch a catalog item config file) or get_file
                (fetch any file or list a directory).
        agnosticv_repo: The agnosticvRepo value from the AgnosticVComponent
                        (e.g. 'rhpds-agnosticv', 'zt-ansiblebu-agnosticv').
        path: Path within the repo. For get_config, this is the
              AgnosticVComponent spec.path (e.g.
              'ansiblebu/AAP2_WORKSHOP_NETWORKING_AUTOMATION/prod.yaml').
              For get_file, any path.
        ref: Git ref (branch/tag). Defaults to 'main'.
    """
    token = _get_github_token()
    if not token:
        return {"error": "GitHub token not configured (github.token). Cannot access private repos."}

    if not agnosticv_repo:
        return {
            "error": "agnosticv_repo is required. Get it from the AgnosticVComponent "
            "spec.agnosticvRepo field (e.g. 'rhpds-agnosticv', 'zt-ansiblebu-agnosticv')."
        }

    owner = "rhpds"
    repo_name = _resolve_github_repo(agnosticv_repo)
    effective_ref = ref or "main"

    try:
        if action == "get_config":
            if not path:
                return {
                    "error": "path is required for get_config. Use the spec.path from "
                    "the AgnosticVComponent (e.g. "
                    "'ansiblebu/AAP2_WORKSHOP_NETWORKING_AUTOMATION/prod.yaml')."
                }
            return await _get_catalog_config(owner, repo_name, effective_ref, path, token)

        elif action == "get_file":
            if not path:
                return {"error": "path is required for get_file."}
            return await _get_file(owner, repo_name, effective_ref, path, token)

        else:
            return {"error": f"Unknown action: '{action}'. Use get_config or get_file."}

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {
                "error": f"Not found: {path} in {owner}/{repo_name} (ref={effective_ref})",
                "repo": f"{owner}/{repo_name}",
                "ref": effective_ref,
            }
        if e.response.status_code == 403:
            return {"error": "GitHub API access denied. Token may lack read access to this repo."}
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except httpx.ConnectError as e:
        return {"error": f"Cannot reach GitHub API: {e}"}
    except Exception:
        logger.exception("Unexpected error in query_agnosticv_repo")
        return {"error": "Internal error querying agnosticv repo — check logs"}


async def _get_catalog_config(
    owner: str, repo: str, ref: str, config_path: str, token: str
) -> dict:
    """Fetch a catalog item config file and its related files.

    config_path is the AgnosticVComponent spec.path, e.g.
    'ansiblebu/AAP2_WORKSHOP_NETWORKING_AUTOMATION/prod.yaml'.
    Also fetches common.yaml from the same directory if it exists.
    """
    files: dict[str, str] = {}

    # Fetch the main config file
    data = await _github_get(owner, repo, config_path, ref, token)
    files[config_path.split("/")[-1]] = _decode_file_content(data)

    # Also fetch common.yaml from the same directory (shared config)
    parent_dir = "/".join(config_path.split("/")[:-1])
    if parent_dir:
        try:
            common_data = await _github_get(owner, repo, f"{parent_dir}/common.yaml", ref, token)
            files["common.yaml"] = _decode_file_content(common_data)
        except httpx.HTTPStatusError:
            pass  # common.yaml is optional

    return {
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "path": config_path,
        "files": files,
    }


async def _get_file(owner: str, repo: str, ref: str, file_path: str, token: str) -> dict:
    """Fetch an arbitrary file or directory listing."""
    data = await _github_get(owner, repo, file_path, ref, token)

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
