"""Parsec agent skills: discovery, parsing, and validation.

A skill is a directory with a SKILL.md file (YAML frontmatter + Markdown body),
conforming to the Anthropic Agent Skills Spec v1.0 with optional Parsec-specific
extensions under a `parsec:` block.

Typical use::

    from src.skills import SkillLoader

    loader = SkillLoader.from_config(get_config())
    manifests = loader.load_all()
    for m in manifests:
        print(m.name, m.description)
"""

from src.skills.errors import (
    SkillError,
    SkillLoadError,
    SkillValidationError,
)
from src.skills.loader import SkillLoader, SkillSource
from src.skills.manifest import ParsecExtensions, SkillManifest

__all__ = [
    "ParsecExtensions",
    "SkillError",
    "SkillLoadError",
    "SkillLoader",
    "SkillManifest",
    "SkillSource",
    "SkillValidationError",
]
