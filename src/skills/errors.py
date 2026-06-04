"""Errors raised by the skills loader."""

from __future__ import annotations

from pathlib import Path


class SkillError(Exception):
    """Base class for all skill-related errors."""


class SkillLoadError(SkillError):
    """Raised when a skill directory cannot be read or parsed.

    Distinct from SkillValidationError: this means the file is structurally
    broken (missing SKILL.md, unreadable, malformed frontmatter delimiters),
    not that its content failed schema validation.
    """

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


class SkillValidationError(SkillError):
    """Raised when a SKILL.md fails schema validation (missing required fields, wrong types)."""

    def __init__(self, path: Path, field: str, message: str) -> None:
        self.path = path
        self.field = field
        super().__init__(f"{path} [{field}]: {message}")
