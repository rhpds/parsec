"""Per-agent SDK enablement and profile construction.

Replaces tests/test_icinga_sdk.py. The property that matters most here is that
``agent.runtime`` stays a universal kill switch: no agent may run on the SDK
when the runtime is ``legacy``, however ``enabled_agents`` is configured.
"""

from __future__ import annotations

import pytest

from src.agent.sdk_profiles import (
    DEFAULT_ENABLED_AGENTS,
    TURN_HEADROOM,
    enabled_sdk_agents,
    sdk_profile_for,
)

ALL_AGENTS = {"cost", "aap2", "babylon", "security", "ocpv", "icinga"}


def _cfg(**sdk):
    return {"agent": {"runtime": "sdk", "sdk": sdk}}


# ------------------------------------------------------------- enablement


def test_defaults_to_icinga_only():
    assert enabled_sdk_agents({}) == DEFAULT_ENABLED_AGENTS
    assert enabled_sdk_agents({}) == frozenset({"icinga"})


def test_explicit_list():
    assert enabled_sdk_agents(_cfg(enabled_agents=["cost", "ocpv"])) == {"cost", "ocpv"}


def test_comma_separated_string():
    """Env-var config (PARSEC_AGENT__SDK__ENABLED_AGENTS) arrives as a string."""
    assert enabled_sdk_agents(_cfg(enabled_agents="cost, ocpv ,icinga")) == {
        "cost",
        "ocpv",
        "icinga",
    }


def test_all_keyword_enables_every_agent():
    assert enabled_sdk_agents(_cfg(enabled_agents=["all"])) == ALL_AGENTS


def test_unknown_agents_are_dropped_not_fatal():
    """A hand-edited ConfigMap must not CrashLoopBackOff a 1-replica pod."""
    assert enabled_sdk_agents(_cfg(enabled_agents=["cost", "nope", "ocpv"])) == {"cost", "ocpv"}


def test_empty_list_disables_all():
    assert enabled_sdk_agents(_cfg(enabled_agents=[])) == frozenset()


@pytest.mark.parametrize("bogus", [42, True, {"a": 1}])
def test_malformed_value_falls_back_to_default(bogus):
    assert enabled_sdk_agents(_cfg(enabled_agents=bogus)) == DEFAULT_ENABLED_AGENTS


# ------------------------------------------------------- the kill switch


@pytest.mark.parametrize("agent", sorted(ALL_AGENTS))
def test_runtime_legacy_beats_enabled_agents(agent):
    """The documented rollback is `PARSEC_AGENT__RUNTIME=legacy`. It must win."""
    from src.agent.agents import _should_use_sdk

    cfg = {"agent": {"runtime": "legacy", "sdk": {"enabled_agents": ["all"]}}}
    assert _should_use_sdk(agent, cfg) is False


@pytest.mark.parametrize("agent", sorted(ALL_AGENTS))
def test_runtime_sdk_plus_enabled_routes_to_sdk(agent):
    from src.agent.agents import _should_use_sdk

    assert _should_use_sdk(agent, _cfg(enabled_agents=["all"])) is True


def test_enabled_but_runtime_unset_stays_legacy():
    from src.agent.agents import _should_use_sdk

    assert _should_use_sdk("cost", {"agent": {"sdk": {"enabled_agents": ["all"]}}}) is False


def test_agent_not_listed_stays_legacy():
    from src.agent.agents import _should_use_sdk

    assert _should_use_sdk("cost", _cfg(enabled_agents=["icinga"])) is False


# ---------------------------------------------------------------- profile


@pytest.fixture
def _fake_sdk(monkeypatch):
    """Stub the SDK so profiles can be built without the real package."""
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "tool", lambda n, d, s: (lambda fn: fn), raising=False)
    monkeypatch.setattr(
        claude_agent_sdk,
        "create_sdk_mcp_server",
        lambda name, version, tools: {"name": name, "count": len(tools)},
        raising=False,
    )


@pytest.mark.parametrize("agent", sorted(ALL_AGENTS))
def test_every_agent_gets_a_usable_profile(agent, _fake_sdk):
    """The old icinga_sdk returned {} for five of six agents — that is the bug."""
    profile = sdk_profile_for(agent, _cfg())

    assert "parsec" in profile["mcp_servers"], f"{agent} has no tool surface"
    assert profile["allowed_tools"], f"{agent} has an empty allow-list"
    assert all(t.startswith("mcp__parsec__") for t in profile["allowed_tools"])
    assert profile["max_turns"] > 0


def test_max_turns_exceeds_legacy_max_rounds(_fake_sdk):
    """The model spends turns on ToolSearch, so parity needs headroom."""
    from src.agent.agents import AGENTS

    for agent, cfg in AGENTS.items():
        profile = sdk_profile_for(agent, _cfg())
        assert profile["max_turns"] == cfg.max_rounds + TURN_HEADROOM
        assert profile["max_turns"] > cfg.max_rounds


def test_skills_attached_to_the_agents_that_declare_them(_fake_sdk):
    """An agent gets every skill mapped to it, not just the first.

    These were one-skill-per-agent assertions. Three shipped skills were then
    reachable by no agent at all — a SKILL.md nobody can load is dead weight —
    so _AGENT_SKILLS maps each agent to a tuple and the profile carries all of
    them. Asserting a superset keeps this from breaking every time a skill is
    added, while still catching a skill silently detaching.
    """
    icinga = sdk_profile_for("icinga", _cfg())["skills"]
    assert icinga == ["icinga-triage"]

    cost = sdk_profile_for("cost", _cfg())["skills"]
    assert "cost-anomaly-triage" in cost
    assert len(cost) > 1, "cost declares several skills; only one was attached"

    aap2 = sdk_profile_for("aap2", _cfg())["skills"]
    assert "root-cause-analysis" in aap2, "the vendored rhdp-rca-plugin skill is orphaned"


def test_agent_with_no_skills_still_gets_tools(_fake_sdk):
    """ocpv maps to no skill, and must still be a usable agent."""
    profile = sdk_profile_for("ocpv", _cfg())
    assert not profile.get("skills")
    assert profile["allowed_tools"]


def test_unknown_agent_returns_empty_profile(_fake_sdk):
    assert sdk_profile_for("nonexistent", _cfg()) == {}


def test_no_remote_mcp_servers_are_exposed(_fake_sdk):
    """Everything must flow through the bridge so src/tools/* keeps running.

    Exposing the raw icinga/github MCP servers is what bypassed the Icinga write
    guardrails and GitHub secret redaction on the Phase-2 pilot.
    """
    for agent in sorted(ALL_AGENTS):
        servers = sdk_profile_for(agent, _cfg())["mcp_servers"]
        assert set(servers) == {"parsec"}, f"{agent} exposes a non-bridged server: {servers}"


def test_writes_are_off_unless_configured(_fake_sdk, monkeypatch):
    captured = {}

    def _spy(schemas, *, allow_writes=False):
        captured["allow_writes"] = allow_writes
        return {"name": "parsec"}

    monkeypatch.setattr("src.agent.parsec_mcp.build_server", _spy)

    sdk_profile_for("icinga", _cfg())
    assert captured["allow_writes"] is False

    sdk_profile_for("icinga", _cfg(allow_writes=True))
    assert captured["allow_writes"] is True


def test_env_var_style_uppercase_config_is_honoured(_fake_sdk):
    """agent.sdk.* supplied by environment must not be silently ignored.

    Dynaconf materialises PARSEC_AGENT__SDK__ORCHESTRATOR=true as
    {"AGENT": {"SDK": {"ORCHESTRATOR": True}}}. `section()` used to read the
    second level off a plain dict, case-sensitively, so every agent.sdk.*
    override set by env var was dropped — the runtime flipped but the
    orchestrator, enabled_agents and allow_writes silently kept their defaults.
    """
    from src.agent.orchestrator import _should_orchestrate_via_sdk
    from src.agent.sdk_profiles import _sdk_section, enabled_sdk_agents

    cfg = {
        "AGENT": {
            "RUNTIME": "sdk",
            "SDK": {"ORCHESTRATOR": True, "ENABLED_AGENTS": "all", "ALLOW_WRITES": True},
        }
    }
    assert _sdk_section(cfg).get("orchestrator") is True
    assert _sdk_section(cfg).get("allow_writes") is True
    assert len(enabled_sdk_agents(cfg)) > 1, "ENABLED_AGENTS=all was dropped"
    assert _should_orchestrate_via_sdk(cfg) is True


def test_lowercase_yaml_style_config_still_works(_fake_sdk):
    """The YAML path must be unchanged by the case-insensitive lookup."""
    from src.agent.sdk_profiles import _sdk_section

    cfg = {"agent": {"runtime": "sdk", "sdk": {"orchestrator": True}}}
    assert _sdk_section(cfg).get("orchestrator") is True
