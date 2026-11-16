"""Bounded global units from live compiled pages, admitted before caching."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import (
    activation_conventions,
    context_catalogue,
    lifecycle_statuses,
    semantic_contract,
    vault,
    working_set,
    working_set_currency,
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


def compile_block(
    root: Path, caller: Any, max_chars: int | None = None, *, today: date | None = None, catalogue: Any = None,
    known_generation: str | None = None,
) -> dict[str, Any]:
    """Compile only admitted inputs; a hidden line changes no generation, count or expiry.

    The block's own bound is the vault's `budgets.stance_max_chars`; `max_chars` only tightens it.
    `known_generation` is the stance generation the caller holds: a block still at it repeats no line.
    """
    today = today or date.today()
    own_catalogue = catalogue is None
    catalogue = catalogue or context_catalogue.ContextCatalogue(root)
    try:
        pending = set(catalogue.pending())
        paths = {entry.path for entry in catalogue.entries_by_id(catalogue.stance_ids()) if entry.path and entry.path not in pending and caller.sees(entry.path)}
    finally:
        if own_catalogue:
            catalogue.close()
    basis = lifecycle_statuses.Basis(root)
    candidates = []
    guards = {}
    for path in sorted(paths):
        # The catalogue narrows the read to marked admitted pages; never walk the corpus on activation.
        source, guard = vault.read_guarded_text(root, root / path)
        guards[path] = guard
        page = semantic_contract.build_page_state(root, path, source)
        if not page.eligible_compiled or page.frontmatter.get("stance") is not True:
            continue
        # Page supersession is an existing authored lifecycle signal, including legacy pages.
        if page.frontmatter.get("superseded_by"):
            continue
        for unit in page.document.units:
            if not unit.unit_ref or any(
                unit.span.start_line <= error.line <= unit.span.end_line
                for error in page.document.errors if error.line is not None
            ):
                continue
            candidates.append(working_set.LaneItem(
                role="", level="unit", ref=unit.unit_ref, path=path, title=page.title,
                text=working_set.bounded_text(unit.content), lifecycle=page.status or "",
                updated=unit.as_of or "", anchor=path,
                # The language schema fixes these fields; relation families come from the registry.
                provenance={
                    **{key: getattr(unit, key) for key in ("as_of", "until", "check_by") if getattr(unit, key)},
                    "supersedes_targets": working_set_currency.relation_targets(
                        [relation.to_dict() for relation in unit.relations], registry=page.definitions.snapshots["relations"].typed if page.definitions is not None else None,
                    ),
                },
            ))
    wire = [working_set._served_unit(item) for item in candidates]  # noqa: SLF001 - exactly the existing unit admission shape
    verdicts = egress.classify_units(root, wire, purpose=caller.purpose)
    admitted = tuple(item for item, verdict in zip(candidates, verdicts, strict=True) if verdict == egress.UNIT_KEPT)
    vault.recheck_path_guards(root, (guards[path] for path in {item.path for item in admitted}))
    admitted = working_set_currency.annotate(admitted, status_basis=basis, visible=caller.visible)
    configured = activation_conventions.load_conventions(root).conventions.budget("stance_max_chars")
    bounds = [value for value in (configured, max_chars) if value is not None]
    # The shipped conventions file always sets the budget; the range's top only caps a broken install.
    limit = max(0, min(bounds) if bounds else activation_conventions.BUDGET_BOUNDS["stance_max_chars"][1])
    # The source path and anchor join the key: terminal egress re-decides the page they name, so a
    # rename must not leave a cached entry pointing at whatever page now holds the old path.
    inputs = [(item.ref, item.text, item.lifecycle, dict(item.provenance), item.path, item.anchor) for item in admitted]
    key = (str(root), caller.binding, _digest(inputs), limit)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and (cached[0] is None or today < cached[0]):
            _CACHE.move_to_end(key)
            block, served = _as_held(copy.deepcopy(cached[1]), copy.deepcopy(cached[2]), known_generation)
            _pin(root, caller, block, served)
            return block
    block = _empty()
    # Terminal egress re-decides the unit shape compile admitted, path included, not the bare served line.
    served: list[dict[str, Any]] = []
    boundaries = []
    used = 0
    for item in admitted:
        ordering = basis.ordering(item.lifecycle, path=item.path)
        if ordering is None or ordering.historical:
            continue
        for field in ("until", "check_by"):  # Closed typed date fields; each changes currency the following day.
            if value := item.provenance.get(field):
                authored_date = date.fromisoformat(value)
                if authored_date < date.max:
                    boundary = authored_date + timedelta(days=1)
                    if boundary > today:
                        boundaries.append(boundary)
        history, review_due = working_set_currency.state(item, today=today, status_basis=basis)
        if history:
            block["expired"] += 1
            continue
        line = {"ref": item.ref, "text": item.text}
        if review_due:
            line["review_due"] = review_due
        cost = len(item_line(line)) + 1
        if not item.text or used + cost > limit:
            block["omitted"] += 1
            continue
        block["items"].append(line)
        served.append(working_set._served_unit(item))  # noqa: SLF001 - the shape compile admitted
        used += cost
    block["state"] = "ready" if block["items"] else "empty"
    block["generation"] = _digest(block)
    with _LOCK:
        _CACHE[key] = (min(boundaries) if boundaries else None, copy.deepcopy(block), copy.deepcopy(served))
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    block, served = _as_held(block, served, known_generation)
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
