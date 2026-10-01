"""Every skill-resolution path must read the config the request runs under.

Startup and reload were fixed to publish into the same SDK root
(``src.skills.sdk_root.sdk_cwd``), but the paths that *consume* that root —
``skills_for``, ``sdk_profile_for``, the orchestrator's ``AgentDefinition``s and
the stream translator's skill badges — still resolved attachment from the global
config and the on-disk filter from ``Path.cwd()``. With ``agent.sdk.cwd`` set,
skills were published under it while the filter looked somewhere else, so every
mounted skill was dropped from every agent.

The fixture reproduces that split: the configured SDK home holds the skill, and
the process cwd holds a *different* published skill. The decoy matters: an
empty cwd would trip the "discovery unavailable, trust the map" fallback and
hide the bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ToolUseBlock

from src.agent.sdk_profiles import sdk_profile_for, skills_for
from src.agent.sdk_stream import SdkEventTranslator

SKILL = "mounted-cost-check"
DECOY = "cwd-only-decoy"


def _write_skill(root: Path, name: str, domain: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\n"
        f"parsec:\n  domain: {domain}\n---\n\nDo the thing.\n",
        encoding="utf-8",
    )
    return d


def _publish(sdk_home: Path, skill_dir: Path) -> None:
    """What ``sync_sdk_skill_root`` does at startup: a symlink under .claude/skills."""
    root = sdk_home / ".claude" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    (root / skill_dir.name).symlink_to(skill_dir, target_is_directory=True)


@pytest.fixture
def split_roots(tmp_path, monkeypatch):
    """SDK home with the skill; process cwd with only a decoy."""
    from src.config import get_config

    # Force Dynaconf to load config/config.yaml before the chdir below, so this
    # module run on its own cannot poison the shared config for later tests.
    get_config().get("agent")

    sdk_home = tmp_path / "sdk-home"
    _publish(sdk_home, _write_skill(sdk_home / "skills", SKILL, "cost"))

    proc_cwd = tmp_path / "proc-cwd"
    _publish(proc_cwd, _write_skill(proc_cwd / "decoy-src", DECOY, "cost"))
    monkeypatch.chdir(proc_cwd)
    return sdk_home


def _cfg(sdk_home: Path, *, state_path: Path | None = None) -> dict:
    skills: dict = {"project_root": str(sdk_home / "skills"), "plugin_paths": [], "user_root": ""}
    if state_path is not None:
        skills["state_path"] = str(state_path)
    return {
        "agent": {"runtime": "sdk", "sdk": {"cwd": str(sdk_home), "enabled_agents": ["cost"]}},
        "skills": skills,
    }


def _env_cfg(sdk_home: Path) -> dict:
    """The shape Dynaconf gives env-only settings (PARSEC_AGENT__SDK__CWD=...)."""
    return {
        "AGENT": {"RUNTIME": "sdk", "SDK": {"CWD": str(sdk_home), "ENABLED_AGENTS": "cost"}},
        "SKILLS": {"PROJECT_ROOT": str(sdk_home / "skills"), "PLUGIN_PATHS": []},
    }


@pytest.fixture
def _fake_sdk(monkeypatch):
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "tool", lambda n, d, s: (lambda fn: fn), raising=False)
    monkeypatch.setattr(
        claude_agent_sdk,
        "create_sdk_mcp_server",
        lambda name, version, tools: {"name": name, "count": len(tools)},
        raising=False,
    )


def _events(tr: SdkEventTranslator, blocks: list) -> str:
    return "".join(tr.translate(AssistantMessage(content=blocks, model="claude-sonnet-4-6")))


# ------------------------------------------------------------------ skills_for


def test_skills_for_filters_against_the_configured_sdk_root(split_roots):
    assert skills_for("cost", _cfg(split_roots)) == [SKILL]


def test_skills_for_honours_env_supplied_uppercase_keys(split_roots):
    """A cwd set only via PARSEC_AGENT__SDK__CWD must not read as unset."""
    assert skills_for("cost", _env_cfg(split_roots)) == [SKILL]


def test_skills_for_reads_overrides_from_the_configured_state_path(split_roots, tmp_path):
    """An operator override saved to skills.state_path must reach the agents.

    The override moves the skill from its derived agent (cost) to babylon; read
    from the default data/skills_state.json instead, it would be invisible.
    """
    state = tmp_path / "elsewhere" / "state.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps({"version": 1, "overrides": {SKILL: {"agents": ["babylon"], "enabled": True}}}),
        encoding="utf-8",
    )
    cfg = _cfg(split_roots, state_path=state)

    assert skills_for("babylon", cfg) == [SKILL]
    assert skills_for("cost", cfg) == []


# ------------------------------------------------------------ SDK consumers


def test_sdk_profile_preloads_the_configured_skill(split_roots, _fake_sdk):
    assert sdk_profile_for("cost", _cfg(split_roots))["skills"] == [SKILL]


def test_orchestrator_agent_definitions_preload_the_configured_skill(split_roots, _fake_sdk):
    from src.agent.sdk_orchestrator import _agent_definitions

    defs = _agent_definitions(_cfg(split_roots))
    assert defs["cost"].skills == [SKILL]


def test_agent_definitions_resolve_attachments_once_per_turn(split_roots, _fake_sdk, monkeypatch):
    """Six agents used to mean six full manifest loads and state reads before the SDK started."""
    import src.agent.sdk_profiles as profiles
    from src.agent.sdk_orchestrator import _agent_definitions

    calls: list[object] = []
    real = profiles.attachment_snapshot

    def counting(config=None):
        calls.append(config)
        return real(config)

    monkeypatch.setattr(profiles, "attachment_snapshot", counting)
    cfg = _cfg(split_roots)
    cfg["agent"]["sdk"]["enabled_agents"] = ["all"]

    defs = _agent_definitions(cfg)

    assert len(defs) > 1
    assert defs["cost"].skills == [SKILL]
    assert calls == [cfg]


@pytest.mark.parametrize("make_cfg", [_cfg, _env_cfg], ids=["yaml", "env"])
def test_translator_surfaces_skill_invoked_from_the_configured_root(split_roots, make_cfg):
    tr = SdkEventTranslator(question="q", history=[], config=make_cfg(split_roots))

    blob = _events(tr, [ToolUseBlock(id="t1", name="Skill", input={"command": SKILL})])
    assert "skill_used" in blob and SKILL in blob

    # Published only under the process cwd, which the SDK never scans.
    decoy = _events(tr, [ToolUseBlock(id="t2", name="Skill", input={"command": DECOY})])
    assert "skill_used" not in decoy


def test_translator_surfaces_preloaded_skills_at_delegation(split_roots):
    tr = SdkEventTranslator(question="q", history=[], config=_cfg(split_roots))

    blob = _events(tr, [ToolUseBlock(id="a1", name="Agent", input={"subagent_type": "cost"})])
    assert "agent_start" in blob
    assert SKILL in blob and "preloaded" in blob
    assert DECOY not in blob
