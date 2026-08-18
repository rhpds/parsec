"""Which agents may use which skills, without a code change per skill.

Before this module, ``src/agent/sdk_profiles._AGENT_SKILLS`` was the only answer
to that question: a hardcoded dict, so mounting a new skill meant editing
Python, opening a PR, rebuilding the image and redeploying. The mount mechanism
was hot; the attachment was not, and the slow half set the pace.

Attachment is resolved from three layers, most specific first:

1. **Operator override** — a persisted decision made through the Skills tab.
   Authoritative: it replaces the derived answer entirely, including with an
   empty list, which is how a skill gets switched off.
2. **The skill's own ``parsec.domain``** — already present in the frontmatter
   and already carrying agent-shaped values (``cost``, ``aap2``, ``icinga``,
   ``security``). Seven of the eight shipped skills declare one that matches an
   agent key exactly, so this alone makes a well-formed mounted skill work with
   no code change.
3. **The static supplement** — the hand-tuned cross-domain attachments that a
   single ``domain`` cannot express, e.g. ``provision-lookup`` serving cost,
   security and babylon. Kept as a union with layer 2 rather than replacing it,
   so adopting domain-derivation does not silently narrow any shipped skill.

The store is a small JSON file under ``data/``, written atomically. It is
deliberately not a database: the whole point is that an operator can read it,
diff it, and delete it to return to derived behaviour.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

#: Type of the static supplement: skill name -> agents. Supplied by the caller
#: (``sdk_profiles`` inverts its own ``_AGENT_SKILLS``) rather than duplicated
#: here, so the shipped mapping stays single-sourced and this module has no
#: import edge back into the agent package.
SupplementMap = dict[str, tuple[str, ...]]

_STATE_VERSION = 1
_DEFAULT_STATE_PATH = Path("data") / "skills_state.json"


@dataclass(frozen=True)
class Attachment:
    """Resolved attachment for one skill, plus where the answer came from."""

    skill: str
    agents: tuple[str, ...]
    origin: str  # "override" | "domain" | "supplement" | "domain+supplement" | "none"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": list(self.agents),
            "origin": self.origin,
            "enabled": self.enabled,
        }


def state_path(config: Any = None) -> Path:
    """Where operator overrides are persisted.

    Configurable so a deployment can point it at a writable volume; defaults to
    ``data/skills_state.json``, which the image already creates and chowns.
    """
    if config is not None:
        try:
            from src.llm.config_section import section

            configured = section(config, "skills").get("state_path")
            if configured:
                return Path(str(configured))
        except Exception:
            logger.exception("Could not read skills.state_path; using default")
    return _DEFAULT_STATE_PATH


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Read the override map. A missing or corrupt file yields no overrides.

    Fail-open by design: a bad state file must not take the app down or hide
    every skill. The loss is the operator's customisation, which is visible in
    the UI immediately, not silent breakage.
    """
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Could not read skills state at %s; ignoring overrides", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("Skills state at %s is not an object; ignoring", path)
        return {}
    overrides = data.get("overrides")
    if not isinstance(overrides, dict):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for name, entry in overrides.items():
        if isinstance(name, str) and isinstance(entry, dict):
            clean[name] = entry
    return clean


def save_override(
    path: Path,
    *,
    skill: str,
    agents: list[str],
    enabled: bool,
    actor: str = "unknown",
) -> None:
    """Persist one skill's attachment, atomically.

    Writes a temp file in the same directory and renames it, so a crash or a
    concurrent read never observes a half-written state file.
    """
    overrides = load_state(path)
    overrides[skill] = {
        "agents": sorted(set(agents)),
        "enabled": bool(enabled),
        "updated_by": actor,
    }
    payload = {"version": _STATE_VERSION, "overrides": overrides}

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skills_state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; the rename is what makes the write visible, so a
        # failure before it leaves the previous state intact.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def clear_override(path: Path, *, skill: str) -> bool:
    """Drop one override so the skill returns to derived attachment.

    Returns True if an override was actually removed.
    """
    overrides = load_state(path)
    if skill not in overrides:
        return False
    del overrides[skill]
    payload = {"version": _STATE_VERSION, "overrides": overrides}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skills_state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return True


def derive(
    manifest: SkillManifest,
    known_agents: frozenset[str],
    supplement: SupplementMap | None = None,
) -> tuple[tuple[str, ...], str]:
    """Attachment implied by the skill itself, ignoring overrides.

    Returns ``(agents, origin)``. An unknown domain is dropped rather than
    invented — attaching a skill to an agent that does not exist would be a
    silent no-op at request time, which is exactly the failure class this whole
    module exists to eliminate.
    """
    from_domain: tuple[str, ...] = ()
    domain = (manifest.parsec.domain or "").strip()
    if domain:
        if domain in known_agents:
            from_domain = (domain,)
        else:
            logger.warning(
                "Skill %r declares parsec.domain=%r which is not a known agent; ignoring",
                manifest.name,
                domain,
            )

    from_supplement = (supplement or {}).get(manifest.name, ())
    from_supplement = tuple(a for a in from_supplement if a in known_agents)

    agents = tuple(sorted(set(from_domain) | set(from_supplement)))
    if from_domain and from_supplement:
        origin = "domain+supplement"
    elif from_domain:
        origin = "domain"
    elif from_supplement:
        origin = "supplement"
    else:
        origin = "none"
    return agents, origin


def resolve(
    manifests: list[SkillManifest],
    *,
    known_agents: frozenset[str],
    overrides: dict[str, dict[str, Any]] | None = None,
    supplement: SupplementMap | None = None,
) -> dict[str, Attachment]:
    """Resolve attachment for every manifest, override layer applied last."""
    overrides = overrides or {}
    resolved: dict[str, Attachment] = {}

    for m in manifests:
        derived, origin = derive(m, known_agents, supplement)
        entry = overrides.get(m.name)
        if entry is None:
            resolved[m.name] = Attachment(skill=m.name, agents=derived, origin=origin)
            continue

        enabled = bool(entry.get("enabled", True))
        raw_agents = entry.get("agents")
        if isinstance(raw_agents, list):
            chosen = tuple(sorted({str(a) for a in raw_agents if str(a) in known_agents}))
        else:
            chosen = derived

        resolved[m.name] = Attachment(
            skill=m.name,
            agents=chosen if enabled else (),
            origin="override",
            enabled=enabled,
        )

    return resolved


def skills_by_agent(attachments: dict[str, Attachment]) -> dict[str, tuple[str, ...]]:
    """Invert the attachment map into the shape ``skills_for`` wants."""
    out: dict[str, list[str]] = {}
    for name, att in attachments.items():
        for agent in att.agents:
            out.setdefault(agent, []).append(name)
    return {a: tuple(sorted(names)) for a, names in out.items()}
