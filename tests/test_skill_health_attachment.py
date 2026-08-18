"""Skill health verdicts and agent attachment resolution.

The regression these guard against is specific and has already happened once:
``skills/root-cause-analysis`` shipped as a SKILL.md whose procedure runs
``scripts/cli.py``, without the ``scripts/`` directory and into a subprocess
that grants no ``Bash``. It loaded cleanly, reported ``sdk_visible: true`` and
was inert. Health has to call that unusable, and it has to stay quiet about the
skills that are merely untidy — a check that fires on most of the fleet is one
nobody reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.skills.attachment import (
    Attachment,
    clear_override,
    derive,
    load_state,
    resolve,
    save_override,
    skills_by_agent,
)
from src.skills.health import ToolSurface, assess
from src.skills.loader import SkillLoader

AGENTS = frozenset({"cost", "aap2", "icinga", "security", "babylon", "ocpv"})


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "Do the thing.") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return d


def _load_one(root: Path, name: str):
    manifests = SkillLoader.from_config(
        {"skills": {"project_root": str(root), "plugin_paths": [], "user_root": ""}}
    ).load_all()
    return next(m for m in manifests if m.name == name)


def _surface(tools: set[str] | None = None) -> ToolSurface:
    names = tools or {"mcp__parsec__query_aap2", "mcp__parsec__query_splunk"}
    return ToolSurface(per_agent=dict.fromkeys(AGENTS, frozenset(names)))


# ------------------------------------------------------------------ health


def test_skill_requesting_bash_is_unusable(tmp_path):
    """Parsec withholds Bash on purpose; a skill built around it cannot run here."""
    _write_skill(
        tmp_path,
        "shell-skill",
        "name: shell-skill\n"
        "description: A skill that wants a shell to do its actual work here.\n"
        "allowed-tools:\n  - Bash\n  - Read\n",
    )
    m = _load_one(tmp_path, "shell-skill")
    health = assess(m, attached_agents=("aap2",), surface=_surface())

    assert health.status == "unusable"
    assert "Bash" in health.unsatisfied_tools
    assert any("does not grant" in r for r in health.reasons)


def test_skill_referencing_unshipped_scripts_is_unusable(tmp_path):
    """The root-cause-analysis case: procedure cites a payload that never arrived."""
    _write_skill(
        tmp_path,
        "copied-skill",
        "name: copied-skill\ndescription: Copied from elsewhere without its payload directory.\n",
        body="Run `.venv/bin/python scripts/cli.py analyze` and read schemas/summary.schema.json.",
    )
    m = _load_one(tmp_path, "copied-skill")
    health = assess(m, attached_agents=("aap2",), surface=_surface())

    assert health.status == "unusable"
    assert "scripts/cli.py" in health.missing_paths
    assert "schemas/summary.schema.json" in health.missing_paths
    assert health.assumes_shell_runtime is True


def test_shipped_payload_is_not_flagged(tmp_path):
    """A skill that actually carries its scripts must stay clean."""
    d = _write_skill(
        tmp_path,
        "complete-skill",
        "name: complete-skill\ndescription: A skill that ships the payload it references.\n",
        body="Run scripts/run.py to do the work.",
    )
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")

    m = _load_one(tmp_path, "complete-skill")
    health = assess(m, attached_agents=("cost",), surface=_surface())

    assert health.missing_paths == ()
    assert health.status == "ok"


def test_unattached_skill_is_orphaned(tmp_path):
    _write_skill(
        tmp_path,
        "lonely-skill",
        "name: lonely-skill\ndescription: Nothing can reach this skill at runtime.\n",
    )
    m = _load_one(tmp_path, "lonely-skill")
    health = assess(m, attached_agents=(), surface=_surface())

    assert health.status == "orphaned"
    assert any("no agent" in r for r in health.reasons)


def test_unresolvable_mcp_names_are_a_note_not_a_status(tmp_path):
    """Three shipped skills declare `mcp__reporting__*`, which never resolves.

    That is stale frontmatter, not breakage — Parsec approves bridged tools
    session-wide. Promoting it to a status would mark most of the fleet
    unhealthy and train operators to ignore the column.
    """
    _write_skill(
        tmp_path,
        "stale-names",
        "name: stale-names\n"
        "description: Declares a tool namespace that does not exist at runtime.\n"
        "allowed-tools:\n  - mcp__reporting__*\n",
    )
    m = _load_one(tmp_path, "stale-names")
    health = assess(m, attached_agents=("cost",), surface=_surface())

    assert health.status == "ok"
    assert health.notes and "do not resolve" in health.notes[0]


def test_wildcard_request_is_satisfied_by_a_matching_grant(tmp_path):
    _write_skill(
        tmp_path,
        "wildcard-skill",
        "name: wildcard-skill\n"
        "description: Uses a wildcard over a namespace that really is granted.\n"
        "allowed-tools:\n  - mcp__parsec__*\n",
    )
    m = _load_one(tmp_path, "wildcard-skill")
    health = assess(m, attached_agents=("aap2",), surface=_surface())

    assert health.unsatisfied_tools == ()
    assert health.status == "ok"


# -------------------------------------------------------------- attachment


def test_domain_alone_attaches_a_mounted_skill(tmp_path):
    """The point of the whole change: no Python edit for a well-formed skill."""
    _write_skill(
        tmp_path,
        "new-skill",
        "name: new-skill\n"
        "description: A freshly mounted skill that declares its own domain.\n"
        "parsec:\n  version: '1.0.0'\n  domain: aap2\n",
    )
    m = _load_one(tmp_path, "new-skill")
    agents, origin = derive(m, AGENTS, supplement={})

    assert agents == ("aap2",)
    assert origin == "domain"


def test_unknown_domain_is_dropped_not_invented(tmp_path):
    _write_skill(
        tmp_path,
        "odd-skill",
        "name: odd-skill\n"
        "description: Declares a domain that is not an agent in this deployment.\n"
        "parsec:\n  version: '1.0.0'\n  domain: nonsense\n",
    )
    m = _load_one(tmp_path, "odd-skill")
    agents, origin = derive(m, AGENTS, supplement={})

    assert agents == ()
    assert origin == "none"


def test_supplement_unions_with_domain_so_nothing_narrows(tmp_path):
    """Adopting domain-derivation must not detach an agent that used to have it."""
    _write_skill(
        tmp_path,
        "provision-lookup",
        "name: provision-lookup\n"
        "description: Shipped skill attached to three agents by the static map.\n"
        "parsec:\n  version: '1.0.0'\n  domain: cost\n",
    )
    m = _load_one(tmp_path, "provision-lookup")
    agents, origin = derive(
        m, AGENTS, supplement={"provision-lookup": ("cost", "security", "babylon")}
    )

    assert agents == ("babylon", "cost", "security")
    assert origin == "domain+supplement"


def test_override_replaces_derived_attachment(tmp_path):
    _write_skill(
        tmp_path,
        "movable",
        "name: movable\ndescription: A skill an operator moves to another agent.\n"
        "parsec:\n  version: '1.0.0'\n  domain: cost\n",
    )
    manifests = SkillLoader.from_config(
        {"skills": {"project_root": str(tmp_path), "plugin_paths": [], "user_root": ""}}
    ).load_all()

    resolved = resolve(
        manifests,
        known_agents=AGENTS,
        overrides={"movable": {"agents": ["icinga"], "enabled": True}},
    )
    assert resolved["movable"].agents == ("icinga",)
    assert resolved["movable"].origin == "override"


def test_disabling_detaches_everything(tmp_path):
    _write_skill(
        tmp_path,
        "switchable",
        "name: switchable\ndescription: A skill an operator switches off entirely.\n"
        "parsec:\n  version: '1.0.0'\n  domain: cost\n",
    )
    manifests = SkillLoader.from_config(
        {"skills": {"project_root": str(tmp_path), "plugin_paths": [], "user_root": ""}}
    ).load_all()

    resolved = resolve(
        manifests,
        known_agents=AGENTS,
        overrides={"switchable": {"agents": ["cost"], "enabled": False}},
    )
    assert resolved["switchable"].agents == ()
    assert resolved["switchable"].enabled is False


def test_override_roundtrip_and_reset(tmp_path):
    state = tmp_path / "skills_state.json"

    save_override(state, skill="a-skill", agents=["cost", "aap2"], enabled=True, actor="me@rh")
    assert load_state(state)["a-skill"]["agents"] == ["aap2", "cost"]

    assert clear_override(state, skill="a-skill") is True
    assert load_state(state) == {}
    assert clear_override(state, skill="a-skill") is False


def test_corrupt_state_file_yields_no_overrides(tmp_path):
    """A bad state file loses customisation, never availability."""
    state = tmp_path / "skills_state.json"
    state.write_text("{not json", encoding="utf-8")
    assert load_state(state) == {}


def test_state_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    state = tmp_path / "skills_state.json"
    save_override(state, skill="s1", agents=["cost"], enabled=True)
    save_override(state, skill="s2", agents=["aap2"], enabled=True)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".skills_state-")]
    assert leftovers == []
    assert set(json.loads(state.read_text())["overrides"]) == {"s1", "s2"}


def test_skills_by_agent_inverts_the_map():
    attachments = {
        "a": Attachment(skill="a", agents=("cost", "security"), origin="domain"),
        "b": Attachment(skill="b", agents=("cost",), origin="domain"),
        "c": Attachment(skill="c", agents=(), origin="none"),
    }
    assert skills_by_agent(attachments) == {"cost": ("a", "b"), "security": ("a",)}


@pytest.mark.parametrize("status", ["ok", "orphaned"])
def test_usable_covers_reachable_states(status):
    from src.skills.health import SkillHealth

    assert SkillHealth(status=status).usable is True
    assert SkillHealth(status="unusable").usable is False


# ------------------------------------------------------- env-var config casing


def test_env_var_style_uppercase_skills_config_is_honoured(tmp_path):
    """skills.* supplied purely by environment must not be silently ignored.

    Found on a live pod, not in review. Dynaconf materialises
    ``PARSEC_SKILLS__INSTALL_ENABLED=true`` as ``{"SKILLS": {"INSTALL_ENABLED":
    True}}`` — UPPERCASE, because config.yaml did not already declare that key.
    Keys that *are* in config.yaml (``plugin_paths``) stay lowercase, so a
    mixed-case section is the normal deployed state and a plain lowercase read
    silently dropped every deploy-var override: install stayed off and the state
    file went to the wrong path.

    This is the same class of bug as ``agent.sdk.*``, which is why
    ``src.llm.config_section.section`` exists. Every skills config read now goes
    through it.
    """
    from src.routes.skills import _skills_section
    from src.skills.attachment import state_path

    cfg = {
        "SKILLS": {
            "plugin_paths": ["/app/data/installed-skills"],
            "INSTALL_ENABLED": True,
            "INSTALL_ROOT": "/app/data/installed-skills",
            "STATE_PATH": str(tmp_path / "state.json"),
        }
    }

    resolved = _skills_section(cfg)
    assert resolved["install_enabled"] is True, "uppercase env key was dropped"
    assert resolved["install_root"] == "/app/data/installed-skills"
    assert resolved["plugin_paths"] == ["/app/data/installed-skills"]
    assert state_path(cfg) == tmp_path / "state.json"


def test_loader_reads_uppercase_env_supplied_roots(tmp_path):
    """The loader has the same exposure: env-only roots must still be found."""
    _write_skill(
        tmp_path,
        "env-mounted",
        "name: env-mounted\ndescription: Discovered from an env-var-supplied project root.\n",
    )
    manifests = SkillLoader.from_config(
        {"SKILLS": {"PROJECT_ROOT": str(tmp_path), "PLUGIN_PATHS": [], "USER_ROOT": ""}}
    ).load_all()

    assert [m.name for m in manifests] == ["env-mounted"]
