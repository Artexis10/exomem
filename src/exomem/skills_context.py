"""The vault's skills as turn-independent session context.

The block is the template stance set: compiled from the catalogue's narrowed read, admitted
before it is served, pinned for the invocation and decided again at terminal egress on the
same unit shape. It is sent once per conversation and again only when it changes.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from . import activation_conventions, context_catalogue, find_corpus, kbdir, recall_policy, vault_skills, working_set
from .governance import egress, principal


def held_generation(block: Mapping[str, Any] | None) -> str | None:
    """The generation a caller holds once it has been served `block`; an empty or unavailable block is owed again."""
    if isinstance(block, Mapping) and block.get("state") in ("ready", "unchanged"):
        return block.get("generation")
    return None


def _empty(state: str = "empty") -> dict[str, Any]:
    unknown = state == "unavailable"
    return {"state": state, "generation": None if unknown else "", "items": [], "omitted": None if unknown else 0}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _unit(item: Mapping[str, Any], path: str) -> dict[str, Any]:
    """The unit shape admission decides for one skill line, with its page path.

    A release-gated vault withholds a unit that names a page only by a bare memory ref, so
    compile and terminal egress both decide this exact shape.
    """
    line = working_set.LaneItem(
        role="", level="page", ref=item["ref"], path=path, title=item["name"],
        text=f"{item['name']}: {item['summary']}" if item["summary"] else item["name"],
        lifecycle="", updated="", anchor=path,
    )
    return working_set._served_unit(line)  # noqa: SLF001 - exactly the existing unit admission shape


def compile_block(
    root: Path, caller: Any, *, native: Collection[str] = (), known_generation: str | None = None,
    catalogue: Any = None,
) -> dict[str, Any]:
    """The visible skills as session context. A skill the caller cannot see changes no item, count or generation.

    The block's size and each summary's length are the vault's `budgets` in its activation conventions.
    """
    conventions = activation_conventions.load_conventions(root).conventions
    max_chars = conventions.budget("skills_max_chars")
    summary_chars = conventions.budget("skill_summary_max_chars")
    own_catalogue = catalogue is None
    catalogue = catalogue or context_catalogue.ContextCatalogue(root)
    try:
        pending = set(catalogue.pending())
        entries = [
            entry for entry in catalogue.entries_by_id(catalogue.skill_ids())
            if entry.path and entry.path not in pending and caller.sees(entry.path)
        ]
    finally:
        if own_catalogue:
            catalogue.close()
    # Native omission is the caller's own listing, so it follows the visibility test and counts nowhere.
    candidates = [
        ({"name": entry.skill, "summary": vault_skills.summary(entry.description, summary_chars), "ref": entry.ref or entry.path}, entry.path)
        for entry in sorted(entries, key=lambda entry: (entry.skill, entry.path))
        if entry.skill not in native
    ]
    verdicts = egress.classify_units(root, [_unit(item, path) for item, path in candidates], purpose=caller.purpose)
    admitted = [pair for pair, verdict in zip(candidates, verdicts, strict=True) if verdict == egress.UNIT_KEPT]
    block = _empty()
    served: list[dict[str, Any]] = []
    used = 0
    for item, path in admitted:
        cost = len(vault_skills.item_line(item)) + 1
        if used + cost > max_chars:
            block["omitted"] += 1
            continue
        block["items"].append(item)
        served.append(_unit(item, path))
        used += cost
    block["state"] = "ready" if block["items"] else "empty"
    block["generation"] = _digest({"items": block["items"], "omitted": block["omitted"]})
    if block["state"] == "ready" and known_generation == block["generation"]:
        # The caller holds exactly these items: repeat none of them, and keep the count they were chosen with.
        block.update(state="unchanged", items=[])
        served = []
    _pin(root, caller, block, served)
    return block


def _pin(root: Path, caller: Any, block: dict[str, Any], served: list[dict[str, Any]]) -> None:
    held = principal.invocation_served()
    if held is not None:
        held[("skills", str(root), caller.binding, block["generation"], block["state"])] = (
            copy.deepcopy(block), copy.deepcopy(served),
        )


def guard_block(root: Path, caller: Any, block: dict[str, Any]) -> dict[str, Any]:
    """Restore this invocation's block and decide its complete unit prose again at terminal egress."""
    held = principal.invocation_served()
    pinned = None if held is None else held.get(
        ("skills", str(root), caller.binding, block.get("generation"), block.get("state"))
    )
    if pinned is None:
        return _empty("unavailable")
    block, served = pinned
    # The units carry the page path a release-gated vault needs for an identified page's memory ref.
    if any(verdict != egress.UNIT_KEPT for verdict in egress.classify_units(root, copy.deepcopy(served), purpose=caller.purpose)):
        return _empty("unavailable")
    return copy.deepcopy(block)


def skill_files(root: Path, skill_path: str, sees: Callable[[str], bool]) -> tuple[list[str], bool]:
    """The caller-visible pages in a skill's directory tree, other than the skill page, in path order, and whether a bound cut the list.

    A page the caller cannot see is neither listed nor counted. The walk examines at most `SKILL_FILES_SCAN_MAX`
    pages, so a skill placed above a large tree costs a bounded read; when that bound or `SKILL_FILES_MAX` cuts
    the list, the answer says it is partial instead of counting what was not examined. A skill directly under the
    vault or the knowledge base has no tree of its own, so it lists nothing instead of the whole corpus.
    """
    folder = skill_path.rpartition("/")[0]
    if folder in ("", kbdir.kb_dirname()):
        return [], False
    walked = list(itertools.islice(find_corpus.walk_md(root / folder), vault_skills.SKILL_FILES_SCAN_MAX + 1))
    scan_cut = len(walked) > vault_skills.SKILL_FILES_SCAN_MAX
    admitted = recall_policy.admitted_relative_paths(root, walked[: vault_skills.SKILL_FILES_SCAN_MAX])
    visible = sorted(rel for rel in admitted if rel != skill_path and sees(rel))
    return visible[: vault_skills.SKILL_FILES_MAX], scan_cut or len(visible) > vault_skills.SKILL_FILES_MAX
