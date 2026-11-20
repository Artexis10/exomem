"""The person's own recorded positions on the pages a turn's task context serves, framed as theirs.

A unit is a position only when its category or its kind carries the registry trait `position`. One batched
unit query over the served leads and the link targets they list finds the pages in that reach that hold one.
`session_units` then admits those pages' position units from the pages' own bytes, under the same page
lifecycle, supersession, expiry and egress rules as the stance and the domain profiles. A page that supplies the
stance or a domain profile is never read here: it has its own block and framing. The block has its own bound, the
vault's `budgets.positions_max_chars`, beside the turn allowance, and a unit is served whole whenever it fits that
bound. Positions answer the turn, so the block is compiled on every turn and never marked held for the conversation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import (
    activation,
    activation_conventions,
    lexstore,
    note_types,
    semantic_language_registry,
    session_units,
    working_set,
    working_set_currency,
    working_set_runtime,
)
from .governance import egress, principal

#: The framing passage. It is part of the contract: it never asks the agent to agree with a position. It names the
#: units as the person's own because the owner gives the trait only to categories that record their positions; in
#: replays, asking the agent to attribute each line as its text does left the person's position out of the replies.
ABOUT = (
    "The person's own recorded positions on this subject, from their notes. Where the turn bears on one, say how "
    "the new evidence relates to it, attributed to them; judge the evidence yourself and say plainly where it does "
    "not support or contradicts the position. A recorded position stays theirs for the rest of this conversation: "
    "when they question your reading, re-examine the evidence, and keep their position, attributed to them, in view."
)
TRAIT = "position"
#: A resource bound, not vault vocabulary: the most position-unit rows one recall reads to find the pages in its
#: reach that hold one. The reach is a few dozen pages, so only hundreds of positions on them reach it, and the
#: block then says `partial`.
ROWS_PER_RECALL = 512


def item_line(item: Mapping[str, Any]) -> str:
    """One item as the block's bound counts it."""
    dated = f"{item['date']}, " if item.get("date") else ""
    due = f" (review due {item['review_due']})" if item.get("review_due") else ""
    return f"- position ({dated}{item['title']}): {item['text']}{due} [{item['ref']}]"


def overflow_line(refs: Sequence[str], omitted: int = 0) -> str:
    """The line for the positions the bound left out: their refs, then a count of those it could not name."""
    if not refs:
        return f"- positions not shown: {omitted} more"
    more = f" (+{omitted} more)" if omitted else ""
    return f"- positions not shown, read by ref: {', '.join(f'[{ref}]' for ref in refs)}{more}"


def chars(block: Mapping[str, Any]) -> int:
    """What the bound counts: the position lines and the refs of those left out.

    The framing line and the count of positions left unnamed ride outside it, so a full block still says that more
    exist; together they add the framing passage and a few characters.
    """
    lines = [item_line(item) for item in block.get("items", ())]
    if block.get("overflow_refs"):
        lines.append(overflow_line(block["overflow_refs"]))
    return len("\n".join(lines)) + (1 if lines else 0)


def _empty(state: str = "empty", reason: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"state": state, "items": [], "overflow_refs": [], "omitted": 0}
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
    root: Path, caller: Any, *, reach: Collection[str], turn: str, focus: str, snapshot: Any,
    today: date | None = None,
) -> dict[str, Any]:
    """The current position units on the `reach` page paths that this caller may see, ranked for the turn.

    Every outcome is pinned for terminal egress, so an empty or unavailable block reaches the wire with its own
    state and reason.
    """
    block, served = _compile(root, caller, reach=reach, query=f"{turn}\n{focus}", snapshot=snapshot, today=today or date.today())
    _pin(root, caller, block, served)
    return block


def _compile(
    root: Path, caller: Any, *, reach: Collection[str], query: str, snapshot: Any, today: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    limit = _limit(root)
    if limit == 0:
        return _empty(reason="disabled"), []
    parents = {path for path in reach if caller.sees(path)}
    if not parents:
        return _empty(), []
    registry = semantic_language_registry.load_admitted(root) or semantic_language_registry.core_registry()
    categories, kinds = registry.categories_with(TRAIT), registry.kinds_with(TRAIT)
    if not categories and not kinds:
        return _empty(), []
    if working_set.budget_exhausted("working_set.positions"):
        return _empty("unavailable", "budget"), []
    from . import context_task
    from .structured_filters import IndexCandidateClause

    # A unit is a position when its category OR its kind carries the trait, so each axis is its own clause.
    clauses = tuple(
        clause for clause in (
            IndexCandidateClause(frozenset(categories), None) if categories else None,
            IndexCandidateClause(None, frozenset(kinds)) if kinds else None,
        ) if clause is not None
    )
    # A caller that reads raw sources passes its reach as released; any other has the store apply its release filter.
    full_access = context_task.full_source_access(root, caller)
    released = parents if full_access else None
    with lexstore.stopping_at_budget():
        # The rows only name the pages that hold a position; the pages' own bytes decide every unit below.
        result = lexstore.get_store(root).search_semantic_units_result(
            [], ROWS_PER_RECALL + 1, (), (), "kb", snapshot.for_scope("kb"),
            clauses=clauses,
            allowed_parent_paths=parents, admitted_parent_paths=parents,
            released_parent_paths=released,
            recall_checkpoint=snapshot.lexical_checkpoint("kb"), allow_delta=False, parents_first=True,
        )
    if result.readiness.status == lexstore.DEADLINE_STATUS:
        working_set.note_stopped("working_set.positions")
        return _empty("unavailable", "budget"), []
    if not result.readiness.complete:
        return _empty("unavailable", "lane_failed"), []
    hits = list(result.value or ())
    page_dates = {hit.parent_path: hit.parent_updated for hit in hits}
    types = note_types.Basis(root)

    def accepts(page: Any) -> bool:
        # A page that supplies the stance or a domain profile is read by its own block, under its own framing, even
        # on a turn where that block does not serve it; framed here, a standing line would read as a dated view and
        # a stance line could arrive twice. These are the closed attributes stance and domain_profiles select by.
        return page.frontmatter.get("stance") is not True and not types.selects(
            activation.normalized_page_type(page.page_type), note_types.profile,
        )

    try:
        admitted, basis = session_units.admit(
            root, caller, set(page_dates), accepts,
            units=lambda unit: unit.category in categories or unit.kind in kinds,
        )
    except working_set_currency.CurrencyUnavailable:
        # A page resolution changed while the units were read; the turn may retry, so say so rather than empty.
        return _empty("unavailable", "currency"), []
    order = {item.ref: index for index, item in enumerate(admitted)}
    current = session_units.current(admitted, basis, today=today)
    # A caller served excerpts of a page reads no more of its units than `read_page` would show of that page.
    page_bounds = {} if full_access else egress.page_text_bounds(
        root, {item.path for item, _review_due in current.items}, purpose=caller.purpose,
    )
    block = _empty()
    served: list[dict[str, Any]] = []
    leftover: list[working_set.LaneItem] = []
    for item, review_due in _ranked(current.items, query, page_dates, order):
        line = {"ref": item.ref, "text": item.text, "title": item.title}
        if item.updated:
            line["date"] = item.updated
        if review_due:
            line["review_due"] = review_due
        # Overflow is a reference, never a cut unit. Beyond the excerpt bound of the unit's page, the block's own bound
        # is the only size test: a careful position runs long, and the working set's per-unit cap would turn exactly
        # those into references.
        page_bound = page_bounds.get(item.path)
        if (page_bound is None or len(item.text) <= page_bound) and chars({"items": [*block["items"], line]}) <= limit:
            block["items"].append(line)
            served.append(working_set._served_unit(item))  # noqa: SLF001 - the shape admission decided
        else:
            leftover.append(item)
    # The references share the bound, after every line that fits.
    for item in leftover:
        refs = [*block["overflow_refs"], item.ref]
        if chars({**block, "overflow_refs": refs}) <= limit:
            block["overflow_refs"] = refs
            served.append(working_set._served_unit(item))  # noqa: SLF001 - a ref names the unit, so egress decides it
    block["omitted"] = len(leftover) - len(block["overflow_refs"])
    if block["items"] or block["overflow_refs"] or block["omitted"]:
        # The framing leads the block: a model reads the items in the light of what it read first.
        block = {"about": ABOUT, **block, "state": "ready"}
    if len(hits) > ROWS_PER_RECALL:
        block.update(state="partial", reason="truncated")
    return block, served


def _ranked(
    current: list[tuple[working_set.LaneItem, str | None]], query: str, page_dates: Mapping[str, str],
    order: Mapping[str, int],
) -> list[tuple[working_set.LaneItem, str | None]]:
    """Units that share more content words with the turn first; on a tie the newest first.

    A unit is read with its page title, because a unit is about its page's subject. The date is the unit's own,
    else its page's. On one date the later line on the page comes first, since a later line revises an earlier one.
    """
    wanted = set(working_set_runtime.content_stems(query))

    def key(entry: tuple[working_set.LaneItem, str | None]) -> tuple:
        item = entry[0]
        shared = len(wanted & set(working_set_runtime.content_stems(f"{item.title}\n{item.text}")))
        return (-shared, _descending(item.updated or page_dates.get(item.path, "")), -order[item.ref])

    return sorted(current, key=key)


def _descending(day: str) -> tuple[int, ...]:
    """An ISO date as a key that sorts the newest first and an undated unit last."""
    digits = [int(part) for part in day[:10].split("-") if part.isdigit()] if day else []
    return tuple(-part for part in digits) if len(digits) == 3 else (1,)


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
