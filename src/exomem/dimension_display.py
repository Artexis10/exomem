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
- `order_in_time` sorts a group by `occurred_at`, earliest first, with undated units after the
  dated ones. Each edge between two members whose relation has the `earlier_than_target` or
  `later_than_target` behaviour then orders its two ends, by a stable topological sort that keeps
  the first order wherever no edge decides. Units whose edges form a cycle keep their first order,
  and the group names them as an order conflict.

A behaviour changes only order and marks; it never removes a unit. Every mark comes from the edges
that the request read in its caller's view (`epistemic_dimensions.read_edges`), so a page withheld
from the caller changes no mark. When those edges are unknown, no unit is marked open, no edge
orders the group, and the group says that its edge state is unknown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
        "order_in_time": ("earlier_than_target", "later_than_target"),
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
    # Each order edge as its (earlier end, later end).
    before: tuple[tuple[End, End], ...] = ()

    @classmethod
    def build(
        cls, edges: Iterable[UnitEdge], relations: RelationRegistry, wanted: frozenset[str]
    ) -> _EdgeIndex:
        settled: set[End] = set()
        successors: dict[End, list[End]] = {}
        sources: dict[End, list[End]] = {}
        before: list[tuple[End, End]] = []
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
            if "earlier_than_target" in behaviours:
                before.append((source, target))
            if "later_than_target" in behaviours:
                before.append((target, source))
        return cls(
            frozenset(settled),
            {end: tuple(found) for end, found in successors.items()},
            {end: tuple(found) for end, found in sources.items()},
            tuple(before),
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
    conflict: tuple[int, ...] = ()
    if "order_in_time" in behaviours:
        order = _by_time(units)
        if known:
            order, conflict = _by_edges(order, units, index)
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
    return GroupView(tuple(order), tuple(marks), conflict, known, counts)


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


def _instant(value: str | None) -> datetime | None:
    """`occurred_at` as one comparable instant: a date at its start, a time with an offset in UTC.
    None when the value is absent or no ISO date or date and time."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def _by_time(units: Sequence[DisplayUnit]) -> list[int]:
    """The units' indices, dated ones earliest first, then the undated ones in their given order."""
    instants = [_instant(unit.occurred_at) for unit in units]
    dated = sorted(
        (i for i, instant in enumerate(instants) if instant is not None),
        key=lambda i: (instants[i], i),
    )
    return dated + [i for i, instant in enumerate(instants) if instant is None]


def _by_edges(
    order: list[int], units: Sequence[DisplayUnit], index: _EdgeIndex
) -> tuple[list[int], tuple[int, ...]]:
    """`order` changed only where an order edge between two members decides, and the members whose
    edges form a cycle.

    A cycle's members are one component that keeps its first order. The components are then sorted
    topologically, taking at each step the waiting component that comes first in `order`.
    """
    reached: dict[End, list[int]] = {}
    for i, unit in enumerate(units):
        for end in unit.ends:
            reached.setdefault(end, []).append(i)
    later: dict[int, set[int]] = {i: set() for i in order}
    for earlier_end, later_end in index.before:
        for earlier in reached.get(earlier_end, ()):
            later[earlier].update(i for i in reached.get(later_end, ()) if i != earlier)
    if not any(later.values()):
        return order, ()

    def reachable(start: int) -> set[int]:
        seen: set[int] = set()
        stack = [start]
        while stack:
            for nxt in later[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    reach = {i: reachable(i) for i in order}
    # Each component is named by its first member in `order`.
    component: dict[int, int] = {}
    for i in order:
        if i not in component:
            for j in order:
                if j == i or (j in reach[i] and i in reach[j]):
                    component.setdefault(j, i)
    waiting_on: dict[int, set[int]] = {c: set() for c in component.values()}
    for i, after in later.items():
        for j in after:
            if component[i] != component[j]:
                waiting_on[component[j]].add(component[i])
    position = {i: p for p, i in enumerate(order)}
    pending = sorted(waiting_on, key=position.__getitem__)
    placed: set[int] = set()
    result: list[int] = []
    while pending:
        # The condensed graph has no cycle, so some pending component always waits on none.
        ready = next(c for c in pending if waiting_on[c] <= placed)
        pending.remove(ready)
        placed.add(ready)
        result.extend(i for i in order if component[i] == ready)
    sizes: dict[int, int] = {}
    for c in component.values():
        sizes[c] = sizes.get(c, 0) + 1
    return result, tuple(i for i in order if sizes[component[i]] > 1)


@dataclass(frozen=True, slots=True)
class Group:
    """One requested group: its dimension's key, the label a reply names it by, and the display
    behaviours the dimension names."""

    key: str
    label: str
    display: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GroupRequest:
    """What a search hands to `group_hits`.

    `groups` are the requested groups in the caller's order. `units` holds, by unit ref, what the
    display reads of each unit the search selected and the requested dimensions it belongs to.
    `edges` are the request's edges in its caller's view, or None when it read none.
    """

    groups: tuple[Group, ...]
    units: Mapping[str, tuple[DisplayUnit, frozenset[str]]]
    edges: DimensionEdges | None
    language: SemanticLanguageRegistry
    relations: RelationRegistry


@dataclass(frozen=True, slots=True)
class Grouped:
    """A search's hits in group order, each marked, and the reply's `dimension_groups` list."""

    hits: list[Any]
    groups: list[dict[str, Any]]


def group_hits(hits: Sequence[Any], request: GroupRequest) -> Grouped:
    """Order a search's released hits by group and mark each one by its group's behaviours.

    A hit is in full in the first requested group it belongs to, where it carries that group's key
    as `dimension` and its marks as `display`. Each later group it belongs to lists it as a ref
    that names the group showing it. Grouping only orders and marks: the limit has already cut the
    hits, so every hit stays, and one in no group keeps its rank after the groups.
    """
    members: dict[str, list[int]] = {group.key: [] for group in request.groups}
    named: dict[End, str] = {}
    for i, hit in enumerate(hits):
        entry = request.units.get(getattr(hit, "unit_ref", ""))
        if entry is None:
            continue
        unit, dimensions = entry
        if unit.node is not None:
            named[(unit.path, unit.node)] = unit.ref
        for group in request.groups:
            if group.key in dimensions:
                members[group.key].append(i)
    shown: dict[int, str] = {}
    ordered: list[Any] = []
    listed_groups: list[dict[str, Any]] = []
    for group in request.groups:
        indices = members[group.key]
        view = display(
            [request.units[hits[i].unit_ref][0] for i in indices],
            group.display,
            language=request.language,
            relations=request.relations,
            edges=request.edges,
            named=named,
        )
        listed: list[dict[str, str]] = []
        for position in view.order:
            i = indices[position]
            hit = hits[i]
            if i in shown:
                listed.append({"unit_ref": hit.unit_ref, "shown_in": shown[i]})
                continue
            shown[i] = group.key
            hit.dimension = group.key
            hit.display = dict(view.marks[position]) or None
            ordered.append(hit)
            listed.append({"unit_ref": hit.unit_ref})
        block: dict[str, Any] = {
            "dimension": group.key,
            "label": group.label,
            "count": len(indices),
            **view.counts,
        }
        if view.order_conflict:
            block["order_conflict"] = [hits[indices[p]].unit_ref for p in view.order_conflict]
        if not view.edges_known:
            block["edges"] = "unknown"
        block["units"] = listed
        listed_groups.append(block)
    ordered.extend(hit for i, hit in enumerate(hits) if i not in shown)
    return Grouped(ordered, listed_groups)


def outcome(groups: Sequence[Mapping[str, Any]]) -> str:
    """One sentence for each group of a `dimension_groups` list that an agent can repeat: the
    group's label, how many units it holds and how many carry each counted mark."""
    sentences = []
    for group in groups:
        count = int(group["count"])
        text = f"{count} {'unit' if count == 1 else 'units'} in '{group['label']}'"
        if "open" in group:
            text += f"; {group['open']} {'is' if group['open'] == 1 else 'are'} open"
        if "replaced" in group:
            text += f"; {group['replaced']} {'was' if group['replaced'] == 1 else 'were'} replaced"
        if group.get("order_conflict"):
            text += f"; {len(group['order_conflict'])} have conflicting order links"
        if group.get("edges") == "unknown":
            text += "; its links could not be read yet, so its marks may be incomplete"
        sentences.append(text + ".")
    return " ".join(sentences)
