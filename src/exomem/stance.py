"""Bounded global units from live compiled pages, admitted before caching."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Any

from . import (
    activation,
    activation_conventions,
    context_catalogue,
    note_types,
    session_units,
    vocabulary_resolution,
)
from .governance import egress, principal

# A resource bound, not vault vocabulary. The stance character cap is the vault's `budgets.stance_max_chars`.
CACHE_SIZE = 128
_CACHE: OrderedDict[tuple, tuple[date | None, dict[str, Any], list[dict[str, Any]]]] = OrderedDict()
_LOCK = threading.Lock()


def enabled() -> bool:
    return os.environ.get("EXOMEM_ACTIVATION_STANCE", "on").strip().casefold() != "off"


def item_line(item: dict[str, Any]) -> str:
    marker = f" (review due {item['review_due']})" if item.get("review_due") else ""
    return f"- stance: {item['text']}{marker} [{item['ref']}]"


def chars(block: dict[str, Any]) -> int:
    lines = [item_line(item) for item in block.get("items", ())]
    return len("\n".join(lines)) + (1 if lines else 0)


def _empty(state: str = "empty") -> dict[str, Any]:
    unknown = state == "unavailable"
    return {"state": state, "generation": None if unknown else "", "items": [], "omitted": None if unknown else 0, "expired": None if unknown else 0}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _is_global_profile(page: Any, types: note_types.Basis) -> bool:
    """A page of a profile type that declares no topics: the global profile, which supplies stance as a marked page does."""
    return types.selects(activation.normalized_page_type(page.page_type), note_types.profile) and not (
        vocabulary_resolution.declared_topics(page.frontmatter.get("topics"))
    )


def compile_block(
    root: Path, caller: Any, max_chars: int | None = None, *, today: date | None = None, catalogue: Any = None,
    known_generation: str | None = None,
) -> dict[str, Any]:
    """Compile only admitted inputs; a hidden line changes no generation, count or expiry.

    The block's own bound is the vault's `budgets.stance_max_chars`; `max_chars` only tightens it.
    `known_generation` is the stance generation the caller holds: a block still at it repeats no line.
    A page marked `stance: true` and a profile page with no topics both supply it, under the same rules.
    """
    today = today or date.today()
    own_catalogue = catalogue is None
    catalogue = catalogue or context_catalogue.ContextCatalogue(root)
    try:
        pending = set(catalogue.pending())
        # The catalogue marks both kinds of page; a profile that names a topic is a domain's, not the stance's.
        paths = {
            entry.path for entry in catalogue.entries_by_id([*catalogue.stance_ids(), *catalogue.profile_ids()])
            if (entry.stance or not entry.topics) and entry.path and entry.path not in pending and caller.sees(entry.path)
        }
    finally:
        if own_catalogue:
            catalogue.close()
    types = note_types.Basis(root)
    admitted, basis = session_units.admit(
        root, caller, paths, lambda page: page.frontmatter.get("stance") is True or _is_global_profile(page, types),
    )
    configured = activation_conventions.load_conventions(root).conventions.budget("stance_max_chars")
    bounds = [value for value in (configured, max_chars) if value is not None]
    # The shipped conventions file always sets the budget; the range's top only caps a broken install.
    limit = max(0, min(bounds) if bounds else activation_conventions.BUDGET_BOUNDS["stance_max_chars"][1])
    key = (str(root), caller.binding, _digest(session_units.identity(admitted)), limit)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and (cached[0] is None or today < cached[0]):
            _CACHE.move_to_end(key)
            block, served = _as_held(copy.deepcopy(cached[1]), copy.deepcopy(cached[2]), known_generation)
            _pin(root, caller, block, served)
            return block
    block = _empty()
    # Terminal egress re-decides the unit shape compile admitted, path included, not the bare served line.
    result = session_units.serve(admitted, basis, cost=lambda line: len(item_line(line)), limit=limit, today=today)
    block.update(items=result.lines, omitted=result.omitted, expired=result.expired)
    block["state"] = "ready" if block["items"] else "empty"
    block["generation"] = _digest(block)
    with _LOCK:
        _CACHE[key] = (
            min(result.boundaries) if result.boundaries else None, copy.deepcopy(block), copy.deepcopy(result.units),
        )
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    block, served = _as_held(block, result.units, known_generation)
    _pin(root, caller, block, served)
    return block


def _as_held(
    block: dict[str, Any], served: list[dict[str, Any]], known_generation: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The block as this caller is owed it: a caller that holds exactly these lines is sent none of them again."""
    if block["state"] == "ready" and known_generation and known_generation == block["generation"]:
        block.update(state="unchanged", items=[])
        return block, []
    return block, served


def _pin(root: Path, caller: Any, block: dict[str, Any], served: list[dict[str, Any]]) -> None:
    held = principal.invocation_served()
    if held is not None:
        held[("stance", str(root), caller.binding, block["generation"], block["state"])] = (
            copy.deepcopy(block), copy.deepcopy(served),
        )


def guard_block(root: Path, caller: Any, block: dict[str, Any]) -> dict[str, Any]:
    """Restore this invocation's block and decide its complete unit prose at terminal egress."""
    held = principal.invocation_served()
    pinned = None if held is None else held.get(
        ("stance", str(root), caller.binding, block.get("generation"), block.get("state")),
    )
    if pinned is None:
        return _empty("unavailable")
    block, served = pinned
    # A bare {ref, text} line lacks the page path a release-gated vault needs for an identified page's unit refs.
    if any(verdict != egress.UNIT_KEPT for verdict in egress.classify_units(root, copy.deepcopy(served), purpose=caller.purpose)):
        return _empty("unavailable")
    return copy.deepcopy(block)
