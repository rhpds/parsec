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
    """Return config sub-section ``key`` as a plain dict (``{}`` if missing).

    Case-insensitive, because Dynaconf materialises env-var settings with
    UPPERCASE keys. ``PARSEC_AGENT__SDK__ORCHESTRATOR=true`` becomes
    ``{"AGENT": {"SDK": {"ORCHESTRATOR": True}}}``. The Dynaconf object itself
    resolves ``get("agent")`` regardless of case, but ``to_dict()`` returns a
    plain dict that does not — so a second-level ``section(agent, "sdk")`` used
    to miss and return ``{}``, silently ignoring every ``agent.sdk.*`` override
    supplied by environment. YAML config was unaffected (its keys are already
    lowercase), which is why this only showed up when someone flipped the
    runtime with env vars on a live deployment.
    """
    if config is None:
        return {}
    raw = config.get(key, {}) if hasattr(config, "get") else getattr(config, key, {})
    if (raw is None or raw == {}) and hasattr(config, "items"):
        # Fall back to a case-insensitive scan of a plain dict.
        lowered = key.lower()
        for k, v in config.items():
            if str(k).lower() == lowered:
                raw = v
                break
    if raw is None:
        return {}
    if hasattr(raw, "to_dict"):
        raw = raw.to_dict()
    # Normalise this level's keys so callers can use lowercase throughout.
    return {str(k).lower(): v for k, v in dict(raw).items()}
