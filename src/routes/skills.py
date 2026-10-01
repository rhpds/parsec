"""Skills API — inventory, health, attachment, hot reload, and install.

``GET /api/skills`` used to answer only "what parsed". That is a weaker question
than "what works", and the gap is where a skill can sit for months looking
healthy while being inert. Every response now carries a health verdict (see
:mod:`src.skills.health`) and a resolved agent attachment (see
:mod:`src.skills.attachment`).

The write endpoints exist so the two frequent operations stop requiring a code
change, a PR and an image rebuild:

* ``POST /api/skills/reload`` re-runs discovery and republishes the SDK root.
  This is the only genuinely startup-bound step in the whole skill path —
  everything downstream (``discoverable_skill_names``, the per-request
  ``build_orchestrator_options``, the SDK subprocess itself) already reads the
  filesystem live. Re-running it is therefore sufficient to make a newly
  arrived skill usable on the *next* request, with no pod restart.
* ``PUT/DELETE /api/skills/{name}/attachment`` moves a skill between agents, or
  switches it off, without editing ``_AGENT_SKILLS``.
* ``POST /api/skills/install`` fetches an external skill bundle at a pinned ref.

The install endpoint is the sharp one: it pulls third-party instruction text
into a pod holding live credentials, and a SKILL.md steers a credentialed agent.
It is therefore admin-gated, **disabled by default**, restricted to an
allowlisted set of hosts — for every submodule as well as the top-level repo —
never copies a symlink, and records provenance for everything it writes.
Turning it on is a deliberate decision, not a default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.agent.learnings import is_admin_user_async
from src.config import get_config
from src.llm.config_section import section
from src.routes.query import _check_user_allowed
from src.skills import SkillLoader, SkillManifest, sync_sdk_skill_root
from src.skills.attachment import (
    Attachment,
    clear_override,
    load_state,
    resolve,
    save_override,
    state_lock,
    state_path,
)
from src.skills.health import assess, build_tool_surface
from src.skills.loader import QUALIFIED_NAME_RE, SkillSource
from src.skills.sdk_root import sdk_cwd, sdk_skills_root
from src.skills.vendoring import (
    MAX_SUBMODULE_DEPTH,
    checkout_command,
    clone_command,
    copy_skill_tree,
    discover_skill_roots,
    fallback_clone_command,
    git_env,
    gitmodules_paths_command,
    parse_config_z,
    submodule_config_command,
    submodule_init_command,
    submodule_problems,
    submodule_settings,
    submodule_unshallow_command,
    submodule_update_command,
    symlink_on_path,
    tree_size,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["skills"])

#: Hosts an install may fetch from unless overridden by ``skills.install_hosts``.
#: An allowlist rather than a denylist: the failure mode of getting this wrong
#: is executing someone else's instructions inside a credentialed pod.
DEFAULT_INSTALL_HOSTS = ("github.com", "gitlab.com", "gitlab.cee.redhat.com")

#: Clone timeout. A hung fetch must not pin a worker forever.
INSTALL_TIMEOUT_SECONDS = 120

#: Refuse absurd bundles before they fill the volume.
INSTALL_MAX_BYTES = 64 * 1024 * 1024

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REF_RE = re.compile(r"^[\w][\w./-]{0,100}$")
_REPO_RE = re.compile(r"^https://([a-zA-Z0-9.-]+)/([\w.-]+/[\w.-]+?)(?:\.git)?$")


# ----------------------------------------------------------------- helpers


def _sdk_cwd(cfg: Any) -> str | None:
    """The cwd the SDK subprocess uses, which is also where its skills root lives."""
    return sdk_cwd(cfg)


def _skills_section(cfg: Any) -> dict[str, Any]:
    """The ``skills`` block, with keys normalised to lowercase.

    Read through :func:`section` rather than ``cfg.get("skills")`` because
    Dynaconf materialises env-supplied settings with UPPERCASE keys when the
    YAML does not already declare them. On a deployed pod,
    ``PARSEC_SKILLS__INSTALL_ENABLED=true`` arrives as ``INSTALL_ENABLED`` while
    ``plugin_paths`` (present in config.yaml) stays lowercase — so a plain
    lowercase read silently ignored every deploy-var override.
    """
    try:
        return section(cfg, "skills")
    except Exception:
        return {}


async def _require_admin(user: str | None) -> None:
    if not await is_admin_user_async(user):
        raise HTTPException(status_code=403, detail="Admin access required")


def _collect(cfg: Any) -> tuple[list[SkillManifest], dict[str, Attachment]]:
    """Load manifests and resolve their attachment in one pass."""
    from src.agent.agents import AGENTS
    from src.agent.sdk_profiles import supplement_map

    manifests = SkillLoader.from_config(cfg).load_all()
    attachments = resolve(
        manifests,
        known_agents=frozenset(AGENTS),
        overrides=load_state(state_path(cfg)),
        supplement=supplement_map(),
    )
    return manifests, attachments


def _is_removable(skill_path: Path, install_root: str | None) -> bool:
    """Whether DELETE /api/skills/{name} would accept this skill.

    Only what the installer wrote is removable. In-repo skills ship in the image
    and are removed by a PR, so the UI must not offer a button that would 404 —
    or worse, imply the API can edit the repo.
    """
    if not install_root:
        return False
    try:
        skill_path.resolve().relative_to(Path(str(install_root)).resolve())
        return True
    except (ValueError, OSError):
        return False


def _serialize(
    m: SkillManifest,
    *,
    attachment: Attachment,
    health_dict: dict[str, Any],
    sdk_visible: bool,
    removable: bool = False,
) -> dict[str, Any]:
    return {
        "name": m.name,
        # Present only for marketplace bundles, whose authors namespace the
        # skill (``agnosticv:validator``). Operators searching upstream will
        # look for this spelling, not the flattened one.
        "qualified_name": m.qualified_name,
        "description": m.description,
        "source": m.source,
        "skill_path": str(m.skill_path),
        "allowed_tools": list(m.allowed_tools),
        "license": m.license,
        "metadata": m.metadata,
        "parsec": {
            "version": m.parsec.version,
            "domain": m.parsec.domain,
            "requires_mcp": list(m.parsec.requires_mcp),
            "permissions": m.parsec.permissions,
            "cost_estimate_per_call_usd": m.parsec.cost_estimate_per_call_usd,
        },
        "is_parsec_native": m.is_parsec_native,
        "warnings": list(m.warnings),
        "sdk_visible": sdk_visible,
        "attachment": attachment.to_dict(),
        "health": health_dict,
        "provenance": _read_provenance(m.skill_path),
        "removable": removable,
    }


def _install_aliases(m: SkillManifest) -> set[str]:
    """Every spelling by which an operator might name this skill in ``skills``.

    A marketplace skill has up to three: the flattened name Parsec uses, the
    namespaced name its author wrote, and the directory it sits in upstream.
    """
    names = {m.name, m.skill_path.name}
    if m.qualified_name:
        names.add(m.qualified_name)
    return names


def _read_provenance(skill_path: Path) -> dict[str, Any] | None:
    """Provenance written by the installer, if this skill came from one.

    Absence is meaningful and is surfaced as such: a skill with no record is
    unverified, which is exactly the state the vendored ``root-cause-analysis``
    copy is in.
    """
    candidate = skill_path / ".parsec-provenance.json"
    try:
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        logger.debug("Unreadable provenance at %s", candidate)
    return None


# ------------------------------------------------------------------ read


@router.get("/skills")
async def list_skills(
    request: Request,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Every discoverable skill, with health and attachment."""
    cfg = get_config()
    user = x_forwarded_email or x_forwarded_user
    # Same gate as the mutating routes: the response carries filesystem paths,
    # plugin_paths, install provenance and whether install is enabled.
    await _check_user_allowed(request, user)

    try:
        manifests, attachments = _collect(cfg)
    except Exception as e:
        logger.exception("Failed to load skills")
        raise HTTPException(status_code=500, detail=f"Skill discovery failed: {e}") from e

    root = sdk_skills_root(_sdk_cwd(cfg))
    try:
        visible = {p.name for p in root.iterdir() if (p / "SKILL.md").is_file()}
    except OSError:
        visible = set()

    surface = build_tool_surface()
    section_cfg = _skills_section(cfg)
    install_root = section_cfg.get("install_root")
    out: list[dict[str, Any]] = []
    counts = {"ok": 0, "orphaned": 0, "unusable": 0}

    for m in manifests:
        att = attachments.get(m.name, Attachment(skill=m.name, agents=(), origin="none"))
        health = assess(m, attached_agents=att.agents, surface=surface)
        counts[health.status] = counts.get(health.status, 0) + 1
        out.append(
            _serialize(
                m,
                attachment=att,
                health_dict=health.to_dict(),
                sdk_visible=m.name in visible,
                removable=_is_removable(m.skill_path, install_root),
            )
        )

    return {
        "count": len(out),
        "sdk_visible_count": sum(1 for s in out if s["sdk_visible"]),
        "sdk_skills_root": str(root),
        "plugin_paths": list(section_cfg.get("plugin_paths") or []),
        "install_enabled": bool(section_cfg.get("install_enabled", False)),
        "is_admin": await is_admin_user_async(user),
        "health_counts": counts,
        "agents": sorted(_known_agents()),
        "skills": out,
    }


def _known_agents() -> list[str]:
    try:
        from src.agent.agents import AGENTS

        return list(AGENTS)
    except Exception:
        logger.exception("Could not enumerate agents")
        return []


# ----------------------------------------------------------------- reload


@router.post("/skills/reload", responses={403: {"description": "Forbidden"}})
async def reload_skills(
    request: Request,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Re-run discovery and republish the SDK skills root, without a restart.

    This mirrors exactly what the startup lifespan does. Everything downstream
    already reads the filesystem per request, so once the symlinks are refreshed
    the next question picks up the change.
    """
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    await _require_admin(user)

    cfg = get_config()
    try:
        manifests = SkillLoader.from_config(cfg).load_all()
        published = sync_sdk_skill_root(manifests, cwd=_sdk_cwd(cfg))
    except Exception as e:
        logger.exception("Skill reload failed")
        raise HTTPException(status_code=500, detail=f"Reload failed: {e}") from e

    logger.info(
        "Skills reloaded by %s: %d discovered, %d published", user, len(manifests), len(published)
    )
    return {
        "reloaded": True,
        "discovered": len(manifests),
        "published": sorted(published),
        "sdk_skills_root": str(sdk_skills_root(_sdk_cwd(cfg))),
    }


# ------------------------------------------------------------- attachment


@router.put("/skills/{name}/attachment", responses={403: {"description": "Forbidden"}})
async def set_attachment(
    request: Request,
    name: str,
    payload: Annotated[dict[str, Any], Body()],
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Attach a skill to an explicit set of agents, or switch it off.

    An explicit empty list with ``enabled: true`` is a valid, meaningful state:
    "known, allowed, currently attached to nothing".
    """
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    await _require_admin(user)

    if not _SKILL_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid skill name")

    known = set(_known_agents())
    raw_agents = payload.get("agents", [])
    if not isinstance(raw_agents, list):
        raise HTTPException(status_code=400, detail="'agents' must be a list")
    agents = [str(a) for a in raw_agents]
    unknown = [a for a in agents if a not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown agents: {', '.join(unknown)}")

    enabled = bool(payload.get("enabled", True))
    cfg = get_config()
    try:
        # In a worker thread: the write waits on a cross-replica lock, and a
        # peer holding it must not stall this replica's chat streams and probes.
        await run_in_threadpool(
            save_override,
            state_path(cfg),
            skill=name,
            agents=agents,
            enabled=enabled,
            actor=user or "unknown",
        )
    except OSError as e:
        logger.exception("Could not persist attachment for %s", name)
        raise HTTPException(status_code=500, detail=f"Could not persist: {e}") from e

    logger.info("Attachment for %r set to %s (enabled=%s) by %s", name, agents, enabled, user)
    return {"skill": name, "agents": sorted(set(agents)), "enabled": enabled, "origin": "override"}


@router.delete("/skills/{name}/attachment", responses={403: {"description": "Forbidden"}})
async def reset_attachment(
    request: Request,
    name: str,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Drop the override so the skill returns to derived attachment."""
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    await _require_admin(user)

    if not _SKILL_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid skill name")

    cfg = get_config()
    try:
        removed = await run_in_threadpool(clear_override, state_path(cfg), skill=name)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not persist: {e}") from e

    logger.info("Attachment override for %r cleared by %s (existed=%s)", name, user, removed)
    return {"skill": name, "reverted": removed}


# ---------------------------------------------------------------- install


@router.post("/skills/install", responses={403: {"description": "Forbidden"}})
async def install_skills(
    request: Request,
    payload: Annotated[dict[str, Any], Body()],
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Fetch an external skill bundle at a pinned ref and publish it.

    Body: ``{"repo_url": "https://github.com/org/repo", "ref": "<sha|tag|branch>",
    "subdir": "skills"}``.

    Guards, in order: feature flag, admin, host allowlist, shape validation,
    bounded clone under a hermetic HTTPS-only git, every submodule URL held to
    the same allowlist before any is fetched, a size cap over exactly what will
    be written, then a symlink-free copy confined to the configured install
    root. The resolved commit SHA is recorded next to the installed bundle so a
    later reader can tell exactly what was pulled and when.
    """
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    await _require_admin(user)

    cfg = get_config()
    section = _skills_section(cfg)
    if not bool(section.get("install_enabled", False)):
        raise HTTPException(
            status_code=403,
            detail="Skill install is disabled. Set skills.install_enabled to enable it.",
        )

    install_root = section.get("install_root")
    if not install_root:
        raise HTTPException(status_code=500, detail="skills.install_root is not configured")
    root = Path(str(install_root))

    repo_url = str(payload.get("repo_url", "")).strip()
    ref = str(payload.get("ref", "")).strip()
    # Empty means "find every skill root in the clone". The old default of
    # "skills" silently limited a marketplace install to whatever sat in the
    # top-level aggregate directory — for rhpds/rhdp-skills-marketplace that is
    # a set of bare SKILL.md symlinks, and it excludes the RCA bundle entirely.
    subdir = str(payload.get("subdir", "")).strip().strip("/")

    raw_only = payload.get("skills")
    only: set[str] | None = None
    if raw_only is not None:
        if not isinstance(raw_only, list):
            raise HTTPException(status_code=400, detail="'skills' must be a list of names")
        only = {str(x) for x in raw_only}
        # Accept the namespaced spelling too: an operator copying a name out of
        # the marketplace sees "showroom:create-lab", not "showroom-create-lab".
        bad = sorted(n for n in only if not QUALIFIED_NAME_RE.match(n))
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid skill names: {', '.join(bad)}")

    match = _REPO_RE.match(repo_url)
    if not match:
        raise HTTPException(status_code=400, detail="repo_url must be https://<host>/<org>/<repo>")
    host = match.group(1)
    allowed_hosts = tuple(section.get("install_hosts") or DEFAULT_INSTALL_HOSTS)
    if host not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail=f"Host {host!r} is not allowlisted. Allowed: {', '.join(allowed_hosts)}",
        )
    if not ref or not _REF_RE.match(ref):
        raise HTTPException(status_code=400, detail="ref must be a SHA, tag or branch name")
    if subdir and (".." in subdir or subdir.startswith("/")):
        raise HTTPException(status_code=400, detail="Invalid subdir")

    if shutil.which("git") is None:
        raise HTTPException(
            status_code=501,
            detail="git is not installed in this image; install from git is unavailable",
        )

    try:
        result = await _clone_and_install(repo_url, ref, subdir, root, only, allowed_hosts)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Skill install failed for %s@%s", repo_url, ref)
        raise HTTPException(status_code=500, detail=f"Install failed: {e}") from e

    # Republish so the new skills are usable on the next request.
    try:
        manifests = SkillLoader.from_config(cfg).load_all()
        published = sync_sdk_skill_root(manifests, cwd=_sdk_cwd(cfg))
    except Exception:
        logger.exception("Installed %s but reload failed", repo_url)
        published = {}

    logger.info(
        "Installed %d skills from %s@%s (%s) by %s",
        len(result.installed),
        repo_url,
        ref,
        result.sha[:8],
        user,
    )
    return {
        "installed": result.installed,
        "repo_url": repo_url,
        "ref": ref,
        "resolved_sha": result.sha,
        "skipped_symlinks": result.skipped_symlinks,
        "skipped_skills": result.skipped_skills,
        "published": sorted(published),
        "hint": "Newly installed skills are attached by parsec.domain; set attachment explicitly if they declare none.",
    }


@router.delete("/skills/{name}", responses={403: {"description": "Forbidden"}})
async def uninstall_skill(
    request: Request,
    name: str,
    x_forwarded_user: Annotated[str | None, Header()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
):
    """Remove a skill that was installed into the writable install root.

    Deliberately narrow. It resolves the target and refuses unless the path sits
    inside ``skills.install_root`` — so an in-repo skill under ``skills/``, which
    is part of the image and belongs to a PR, can never be deleted through the
    API. Without this, install was a one-way door: a bundle that turned out to
    contain skills Parsec cannot run had to be removed by rebuilding the pod.
    """
    user = x_forwarded_email or x_forwarded_user
    await _check_user_allowed(request, user)
    await _require_admin(user)

    if not _SKILL_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid skill name")

    cfg = get_config()
    install_root = _skills_section(cfg).get("install_root")
    if not install_root:
        raise HTTPException(status_code=409, detail="skills.install_root is not configured")

    root = Path(str(install_root)).resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Refusing to delete outside install_root"
        ) from None
    if not target.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"{name!r} is not an installed skill (in-repo skills are removed by a PR, not here)",
        )

    await run_in_threadpool(_remove_skill_dir, target)

    # Republish so the SDK root loses its symlink on the same request.
    try:
        manifests = SkillLoader.from_config(cfg).load_all()
        published = sync_sdk_skill_root(manifests, cwd=_sdk_cwd(cfg))
    except Exception:
        logger.exception("Removed %s but reload failed", name)
        published = {}

    logger.info("Skill %r uninstalled by %s", name, user)
    return {"uninstalled": name, "remaining": len(published), "published": sorted(published)}


async def _run(
    *args: str, cwd: str | None = None, env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a git command with a hard timeout, returning (rc, stdout, stderr).

    Always under the hermetic :func:`git_env` unless the caller hands one in, so
    a new call site cannot forget it.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=dict(env) if env is not None else git_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=504, detail="git operation timed out") from None
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


@dataclass
class _InstallResult:
    """What one install wrote, and what it deliberately left behind."""

    installed: list[str]
    sha: str
    #: Installed skill -> relative paths inside it that were links, not copied.
    skipped_symlinks: dict[str, list[str]] = field(default_factory=dict)
    #: Skills not installed at all: ``{"skill", "source_path", "reason"}``.
    skipped_skills: list[dict[str, str]] = field(default_factory=list)


async def _init_submodules(
    repo: Path, allowed_hosts: tuple[str, ...], env: Mapping[str, str], *, depth: int = 0
) -> None:
    """Fetch ``repo``'s submodules — only after every one passes the allowlist.

    ``git submodule init`` resolves each URL (a relative one against this
    repo's own origin) without touching the network. Every resolved URL is then
    held to the ``repo_url`` rule, and one failure refuses the lot before
    anything is fetched. Every fetch's exit code is checked: an ignored failure
    used to leave an empty bundle directory behind a recorded ``resolved_sha``.
    Recurses into each submodule's own ``.gitmodules``, to
    :data:`MAX_SUBMODULE_DEPTH` levels.
    """
    cwd = str(repo)

    # Run even when there is no .gitmodules: init is a no-op (rc 0) on a repo
    # without gitlinks, and fails on a gitlink that .gitmodules does not map —
    # which would otherwise install "successfully" with that directory empty.
    rc, _, err = await _run(*submodule_init_command(), cwd=cwd, env=env)
    if rc != 0:
        raise HTTPException(status_code=400, detail=f"git submodule init failed: {err.strip()}")

    # Exit code 1 means no matching keys: nothing to fetch at this level.
    rc, out, err = await _run(*submodule_config_command(), cwd=cwd, env=env)
    if rc not in (0, 1):
        raise HTTPException(
            status_code=400, detail=f"could not read submodule config: {err.strip()}"
        )
    settings = submodule_settings(parse_config_z(out))
    if not settings:
        return
    if depth >= MAX_SUBMODULE_DEPTH:
        raise HTTPException(
            status_code=400, detail=f"submodules nest deeper than {MAX_SUBMODULE_DEPTH} levels"
        )

    problems = submodule_problems(settings, allowed_hosts)
    if problems:
        detail = "; ".join(f"submodule {name!r}: {reason}" for name, reason in problems)
        raise HTTPException(status_code=400, detail=f"refusing to fetch submodules: {detail}")

    rc, out, _ = await _run(*gitmodules_paths_command(), cwd=cwd, env=env)
    declared = submodule_settings(parse_config_z(out)) if rc == 0 else {}
    for name in sorted(settings):
        path = declared.get(name, {}).get("path") or ""
        rel = Path(path)
        if not path or rel.is_absolute() or ".." in rel.parts or symlink_on_path(repo, repo / rel):
            raise HTTPException(
                status_code=400, detail=f"submodule {name!r} has an unusable path {path!r}"
            )
        rc, _, err = await _run(*submodule_update_command(path), cwd=cwd, env=env)
        if rc != 0:
            # Not every server serves a shallow fetch of a commit that is not a
            # branch tip; retry the same, already-validated URL in full. The
            # retry reuses whatever the shallow attempt cloned, so deepen that
            # first or it asks for the same commit and is refused the same way.
            if (repo / rel / ".git").exists():
                await _run(*submodule_unshallow_command(), cwd=str(repo / rel), env=env)
            rc, _, err = await _run(
                *submodule_update_command(path, shallow=False), cwd=cwd, env=env
            )
        if rc != 0:
            raise HTTPException(
                status_code=400, detail=f"git submodule update failed for {name!r}: {err.strip()}"
            )
        await _init_submodules(repo / rel, allowed_hosts, env, depth=depth + 1)


#: Scratch space for an install, inside install_root so the final swap is a
#: same-filesystem rename. The loader only looks for a SKILL.md in
#: install_root's direct children and this directory never holds one itself,
#: so a half-copied skill left by a crash can never be discovered — or shadow
#: the installed copy, which a `.name.tmp` sibling would.
_STAGING_DIRNAME = ".staging"


def _remove_skill_dir(target: Path) -> None:
    """Use the same lock as install so deletion cannot interrupt a replacement."""
    with state_lock(target):
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="Skill is no longer installed")
        shutil.rmtree(target)


def _replace_skill_dir(
    src: Path, dest: Path, staging_root: Path, provenance: dict[str, Any] | None = None
) -> list[str]:
    """Install ``src`` at ``dest``, keeping the previous ``dest`` until the new one is in place.

    Deleting ``dest`` before copying meant a failed copy — disk full, an
    unreadable file — lost a skill that was working a moment earlier. The copy
    now lands in staging first; ``dest`` is only moved aside once it has
    succeeded, and is put back if the final rename fails. Returns the links
    :func:`copy_skill_tree` skipped. A per-skill inter-process lock protects
    staging and the swap; provenance travels with the staged payload.
    """
    with state_lock(dest):
        return _replace_skill_dir_locked(src, dest, staging_root, provenance)


def _replace_skill_dir_locked(
    src: Path, dest: Path, staging_root: Path, provenance: dict[str, Any] | None
) -> list[str]:
    staging_root.mkdir(exist_ok=True)
    staged = staging_root / dest.name
    previous = staging_root / f"{dest.name}.previous"
    # A process may have died after moving the old copy aside. Restore it
    # before attempting another copy, so another failure cannot destroy it.
    if previous.exists() and not dest.exists():
        previous.rename(dest)
    for leftover in (staged, previous):
        if leftover.exists():
            shutil.rmtree(leftover)
    try:
        links = copy_skill_tree(src, staged)
        if provenance is not None:
            (staged / ".parsec-provenance.json").write_text(
                json.dumps({**provenance, "skipped_symlinks": links}, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
    except BaseException:
        _rmtree_logged(staged)
        raise
    had_previous = dest.exists()
    if had_previous:
        dest.rename(previous)
    try:
        staged.rename(dest)
    except BaseException:
        if had_previous:
            previous.rename(dest)
        _rmtree_logged(staged)
        raise
    if had_previous:
        _rmtree_logged(previous)
    return links


def _rmtree_logged(path: Path) -> None:
    """Best-effort cleanup that says so when it fails, rather than hiding it."""
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError:
        logger.warning("Could not remove %s after an install step", path, exc_info=True)


def _symlink_reason(clone_dir: Path, m: SkillManifest) -> str | None:
    """Why this skill must not be installed because of a link, if it must not."""
    link = symlink_on_path(clone_dir, m.skill_path)
    if link is not None:
        return f"reached through a symlink ({link.relative_to(clone_dir).as_posix()})"
    if (m.skill_path / "SKILL.md").is_symlink():
        # The marketplace's aggregate-stub shape: the real skill, scripts and
        # all, lives wherever the link points, and is installed from there.
        return "SKILL.md is a symlink"
    return None


async def _clone_and_install(
    repo_url: str,
    ref: str,
    subdir: str,
    root: Path,
    only: set[str] | None = None,
    allowed_hosts: tuple[str, ...] = DEFAULT_INSTALL_HOSTS,
) -> _InstallResult:
    """Clone at ``ref``, copy each selected skill into ``root``, record provenance.

    ``--depth 1`` against an explicit ref keeps the fetch small. Submodules are
    fetched only once :func:`_init_submodules` has held every URL to
    ``allowed_hosts``. No symlink is ever copied: a skill whose directory or
    ``SKILL.md`` is a link is not installed at all, and a link anywhere inside
    an installed skill is left behind and reported in ``skipped_symlinks`` — so
    a bundle cannot smuggle a host file into the SDK's discovery directory.
    Nothing is written under ``root`` until the size cap has passed.
    """
    env = git_env()
    with tempfile.TemporaryDirectory(prefix="skill-install-") as tmp:
        clone_dir = Path(tmp) / "repo"
        rc, _, err = await _run(*clone_command(repo_url, ref, clone_dir), env=env)
        if rc != 0:
            # A SHA cannot be used with --branch; fall back to a full clone plus
            # checkout, starting from nothing so a partial first attempt cannot
            # leave the second one cloning into a non-empty directory.
            shutil.rmtree(clone_dir, ignore_errors=True)
            rc2, _, err2 = await _run(*fallback_clone_command(repo_url, clone_dir), env=env)
            if rc2 != 0:
                raise HTTPException(
                    status_code=400, detail=f"git clone failed: {err.strip() or err2.strip()}"
                )
            rc3, _, err3 = await _run(*checkout_command(ref), cwd=str(clone_dir), env=env)
            if rc3 != 0:
                raise HTTPException(
                    status_code=400, detail=f"git checkout {ref} failed: {err3.strip()}"
                )

        # Both paths arrive here with HEAD at `ref` and no submodule fetched.
        await _init_submodules(clone_dir, allowed_hosts, env)

        rc, sha_out, _ = await _run("git", "rev-parse", "HEAD", cwd=str(clone_dir), env=env)
        sha = sha_out.strip() if rc == 0 else "unknown"

        # Everything from here on is blocking filesystem work — discovery, the
        # size walk, up to INSTALL_MAX_BYTES of copying — so it runs in a worker
        # thread rather than stalling every chat stream on this replica.
        return await run_in_threadpool(
            _install_from_clone, clone_dir, repo_url, ref, subdir, root, only, sha
        )


def _install_from_clone(
    clone_dir: Path,
    repo_url: str,
    ref: str,
    subdir: str,
    root: Path,
    only: set[str] | None,
    sha: str,
) -> _InstallResult:
    """Select, size-check, copy and record provenance for a fetched clone."""
    if subdir:
        source_root = clone_dir / subdir
        if symlink_on_path(clone_dir, source_root) is not None:
            raise HTTPException(status_code=400, detail=f"subdir {subdir!r} is a symlink")
        if not source_root.is_dir():
            raise HTTPException(status_code=400, detail=f"subdir {subdir!r} not found in repo")
        source_roots = [source_root]
    else:
        # A marketplace holds several bundles at once, and the RHDP one keeps
        # its AIOps bundle behind a submodule. Discovery walks the clone so
        # an operator pastes a URL rather than reverse-engineering a layout.
        source_roots = discover_skill_roots(clone_dir)
        if not source_roots:
            raise HTTPException(
                status_code=400, detail="no directories of SKILL.md folders found in repo"
            )

    # Load through the real loader rather than walking directories here: it
    # applies the same validation and the same first-root-wins de-duplication
    # that discovery will apply later, so what installs is exactly what would
    # load. Order matters — `discover_skill_roots` puts canonical bundle
    # roots ahead of an aggregate whose entries are SKILL.md symlinks with no
    # scripts beside them.
    manifests = SkillLoader([SkillSource("plugin", r) for r in source_roots]).load_all()

    selected: list[SkillManifest] = []
    skipped_skills: list[dict[str, str]] = []
    for m in manifests:
        if not _SKILL_NAME_RE.match(m.name):
            logger.warning("Skipping skill with unusable name: %r", m.name)
            continue
        if only is not None and not (_install_aliases(m) & only):
            # Selective install. Pulling a whole repo drags in skills that
            # cannot run here (ET's shell-based ones) and templates that
            # were never meant to ship, and every one of them then needs
            # explaining in the UI.
            continue
        reason = _symlink_reason(clone_dir, m)
        if reason:
            skipped_skills.append(
                {
                    "skill": m.name,
                    "source_path": m.skill_path.relative_to(clone_dir).as_posix(),
                    "reason": reason,
                }
            )
            continue
        selected.append(m)

    if not selected:
        where = repr(subdir) if subdir else "the repo"
        detail = f"no installable skills found under {where}"
        if skipped_skills:
            detail += "; skipped: " + ", ".join(
                f"{s['skill']} ({s['reason']})" for s in skipped_skills
            )
        raise HTTPException(status_code=400, detail=detail)

    # Measured over exactly what will land: the selected skills only, and
    # only their regular files — the same walk the copy makes.
    size = sum(tree_size(m.skill_path) for m in selected)
    if size > INSTALL_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"bundle is {size} bytes, over the {INSTALL_MAX_BYTES} byte limit",
        )

    root.mkdir(parents=True, exist_ok=True)
    installed = [m.name for m in selected]
    skipped_symlinks: dict[str, list[str]] = {}
    provenance = {
        "repo_url": repo_url,
        "ref": ref,
        "resolved_sha": sha,
        "subdir": subdir,
        "skill_roots": sorted(str(p.relative_to(clone_dir)) for p in source_roots),
        "skills": installed,
        "requested": sorted(only) if only is not None else None,
    }
    for m in selected:
        links = _replace_skill_dir(
            m.skill_path,
            root / m.name,
            root / _STAGING_DIRNAME,
            {
                **provenance,
                "skill": m.name,
                "source_path": str(m.skill_path.relative_to(clone_dir)),
            },
        )
        if links:
            logger.warning("Installed %s without its symlinks: %s", m.name, links)
            skipped_symlinks[m.name] = links

    return _InstallResult(installed, sha, skipped_symlinks, skipped_skills)
