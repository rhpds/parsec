"""Make discovered skills reachable by the Claude Agent SDK.

Parsec has two independent skill-discovery planes that share a directory but
not a code path:

* :class:`~src.skills.loader.SkillLoader` reads ``skills.project_root``,
  ``skills.plugin_paths`` and ``skills.user_root``. It backs ``GET /api/skills``
  and the Skills UI tab. It does not execute anything.
* The Agent SDK discovers skills **only** under ``<cwd>/.claude/skills/``,
  because ``agent.sdk.setting_sources`` is ``["project"]``. That is what
  ``AgentSdkClient.complete(skills=[...])`` resolves against.

The image bridges the two for in-repo skills by symlinking ``/app/.claude/skills``
to ``/app/skills``. Anything mounted at a ``plugin_paths`` root is therefore
listed in the UI but is **invisible to the SDK** — it looks installed and can
never run, which is the worst failure mode available for a cross-team skill
dependency.

This module closes that gap: it materialises ``<cwd>/.claude/skills/`` as a real
directory of symlinks, one per discovered skill, so the set the SDK can execute
is exactly the set the loader reports.

Symlinking *here* is safe even though ``SkillLoader._iter_skill_dirs`` rejects
symlinked skill directories. The loader only ever scans the configured source
roots; it never scans ``.claude/skills``. The two planes stay disjoint — real
directories for the loader, symlinks for the SDK.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from src.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

#: Path, relative to the SDK subprocess cwd, that ``setting_sources: ["project"]``
#: scans for skills.
SDK_SKILLS_RELPATH = Path(".claude") / "skills"


def sdk_skills_root(cwd: str | os.PathLike[str] | None = None) -> Path:
    """Return the directory the SDK scans for skills."""
    base = Path(cwd) if cwd else Path.cwd()
    return base / SDK_SKILLS_RELPATH


def sync_sdk_skill_root(
    manifests: list[SkillManifest],
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, Path]:
    """Publish every discovered skill into the SDK's discovery root.

    Creates ``<cwd>/.claude/skills/<name>`` as a symlink to each skill's real
    directory, and removes symlinks it previously created that no longer
    correspond to a discovered skill.

    Returns a mapping of skill name to the published path, for the skills that
    are now SDK-visible. Failures are logged and skipped rather than raised:
    an unwritable ``.claude`` directory must not stop the app from serving the
    legacy runtime, which needs none of this.

    Real (non-symlink) directories already present in the root are left alone.
    In the shipped image ``/app/.claude/skills`` is itself a symlink to
    ``/app/skills``; that is replaced with a real directory here so native and
    plugin skills can coexist as siblings.
    """
    root = sdk_skills_root(cwd)
    published: dict[str, Path] = {}

    try:
        _ensure_real_directory(root)
    except OSError:
        logger.exception("Cannot prepare SDK skills root %s — SDK skills unavailable", root)
        return published

    wanted = {m.name: m.skill_path.resolve() for m in manifests}

    for name, target in wanted.items():
        link = root / name
        try:
            if not target.is_dir():
                logger.warning("Skill %r target %s is not a directory; skipping", name, target)
                continue
            if link.is_symlink():
                if Path(os.readlink(link)) == target:
                    published[name] = link
                    continue
                link.unlink()
            elif link.exists():
                # A real directory shipped in the image — already SDK-visible.
                published[name] = link
                continue
            link.symlink_to(target, target_is_directory=True)
            published[name] = link
        except OSError:
            logger.exception("Failed to publish skill %r to %s", name, link)

    _prune_stale_links(root, wanted)

    logger.info(
        "SDK skills root %s: %d/%d skills published (%s)",
        root,
        len(published),
        len(manifests),
        ", ".join(sorted(published)) or "none",
    )
    return published


def _ensure_real_directory(root: Path) -> None:
    """Make ``root`` a real directory, replacing a legacy root-level symlink.

    The image ships ``/app/.claude/skills -> /app/skills``. Publishing a plugin
    skill into that symlink would write into ``skills/``, where the loader would
    then reject it as a symlinked skill directory. So the root itself must be a
    real directory whose children are the symlinks.
    """
    if root.is_symlink():
        legacy_target = root.resolve()
        root.unlink()
        root.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Replaced legacy skills-root symlink %s -> %s with a real directory",
            root,
            legacy_target,
        )
        return
    root.mkdir(parents=True, exist_ok=True)


def _prune_stale_links(root: Path, wanted: dict[str, Path]) -> None:
    """Remove symlinks for skills that are no longer discovered.

    Only symlinks are removed — a real directory in the root is never deleted,
    so a mistake here can't destroy shipped content.
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        logger.exception("Cannot list SDK skills root %s", root)
        return

    for entry in entries:
        if not entry.is_symlink() or entry.name in wanted:
            continue
        try:
            entry.unlink()
            logger.info("Removed stale SDK skill link %s", entry)
        except OSError:
            logger.exception("Failed to remove stale SDK skill link %s", entry)
