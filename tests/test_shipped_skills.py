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
#: Parsec-authored. We own these, so a warning here is our bug.
NATIVE_SKILLS = {
    "cost-spike-investigation",
    "abuse-account-detection",
    "aap2-job-failure-triage",
    "icinga-triage",
    "cost-anomaly-triage",
    "provision-lookup",
    "aap2-job-failure-rca",
}

#: Vendored from another repo — currently redhat-et/rhdp-rca-plugin. They must
#: load and describe themselves, but we do not own their frontmatter, so
#: `parsec.version` is not required of them.
VENDORED_SKILLS = {
    "root-cause-analysis",
}

EXPECTED_SKILLS = NATIVE_SKILLS | VENDORED_SKILLS


def _load() -> list:
    loader = SkillLoader([SkillSource(label="project", root=SKILLS_ROOT)])
    return loader.load_strict()  # raises on any structurally invalid shipped skill


def test_shipped_skills_load_strictly_and_match_expected():
    manifests = _load()
    assert {m.name for m in manifests} == EXPECTED_SKILLS


def test_expected_skills_match_the_directory_listing():
    """A new skill directory must not drift from the declared sets."""
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
    """Relaxed bar: they load and say what they do; frontmatter is upstream's."""
    seen = {m.name for m in _load() if m.name in VENDORED_SKILLS}
    assert seen == VENDORED_SKILLS
    for m in _load():
        if m.name in VENDORED_SKILLS:
            assert m.description, f"{m.name} has no description"


def test_every_shipped_skill_is_reachable_by_some_agent():
    """A skill no agent can use is dead weight that still looks installed.

    Three skills previously shipped, loaded, and appeared in the Skills tab
    while being attached to no agent at all.
    """
    from src.agent.sdk_profiles import _AGENT_SKILLS

    attached = {s for skills in _AGENT_SKILLS.values() for s in skills}
    orphans = EXPECTED_SKILLS - attached
    assert not orphans, f"skills shipped but unreachable by any agent: {sorted(orphans)}"
