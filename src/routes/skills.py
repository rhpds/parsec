"""GET /api/skills — list discoverable skills and their manifests.

Read-only diagnostic endpoint for operators verifying that mounted skill
sources (project, plugin, user) are correctly discovered. Does not invoke
skills — that's the agent runtime's job.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from src.config import get_config
from src.skills import SkillLoader, SkillManifest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["skills"])


def _serialize(m: SkillManifest) -> dict:
    return {
        "name": m.name,
        "description": m.description,
        "source": m.source,
        "skill_path": str(m.skill_path),
        "allowed_tools": list(m.allowed_tools),
        "license": m.license,
        "metadata": m.metadata,
        "parsec": {
            "version": m.parsec.version,
            "domain": m.parsec.domain,
            "requires_mcp": list(m.parsec.requires_mcp),
            "permissions": m.parsec.permissions,
            "cost_estimate_per_call_usd": m.parsec.cost_estimate_per_call_usd,
        },
        "is_parsec_native": m.is_parsec_native,
        "warnings": list(m.warnings),
    }


@router.get("/skills")
async def list_skills():
    """Return all discoverable skills across configured sources."""
    try:
        loader = SkillLoader.from_config(get_config())
        manifests = loader.load_all()
    except Exception as e:
        logger.exception("Failed to load skills")
        raise HTTPException(status_code=500, detail=f"Skill discovery failed: {e}") from e

    return {
        "count": len(manifests),
        "skills": [_serialize(m) for m in manifests],
    }
