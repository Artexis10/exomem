"""Admitted units of live compiled pages, as session context lines.

The stance block, the domain profiles and the recorded positions all serve a bounded set of lines from a few
index-narrowed pages. They differ in which pages and units they take and in their bounds; they share the rule
that a unit is admitted before anything is counted, cached or served, and that currency and the budget come
after admission.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import lifecycle_statuses, semantic_contract, vault, working_set, working_set_currency
from .governance import egress


@dataclass(slots=True)
class Served:
    """The lines one set of admitted units yields within a bound, and what the bound and currency left out."""

    lines: list[dict[str, Any]] = field(default_factory=list)
    #: The unit shape each line was admitted as; terminal egress decides it again.
    units: list[dict[str, Any]] = field(default_factory=list)
    omitted: int = 0
    expired: int = 0
    #: The days on which a unit's currency changes without a write.
    boundaries: list[date] = field(default_factory=list)


@dataclass(slots=True)
class Current:
    """The admitted units that are current today, each with the review date it is due by, if any."""

    items: list[tuple[working_set.LaneItem, str | None]] = field(default_factory=list)
    expired: int = 0
    #: The days on which a unit's currency changes without a write.
    boundaries: list[date] = field(default_factory=list)


def admit(
    root: Path, caller: Any, paths: Iterable[str], accepts: Callable[[Any], bool],
    units: Callable[[Any], bool] | None = None,
) -> tuple[tuple[working_set.LaneItem, ...], lifecycle_statuses.Basis]:
    """The units that pass unit admission, from the pages `accepts` takes and, when given, the units `units` takes.

    A hidden unit changes nothing downstream: it is not counted, hashed or ordered. An index narrows `paths`
    to marked, admitted pages, so activation never walks the corpus.
    """
    candidates = []
    guards = {}
    for path in sorted(paths):
        source, guard = vault.read_guarded_text(root, root / path)
        guards[path] = guard
        page = semantic_contract.build_page_state(root, path, source)
        if not page.eligible_compiled or not accepts(page):
            continue
        # Page supersession is an existing authored lifecycle signal, including legacy pages.
        if page.frontmatter.get("superseded_by"):
            continue
        for unit in page.document.units:
            if not unit.unit_ref or (units is not None and not units(unit)) or any(
                unit.span.start_line <= error.line <= unit.span.end_line
                for error in page.document.errors if error.line is not None
            ):
                continue
            candidates.append(working_set.LaneItem(
                role="", level="unit", ref=unit.unit_ref, path=path, title=page.title,
                text=working_set.bounded_text(unit.content), lifecycle=page.status or "",
                # The unit's own authored date, read as the working set's unit lane reads it.
                updated=unit.as_of or working_set_currency.own_time(unit.context), anchor=path,
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
    basis = lifecycle_statuses.Basis(root)
    return working_set_currency.annotate(admitted, status_basis=basis, visible=caller.visible), basis


def identity(admitted: Iterable[working_set.LaneItem]) -> list[tuple]:
    """What a cached serve depends on. The source path and anchor join it: terminal egress re-decides the page
    they name, so a rename must not leave a cached entry pointing at whatever page now holds the old path."""
    return [(item.ref, item.text, item.lifecycle, dict(item.provenance), item.path, item.anchor) for item in admitted]


def current(admitted: Iterable[working_set.LaneItem], basis: lifecycle_statuses.Basis, *, today: date) -> Current:
    """The units whose page lifecycle is not historical and that no expiry or unit supersession has retired, in order."""
    now = Current()
    for item in admitted:
        ordering = basis.ordering(item.lifecycle, path=item.path)
        if ordering is None or ordering.historical:
            continue
        for name in ("until", "check_by"):  # Closed typed date fields; each changes currency the following day.
            if value := item.provenance.get(name):
                authored_date = date.fromisoformat(value)
                if authored_date < date.max:
                    boundary = authored_date + timedelta(days=1)
                    if boundary > today:
                        now.boundaries.append(boundary)
        history, review_due = working_set_currency.state(item, today=today, status_basis=basis)
        if history:
            now.expired += 1
            continue
        now.items.append((item, review_due))
    return now


def serve(
    admitted: Iterable[working_set.LaneItem], basis: lifecycle_statuses.Basis, *,
    cost: Callable[[dict[str, Any]], int], limit: int, today: date,
) -> Served:
    """Whole lines within `limit`, in the order given. `cost` is what one line occupies where it is rendered."""
    now = current(admitted, basis, today=today)
    served = Served(expired=now.expired, boundaries=now.boundaries)
    used = 0
    for item, review_due in now.items:
        line = {"ref": item.ref, "text": item.text}
        if review_due:
            line["review_due"] = review_due
        line_cost = cost(line) + 1
        if not item.text or used + line_cost > limit:
            served.omitted += 1
            continue
        served.lines.append(line)
        served.units.append(working_set._served_unit(item))  # noqa: SLF001 - the shape admission decided
        used += line_cost
    return served
