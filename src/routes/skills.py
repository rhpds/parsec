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
allowlisted set of hosts, and it records provenance for everything it writes.
Turning it on is a deliberate decision, not a default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Request

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
    state_path,
)
from src.skills.health import assess, build_tool_surface
from src.skills.sdk_root import sdk_skills_root

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
    try:
        return section(section(cfg, "agent"), "sdk").get("cwd") or None
    except Exception:
        return None


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
    counts = {"ok": 0, "degraded": 0, "orphaned": 0, "unusable": 0}

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

    section_cfg = _skills_section(cfg)
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
        save_override(
            state_path(cfg), skill=name, agents=agents, enabled=enabled, actor=user or "unknown"
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
        removed = clear_override(state_path(cfg), skill=name)
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
    bounded clone, size cap, then a write confined to the configured install
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
    subdir = str(payload.get("subdir", "skills")).strip().strip("/")

    raw_only = payload.get("skills")
    only: set[str] | None = None
    if raw_only is not None:
        if not isinstance(raw_only, list):
            raise HTTPException(status_code=400, detail="'skills' must be a list of names")
        only = {str(x) for x in raw_only}
        bad = sorted(n for n in only if not _SKILL_NAME_RE.match(n))
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
        installed, sha = await _clone_and_install(repo_url, ref, subdir, root, only)
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
        "Installed %d skills from %s@%s (%s) by %s", len(installed), repo_url, ref, sha[:8], user
    )
    return {
        "installed": installed,
        "repo_url": repo_url,
        "ref": ref,
        "resolved_sha": sha,
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

    shutil.rmtree(target)

    # Republish so the SDK root loses its symlink on the same request.
    try:
        manifests = SkillLoader.from_config(cfg).load_all()
        published = sync_sdk_skill_root(manifests, cwd=_sdk_cwd(cfg))
    except Exception:
        logger.exception("Removed %s but reload failed", name)
        published = {}

    logger.info("Skill %r uninstalled by %s", name, user)
    return {"uninstalled": name, "remaining": len(published), "published": sorted(published)}


async def _run(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command with a hard timeout, returning (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
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


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


async def _clone_and_install(
    repo_url: str, ref: str, subdir: str, root: Path, only: set[str] | None = None
) -> tuple[list[str], str]:
    """Clone at ``ref``, copy each skill directory into ``root``, record provenance.

    ``--depth 1`` against an explicit ref keeps the fetch small. The tree is
    copied with symlinks skipped, so a bundle cannot smuggle a link that escapes
    the install root once it is published into the SDK's discovery directory.
    """
    with tempfile.TemporaryDirectory(prefix="skill-install-") as tmp:
        clone_dir = Path(tmp) / "repo"
        rc, _, err = await _run(
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref,
            "--single-branch",
            repo_url,
            str(clone_dir),
        )
        if rc != 0:
            # A SHA cannot be used with --branch; fall back to a full clone + checkout.
            rc2, _, err2 = await _run("git", "clone", repo_url, str(clone_dir))
            if rc2 != 0:
                raise HTTPException(
                    status_code=400, detail=f"git clone failed: {err.strip() or err2.strip()}"
                )
            rc3, _, err3 = await _run("git", "checkout", ref, cwd=str(clone_dir))
            if rc3 != 0:
                raise HTTPException(
                    status_code=400, detail=f"git checkout {ref} failed: {err3.strip()}"
                )

        rc, sha_out, _ = await _run("git", "rev-parse", "HEAD", cwd=str(clone_dir))
        sha = sha_out.strip() if rc == 0 else "unknown"

        source_root = clone_dir / subdir if subdir else clone_dir
        if not source_root.is_dir():
            raise HTTPException(status_code=400, detail=f"subdir {subdir!r} not found in repo")

        size = _dir_size(source_root)
        if size > INSTALL_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"bundle is {size} bytes, over the {INSTALL_MAX_BYTES} byte limit",
            )

        root.mkdir(parents=True, exist_ok=True)
        installed: list[str] = []
        for child in sorted(source_root.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            if not (child / "SKILL.md").is_file():
                continue
            if not _SKILL_NAME_RE.match(child.name):
                logger.warning("Skipping skill dir with unusable name: %s", child.name)
                continue
            if only is not None and child.name not in only:
                # Selective install. Pulling a whole repo drags in skills that
                # cannot run here (ET's shell-based ones) and templates that
                # were never meant to ship, and every one of them then needs
                # explaining in the UI.
                continue
            dest = root / child.name
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(
                child, dest, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", ".git")
            )
            installed.append(child.name)

        if not installed:
            raise HTTPException(
                status_code=400, detail=f"no SKILL.md directories found under {subdir!r}"
            )

        provenance = {
            "repo_url": repo_url,
            "ref": ref,
            "resolved_sha": sha,
            "subdir": subdir,
            "skills": installed,
            "requested": sorted(only) if only is not None else None,
        }
        # One record per installed skill, inside the skill. A single file at the
        # install root would be read by every skill sharing that root via the
        # parent lookup, so a later bundle would silently relabel an earlier
        # one — and it would outlive an uninstall as a stale claim of provenance.
        for name in installed:
            try:
                (root / name / ".parsec-provenance.json").write_text(
                    json.dumps({**provenance, "skill": name}, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                logger.exception("Installed %s but could not write its provenance", name)

        return installed, sha
