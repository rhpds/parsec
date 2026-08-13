"""GET /api/skills — list discoverable skills and their manifests.

Read-only diagnostic endpoint for operators verifying that mounted skill
sources (project, plugin, user) are correctly discovered. Does not invoke
skills — that's the agent runtime's job.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from src.config import get_config
from src.skills import SkillLoader, SkillManifest, sdk_skills_root

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["skills"])


def _serialize(m: SkillManifest, sdk_visible: bool) -> dict:
    return {
        "name": m.name,
        # Whether the Agent SDK can actually load this skill, i.e. whether it is
        # present under <cwd>/.claude/skills. Discovery by the loader alone does
        # NOT imply executability — a plugin_paths mount is listed here but is
        # only reachable by the SDK once it has been published into that root.
        "sdk_visible": sdk_visible,
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


@router.get("/skills", responses={500: {"description": "Internal Server Error"}})
async def list_skills():
    """Return all discoverable skills across configured sources."""
    try:
        cfg = get_config()
        loader = SkillLoader.from_config(cfg)
        manifests = loader.load_all()
    except Exception as e:
        logger.exception("Failed to load skills")
        raise HTTPException(status_code=500, detail=f"Skill discovery failed: {e}") from e

    sdk_root = sdk_skills_root(cfg.get("agent", {}).get("sdk", {}).get("cwd") or None)
    visible = {m.name for m in manifests if (sdk_root / m.name).exists()}

    return {
        "count": len(manifests),
        "sdk_visible_count": len(visible),
        "sdk_skills_root": str(sdk_root),
        "skills": [_serialize(m, m.name in visible) for m in manifests],
    }
