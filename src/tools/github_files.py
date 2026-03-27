"""Tool: fetch_github_file — fetch files and directories from GitHub repos via MCP."""

import json
import logging
import re
import time

import httpx

from src.connections.github_mcp import call_tool, get_token

logger = logging.getLogger(__name__)

# ─── Catalog item index (cached in memory) ───
# Maps lowercase catalog item name → {owner, repo, account, directory, files}
_catalog_index: dict[str, dict] = {}
_index_built_at: float = 0.0
_INDEX_TTL = 3600  # Rebuild after 1 hour

# AgnosticV repos to index
_AGNOSTICV_REPOS: list[tuple[str, str]] = [
    ("rhpds", "agnosticv"),
    ("rhpds", "partner-agnosticv"),
    ("rhpds", "zt-ansiblebu-agnosticv"),
    ("rhpds", "zt-rhelbu-agnosticv"),
]

_SECRET_PATTERNS = re.compile(
    r"(access_key|secret_key|password|token|pull_secret|hmac_key|eab_key|"
    r"ssh_pass|secret_access|api_key|client_secret|activationkey)",
    re.IGNORECASE,
)


def _redact_secrets(text: str) -> str:
    """Redact lines that look like they contain secrets."""
    lines = text.split("\n")
    redacted: list[str] = []
    for line in lines:
        if _SECRET_PATTERNS.search(line):
            key_part = line.split(":")[0] if ":" in line else line.split("=")[0]
            redacted.append(f"{key_part.rstrip()}: <REDACTED>")
        else:
            redacted.append(line)
    return "\n".join(redacted)


async def fetch_github_file(
    owner: str,
    repo: str,
    path: str,
    ref: str = "",
) -> dict:
    """Fetch a file or directory listing from a GitHub repository.

    Uses the GitHub remote MCP server's get_file_contents tool.

    Args:
        owner: Repository owner (e.g. "rhpds")
        repo:  Repository name (e.g. "agnosticv")
        path:  Path within the repo (e.g. "sandboxes-gpte/ANS_BU_WKSP_RHEL_90/common.yaml")
        ref:   Git ref — branch name, tag, or commit SHA (default: repo default branch)

    Returns:
        dict with "content" (file text or directory listing) or "error".
    """
    arguments: dict[str, str] = {
        "owner": owner,
        "repo": repo,
        "path": path,
    }
    if ref:
        arguments["ref"] = ref

    result = await call_tool("get_file_contents", arguments)

    if "error" in result:
        return result

    content = result.get("content", "")
    if content:
        content = _simplify_directory_listing(content)
        result["content"] = _redact_secrets(content)

    return result


def _simplify_directory_listing(content: str) -> str:
    """If content is a JSON directory listing, return compact name/type list."""
    try:
        entries = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content

    if not isinstance(entries, list) or not entries:
        return content

    # Check if it looks like a GitHub directory listing
    if not isinstance(entries[0], dict) or "name" not in entries[0]:
        return content

    lines = []
    for entry in entries:
        name = entry.get("name", "")
        entry_type = entry.get("type", "file")
        suffix = "/" if entry_type == "dir" else ""
        lines.append(f"{name}{suffix}")

    return "\n".join(sorted(lines))


async def _build_catalog_index() -> None:
    """Build the catalog item index from all agnosticv repos."""
    global _catalog_index, _index_built_at  # noqa: PLW0603

    token = get_token()
    if not token:
        logger.warning("No GitHub token — catalog index not built")
        return

    new_index: dict[str, dict] = {}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        for owner, repo in _AGNOSTICV_REPOS:
            try:
                # Get repo metadata for default branch name
                repo_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}",
                    headers=headers,
                )
                default_branch = "main"
                if repo_resp.status_code == 200:
                    default_branch = repo_resp.json().get("default_branch", "main")

                url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.warning("Failed to index %s/%s: HTTP %s", owner, repo, resp.status_code)
                    continue

                tree = resp.json().get("tree", [])

                # Find catalog item directories: account/item_name/common.yaml
                # Catalog items are directories that contain a common.yaml file
                catalog_dirs: dict[str, list[str]] = {}
                for entry in tree:
                    parts = entry["path"].split("/")
                    if len(parts) >= 2 and entry["type"] == "tree":
                        # Skip hidden/special directories
                        if parts[0].startswith(".") or parts[0] in (
                            "includes",
                            "tests",
                            "EXAMPLE_ACCOUNT",
                        ):
                            continue
                        if len(parts) == 2:
                            catalog_dirs.setdefault(entry["path"], [])
                    elif len(parts) == 3 and entry["type"] == "blob":
                        parent = "/".join(parts[:2])
                        if parent in catalog_dirs:
                            catalog_dirs[parent].append(parts[2])

                for dir_path, files in catalog_dirs.items():
                    if "common.yaml" not in files:
                        continue
                    parts = dir_path.split("/")
                    account = parts[0]
                    item_dir = parts[1]
                    key = item_dir.lower().replace("_", "-")
                    new_index[key] = {
                        "owner": owner,
                        "repo": repo,
                        "account": account,
                        "directory": item_dir,
                        "path": dir_path,
                        "files": files,
                        "default_branch": default_branch,
                    }

                logger.info(
                    "Indexed %s/%s: %d catalog items",
                    owner,
                    repo,
                    sum(1 for v in new_index.values() if v["repo"] == repo),
                )

            except Exception:
                logger.exception("Failed to index %s/%s", owner, repo)

    _catalog_index = new_index
    _index_built_at = time.monotonic()
    logger.info("Catalog index built: %d total items", len(_catalog_index))


async def _ensure_index() -> None:
    """Build index if not yet built or expired."""
    if not _catalog_index or (time.monotonic() - _index_built_at) > _INDEX_TTL:
        await _build_catalog_index()


async def lookup_catalog_item(search: str) -> dict:
    """Look up a catalog item across all agnosticv repos using the cached index.

    Searches by exact match first (normalized), then by substring.
    Returns the item's location and files, or similar matches if not found.
    """
    await _ensure_index()

    if not _catalog_index:
        return {"error": "Catalog index is empty (GitHub token may not be configured)"}

    key = search.lower().replace("_", "-")

    # Exact match
    if key in _catalog_index:
        item = _catalog_index[key]
        return {
            "found": True,
            "owner": item["owner"],
            "repo": item["repo"],
            "account": item["account"],
            "directory": item["directory"],
            "path": item["path"],
            "files": item["files"],
            "default_branch": item.get("default_branch", "main"),
        }

    # Substring match
    matches = [{"name": k, **v} for k, v in _catalog_index.items() if key in k]

    if matches:
        return {
            "found": False,
            "similar_items": [
                {
                    "name": m["directory"],
                    "repo": f"{m['owner']}/{m['repo']}",
                    "account": m["account"],
                    "path": m["path"],
                }
                for m in matches[:20]
            ],
            "message": f"No exact match for '{search}'. Found {len(matches)} similar items.",
        }

    return {
        "found": False,
        "similar_items": [],
        "message": (
            f"No catalog item matching '{search}' found in any agnosticv repo "
            f"(searched {len(_catalog_index)} items across "
            f"{len(_AGNOSTICV_REPOS)} repos). "
            "This is a complete index — do not search further. "
            "Report not found to the user."
        ),
    }


async def search_github_repo(
    owner: str,
    repo: str,
    search: str,
    ref: str = "",
) -> dict:
    """Search a GitHub repo's file tree for paths matching a substring.

    Uses the Git Trees API with recursive=1 to fetch the entire tree in one
    call, then filters locally. Much faster than listing directories one by one.
    """
    token = get_token()
    if not token:
        return {"error": "GitHub token not configured"}

    tree_ref = ref or "HEAD"
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{tree_ref}?recursive=1"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code != 200:
                return {"error": f"GitHub API returned {resp.status_code}: {resp.text[:200]}"}

            data = resp.json()
            tree = data.get("tree", [])

            search_lower = search.lower()
            matches = [entry["path"] for entry in tree if search_lower in entry["path"].lower()]

            return {
                "matches": matches[:100],
                "total_matches": len(matches),
                "truncated": len(matches) > 100,
            }

    except Exception as exc:
        logger.exception("GitHub tree search failed")
        return {"error": f"GitHub tree search failed: {exc}"}


async def search_agnosticv_prs(
    search: str,
    state: str = "open",
    max_results: int = 10,
) -> dict:
    """Search open PRs across agnosticv repos for a catalog item or keyword.

    Useful when lookup_catalog_item returns not found — the item may exist
    only on an unmerged PR branch.

    Args:
        search: Keyword to search for in PR titles and changed file paths.
        state: PR state filter — "open" (default), "closed", or "all".
        max_results: Max PRs to return per repo (default 10).
    """
    token = get_token()
    if not token:
        return {"error": "GitHub token not configured — cannot search PRs"}

    search_lower = search.lower()
    all_matches: list[dict] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for owner, repo in _AGNOSTICV_REPOS:
            try:
                # Search PRs by title
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls",
                    params={"state": state, "per_page": 50, "sort": "updated", "direction": "desc"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if resp.status_code == 403:
                    return {
                        "error": "GitHub token lacks 'Pull requests: Read' permission. "
                        "Update the fine-grained PAT to include this scope.",
                        "search": search,
                    }
                if resp.status_code != 200:
                    logger.warning(
                        "GitHub PR list failed for %s/%s: %d", owner, repo, resp.status_code
                    )
                    continue

                prs = resp.json()

                for pr in prs:
                    title = pr.get("title", "")
                    pr_number = pr.get("number", 0)
                    branch = pr.get("head", {}).get("ref", "")

                    # Check title match
                    title_match = search_lower in title.lower()

                    # Check changed files for path match
                    file_match = False
                    matched_files: list[str] = []
                    if not title_match:
                        # Only fetch files if title didn't match (save API calls)
                        try:
                            files_resp = await client.get(
                                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files",
                                params={"per_page": 100},
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github+json",
                                },
                            )
                            if files_resp.status_code == 200:
                                pr_files = files_resp.json()
                                for f in pr_files:
                                    if search_lower in f.get("filename", "").lower():
                                        file_match = True
                                        matched_files.append(f["filename"])
                        except Exception:
                            pass
                    else:
                        # Title matched — still get files for context
                        try:
                            files_resp = await client.get(
                                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files",
                                params={"per_page": 100},
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github+json",
                                },
                            )
                            if files_resp.status_code == 200:
                                pr_files = files_resp.json()
                                matched_files = [f["filename"] for f in pr_files]
                        except Exception:
                            pass

                    if title_match or file_match:
                        all_matches.append(
                            {
                                "owner": owner,
                                "repo": repo,
                                "pr_number": pr_number,
                                "title": title,
                                "branch": branch,
                                "state": pr.get("state", ""),
                                "author": pr.get("user", {}).get("login", ""),
                                "url": pr.get("html_url", ""),
                                "created_at": pr.get("created_at", ""),
                                "updated_at": pr.get("updated_at", ""),
                                "files": matched_files[:20],
                            }
                        )

                        if len(all_matches) >= max_results:
                            break

            except Exception as exc:
                logger.warning("PR search failed for %s/%s: %s", owner, repo, exc)

            if len(all_matches) >= max_results:
                break

    return {
        "results": all_matches,
        "count": len(all_matches),
        "search": search,
        "repos_searched": [f"{o}/{r}" for o, r in _AGNOSTICV_REPOS],
    }
