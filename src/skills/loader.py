"""Skill discovery and loading."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.skills.errors import SkillLoadError, SkillValidationError
from src.skills.manifest import ParsecExtensions, SkillManifest

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)

# Anthropic spec: name is "Lowercase + hyphens, matches folder"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class SkillSource:
    """One root directory where skills live, with a label for provenance."""

    label: str  # "project" | "plugin" | "user"
    root: Path
    required: bool = False  # If True, raise when root is missing rather than skip.


class SkillLoader:
    """Discover and load SKILL.md files from one or more source roots.

    Discovery walks each root recursively. A directory is considered a skill
    iff it contains a SKILL.md file at its top level. Subdirectories of a
    skill (scripts/, schemas/, tests/) are not scanned for nested skills.
    """

    def __init__(self, sources: list[SkillSource]):
        self._sources = sources

    @classmethod
    def from_config(cls, config: Any) -> SkillLoader:
        """Build a loader from Dynaconf-style config.

        Reads ``skills.project_root``, ``skills.plugin_paths``, and
        ``skills.user_root`` with sensible defaults. Empty/missing values
        skip the corresponding source.
        """
        skills_cfg = _get_skills_section(config)

        sources: list[SkillSource] = []

        project_root = skills_cfg.get("project_root", "skills")
        if project_root:
            sources.append(SkillSource(label="project", root=Path(project_root)))

        plugin_paths = skills_cfg.get("plugin_paths", []) or []
        for p in plugin_paths:
            sources.append(SkillSource(label="plugin", root=Path(p)))

        user_root = skills_cfg.get("user_root", "")
        if user_root:
            sources.append(SkillSource(label="user", root=Path(user_root).expanduser()))

        return cls(sources)

    def load_all(self) -> list[SkillManifest]:
        """Discover and load every skill across all sources.

        Validation failures (missing required fields, malformed YAML) are
        logged and skipped rather than raised — the loader is designed to be
        forgiving so one broken skill doesn't disable the rest. Use
        :meth:`load_strict` if you want failures to surface.
        """
        manifests: list[SkillManifest] = []
        for skill_dir, source in self._iter_skill_dirs():
            try:
                manifest = self._load_one(skill_dir, source.label)
            except (SkillLoadError, SkillValidationError) as e:
                logger.warning("Skipping skill at %s: %s", skill_dir, e)
                continue
            manifests.append(manifest)
        return self._deduplicate(manifests)

    def load_strict(self) -> list[SkillManifest]:
        """Like :meth:`load_all` but raises on the first invalid skill.

        Use in tests / CI to surface broken skills loudly.
        """
        manifests: list[SkillManifest] = []
        for skill_dir, source in self._iter_skill_dirs():
            manifests.append(self._load_one(skill_dir, source.label))
        return self._deduplicate(manifests)

    def _iter_skill_dirs(self) -> Iterator[tuple[Path, SkillSource]]:
        """Yield (skill_dir, source) for every directory containing a SKILL.md."""
        for source in self._sources:
            if not source.root.exists():
                if source.required:
                    raise SkillLoadError(source.root, "required source root missing")
                logger.debug("Skipping missing skill source %s (%s)", source.root, source.label)
                continue
            if not source.root.is_dir():
                logger.warning("Skill source %s is not a directory; skipping", source.root)
                continue
            # Only check top-level children — skills are flat directories under root.
            for child in sorted(source.root.iterdir()):
                if not child.is_dir():
                    continue
                if (child / "SKILL.md").is_file():
                    yield child, source

    def _load_one(self, skill_dir: Path, source_label: str) -> SkillManifest:
        skill_md = skill_dir / "SKILL.md"
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError as e:
            raise SkillLoadError(skill_md, f"cannot read file: {e}") from e

        frontmatter, body = _split_frontmatter(skill_md, raw)
        data = _parse_yaml(skill_md, frontmatter)
        warnings = _validate(skill_md, skill_dir, data)

        parsec_block = data.get("parsec") or {}
        if parsec_block and not isinstance(parsec_block, dict):
            raise SkillValidationError(skill_md, "parsec", "must be a mapping if present")
        parsec_ext = ParsecExtensions.from_dict(parsec_block)
        if parsec_ext.extra:
            warnings.append(f"unknown parsec.* keys ignored: {sorted(parsec_ext.extra)}")

        allowed_tools_raw = data.get("allowed-tools") or []
        if allowed_tools_raw and not isinstance(allowed_tools_raw, list):
            raise SkillValidationError(skill_md, "allowed-tools", "must be a list of strings")

        metadata = data.get("metadata") or {}
        if metadata and not isinstance(metadata, dict):
            raise SkillValidationError(skill_md, "metadata", "must be a mapping if present")

        return SkillManifest(
            name=data["name"],
            description=data["description"].strip(),
            skill_path=skill_dir,
            skill_md_path=skill_md,
            body=body,
            allowed_tools=tuple(allowed_tools_raw),
            license=data.get("license"),
            metadata=metadata,
            parsec=parsec_ext,
            source=source_label,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _deduplicate(manifests: list[SkillManifest]) -> list[SkillManifest]:
        """Keep the first occurrence of each skill name across sources.

        Project skills override plugin skills override user skills because
        :class:`from_config` registers them in that priority order.
        """
        seen: dict[str, SkillManifest] = {}
        for m in manifests:
            if m.name in seen:
                logger.info(
                    "Skill name %r already loaded from %s; ignoring duplicate from %s",
                    m.name,
                    seen[m.name].source,
                    m.source,
                )
                continue
            seen[m.name] = m
        return list(seen.values())


def _get_skills_section(config: Any) -> dict[str, Any]:
    """Pull ``skills`` from a Dynaconf or plain-dict config, returning a dict.

    Dynaconf exposes nested sections as dotted attribute access but also as
    a ``.get`` method that returns a Box. We coerce to dict for simplicity.
    """
    raw = config.get("skills", {}) if hasattr(config, "get") else getattr(config, "skills", {})
    if raw is None:
        return {}
    if hasattr(raw, "to_dict"):
        return raw.to_dict()
    return dict(raw)


def _split_frontmatter(path: Path, raw: str) -> tuple[str, str]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise SkillLoadError(
            path,
            "missing YAML frontmatter (expected file to start with '---' delimited block)",
        )
    return match.group("frontmatter"), match.group("body")


def _parse_yaml(path: Path, frontmatter: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(frontmatter)
    except yaml.YAMLError as e:
        raise SkillLoadError(path, f"invalid YAML frontmatter: {e}") from e
    if parsed is None:
        raise SkillLoadError(path, "empty YAML frontmatter")
    if not isinstance(parsed, dict):
        raise SkillLoadError(path, f"frontmatter must be a mapping, got {type(parsed).__name__}")
    return parsed


def _validate(skill_md: Path, skill_dir: Path, data: dict[str, Any]) -> list[str]:
    """Validate required fields; return list of non-fatal warning strings."""
    warnings: list[str] = []

    name = data.get("name")
    if not name or not isinstance(name, str):
        raise SkillValidationError(skill_md, "name", "required string field")
    if not _NAME_RE.match(name):
        raise SkillValidationError(
            skill_md, "name", f"must match {_NAME_RE.pattern!r} (got {name!r})"
        )
    if name != skill_dir.name:
        warnings.append(f"name {name!r} does not match folder name {skill_dir.name!r}")

    description = data.get("description")
    if not description or not isinstance(description, str):
        raise SkillValidationError(skill_md, "description", "required string field")
    if len(description.strip()) < 20:
        warnings.append(
            "description is very short (<20 chars); Claude needs context to decide when to invoke"
        )

    return warnings
