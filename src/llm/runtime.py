"""Feature-flag selection between legacy Anthropic SDK and Claude Agent SDK."""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

RuntimeName = Literal["legacy", "sdk"]

RUNTIME_LEGACY: RuntimeName = "legacy"
RUNTIME_SDK: RuntimeName = "sdk"

_VALID = {RUNTIME_LEGACY, RUNTIME_SDK}


def get_runtime(config: Any) -> RuntimeName:
    """Return the active agent runtime, defaulting to legacy.

    Reads ``agent.runtime`` from config. Unknown values are logged and
    coerced to legacy so a typo never silently routes traffic to the new
    untested path.
    """
    section = config.get("agent", {}) if hasattr(config, "get") else getattr(config, "agent", {})
    if section is None:
        return RUNTIME_LEGACY

    if hasattr(section, "get"):
        raw = section.get("runtime", RUNTIME_LEGACY)
    else:
        raw = getattr(section, "runtime", RUNTIME_LEGACY)

    if raw not in _VALID:
        logger.warning(
            "Unknown agent.runtime=%r; falling back to %r. Valid values: %s",
            raw,
            RUNTIME_LEGACY,
            sorted(_VALID),
        )
        return RUNTIME_LEGACY

    return raw  # type: ignore[no-any-return]
