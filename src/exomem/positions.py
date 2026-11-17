"""The person's own recorded positions on the pages a turn's task context serves, framed as theirs.

A unit is a position only when its category or its kind carries the registry trait `position`. The block
reads the served leads and the link targets they list, in one batched unit query, and has its own bound,
the vault's `budgets.positions_max_chars`, beside the turn allowance. Positions answer the turn, so the
block is compiled on every turn and never marked held for the conversation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from . import (
    activation_conventions,
    lexstore,
    semantic_language_registry,
    working_set,
    working_set_currency,
    working_set_runtime,
)
from .governance import egress, principal

#: The framing passage. It is part of the contract: it never asks the agent to agree with a position.
ABOUT = (
    "The person's own recorded positions on this subject, from their notes. Where the turn bears on one, say how "
    "the new evidence relates to it, attributed to them; judge the evidence yourself and say plainly where it does "
    "not support or contradicts the position. A recorded position stays theirs for the rest of this conversation: "
    "when they question your reading, re-examine the evidence, and keep their position, attributed to them, in view."
)
TRAIT = "position"
#: A resource bound, not vault vocabulary: the most position units one recall reads from its reach.
UNITS_PER_RECALL = 32
#: The stored date of a page that declares none.
_UNDATED_PAGE = "0000-00-00"


def item_line(item: Mapping[str, Any]) -> str:
    """One item as the block's bound counts it."""
    dated = f"{item['date']}, " if item.get("date") else ""
    return f"- position ({dated}{item['title']}): {item['text']} [{item['ref']}]"


def chars(block: Mapping[str, Any]) -> int:
    lines = [item_line(item) for item in block.get("items", ())]
    return len("\n".join(lines)) + (1 if lines else 0)


def _empty(state: str = "empty", reason: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"state": state, "items": [], "overflow_refs": []}
    if reason:
        block["reason"] = reason
    return block


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _limit(root: Path) -> int:
    configured = activation_conventions.load_conventions(root).conventions.budget("positions_max_chars")
    # The shipped conventions file always sets the budget; the range's top only caps a broken install.
    return max(0, configured if configured is not None else activation_conventions.BUDGET_BOUNDS["positions_max_chars"][1])


def compile_block(
    root: Path, caller: Any, *, reach: Mapping[str, str], turn: str, focus: str, snapshot: Any,
    skip: Collection[str] = (),
) -> dict[str, Any]:
    """The position units on `reach` ({page path: title}) that this caller may see, ranked for `turn` and `focus`.

    `skip` holds the unit refs the same response already serves as session context.
    """
    if not reach:
        return _empty()
    registry = semantic_language_registry.load_admitted(root) or semantic_language_registry.core_registry()
    categories, kinds = registry.categories_with(TRAIT), registry.kinds_with(TRAIT)
    if not categories and not kinds:
        return _empty()
    if working_set.budget_exhausted("working_set.positions"):
        return _empty("unavailable", "budget")
    from . import context_task
    from .structured_filters import IndexCandidateClause

    # A unit is a position when its category OR its kind carries the trait, so each axis is its own clause.
    clauses = tuple(
        clause for clause in (
            IndexCandidateClause(frozenset(categories), None) if categories else None,
            IndexCandidateClause(None, frozenset(kinds)) if kinds else None,
        ) if clause is not None
    )
    parents = set(reach)
    # A caller that reads raw sources passes its reach as released; any other has the store apply its release filter.
    released = parents if context_task.full_source_access(root, caller) else None
    with lexstore.stopping_at_budget():
        # No query terms: the store lists every position unit on these parents, so a unit that shares no word
        # with the turn is still served by recency.
        result = lexstore.get_store(root).search_semantic_units_result(
            [], UNITS_PER_RECALL + 1, (), (), "kb", snapshot.for_scope("kb"),
            clauses=clauses,
            allowed_parent_paths=parents, admitted_parent_paths=parents,
            released_parent_paths=released,
            recall_checkpoint=snapshot.lexical_checkpoint("kb"), allow_delta=False,
        )
    if result.readiness.status == lexstore.DEADLINE_STATUS:
        working_set.note_stopped("working_set.positions")
        return _empty("unavailable", "budget")
    if not result.readiness.complete:
        return _empty("unavailable", "lane_failed")
    hits = list(result.value or ())
    truncated = len(hits) > UNITS_PER_RECALL
    candidates = [(hit, _lane_item(hit, reach)) for hit in hits[:UNITS_PER_RECALL] if hit.unit_ref not in skip]
    wire = [working_set._served_unit(item) for _hit, item in candidates]  # noqa: SLF001 - exactly the existing unit admission shape
    verdicts = egress.classify_units(root, wire, purpose=caller.purpose)
    admitted = [pair for pair, verdict in zip(candidates, verdicts, strict=True) if verdict == egress.UNIT_KEPT]
    block = _empty()
    served: list[dict[str, Any]] = []
    used, limit = 0, _limit(root)
    for item in _ranked(admitted, f"{turn}\n{focus}"):
        line = {"ref": item.ref, "text": item.text, "title": item.title}
        if item.updated:
            line["date"] = item.updated
        cost = len(item_line(line)) + 1
        # Overflow is a reference, never a cut unit; a unit too long to serve whole is one too.
        if len(item.text) > working_set.MAX_UNIT_HARD_CHARS or used + cost > limit:
            block["overflow_refs"].append(item.ref)
        else:
            block["items"].append(line)
            used += cost
        served.append(working_set._served_unit(item))  # noqa: SLF001 - the shape compile admitted
    if block["items"] or block["overflow_refs"]:
        block["about"] = ABOUT
        block["state"] = "ready"
    if truncated:
        block.update(state="partial", reason="truncated")
    _pin(root, caller, block, served)
    return block


def _lane_item(hit: Any, reach: Mapping[str, str]) -> working_set.LaneItem:
    return working_set.LaneItem(
        role="", level="unit", ref=hit.unit_ref, path=hit.parent_path, title=reach.get(hit.parent_path) or hit.parent_path,
        text=working_set.bounded_text(hit.content), lifecycle="",
        # The unit's own authored date only: the page changes whenever any unit on it does.
        updated=hit.as_of or working_set_currency.own_time(hit.context), anchor=hit.parent_path,
        provenance={"category": hit.category, "kind": hit.kind, "source_hash": hit.source_hash},
    )


def _ranked(admitted: list[tuple[Any, working_set.LaneItem]], query: str) -> list[working_set.LaneItem]:
    """Units that share a content word with the turn first; within each group the newest first.

    A unit is read with its page title, because a unit is about its page's subject. The date is the unit's own,
    else its page's. On one date the later line on the page comes first, since a later line revises an earlier one.
    """
    wanted = set(working_set_runtime.content_stems(query))

    def key(pair: tuple[Any, working_set.LaneItem]) -> tuple:
        hit, item = pair
        stems = set(working_set_runtime.content_stems(f"{item.title}\n{item.text}"))
        page = "" if hit.parent_updated == _UNDATED_PAGE else hit.parent_updated
        return (not (wanted & stems), _descending(item.updated or page), -hit.source_order)

    return [item for _hit, item in sorted(admitted, key=key)]


def _descending(day: str) -> tuple[int, ...]:
    """An ISO date as a key that sorts the newest first and an undated unit last."""
    digits = [int(part) for part in day[:10].split("-") if part.isdigit()] if day else []
    return tuple(-part for part in digits) if len(digits) == 3 else (1,)


def skip_refs(packet: Mapping[str, Any]) -> frozenset[str]:
    """The unit refs this response already serves as session context, which the block does not repeat."""
    # nosemgrep: ep-word-set -- the packet's own session-block keys, a closed wire set this module reads
    keys = ("stance", "domain_profiles")
    return frozenset(
        str(item["ref"])
        for key in keys
        if isinstance(packet.get(key), Mapping)
        for item in packet[key].get("items") or ()
        if isinstance(item, Mapping) and item.get("ref")
    )


def _pin(root: Path, caller: Any, block: dict[str, Any], served: list[dict[str, Any]]) -> None:
    held = principal.invocation_served()
    if held is not None:
        held[("positions", str(root), caller.binding, _digest(block))] = (copy.deepcopy(block), copy.deepcopy(served))


def guard_block(root: Path, caller: Any, block: dict[str, Any]) -> dict[str, Any]:
    """Restore this invocation's block and decide its complete unit prose at terminal egress."""
    held = principal.invocation_served()
    pinned = None if held is None or not isinstance(block, dict) else held.get(
        ("positions", str(root), caller.binding, _digest(block)),
    )
    if pinned is None:
        return _empty("unavailable")
    block, served = pinned
    if any(verdict != egress.UNIT_KEPT for verdict in egress.classify_units(root, copy.deepcopy(served), purpose=caller.purpose)):
        return _empty("unavailable")
    return copy.deepcopy(block)
