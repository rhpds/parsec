"""SkillManifest and Parsec-specific extension types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Parsec-specific keys we recognize under the `parsec:` block. Unknown keys
# are preserved in ParsecExtensions.extra and surfaced as a warning so plain
# Claude Code skills continue to load.
_KNOWN_PARSEC_KEYS = {
    "version",
    "domain",
    "requires_mcp",
    "permissions",
    "cost_estimate_per_call_usd",
}


@dataclass(frozen=True)
class ParsecExtensions:
    """Parsec-specific metadata layered on top of the Anthropic skill spec.

    Skills authored for plain Claude Code (no `parsec:` block) get an empty
    instance — they remain fully loadable.
    """

    version: str | None = None
    domain: str | None = None
    requires_mcp: tuple[str, ...] = ()
    permissions: dict[str, Any] = field(default_factory=dict)
    cost_estimate_per_call_usd: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ParsecExtensions:
        if not data:
            return cls()
        known: dict[str, Any] = {k: v for k, v in data.items() if k in _KNOWN_PARSEC_KEYS}
        extra = {k: v for k, v in data.items() if k not in _KNOWN_PARSEC_KEYS}
        # Normalize requires_mcp into a tuple to keep ParsecExtensions hashable-shape.
        requires = known.get("requires_mcp")
        if requires is not None:
            known["requires_mcp"] = tuple(requires)
        return cls(**known, extra=extra)


@dataclass(frozen=True)
class SkillManifest:
    """A loaded SKILL.md, normalized and validated.

    Immutable so it can be safely shared across request handlers.
    """

    name: str
    description: str
    skill_path: Path  # directory containing SKILL.md
    skill_md_path: Path  # path to SKILL.md itself
    body: str  # Markdown body (everything after frontmatter)
    allowed_tools: tuple[str, ...] = ()
    license: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    parsec: ParsecExtensions = field(default_factory=ParsecExtensions)
    source: str = "unknown"  # "project" | "plugin" | "user"
    warnings: tuple[str, ...] = ()  # non-fatal validation issues

    @property
    def is_parsec_native(self) -> bool:
        """True iff the skill declares a parsec.version (i.e. authored with Parsec in mind)."""
        return self.parsec.version is not None
