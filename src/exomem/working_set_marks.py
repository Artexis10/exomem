"""Display marks on the units that a context packet serves.

A packet's units carry the marks that a search's dimension groups give the same units
(`dimension_display`): `open`, `replaced` and `origin`. Every dimension that names a display
behaviour takes part, in registry order, so a vault dimension needs no code. Nothing here names a
dimension, a kind or a relation key.

`compile` reads the edges of the packet's candidate units once, in the reader view and declared
purpose that admitted the units, and gives each unit the union of the marks its dimensions give it.
A mark depends only on its own unit, the edges and the refs of the candidates, so the budget can
charge a unit's marks before it decides what to serve. `PacketMarks.ordered` then lets each
dimension's display order fill only the packet positions that its served members hold. Where two
dimensions order the same units, the later one in the registry stands.

When the edges are unknown, no unit is marked open, no edge orders a unit, and the packet's
`generation` says that the edge state is unknown.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import dimension_display, epistemic_dimensions, semantic_labels

if TYPE_CHECKING:
    from .lifecycle_statuses import Basis
    from .relation_registry import RelationRegistry
    from .semantic_language_registry import SemanticLanguageRegistry
    from .working_set import LaneItem

#: The packet's `generation` key that reports the edge state, and its value when the edges that
#: the marks read are unknown. The key is absent when the edges were read.
GENERATION_KEY = "display_edges"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EdgeView:
    """Where and for whom a packet reads its units' edges.

    `visible` is the reader view that admitted the packet's units, or None when nothing is
    withheld from the caller. `purpose` is the declared purpose of the request, or None.
    `snapshot` is the request's freshness snapshot, which builds a restricted caller's link
    resolver; None builds one at the read.
    """

    root: Path
    visible: Callable[[str], bool] | None
    purpose: str | None
    snapshot: Any = None


@dataclass(frozen=True, slots=True)
class PacketMarks:
    """The marks of a packet's candidate units and what orders the served ones.

    `marks` holds each marked unit's marks by its ref. `groups` holds, for each dimension that
    names a display behaviour, in registry order, its behaviours and the refs of its candidate
    members.
    """

    marks: Mapping[str, Mapping[str, Any]]
    groups: tuple[tuple[tuple[str, ...], frozenset[str]], ...]
    units: Mapping[str, dimension_display.DisplayUnit]
    edges: epistemic_dimensions.DimensionEdges
    language: SemanticLanguageRegistry
    relations: RelationRegistry
    named: Mapping[dimension_display.End, str]

    @property
    def edges_known(self) -> bool:
        return self.edges.status == "available"

    def ordered(self, served: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """`served`, with each dimension's display order filling the positions its members hold.

        Every other unit keeps its position. A dimension orders its members as they stand after
        the dimensions before it in the registry, so the later order stands where two disagree.
        """
        out = list(served)
        for behaviours, members in self.groups:
            positions = [i for i, unit in enumerate(out) if unit.get("ref") in members]
            if len(positions) < 2:
                continue
            view = dimension_display.display(
                [self.units[out[i]["ref"]] for i in positions],
                behaviours,
                language=self.language,
                relations=self.relations,
                edges=self.edges,
                named=self.named,
            )
            placed = [out[positions[p]] for p in view.order]
            for i, unit in zip(positions, placed, strict=True):
                out[i] = unit
        return out


def compile(
    items: Sequence[LaneItem], view: EdgeView, *, status_basis: Basis
) -> PacketMarks | None:
    """The marks of `items`, a packet's candidates in packet order, read in `view`.

    None when no item is a selected unit or no dimension names a display behaviour: the packet
    then reads no edges and carries no mark.
    """
    from . import find, relation_registry, semantic_language_registry
    from .vocabulary.registry import PUBLIC_INSTANCE

    hits = {item.ref: item.unit_hit for item in items if item.unit_hit is not None and item.ref}
    if not hits:
        return None
    language = semantic_language_registry.load_registry(view.root)
    displaying = [
        key
        for key, dimension in language.label_definitions(semantic_labels.DIMENSIONS).items()
        if dimension.display
    ]
    if not displaying:
        return None
    lookups = epistemic_dimensions.membership(
        language, relation_registry.load_registry(view.root), only=displaying
    )
    edges = _read_edges(view, lookups, pages={hit.parent_path for hit in hits.values()})
    dimensions = epistemic_dimensions.UnitDimensions.from_edges(lookups, edges.edges)
    units: dict[str, dimension_display.DisplayUnit] = {}
    belongs: dict[str, frozenset[str]] = {}
    for ref, hit in hits.items():
        # Unit values are read in the public instance, as a search reads them.
        units[ref], belongs[ref] = find.display_unit_of(
            hit, dimensions, instance=PUBLIC_INSTANCE, status_basis=status_basis
        )
    named = {(unit.path, unit.node): ref for ref, unit in units.items() if unit.node is not None}
    marks: dict[str, dict[str, Any]] = {}
    groups: list[tuple[tuple[str, ...], frozenset[str]]] = []
    for key, behaviours in lookups.display.items():
        members = [ref for ref in hits if key in belongs[ref]]
        if not members:
            continue
        shown = dimension_display.display(
            [units[ref] for ref in members],
            behaviours,
            language=language,
            relations=lookups.relations,
            edges=edges,
            named=named,
        )
        for ref, mark in zip(members, shown.marks, strict=True):
            if mark:
                marks.setdefault(ref, {}).update(mark)
        groups.append((behaviours, frozenset(members)))
    return PacketMarks(marks, tuple(groups), units, edges, language, lookups.relations, named)


def _read_edges(
    view: EdgeView, lookups: epistemic_dimensions.DimensionMembership, *, pages: set[str]
) -> epistemic_dimensions.DimensionEdges:
    """One graph read of the edges that the displaying dimensions read, for `pages`, in the
    caller's view under the request's declared purpose."""
    from . import find
    from .governance import principal

    def resolver():
        snapshot = view.snapshot or find.FreshnessSnapshot(view.root)
        return find._request_resolver(view.root, snapshot)()

    behaviours = dimension_display.edge_behaviours(
        behaviour for display in lookups.display.values() for behaviour in display
    )
    scope = (
        principal.request_scope(principal.effective_principal().with_purpose(view.purpose))
        if view.purpose is not None
        else contextlib.nullcontext()
    )
    with scope:
        return epistemic_dimensions.read_edges(
            view.root,
            lookups,
            pages=pages,
            admission=view.visible,
            resolver=resolver,
            behaviours=behaviours,
        )


def chars(marks: Mapping[str, Any] | None) -> int:
    """What a unit's marks cost the packet's budget: their compact JSON form, as the reply
    carries them. No marks cost nothing."""
    if not marks:
        return 0
    return len(json.dumps(marks, separators=(",", ":"), ensure_ascii=False))
