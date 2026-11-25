"""The vault's skills and Records collections as turn-independent session context.

The block is the template stance set: compiled from the catalogue's narrowed read, admitted
before it is served, pinned for the invocation and decided again at terminal egress on the
same unit shape. It is sent once per conversation and again only when it changes. A catalogue that
fits lists every visible item whatever the turn is; an overflowing one keeps the same listing and names the turn's
unlisted items beside it, outside the generation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    activation_conventions,
    capabilities,
    context_catalogue,
    source_taxonomy,
    vault_skills,
    vocabulary_resolution,
    working_set,
    working_set_index,
)
from .governance import egress, principal


def _empty(state: str = "empty") -> dict[str, Any]:
    unknown = state == "unavailable"
    return {"state": state, "generation": None if unknown else "", "items": [], "omitted": None if unknown else 0}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _advice() -> str:
    """How to use the catalogue, naming the read tool as the serving surface spells it."""
    read = capabilities.active_call_text("read_memory", {"path": "<ref>"})
    return (
        "These are the owner's own skills and collections; "
        f"when one covers this turn, open it with {read} and follow it within existing authority."
    )


def _names_advice() -> str:
    """How to open an item that the catalogue names without a line, as the serving surface spells each call."""
    query = capabilities.active_call_text("record_memory", {"action": "query", "collection": "<name>"})
    find = capabilities.active_call_text("search_memory", {"query": "<name>"})
    return (
        "More of the owner's skills and collections, by name only; when one fits this turn, "
        f"open a collection with {query} and find a skill with {find}."
    )


def _fit_names(candidates: list[_Candidate], max_chars: int | None) -> tuple[dict[str, list[str]], list[_Candidate]]:
    """The names of `candidates` that fit `max_chars` as `vault_skills.names_text` writes them, by kind, in order.

    The length is kept running, so fitting is linear in the candidates: each kind's label, colon, space and closing
    full stop are paid once when its group opens, a space joins the groups, and each name pays its length plus the
    `; ` that joins it to the one before. A name that does not fit is skipped, and a shorter later one may still fit.
    """
    labels = dict(vault_skills.NAMES_GROUPS)
    names: dict[str, list[str]] = {}
    named: list[_Candidate] = []
    used = 0
    for candidate in candidates if max_chars else ():
        kind, name = candidate.item["kind"], " ".join(str(candidate.item["name"]).split())
        group = names.get(kind)
        cost = len(name) + (2 if group else len(labels[kind]) + 3 + (1 if names else 0))
        if used + cost > max_chars:
            continue
        names.setdefault(kind, []).append(name)
        named.append(candidate)
        used += cost
    return {kind: names[kind] for kind, _ in vault_skills.NAMES_GROUPS if kind in names}, named


def _unit(item: Mapping[str, Any], path: str) -> dict[str, Any]:
    """The unit shape admission decides for one catalogue line, with its page path.

    A release-gated vault withholds a unit that names a page only by a bare memory ref, so
    compile and terminal egress both decide this exact shape.
    """
    line = working_set.LaneItem(
        role="", level="page", ref=item["ref"], path=path, title=item["name"],
        text=f"{item['name']}: {item['summary']}" if item["summary"] else item["name"],
        lifecycle="", updated="", anchor=path,
    )
    return working_set._served_unit(line)  # noqa: SLF001 - exactly the existing unit admission shape


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One visible catalogue item: what is served, and the page it is read from."""

    item: dict[str, Any]
    path: str
    topics: tuple[str, ...]


def _records_manifest_paths(root: Path) -> list[str]:
    """The manifest path of each Records collection, from the activation index's anchors.

    The index holds each Records manifest as an anchor of kind `collection`, cached on the sidecar's token, so this reads
    no directory. The catalogue also holds the vault's other collections, such as Planning, which this leaves out.
    """
    index = working_set_index.WorkingSetIndex(root)
    try:
        return [row.path for row in index.anchors() if row.kind == "collection" and row.path]
    finally:
        index.close()


def _visible(caller: Any, entries: Collection[Any], pending: Collection[str]) -> list[Any]:
    """The entries whose page is current in the catalogue and visible to the caller."""
    return [entry for entry in entries if entry.path and entry.path not in pending and caller.sees(entry.path)]


def _of_the_turn(root: Path, candidates: list[_Candidate], domains: Collection[str], reached: Collection[str]) -> list[_Candidate]:
    """The candidates of the touched domains, then those the turn reached; each tier keeps its order, and the rest are left out.

    An item's domains are what its own `topics` resolve to in the domain registry, as a profile page's are.
    """
    domains, reached = set(domains), set(reached)
    if not domains and not reached:
        return []
    taxonomy = source_taxonomy.load_taxonomy(root) if domains else None

    def tier(candidate: _Candidate) -> int:
        if taxonomy is not None:
            resolved = vocabulary_resolution.resolve_topics(taxonomy, vocabulary_resolution.declared_topics(candidate.topics)).resolved
            if domains.intersection(resolved):
                return 0
        return 1 if candidate.path in reached else 2

    tiers = [tier(candidate) for candidate in candidates]
    return [candidates[at] for at in sorted(range(len(candidates)), key=tiers.__getitem__) if tiers[at] < 2]


def compile_block(
    root: Path, caller: Any, *, native: Collection[str] = (), known_generation: str | None = None,
    catalogue: Any = None, domains: Collection[str] = (), reached: Collection[str] = (),
) -> dict[str, Any]:
    """The visible skills and Records collections as session context. A page the caller cannot see changes no item, count or generation.

    The block's size and each summary's length are the vault's `budgets` in its activation conventions. Skills come
    first, then collections, each by name; whole items fit in that order, and `omitted` counts the visible items left out.
    The listing and its generation never read the turn, so a held catalogue stays `unchanged` on every later turn.
    When the catalogue overflows, `turn_items` names the unlisted items of the turn within `skills_turn_max_chars`:
    first those whose topics resolve to one of `domains` (the registered domains the turn touches, with their
    ancestors), then those on a page of `reached` (the paths the turn's task context reached). They sit outside the
    generation and are sent on every turn that has them, held catalogue or not.
    """
    conventions = activation_conventions.load_conventions(root).conventions
    max_chars = conventions.budget("skills_max_chars")
    summary_chars = conventions.budget("skill_summary_max_chars")
    turn_chars = conventions.budget("skills_turn_max_chars")
    names_chars = conventions.budget("skills_names_max_chars")
    own_catalogue = catalogue is None
    catalogue = catalogue or context_catalogue.ContextCatalogue(root)
    try:
        pending = set(catalogue.pending())
        skills = _visible(caller, catalogue.entries_by_id(catalogue.skill_ids()), pending)
        collections = _visible(caller, catalogue.entries_by_id(_records_manifest_paths(root)), pending)
    finally:
        if own_catalogue:
            catalogue.close()
    # Native omission is the caller's own listing, so it follows the visibility test and counts nowhere.
    candidates = [
        _Candidate(
            {"kind": vault_skills.KIND_SKILL, "name": entry.skill, "summary": vault_skills.summary(entry.description, summary_chars), "ref": entry.ref or entry.path},
            entry.path, entry.topics,
        )
        for entry in sorted(skills, key=lambda entry: (entry.skill, entry.path))
        if entry.skill not in native
    ] + [
        _Candidate(
            {"kind": vault_skills.KIND_COLLECTION, "name": entry.title, "summary": vault_skills.summary(entry.description, summary_chars), "ref": entry.ref or entry.path},
            entry.path, entry.topics,
        )
        for entry in sorted(collections, key=lambda entry: (entry.title, entry.path))
    ]
    verdicts = egress.classify_units(root, [_unit(c.item, c.path) for c in candidates], purpose=caller.purpose)
    admitted = [candidate for candidate, verdict in zip(candidates, verdicts, strict=True) if verdict == egress.UNIT_KEPT]
    block = _empty()
    served: list[dict[str, Any]] = []
    unlisted: list[_Candidate] = []
    used = 0
    for candidate in admitted:
        cost = len(vault_skills.item_line(candidate.item)) + 1
        if used + cost > max_chars:
            block["omitted"] += 1
            unlisted.append(candidate)
            continue
        block["items"].append(candidate.item)
        served.append(_unit(candidate.item, candidate.path))
        used += cost
    # Each item left without a line is still named while its name fits, so the agent sees every collection and
    # skill it may choose from and decides itself; `omitted` then counts only the items not even named.
    names, named = _fit_names(unlisted, names_chars)
    block["omitted"] -= len(named)
    if named:
        block["names"] = names
    block["state"] = "ready" if block["items"] or named else "empty"
    block["generation"] = _digest({"items": block["items"], "names": names, "omitted": block["omitted"]})
    if block["state"] == "ready" and known_generation == block["generation"]:
        # The caller holds exactly these items: repeat none of them, and keep the count they were chosen with.
        block.update(state="unchanged", items=[])
        block.pop("names", None)
        served = []
    elif block["state"] == "ready":
        block["advice"] = _advice()
        if named:
            block["names_advice"] = _names_advice()
        # Terminal egress decides the whole unit each name comes from, so a name never outlives its page's release.
        served.extend(_unit(candidate.item, candidate.path) for candidate in named)
    turn_items, turn_used = [], 0
    for candidate in _of_the_turn(root, unlisted, domains, reached) if unlisted and turn_chars else ():
        cost = len(vault_skills.item_line(candidate.item)) + 1
        if turn_used + cost > turn_chars:
            continue
        turn_items.append(candidate.item)
        served.append(_unit(candidate.item, candidate.path))
        turn_used += cost
    if turn_items:
        block["turn_items"] = turn_items
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
