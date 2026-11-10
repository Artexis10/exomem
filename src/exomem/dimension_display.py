"""How a dimension's group shows its units: their order and their marks.

A search whose filter asks for dimension groups (`structured_filters.requested_groups`) gets each
requested dimension's view. The display behaviours a dimension names in the registry compile it.
Each behaviour reads only closed schema values, so a vault dimension that names one needs no code:

- `flag_open` marks a unit `open` when its kind has the `closable` trait, it has no `verdict`, and
  no edge whose relation has the `settles` behaviour ends at the unit or at its page.
- `lead_with_current` puts current units before replaced ones and marks each replaced one
  `replaced`. A unit is replaced when its page's lifecycle class is history, or when an edge whose
  relation has the `replaces` behaviour ends at the unit or at its page; that edge's source is the
  unit's successor.
- `show_origin` gives a unit an `origin`: `by` is its `asserted_by` or `credence_holder`, and `from`
  names the targets of the edges whose relation has the `sources` behaviour and that start at the
  unit or at its page.

A behaviour changes only order and marks; it never removes a unit. Every mark comes from the edges
that the request read in its caller's view (`epistemic_dimensions.read_edges`), so a page withheld
from the caller changes no mark. When those edges are unknown, no unit is marked open, no edge
orders the group, and the group says that its edge state is unknown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from . import context_refs

if TYPE_CHECKING:
    from .epistemic_dimensions import DimensionEdges
    from .epistemic_graph import UnitEdge
    from .relation_registry import RelationRegistry
    from .semantic_language_registry import SemanticLanguageRegistry

#: The relation behaviours each display behaviour reads from the request's edges.
# nosemgrep: ep-word-set -- the closed display behaviours mapped to the closed relation behaviours they read
EDGE_BEHAVIOURS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "flag_open": ("settles",),
        "lead_with_current": ("replaces",),
        "show_origin": ("sources",),
    }
)
#: The mark each counting display behaviour puts on a unit; a group counts the units that carry it.
# nosemgrep: ep-word-set -- the closed display behaviours mapped to the closed mark keys they produce
COUNTED_MARKS: Mapping[str, str] = MappingProxyType(
    {"flag_open": "open", "lead_with_current": "replaced"}
)
#: How many origin refs a unit shows; `more` counts the rest.
ORIGIN_CAP = 3

# One end of an edge: a page and the graph node key of one of its units, or None for the page.
End = tuple[str, str | None]


def edge_behaviours(display: Iterable[str]) -> frozenset[str]:
    """The relation behaviours whose edges the display behaviours `display` read."""
    return frozenset(behaviour for name in display for behaviour in EDGE_BEHAVIOURS.get(name, ()))


@dataclass(frozen=True, slots=True)
class DisplayUnit:
    """What the display behaviours read of one unit.

    `node` is the unit's graph node key, or None when it has none. `kind` is its kind key, or None
    when the kind is another vocabulary instance's entry, which has no trait here. `historical` is
    whether its page's lifecycle class is history (`lifecycle_statuses.Classification.historical`).
    """

    ref: str
    path: str
    node: str | None
    kind: str | None
    verdict: str | None = None
    historical: bool = False
    holder: str | None = None
    occurred_at: str | None = None

    @property
    def ends(self) -> tuple[End, ...]:
        """The edge ends that reach this unit: its own node, and its whole page."""
        page: End = (self.path, None)
        return ((self.path, self.node), page) if self.node is not None else (page,)


@dataclass(frozen=True, slots=True)
class GroupView:
    """One group as its display behaviours show it.

    `order` lists the indices of the input units in display order, and `marks` holds each input
    unit's marks, with the closed keys `open`, `replaced` and `origin`. `order_conflict` lists the
    indices of units whose order edges form a cycle. `edges_known` is false when a behaviour needs
    edges that the request could not read. `counts` gives, for each mark that the group's
    behaviours produce, how many units carry it; a count is absent when the edges are unknown.
    """

    order: tuple[int, ...]
    marks: tuple[Mapping[str, Any], ...]
    order_conflict: tuple[int, ...] = ()
    edges_known: bool = True
    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _EdgeIndex:
    """The request's edges that a group's behaviours read, by the end each behaviour looks up."""

    settled: frozenset[End] = frozenset()
    successors: Mapping[End, tuple[End, ...]] = field(default_factory=dict)
    sources: Mapping[End, tuple[End, ...]] = field(default_factory=dict)

    @classmethod
    def build(
        cls, edges: Iterable[UnitEdge], relations: RelationRegistry, wanted: frozenset[str]
    ) -> _EdgeIndex:
        settled: set[End] = set()
        successors: dict[End, list[End]] = {}
        sources: dict[End, list[End]] = {}
        for edge in edges:
            behaviours = _behaviours_of(relations, edge) & wanted
            if not behaviours:
                continue
            source: End = (edge.source_path, edge.source_unit)
            target: End = (edge.target_path, edge.target_unit)
            if "settles" in behaviours:
                settled.add(target)
            if "replaces" in behaviours:
                successors.setdefault(target, []).append(source)
            if "sources" in behaviours:
                sources.setdefault(source, []).append(target)
        return cls(
            frozenset(settled),
            {end: tuple(found) for end, found in successors.items()},
            {end: tuple(found) for end, found in sources.items()},
        )


def _behaviours_of(relations: RelationRegistry, edge: UnitEdge) -> frozenset[str]:
    """What an edge's relation does: its replacement's behaviours when the key is deprecated, and
    its parent's when this registry does not hold the key, as `DimensionMembership.family_of`."""
    key = relations.terminal_replacement(edge.relation) or edge.relation
    found = relations.behaviours_of(key)
    if not found and edge.parent is not None:
        found = relations.behaviours_of(edge.parent)
    return found


def display(
    units: Sequence[DisplayUnit],
    behaviours: Iterable[str],
    *,
    language: SemanticLanguageRegistry,
    relations: RelationRegistry,
    edges: DimensionEdges | None,
    named: Mapping[End, str] | None = None,
) -> GroupView:
    """Order and mark one group's `units` by the display behaviours its dimension names.

    `edges` are the request's edges in its caller's view, or None when the request read none.
    `named` maps the edge end of each unit in the whole result to its ref, so a successor or an
    origin that is in the result is named by its own ref; any other end is named by its page's.
    """
    behaviours = frozenset(behaviours)
    wanted = edge_behaviours(behaviours)
    known = not wanted or (edges is not None and edges.status == "available")
    index = _EdgeIndex.build(edges.edges if known and edges is not None else (), relations, wanted)
    refs = {**{(unit.path, unit.node): unit.ref for unit in units if unit.node}, **(named or {})}

    def name(end: End) -> str:
        return (refs.get(end) if end[1] is not None else None) or context_refs.vault_ref(end[0])

    marks: list[dict[str, Any]] = []
    for unit in units:
        mark: dict[str, Any] = {}
        if "flag_open" in behaviours and known and _is_open(unit, index, language):
            mark["open"] = True
        if "lead_with_current" in behaviours:
            replaced = _replaced(unit, index, name)
            if replaced is not None:
                mark["replaced"] = replaced
        if "show_origin" in behaviours:
            origin = _origin(unit, index, name, known=known)
            if origin:
                mark["origin"] = origin
        marks.append(mark)

    order = list(range(len(units)))
    if "lead_with_current" in behaviours:
        # A stable partition: each side keeps the order it had.
        order = [i for i in order if "replaced" not in marks[i]] + [
            i for i in order if "replaced" in marks[i]
        ]
    counts: dict[str, int] = {}
    if known:
        for behaviour, counted in COUNTED_MARKS.items():
            if behaviour in behaviours:
                counts[counted] = sum(counted in mark for mark in marks)
    return GroupView(tuple(order), tuple(marks), (), known, counts)


def _is_open(unit: DisplayUnit, index: _EdgeIndex, language: SemanticLanguageRegistry) -> bool:
    return (
        unit.kind is not None
        and language.has(unit.kind, "closable")
        and not unit.verdict
        and not any(end in index.settled for end in unit.ends)
    )


def _replaced(unit: DisplayUnit, index: _EdgeIndex, name) -> dict[str, str] | bool | None:
    """`{"by": ref}` when an edge names the unit's successor, True when only its page's lifecycle
    says it is history, None when it is current."""
    successors = sorted(
        {end for own in unit.ends for end in index.successors.get(own, ())},
        key=lambda end: (end[0], end[1] or ""),
    )
    if successors:
        return {"by": name(successors[0])}
    return True if unit.historical else None


def _origin(unit: DisplayUnit, index: _EdgeIndex, name, *, known: bool) -> dict[str, Any]:
    origin: dict[str, Any] = {}
    if unit.holder:
        origin["by"] = unit.holder
    if known:
        found = sorted({name(end) for own in unit.ends for end in index.sources.get(own, ())})
        if found:
            origin["from"] = found[:ORIGIN_CAP]
            if len(found) > ORIGIN_CAP:
                origin["more"] = len(found) - ORIGIN_CAP
    return origin
