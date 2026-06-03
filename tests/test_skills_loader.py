"""Tests for src.skills.loader and friends.

Fixtures live in tests/skills_fixtures/. Each subdirectory is one fixture
skill that exercises one validation outcome (valid, missing field, bad YAML,
unknown parsec keys, not-a-skill).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skills import (
    SkillLoader,
    SkillLoadError,
    SkillSource,
    SkillValidationError,
)
from src.skills.manifest import ParsecExtensions

FIXTURES = Path(__file__).parent / "skills_fixtures"


def _src(label: str = "project", root: Path = FIXTURES) -> SkillSource:
    return SkillSource(label=label, root=root)


# ----- discovery ---------------------------------------------------------


def test_load_all_finds_only_valid_skills_and_skips_broken_ones():
    """The forgiving loader returns valid skills and silently drops broken ones."""
    loader = SkillLoader([_src()])
    manifests = loader.load_all()

    names = {m.name for m in manifests}
    assert "valid-minimal" in names
    assert "valid-with-parsec" in names
    assert "unknown-parsec-key" in names
    # Broken ones must NOT appear
    assert "missing-name" not in names
    assert "invalid-yaml" not in names


def test_load_all_skips_directories_without_skill_md():
    """A subdirectory of the source root without a SKILL.md is silently ignored."""
    loader = SkillLoader([_src()])
    manifests = loader.load_all()
    assert all(m.name != "not-a-skill-dir" for m in manifests)


def test_load_all_returns_empty_when_source_root_missing(tmp_path: Path):
    """Missing optional source roots don't raise — just yield nothing."""
    loader = SkillLoader([SkillSource(label="project", root=tmp_path / "nope")])
    assert loader.load_all() == []


def test_load_all_raises_when_required_source_missing(tmp_path: Path):
    """Required sources raise so misconfiguration surfaces loudly."""
    loader = SkillLoader([SkillSource(label="project", root=tmp_path / "nope", required=True)])
    with pytest.raises(SkillLoadError):
        loader.load_all()


def test_load_all_skips_when_source_root_is_a_file(tmp_path: Path):
    """If a configured source root is a file instead of a dir, skip it (with a warning)."""
    file_root = tmp_path / "not-a-dir"
    file_root.write_text("oops")
    loader = SkillLoader([SkillSource(label="project", root=file_root)])
    assert loader.load_all() == []


def test_symlinked_skill_directory_is_skipped(tmp_path: Path):
    """A symlinked child dir is skipped — iterdir() follows symlinks, so this
    guards against path traversal out of source.root."""
    # A real, valid skill living outside the source root...
    external = tmp_path / "external" / "escapee"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text(
        "---\nname: escapee\ndescription: only reachable through a symlink escape out of root\n---\n"
    )
    root = tmp_path / "root"
    root.mkdir()
    # ...reachable from inside the root only via a symlink.
    link = root / "escapee"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support directory symlinks")

    loader = SkillLoader([SkillSource(label="plugin", root=root)])
    assert loader.load_all() == []


# ----- parsing & validation ---------------------------------------------


def test_minimal_skill_parses_with_expected_defaults():
    loader = SkillLoader([_src()])
    manifests = {m.name: m for m in loader.load_all()}
    m = manifests["valid-minimal"]

    assert m.description.startswith("A minimal valid skill")
    assert m.allowed_tools == ()
    assert m.license is None
    assert m.metadata == {}
    assert m.parsec == ParsecExtensions()
    assert m.is_parsec_native is False
    assert m.source == "project"
    assert m.body.startswith("# Valid Minimal Skill")


def test_parsec_extensions_parsed_in_full():
    loader = SkillLoader([_src()])
    manifests = {m.name: m for m in loader.load_all()}
    m = manifests["valid-with-parsec"]

    assert m.is_parsec_native is True
    assert m.parsec.version == "1.0.0"
    assert m.parsec.domain == "cost"
    assert m.parsec.requires_mcp == ("reporting", "github")
    assert m.parsec.cost_estimate_per_call_usd == 0.15
    assert m.parsec.permissions == {"bash": {"allowed_paths": ["/tmp", "./reports"]}}
    assert m.parsec.extra == {}

    assert m.license == "MIT"
    assert m.metadata == {"author": "parsec-team", "tags": ["test", "fixture"]}
    assert m.allowed_tools == ("Bash", "Read", "mcp__reporting__*")


def test_unknown_parsec_key_loads_with_warning():
    loader = SkillLoader([_src()])
    manifests = {m.name: m for m in loader.load_all()}
    m = manifests["unknown-parsec-key"]

    # Loaded but warned
    assert m.parsec.version == "1.0.0"
    assert "this_is_not_a_real_key" in m.parsec.extra
    assert "another_unknown" in m.parsec.extra
    assert any("unknown parsec.* keys ignored" in w for w in m.warnings)


def test_strict_load_surfaces_validation_errors():
    """load_strict() raises on the first invalid skill instead of skipping."""
    loader = SkillLoader([_src()])
    with pytest.raises((SkillLoadError, SkillValidationError)):
        loader.load_strict()


def test_missing_name_raises_validation_error_in_strict_mode(tmp_path: Path):
    """Isolated reproduction of the missing-name fixture failure mode."""
    skill_dir = tmp_path / "no-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\ndescription: hi there\n---\n\nbody\n")

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    with pytest.raises(SkillValidationError) as exc:
        loader.load_strict()
    assert exc.value.field == "name"


def test_invalid_yaml_raises_load_error_in_strict_mode(tmp_path: Path):
    skill_dir = tmp_path / "bad-yaml"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: bad-yaml\nallowed-tools: [oops\n---\n\nbody\n")

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    with pytest.raises(SkillLoadError):
        loader.load_strict()


def test_missing_frontmatter_raises(tmp_path: Path):
    skill_dir = tmp_path / "no-fm"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Just a markdown file, no frontmatter\n")

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    with pytest.raises(SkillLoadError):
        loader.load_strict()


def test_empty_frontmatter_raises(tmp_path: Path):
    skill_dir = tmp_path / "empty-fm"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\n\n---\n\nbody\n")

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    with pytest.raises(SkillLoadError):
        loader.load_strict()


def test_name_must_match_kebab_case(tmp_path: Path):
    skill_dir = tmp_path / "BadName"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: BadName\ndescription: uppercase name should fail validation here\n---\n"
    )

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    with pytest.raises(SkillValidationError) as exc:
        loader.load_strict()
    assert exc.value.field == "name"


def test_name_folder_mismatch_warns(tmp_path: Path):
    skill_dir = tmp_path / "folder-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: declared-name\ndescription: name differs from folder — should warn not fail\n---\n"
    )

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    manifests = loader.load_all()
    assert len(manifests) == 1
    assert any("does not match folder name" in w for w in manifests[0].warnings)


def test_oversized_skill_md_is_rejected(tmp_path: Path):
    """A SKILL.md above the size cap is rejected rather than read whole into memory."""
    from src.skills.loader import MAX_SKILL_SIZE_BYTES

    skill_dir = tmp_path / "huge"
    skill_dir.mkdir()
    padding = "x" * (MAX_SKILL_SIZE_BYTES + 1)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: huge\ndescription: an enormous skill file that exceeds the size limit\n---\n{padding}"
    )

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    # Forgiving mode drops it...
    assert loader.load_all() == []
    # ...strict mode surfaces it loudly.
    with pytest.raises(SkillLoadError):
        loader.load_strict()


def test_short_description_warns(tmp_path: Path):
    skill_dir = tmp_path / "short"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: short\ndescription: tiny\n---\n")

    loader = SkillLoader([SkillSource(label="project", root=tmp_path)])
    manifests = loader.load_all()
    assert any("very short" in w for w in manifests[0].warnings)


# ----- multi-source priority & dedupe ------------------------------------


def test_duplicate_skill_name_keeps_first_source(tmp_path: Path):
    """Project sources win over plugin sources for the same skill name."""
    project_root = tmp_path / "project"
    plugin_root = tmp_path / "plugin"
    for root in (project_root, plugin_root):
        d = root / "shared"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: shared\ndescription: copy under {root.name} for dedupe test purposes\n---\n"
        )

    loader = SkillLoader(
        [
            SkillSource(label="project", root=project_root),
            SkillSource(label="plugin", root=plugin_root),
        ]
    )
    manifests = loader.load_all()
    assert len(manifests) == 1
    assert manifests[0].source == "project"


# ----- from_config -------------------------------------------------------


def test_from_config_with_plain_dict(tmp_path: Path):
    """Loader can be built from a plain dict config (not just Dynaconf)."""
    skill_dir = tmp_path / "dict-test"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: dict-test\ndescription: tests config dispatch through plain dicts here\n---\n"
    )

    config = {
        "skills": {
            "project_root": str(tmp_path),
            "plugin_paths": [],
            "user_root": "",
        }
    }
    loader = SkillLoader.from_config(config)
    manifests = loader.load_all()
    assert {m.name for m in manifests} == {"dict-test"}


def test_from_config_with_dynaconf_like_object(tmp_path: Path):
    """Loader handles Dynaconf-style objects exposing .get + .to_dict on nested sections."""
    skill_dir = tmp_path / "dynaconf-test"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: dynaconf-test\ndescription: tests dynaconf-style dispatch through .to_dict.\n---\n"
    )

    class _Box(dict):
        def to_dict(self) -> dict:
            return dict(self)

    class _Cfg:
        def __init__(self, data: dict):
            self._data = data

        def get(self, key: str, default=None):
            return self._data.get(key, default)

    cfg = _Cfg({"skills": _Box(project_root=str(tmp_path), plugin_paths=[], user_root="")})
    loader = SkillLoader.from_config(cfg)
    manifests = loader.load_all()
    assert {m.name for m in manifests} == {"dynaconf-test"}


def test_from_config_with_no_skills_section(tmp_path: Path):
    """Missing skills section yields the default project root (./skills)."""
    loader = SkillLoader.from_config({})
    # No exception even though ./skills probably doesn't exist in test cwd.
    assert isinstance(loader.load_all(), list)


# ----- manifest shape ----------------------------------------------------


def test_manifest_is_immutable():
    """Frozen dataclasses raise FrozenInstanceError on attribute assignment."""
    import dataclasses

    loader = SkillLoader([_src()])
    m = next(x for x in loader.load_all() if x.name == "valid-minimal")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.name = "new-name"  # type: ignore[misc]


def test_manifest_skill_path_points_to_directory():
    loader = SkillLoader([_src()])
    m = next(x for x in loader.load_all() if x.name == "valid-with-parsec")
    assert m.skill_path.is_dir()
    assert m.skill_md_path == m.skill_path / "SKILL.md"
    assert (m.skill_path / "scripts" / "helper.py").exists()
