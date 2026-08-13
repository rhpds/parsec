"""Skills discovered by the loader must also be loadable by the Agent SDK.

The two discovery planes are independent: ``SkillLoader`` reads
``skills.project_root`` / ``skills.plugin_paths`` / ``skills.user_root`` and
backs ``GET /api/skills``, while the SDK reads only ``<cwd>/.claude/skills``.
Before ``sync_sdk_skill_root`` a skill mounted at a plugin path appeared in the
Skills tab and could never execute.
"""

from __future__ import annotations

from pathlib import Path

from src.skills import SkillLoader, SkillSource, sdk_skills_root, sync_sdk_skill_root

SKILL_MD = """---
name: {name}
description: A test skill used to verify SDK discovery wiring end to end.
---

# {name}

Body.
"""


def _make_skill(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD.format(name=name))
    return d


def _load(*sources: SkillSource):
    return SkillLoader(list(sources)).load_all()


def test_plugin_skill_becomes_sdk_visible(tmp_path):
    """The headline case: an externally mounted skill can actually be loaded."""
    project = tmp_path / "skills"
    plugin = tmp_path / "opt" / "rhdp-rca-plugin" / "skills"
    _make_skill(project, "icinga-triage")
    _make_skill(plugin, "root-cause-analysis")

    manifests = _load(
        SkillSource(label="project", root=project),
        SkillSource(label="plugin", root=plugin),
    )
    assert {m.name for m in manifests} == {"icinga-triage", "root-cause-analysis"}

    published = sync_sdk_skill_root(manifests, cwd=tmp_path)

    root = sdk_skills_root(tmp_path)
    assert set(published) == {"icinga-triage", "root-cause-analysis"}
    # What the SDK sees is exactly what the loader reported.
    assert {p.name for p in root.iterdir()} == {m.name for m in manifests}
    # And the SKILL.md is readable through the published path.
    assert (root / "root-cause-analysis" / "SKILL.md").is_file()
    assert "root-cause-analysis" in (root / "root-cause-analysis" / "SKILL.md").read_text()


def test_legacy_root_symlink_is_replaced_with_a_real_directory(tmp_path):
    """The image ships /app/.claude/skills -> /app/skills.

    Publishing into that symlink would write into skills/, where the loader
    rejects symlinked skill directories. So the root itself must become real.
    """
    project = tmp_path / "skills"
    plugin = tmp_path / "plugin-skills"
    _make_skill(project, "cost-spike-investigation")
    _make_skill(plugin, "root-cause-analysis")

    root = sdk_skills_root(tmp_path)
    root.parent.mkdir(parents=True)
    root.symlink_to(project, target_is_directory=True)
    assert root.is_symlink()

    manifests = _load(
        SkillSource(label="project", root=project),
        SkillSource(label="plugin", root=plugin),
    )
    sync_sdk_skill_root(manifests, cwd=tmp_path)

    assert not root.is_symlink(), "root must be a real directory"
    assert root.is_dir()
    assert {p.name for p in root.iterdir()} == {
        "cost-spike-investigation",
        "root-cause-analysis",
    }
    # The real skills directory was not damaged.
    assert (project / "cost-spike-investigation" / "SKILL.md").is_file()
    assert not (project / "root-cause-analysis").exists()


def test_publishing_does_not_make_the_loader_reject_skills(tmp_path):
    """Regression guard for the interaction between the two planes.

    ``SkillLoader._iter_skill_dirs`` skips symlinked skill directories. If
    publishing wrote symlinks into a source root, every published skill would
    vanish from ``GET /api/skills``. It must not.
    """
    project = tmp_path / "skills"
    plugin = tmp_path / "plugin-skills"
    _make_skill(project, "abuse-account-detection")
    _make_skill(plugin, "logs-fetcher")

    sources = [
        SkillSource(label="project", root=project),
        SkillSource(label="plugin", root=plugin),
    ]
    sync_sdk_skill_root(SkillLoader(sources).load_all(), cwd=tmp_path)

    after = {m.name for m in SkillLoader(sources).load_all()}
    assert after == {"abuse-account-detection", "logs-fetcher"}


def test_stale_links_are_pruned(tmp_path):
    """Unmounting a plugin path removes it from the SDK's view too."""
    project = tmp_path / "skills"
    _make_skill(project, "kept")
    _make_skill(project, "removed")
    sources = [SkillSource(label="project", root=project)]
    sync_sdk_skill_root(SkillLoader(sources).load_all(), cwd=tmp_path)

    root = sdk_skills_root(tmp_path)
    assert {p.name for p in root.iterdir()} == {"kept", "removed"}

    # Skill disappears from the source (e.g. the ConfigMap was unmounted).
    import shutil

    shutil.rmtree(project / "removed")
    sync_sdk_skill_root(SkillLoader(sources).load_all(), cwd=tmp_path)

    assert {p.name for p in root.iterdir()} == {"kept"}


def test_real_directories_in_the_root_are_never_deleted(tmp_path):
    """Pruning only removes symlinks — it can't destroy baked content."""
    project = tmp_path / "skills"
    _make_skill(project, "native")

    root = sdk_skills_root(tmp_path)
    root.mkdir(parents=True)
    baked = root / "baked-in-image"
    baked.mkdir()
    (baked / "SKILL.md").write_text(SKILL_MD.format(name="baked-in-image"))

    sync_sdk_skill_root(
        SkillLoader([SkillSource(label="project", root=project)]).load_all(), cwd=tmp_path
    )

    assert baked.is_dir()
    assert (baked / "SKILL.md").is_file()


def test_sync_is_idempotent(tmp_path):
    project = tmp_path / "skills"
    _make_skill(project, "one")
    sources = [SkillSource(label="project", root=project)]

    first = sync_sdk_skill_root(SkillLoader(sources).load_all(), cwd=tmp_path)
    second = sync_sdk_skill_root(SkillLoader(sources).load_all(), cwd=tmp_path)

    assert first == second
    assert {p.name for p in sdk_skills_root(tmp_path).iterdir()} == {"one"}


def test_missing_skill_target_is_skipped_not_fatal(tmp_path):
    """A half-mounted volume must not take the app down."""
    project = tmp_path / "skills"
    _make_skill(project, "present")
    manifests = SkillLoader([SkillSource(label="project", root=project)]).load_all()

    import shutil

    shutil.rmtree(project / "present")

    published = sync_sdk_skill_root(manifests, cwd=tmp_path)
    assert published == {}
