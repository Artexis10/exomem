"""Workflow-skill manifest helpers.

The canonical skill documents live inside the shipped `_Schema/` scaffold so
`exomem init`, `exomem install-skill`, and `bootstrap()` all describe the same
product surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import semantic_authoring
from .kbdir import kb_dirname

WORKFLOW_SKILLS_DIR = Path(__file__).parent / "_scaffold" / "_Schema" / "workflow-skills"
WORKFLOW_SKILLS_INDEX = WORKFLOW_SKILLS_DIR / "index.yaml"


def load_index() -> dict[str, Any]:
    """Load the packaged workflow-skill index."""
    if not WORKFLOW_SKILLS_INDEX.is_file():
        raise FileNotFoundError(
            f"workflow skill index missing at {WORKFLOW_SKILLS_INDEX} "
            "(is the exomem install intact?)"
        )
    data = yaml.safe_load(WORKFLOW_SKILLS_INDEX.read_text(encoding="utf-8")) or {}
    skills = data.get("skills")
    if not isinstance(skills, list):
        raise ValueError("workflow skill index must contain a skills list")
    return data


def list_skills() -> list[dict[str, Any]]:
    """Return workflow-skill manifest entries in configured order."""
    return list(load_index()["skills"])


def source_dir(name: str) -> Path:
    """Return the packaged source directory for one workflow skill."""
    return WORKFLOW_SKILLS_DIR / name


def is_standalone_authoring(name: str) -> bool:
    """Return whether a workflow can author or change an active compiled note."""
    return any(
        str(skill["name"]) == name and skill.get("standalone_authoring") is True
        for skill in list_skills()
    )


def validate_contract_projection(
    name: str,
    skill_dir: Path,
    *,
    core: bool = False,
) -> None:
    """Reject a standalone authoring boundary whose canonical contract drifted."""
    if not core and not is_standalone_authoring(name):
        return
    skill_md = Path(skill_dir) / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    concise = semantic_authoring.render_concise()
    if text.count(concise) != 1:
        raise ValueError(
            f"{name} must embed the complete canonical semantic authoring contract "
            "exactly once; a reference to the core skill is not sufficient"
        )


def bootstrap_entries() -> list[dict[str, Any]]:
    """Return compact, public-safe workflow-skill metadata for bootstrap()."""
    entries: list[dict[str, Any]] = []
    for skill in list_skills():
        name = str(skill["name"])
        entries.append(
            {
                "name": name,
                "purpose": str(skill.get("purpose", "")),
                "triggers": [str(t) for t in skill.get("triggers", [])],
                "path": f"{kb_dirname()}/_Schema/workflow-skills/{name}/SKILL.md",
            }
        )
    return entries
