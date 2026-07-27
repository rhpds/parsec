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

# Parsec-authored skills. These carry `parsec.version` frontmatter and must load
# with zero warnings — we own them, so a warning here is our bug. Update this set
# when adding or removing a Parsec-authored skill under skills/.
NATIVE_SKILLS = {
    "cost-spike-investigation",
    "abuse-account-detection",
    "aap2-job-failure-triage",
    "icinga-triage",
}

# Skills vendored from another repo (e.g. redhat-et/rhdp-rca-plugin). They must
# still load strictly, but we do not own their frontmatter, so `parsec.version`
# and a zero-warning bill of health are not required of them.
VENDORED_SKILLS: set[str] = set()

EXPECTED_SKILLS = NATIVE_SKILLS | VENDORED_SKILLS


def _load() -> list:
    loader = SkillLoader([SkillSource(label="project", root=SKILLS_ROOT)])
    return loader.load_strict()  # raises on any structurally invalid shipped skill


def test_shipped_skills_load_strictly_and_match_expected():
    manifests = _load()
    assert {m.name for m in manifests} == EXPECTED_SKILLS


def test_expected_skills_match_the_directory_listing():
    """The declared sets must match what `COPY skills/` actually ships.

    Guards the failure mode this test hit on main: PR #34 added
    skills/icinga-triage/ without updating the expected set, and CI ran no
    tests, so the suite stayed red for six weeks unnoticed.
    """
    on_disk = {p.name for p in SKILLS_ROOT.iterdir() if p.is_dir()}
    assert on_disk == EXPECTED_SKILLS


def test_native_skills_are_parsec_native_with_no_warnings():
    for m in _load():
        if m.name not in NATIVE_SKILLS:
            continue
        assert m.is_parsec_native, f"{m.name} is missing parsec.version"
        assert m.warnings == (), f"{m.name} has validation warnings: {m.warnings}"
        assert m.description  # non-empty
        assert m.source == "project"


def test_vendored_skills_load_and_describe_themselves():
    """Vendored skills get the relaxed bar: they load, and they say what they do."""
    for m in _load():
        if m.name not in VENDORED_SKILLS:
            continue
        assert m.description, f"{m.name} has no description"
        assert m.source == "project"
