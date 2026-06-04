"""Guards the SKILL.md files shipped in the repo's ``skills/`` root.

These ship inside the container image (``COPY skills/`` in dockerfiles/Dockerfile)
and populate ``GET /api/skills`` (and the Skills UI) on deployed Parsec. Loading
them with ``load_strict()`` here ensures a typo in frontmatter can't silently
ship a broken or warning-laden skill.
"""

from __future__ import annotations

from pathlib import Path

from src.skills import SkillLoader, SkillSource

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"

# The skills intentionally shipped in the image. Update this set when adding or
# removing a top-level skill under skills/.
EXPECTED_SKILLS = {
    "cost-spike-investigation",
    "abuse-account-detection",
    "aap2-job-failure-triage",
}


def _load() -> list:
    loader = SkillLoader([SkillSource(label="project", root=SKILLS_ROOT)])
    return loader.load_strict()  # raises on any structurally invalid shipped skill


def test_shipped_skills_load_strictly_and_match_expected():
    manifests = _load()
    assert {m.name for m in manifests} == EXPECTED_SKILLS


def test_shipped_skills_are_parsec_native_with_no_warnings():
    for m in _load():
        assert m.is_parsec_native, f"{m.name} is missing parsec.version"
        assert m.warnings == (), f"{m.name} has validation warnings: {m.warnings}"
        assert m.description  # non-empty
        assert m.source == "project"
