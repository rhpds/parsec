"""Shared helper for reading a config sub-section as a plain dict.

``from_config`` / ``build_tracing_env`` accept both Dynaconf objects (sections are
Boxes exposing ``.get`` + ``.to_dict``) and plain dicts (used throughout the tests),
so we read via ``.get``/``getattr`` and coerce to a real dict. Attribute access
(``cfg.agent``) is deliberately avoided: it only works for Dynaconf, not the
plain-dict configs the tests pass.
"""

from __future__ import annotations

from typing import Any


def section(config: Any, key: str) -> dict[str, Any]:
    """Return config sub-section ``key`` as a plain dict (``{}`` if missing)."""
    if config is None:
        return {}
    raw = config.get(key, {}) if hasattr(config, "get") else getattr(config, key, {})
    if raw is None:
        return {}
    if hasattr(raw, "to_dict"):
        return raw.to_dict()
    return dict(raw)
