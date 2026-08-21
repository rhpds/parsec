"""Whether a discovered skill can actually do anything.

``SkillLoader`` answers "is this a well-formed SKILL.md". That is a much weaker
question than "will this work", and the gap between them is where the
``root-cause-analysis`` incident lived: a skill that parsed cleanly, loaded
without warnings, reported ``sdk_visible: true``, and could not execute a single
step of its own procedure because its ``scripts/`` directory was never vendored
and the SDK subprocess grants no ``Bash``.

Nothing in the loader was wrong. The inventory simply had no vocabulary for
"present but inert", so the Skills tab rendered a broken skill and a working one
identically. This module supplies that vocabulary. It is deliberately read-only
and side-effect free: it inspects manifests plus the tool surface Parsec
actually grants, and returns a verdict.

Three failure modes, all of which have real instances today:

* **unusable** — the skill's own procedure cannot run here. Either it asks for
  tools the subprocess does not expose (``Bash``, ``Read``, ``Write``), or its
  body references files that were never delivered alongside it.
* **orphaned** — no agent can reach it. It loads, it lists, and no code path
  will ever activate it.
* **degraded** — it works, but asks for at least one MCP tool that none of its
  attached agents can call, so part of its procedure will silently no-op.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

#: Built-in tools the SDK subprocess exposes. Mirrors
#: ``AgentSdkConfig.builtin_tools``. Anything outside this set and the bridged
#: ``mcp__parsec__*`` namespace is simply not callable, no matter what a
#: skill's ``allowed-tools`` frontmatter requests.
GRANTED_BUILTIN_TOOLS: frozenset[str] = frozenset({"ToolSearch", "Skill"})

#: Built-ins a skill may plausibly ask for that Parsec deliberately withholds,
#: because the subprocess cwd is ``/app`` beside mounted kubeconfigs and a GCP
#: service-account JSON. Requesting one of these is not a warning — it means the
#: skill was written for Claude Code's tool surface, not Parsec's.
WITHHELD_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
        "Glob",
        "Grep",
    }
)

#: Directories a skill may ship alongside SKILL.md. A reference to one of these
#: that does not resolve on disk means the skill was copied without its payload.
_PAYLOAD_DIRS = ("scripts", "schemas", "tests", "assets", "templates", "data", "prompts")

#: Relative paths referenced from a skill body. Matches ``scripts/cli.py`` and
#: bare ``requirements.txt``. Deliberately conservative: it only fires on the
#: payload directories above plus the one well-known root file, so ordinary
#: prose mentioning a word with a slash in it does not produce a false alarm.
_REF_RE = re.compile(
    r"(?<![\w./-])((?:" + "|".join(_PAYLOAD_DIRS) + r")/[\w][\w./-]*|requirements\.txt)"
)

#: A skill telling the model to build or use a Python virtualenv presumes a
#: shell. Flagged separately from a missing file because the venv legitimately
#: does not exist in the repo — its absence is not the problem, the assumption is.
_RUNTIME_RE = re.compile(r"\.venv|python3?\s+-m\s+venv|pip\s+install|npm\s+install")


@dataclass(frozen=True)
class SkillHealth:
    """Verdict for one skill against the tool surface Parsec actually grants."""

    status: str  # "ok" | "orphaned" | "unusable"
    reasons: tuple[str, ...] = ()
    #: Non-blocking observations — frontmatter hygiene, naming drift. Kept
    #: separate from ``reasons`` on purpose: a health signal that fires on most
    #: of the fleet is one operators learn to ignore, and then it protects
    #: nothing. Only genuine breakage moves ``status``.
    notes: tuple[str, ...] = ()
    requested_tools: tuple[str, ...] = ()
    unsatisfied_tools: tuple[str, ...] = ()
    missing_paths: tuple[str, ...] = ()
    attached_agents: tuple[str, ...] = ()
    assumes_shell_runtime: bool = False

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "orphaned")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "usable": self.usable,
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "requested_tools": list(self.requested_tools),
            "unsatisfied_tools": list(self.unsatisfied_tools),
            "missing_paths": list(self.missing_paths),
            "attached_agents": list(self.attached_agents),
            "assumes_shell_runtime": self.assumes_shell_runtime,
        }


@dataclass(frozen=True)
class ToolSurface:
    """The tools reachable from a given set of agents.

    Built once per request and reused across skills, because assembling the
    bridged tool names walks every agent's schema list.
    """

    builtin: frozenset[str] = field(default_factory=lambda: GRANTED_BUILTIN_TOOLS)
    #: agent_type -> the ``mcp__parsec__*`` names that agent may call.
    per_agent: dict[str, frozenset[str]] = field(default_factory=dict)

    def granted_for(self, agents: tuple[str, ...]) -> frozenset[str]:
        """Every tool name reachable by at least one of ``agents``."""
        granted = set(self.builtin)
        for a in agents:
            granted |= self.per_agent.get(a, frozenset())
        return frozenset(granted)


def build_tool_surface() -> ToolSurface:
    """Snapshot which bridged tools each agent can call.

    Imported lazily and defensively: the health view is a diagnostic, and a
    failure to introspect the agent registry must degrade the report rather than
    500 the endpoint that operators use to find out what is wrong.
    """
    per_agent: dict[str, frozenset[str]] = {}
    try:
        from src.agent.agents import AGENTS
        from src.agent.parsec_mcp import tool_names_for

        for agent_type, agent_cfg in AGENTS.items():
            try:
                per_agent[agent_type] = frozenset(tool_names_for(list(agent_cfg.tools)))
            except Exception:
                logger.exception("Could not resolve tools for agent %s", agent_type)
                per_agent[agent_type] = frozenset()
    except Exception:
        logger.exception("Could not build tool surface; health will omit tool checks")
    return ToolSurface(per_agent=per_agent)


def _missing_payload_paths(manifest: SkillManifest) -> tuple[str, ...]:
    """Relative paths the body references that do not exist in the skill dir.

    This is the check that catches a SKILL.md copied without its ``scripts/``.
    Matching is on the skill directory only — a reference is "missing" when the
    skill does not carry it, regardless of whether some other copy elsewhere on
    disk happens to have it.
    """
    root: Path = manifest.skill_path
    seen: dict[str, None] = {}
    for match in _REF_RE.finditer(manifest.body or ""):
        rel = match.group(1).rstrip(".,);:`'\"")
        if rel in seen:
            continue
        # A trailing glob or placeholder is a documentation convention, not a
        # concrete file; treat the directory as the thing that must exist.
        probe = root / rel
        try:
            if probe.exists():
                continue
            # `scripts/foo.py <ARG>` style references sometimes carry a suffix
            # the regex kept; fall back to the parent directory before flagging.
            if probe.parent.is_dir() and probe.parent != root:
                continue
        except OSError:
            logger.debug("Could not stat %s while checking skill %s", probe, manifest.name)
            continue
        seen[rel] = None
    return tuple(sorted(seen))


def assess(
    manifest: SkillManifest,
    *,
    attached_agents: tuple[str, ...],
    surface: ToolSurface,
) -> SkillHealth:
    """Return the health verdict for one skill.

    ``attached_agents`` is the resolved attachment (see
    :mod:`src.skills.attachment`), not the skill's own opinion — a skill cannot
    grant itself an agent.
    """
    requested = tuple(manifest.allowed_tools)
    granted = surface.granted_for(attached_agents)

    unsatisfied = tuple(
        t for t in requested if t not in granted and not _is_wildcard_match(t, granted)
    )
    withheld = tuple(t for t in unsatisfied if t in WITHHELD_BUILTIN_TOOLS)
    missing_paths = _missing_payload_paths(manifest)
    assumes_shell = bool(_RUNTIME_RE.search(manifest.body or ""))

    reasons: list[str] = []
    notes: list[str] = []
    status = "ok"

    if withheld:
        status = "unusable"
        reasons.append("requests tools Parsec does not grant: " + ", ".join(withheld))
    if missing_paths:
        status = "unusable"
        reasons.append("references files it did not ship: " + ", ".join(missing_paths))
    if assumes_shell and status != "unusable":
        # A shell assumption without a Bash request still means the procedure
        # cannot be followed, it just failed to declare why.
        status = "unusable"
        reasons.append(
            "procedure assumes a shell runtime (venv/pip/npm), which the subprocess has none"
        )
    elif assumes_shell:
        reasons.append("procedure assumes a shell runtime (venv/pip/npm)")

    if not attached_agents:
        # Orphaned outranks degraded but not unusable: a broken skill nobody can
        # reach is still primarily broken.
        if status == "ok":
            status = "orphaned"
        reasons.append("attached to no agent, so nothing can activate it")

    mcp_unsatisfied = tuple(t for t in unsatisfied if t not in WITHHELD_BUILTIN_TOOLS)
    if mcp_unsatisfied:
        # Not a status change. Parsec approves bridged tools session-wide, so a
        # skill naming a namespace that does not resolve (three shipped skills
        # declare `mcp__reporting__*`, but the Reporting tools are bridged as
        # `mcp__parsec__db_*`) is stale frontmatter, not a broken skill.
        notes.append(
            "declares tool names that do not resolve at runtime: " + ", ".join(mcp_unsatisfied)
        )

    return SkillHealth(
        status=status,
        reasons=tuple(reasons),
        notes=tuple(notes),
        requested_tools=requested,
        unsatisfied_tools=unsatisfied,
        missing_paths=missing_paths,
        attached_agents=attached_agents,
        assumes_shell_runtime=assumes_shell,
    )


def _is_wildcard_match(requested: str, granted: frozenset[str]) -> bool:
    """Whether a trailing-``*`` request is satisfied by any granted name.

    Skills legitimately declare ``mcp__reporting__*``; treating that as
    unsatisfied would flag every correct skill in the repo.
    """
    if not requested.endswith("*"):
        return False
    prefix = requested[:-1]
    return any(g.startswith(prefix) for g in granted)
