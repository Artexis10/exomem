"""Workflow-skill manifest helpers.

The canonical skill documents live inside the shipped `_Schema/` scaffold so
`exomem init`, `exomem install-skill`, and `bootstrap()` all describe the same
product surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import semantic_authoring
from .vault import SHIPPED_SCHEMA_DIRNAME

WORKFLOW_SKILLS_DIR = Path(__file__).parent / "_scaffold" / "_Schema" / "workflow-skills"
WORKFLOW_SKILLS_INDEX = WORKFLOW_SKILLS_DIR / "index.yaml"
_SKILL_CONTRACT_LINE = re.compile(r"(?m)^  skill_contract: [^\r\n]*(?:\r?\n)?")


def _schema_root(schema_root: Path | None = None) -> Path:
    return Path(schema_root) if schema_root is not None else WORKFLOW_SKILLS_DIR.parent


def _skill_names(schema_root: Path) -> list[str]:
    index = schema_root / "workflow-skills" / "index.yaml"
    data = yaml.safe_load(index.read_text(encoding="utf-8")) or {}
    skills = data.get("skills")
    if not isinstance(skills, list):
        raise ValueError("workflow skill index must contain a skills list")
    return [str(skill["name"]) for skill in skills if isinstance(skill, dict) and skill.get("name")]


def contract_sources(schema_root: Path | None = None) -> tuple[tuple[str, Path], ...]:
    """Return the canonical skill contract inputs in deterministic path order."""
    root = _schema_root(schema_root)
    sources = [("SKILL.md", root / "SKILL.md")]
    sources.extend(
        (f"references/{path.name}", path)
        for path in sorted((root / "references").glob("*.md"))
    )
    sources.extend(
        (f"workflow-skills/{name}/SKILL.md", root / "workflow-skills" / name / "SKILL.md")
        for name in _skill_names(root)
    )
    return tuple(sources)


def _frontmatter_parts(text: str, source: Path) -> tuple[str, str, str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{source}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{source}: missing frontmatter")
    return text[:4], text[4:end], text[end:]


def _metadata_contract_line(lines: list[str]) -> int | None:
    in_metadata = False
    for index, line in enumerate(lines):
        if line == "metadata:\n":
            in_metadata = True
            continue
        if in_metadata and line and not line[0].isspace():
            return None
        if in_metadata and _SKILL_CONTRACT_LINE.fullmatch(line):
            return index
    return None


def _contract_text(source: Path) -> str:
    text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
    if source.name != "SKILL.md":
        return text
    prefix, frontmatter, body = _frontmatter_parts(text, source)
    lines = frontmatter.splitlines(keepends=True)
    contract_line = _metadata_contract_line(lines)
    if contract_line is not None:
        del lines[contract_line]
    return prefix + "".join(lines) + body


def skill_contract(schema_root: Path | None = None) -> str:
    """Hash the canonical operating rules without their self-referential stamp."""
    payload = {relative: _contract_text(source) for relative, source in contract_sources(schema_root)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def refresh_skill_contract_stamps(schema_root: Path | None = None) -> str:
    """Stamp each canonical SKILL.md with the current contract digest."""
    root = _schema_root(schema_root)
    digest = skill_contract(root)
    for _, source in contract_sources(root):
        if source.name != "SKILL.md":
            continue
        text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
        prefix, frontmatter, body = _frontmatter_parts(text, source)
        lines = frontmatter.splitlines(keepends=True)
        replacement = f"  skill_contract: {digest}\n"
        contract_line = _metadata_contract_line(lines)
        if contract_line is not None:
            lines[contract_line] = replacement
        else:
            try:
                metadata_line = lines.index("metadata:\n")
            except ValueError as error:
                raise ValueError(f"{source}: missing metadata frontmatter") from error
            lines.insert(metadata_line + 1, replacement)
        source.write_text(prefix + "".join(lines) + body, encoding="utf-8", newline="\n")
    return digest


def validate_skill_contract(schema_root: Path | None = None) -> str:
    """Reject a packaged skill set whose stamped sources no longer match."""
    root = _schema_root(schema_root)
    digest = skill_contract(root)
    for _, source in contract_sources(root):
        if source.name != "SKILL.md":
            continue
        text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise ValueError(f"{source}: missing frontmatter")
        raw_frontmatter = text[4 : text.index("\n---\n", 4)]
        frontmatter = yaml.safe_load(raw_frontmatter) or {}
        metadata = frontmatter.get("metadata") if isinstance(frontmatter, dict) else None
        if not isinstance(metadata, dict) or metadata.get("skill_contract") != digest:
            raise ValueError(f"{source}: skill contract stamp is stale")
    return digest


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
    if core:
        validate_skill_contract(skill_dir)
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
                # Vault-relative, and it moved out of the note namespace with
                # the rest of the shipped markdown (#488). A stale path here is
                # not a cosmetic defect: it is the address the agent is told to
                # read the skill from.
                "path": f"{SHIPPED_SCHEMA_DIRNAME}/schema/workflow-skills/{name}/SKILL.md",
            }
        )
    return entries
