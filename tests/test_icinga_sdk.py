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
    # Read-only tools only. A bare ``mcp__icinga`` would match *every* tool the
    # server exposes, including the six state-mutating ones. [#32 review]
    assert set(profile["allowed_tools"]) == {
        "mcp__icinga__get_problems",
        "mcp__icinga__get_services",
        "mcp__icinga__get_hosts",
        "mcp__icinga__get_downtimes",
        "mcp__icinga__get_comments",
        "mcp__github__get_file_contents",
    }


def test_profile_excludes_write_actions_by_default() -> None:
    profile = build_icinga_sdk_profile(
        {"icinga": {"mcp_url": "http://i/sse"}, "github": {"mcp_url": "https://gh/mcp"}}
    )
    granted = set(profile["allowed_tools"])
    for write_tool in (
        "mcp__icinga__acknowledge_problem",
        "mcp__icinga__schedule_downtime",
        "mcp__icinga__reschedule_check",
        "mcp__icinga__add_comment",
        "mcp__icinga__remove_comment",
        "mcp__icinga__remove_downtime",
    ):
        assert write_tool not in granted, f"{write_tool} must not be granted by default"
    assert "mcp__icinga" not in granted, "bare server grant re-admits every write action"


def test_profile_writes_can_be_enabled_explicitly() -> None:
    profile = build_icinga_sdk_profile(
        {"icinga": {"mcp_url": "http://i/sse", "sdk_allow_writes": True}}
    )
    assert "mcp__icinga__acknowledge_problem" in profile["allowed_tools"]
    assert "mcp__icinga__get_problems" in profile["allowed_tools"]


def test_profile_allowed_tools_can_be_overridden() -> None:
    """A sidecar naming its tools differently is a config fix, not a redeploy."""
    profile = build_icinga_sdk_profile(
        {"icinga": {"mcp_url": "http://i/sse", "sdk_allowed_tools": ["mcp__icinga__list_alerts"]}}
    )
    assert profile["allowed_tools"] == ["mcp__icinga__list_alerts"]


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
    """Unconfigured servers contribute no tools — a partial config degrades cleanly."""
    profile = build_icinga_sdk_profile({"icinga": {"mcp_url": "http://i/sse"}})
    assert set(profile["allowed_tools"]) == {
        "mcp__icinga__get_problems",
        "mcp__icinga__get_services",
        "mcp__icinga__get_hosts",
        "mcp__icinga__get_downtimes",
        "mcp__icinga__get_comments",
    }
    assert "github" not in profile["mcp_servers"]
    assert not any(t.startswith("mcp__github") for t in profile["allowed_tools"])


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
    # Under the SDK runtime the icinga/github backends are MCP tools, so the skill's
    # allowed-tools list the real mcp__ names (not the legacy query_icinga wrappers).
    assert set(skill.allowed_tools) == {
        "mcp__icinga__get_hosts",
        "mcp__icinga__get_services",
        "mcp__icinga__get_problems",
        "mcp__icinga__get_downtimes",
        "mcp__icinga__get_comments",
        "mcp__github__get_file_contents",
    }
    # description drives SDK auto-discovery — must mention the trigger
    assert "alert" in (skill.description or "").lower()
