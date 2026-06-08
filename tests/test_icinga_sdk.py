"""Tests for the Icinga SDK profile + the icinga-triage skill."""

from __future__ import annotations

from pathlib import Path

from src.agent.icinga_sdk import (
    ICINGA_SKILL,
    build_icinga_sdk_profile,
    sdk_profile_for,
)
from src.skills.loader import SkillLoader, SkillSource

# --------------------------------------------------------- profile builder


def test_profile_both_servers() -> None:
    profile = build_icinga_sdk_profile(
        {
            "icinga": {"mcp_url": "http://icinga-mcp:8080/sse"},
            "github": {"mcp_url": "https://api.githubcopilot.com/mcp/"},
        }
    )
    assert profile["skills"] == [ICINGA_SKILL]
    assert profile["mcp_servers"]["icinga"] == {"type": "sse", "url": "http://icinga-mcp:8080/sse"}
    assert profile["mcp_servers"]["github"]["url"] == "https://api.githubcopilot.com/mcp/"
    assert set(profile["allowed_tools"]) == {"mcp__icinga", "mcp__github"}


def test_profile_github_token_becomes_auth_header() -> None:
    profile = build_icinga_sdk_profile(
        {"github": {"mcp_url": "https://gh/mcp", "token": "ght_abc"}}
    )
    assert profile["mcp_servers"]["github"]["headers"] == {"Authorization": "Bearer ght_abc"}


def test_profile_no_servers_only_skill() -> None:
    # Skill still loads even with no MCP configured (degrades gracefully).
    profile = build_icinga_sdk_profile({})
    assert profile == {"skills": [ICINGA_SKILL]}
    assert "mcp_servers" not in profile
    assert "allowed_tools" not in profile


def test_profile_only_icinga_configured() -> None:
    profile = build_icinga_sdk_profile({"icinga": {"mcp_url": "http://i/sse"}})
    assert profile["allowed_tools"] == ["mcp__icinga"]
    assert "github" not in profile["mcp_servers"]


# ------------------------------------------------------------ dispatch helper


def test_sdk_profile_for_icinga() -> None:
    assert sdk_profile_for("icinga", {})["skills"] == [ICINGA_SKILL]


def test_sdk_profile_for_other_agent_is_empty() -> None:
    assert sdk_profile_for("cost", {"icinga": {"mcp_url": "x"}}) == {}


# ---------------------------------------------------------- the skill itself


def test_icinga_triage_skill_loads_strict() -> None:
    """The shipped icinga-triage SKILL.md must load with zero warnings."""
    root = Path(__file__).resolve().parent.parent / "skills"
    loader = SkillLoader([SkillSource(label="project", root=root)])
    manifests = {m.name: m for m in loader.load_strict()}

    assert "icinga-triage" in manifests
    skill = manifests["icinga-triage"]
    assert skill.warnings == ()
    assert skill.parsec is not None
    assert skill.parsec.domain == "icinga"
    assert set(skill.allowed_tools) == {"query_icinga", "fetch_github_file", "search_github_repo"}
    # description drives SDK auto-discovery — must mention the trigger
    assert "alert" in (skill.description or "").lower()
