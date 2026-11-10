"""Which epistemic dimensions a unit belongs to.

A dimension is registry data (`semantic_labels.DIMENSIONS`): a key with members, which are
relation families, unit kinds and governed unit-metadata keys. This module is the one place that
applies the membership rule. Nothing here names a dimension, a family, a kind or a key; every
word comes from the registries, so a vault dimension needs no code.

A unit belongs to a dimension when its kind is a member, when the dimension's kinds hold the
wildcard, when it carries a member key, or when it is an end of an edge whose relation family is
a member. A family comes from the relation registry entry: a core key's own, a vault extension's
parent's, never a saved family.

The graph holds an edge at the grain it was written. A unit's own relation row starts at that
unit, and a link with an anchor ends at the unit the anchor names. A frontmatter field, a page's
relations section and a link with no anchor start or end at the whole page, and such an end
reaches every unit of that page. Membership is computed in two steps: one graph read returns the
edges of a candidate set (`read_edges`), and a pure step places each unit (`UnitDimensions`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import semantic_labels, semantic_units
from .relation_registry import RelationRegistry
from .semantic_language_registry import SemanticLanguageRegistry

if TYPE_CHECKING:
    from .epistemic_graph import UnitEdge
    from .vault import WikilinkResolver


@dataclass(frozen=True, slots=True)
class DimensionMembership:
    """The registries' dimensions folded into lookups, built once per request.

    Building reads the registries; `of` reads only these lookups, so a request that tests many
    units pays for the registries once.
    """

    relations: RelationRegistry
    # Each member list inverted: the dimensions a kind, a metadata key or a family belongs to.
    by_kind: Mapping[str, frozenset[str]]
    by_key: Mapping[str, frozenset[str]]
    by_family: Mapping[str, frozenset[str]]
    # The dimensions whose `kinds` holds the wildcard: every unit belongs to them.
    every_kind: frozenset[str]
    # Each dimension's own member lists, as `MEMBER_LISTS` names them.
    members: Mapping[str, Mapping[str, tuple[str, ...]]]
    # Each dimension's display behaviours, from `DISPLAY_BEHAVIOURS`, as the registry lists them.
    display: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def needs_edges(self) -> bool:
        """Whether any dimension here has a family member, so a unit's edges can change its
        membership. A reader that has no edges to give skips the edge read when this is false."""
        return bool(self.by_family)

    def family_of(self, relation: str, parent: str | None = None) -> str | None:
        """The family of an edge's relation key: the roll-up rule in `RelationRegistry.family_of`.

        An edge authored with a deprecated key belongs to its replacement's family. A key this
        registry does not hold, such as another instance's extension, rolls up through `parent`.
        """
        key = self.relations.terminal_replacement(relation) or relation
        family = self.relations.family_of(key)
        if family is None and parent is not None:
            family = self.relations.family_of(parent)
        return family

    def families_of(self, edge_relations: Iterable[str]) -> frozenset[str]:
        """The families of relation keys, each through `family_of`."""
        families = (self.family_of(relation) for relation in edge_relations)
        return frozenset(family for family in families if family is not None)

    def of(
        self,
        *,
        kind: str | None,
        row_keys: Collection[str] = (),
        edge_families: Iterable[str] = (),
    ) -> frozenset[str]:
        """The dimensions a unit belongs to.

        `kind` is the unit's resolved kind key, or None when the kind is another vocabulary
        instance's entry, which matches no member kind, as `unit.kind` matches none. `row_keys`
        are the governed rows the unit carries and `edge_families` the relation families of every
        edge that has the unit as an end. A unit can belong to several dimensions, and to none.
        """
        found = set(self.every_kind)
        if kind is not None:
            found |= self.by_kind.get(kind, frozenset())
        for key in row_keys:
            found |= self.by_key.get(key, frozenset())
        for family in edge_families:
            found |= self.by_family.get(family, frozenset())
        return frozenset(found)


def membership(
    language: SemanticLanguageRegistry,
    relations: RelationRegistry,
    *,
    only: Collection[str] | None = None,
) -> DimensionMembership:
    """The membership lookups of the dimensions `language` has, or only those named in `only`."""
    by: dict[str, dict[str, set[str]]] = {name: {} for name in semantic_labels.MEMBER_LISTS}
    every_kind: set[str] = set()
    members: dict[str, dict[str, tuple[str, ...]]] = {}
    display: dict[str, tuple[str, ...]] = {}
    for key, dimension in language.label_definitions(semantic_labels.DIMENSIONS).items():
        if only is not None and key not in only:
            continue
        members[key] = dict(dimension.members)
        display[key] = dimension.display
        for list_name, values in dimension.members:
            for value in values:
                if list_name == "kinds" and value == semantic_labels.ALL_KINDS:
                    every_kind.add(key)
                else:
                    by[list_name].setdefault(value, set()).add(key)
    frozen = {
        name: {value: frozenset(keys) for value, keys in lookup.items()}
        for name, lookup in by.items()
    }
    return DimensionMembership(
        relations=relations,
        by_kind=frozen["kinds"],
        by_key=frozen["keys"],
        by_family=frozen["families"],
        every_kind=frozenset(every_kind),
        members=members,
        display=display,
    )


def dimensions_of(
    unit: object,
    *,
    language: SemanticLanguageRegistry,
    relations: RelationRegistry,
    edge_relations: Iterable[str] = (),
) -> frozenset[str]:
    """`DimensionMembership.of` for one parsed unit, given the relation keys of its edges.

    A reader that tests many units builds `membership` once and reads `UnitDimensions` instead.
    """
    lookups = membership(language, relations)
    return lookups.of(
        kind=str(getattr(unit, "kind", "")),
        row_keys=semantic_units.carried_row_keys(unit),
        edge_families=lookups.families_of(edge_relations),
    )


@dataclass(frozen=True, slots=True)
class DimensionEdges:
    """The edges of a candidate set whose family some dimension has, or why the graph could not
    say. `status` is the graph's: only "available" makes `edges` authoritative, where an empty
    tuple is a real "no such edges"."""

    status: str
    edges: tuple[UnitEdge, ...] = ()


def read_edges(
    vault_root: Path,
    lookups: DimensionMembership,
    *,
    pages: Collection[str] | None = None,
    admission: Callable[[str], bool] | None = None,
    resolver: Callable[[], WikilinkResolver] | None = None,
    behaviours: Collection[str] = (),
) -> DimensionEdges:
    """Read, in one graph read, every edge whose family a dimension of `lookups` has, and every
    edge whose relation has one of the relation `behaviours` that a group's display reads
    (`dimension_display.edge_behaviours`). Membership keeps only the edges of its families.

    `pages` is the candidate set's pages: the read keeps the edges with an end on one of them.
    Without it the read covers the vault, for a caller whose candidate set is every unit. The
    shipped keys of each family find a vault extension's edges too, because an extension rolls
    up to its parent's key.

    An edge counts only when the caller may see both of its pages (`egress.edge_view`), so
    membership reads as if every page withheld from the caller were absent. `admission` is the
    request's content admission, or None when it has none. `resolver` builds the request's link
    resolver; the read calls it only for a restricted caller, to re-derive a link that resolves
    differently for that caller. The read never scans the corpus and never reports an unreadable
    graph as an empty one.
    """
    if not lookups.needs_edges and not behaviours:
        return DimensionEdges("available")
    from . import epistemic_graph
    from .governance import egress

    keys = sorted(
        key
        for key, entry in lookups.relations.core.items()
        if entry.family in lookups.by_family or not entry.behaviours.isdisjoint(behaviours)
    )
    keep = egress.edge_view(vault_root, admission)
    result = epistemic_graph.EpistemicGraphIndex(vault_root).unit_edges(
        keys,
        pages=pages,
        keep=keep,
        resolver=resolver() if keep is not None and resolver is not None else None,
    )
    return DimensionEdges(result.status, result.edges if result.status == "available" else ())


@dataclass(frozen=True, slots=True)
class UnitDimensions:
    """Answers `unit.dimension` for one candidate set: the dimensions each of its units belongs to.

    Built from the set's edges by `from_edges`, with no further read. A unit view names its kind,
    the governed rows it carries (`row_keys`) and where the graph holds it (`occurrence`); a page
    view names its path.
    """

    membership: DimensionMembership
    # The families of the edges that end at each unit, by graph node key, and at each whole page.
    unit_families: Mapping[str, frozenset[str]]
    page_families: Mapping[str, frozenset[str]]
    # The pages that hold a unit some edge ends at, so the unit's node key is derived only there.
    unit_pages: frozenset[str]

    @classmethod
    def from_edges(cls, lookups: DimensionMembership, edges: Iterable[UnitEdge]) -> UnitDimensions:
        units: dict[str, set[str]] = {}
        pages: dict[str, set[str]] = {}
        unit_pages: set[str] = set()
        for edge in edges:
            family = lookups.family_of(edge.relation, edge.parent)
            if family not in lookups.by_family:
                continue
            for path, unit in (
                (edge.source_path, edge.source_unit),
                (edge.target_path, edge.target_unit),
            ):
                if unit is None:
                    pages.setdefault(path, set()).add(family)
                else:
                    units.setdefault(unit, set()).add(family)
                    unit_pages.add(path)
        return cls(
            lookups,
            {unit: frozenset(families) for unit, families in units.items()},
            {page: frozenset(families) for page, families in pages.items()},
            frozenset(unit_pages),
        )

    def __call__(
        self, page: Mapping[str, Any], unit: Mapping[str, Any], *, own_kind: bool = True
    ) -> frozenset[str]:
        families: set[str] = set()
        if self.unit_families or self.page_families:
            path = page.get("path")
            if path is None:
                raise ValueError("a page view that is evaluated for a dimension names its path")
            families |= self.page_families.get(path, frozenset())
            if path in self.unit_pages:
                node = node_key(str(path), unit)
                if node is not None:
                    families |= self.unit_families.get(node, frozenset())
        return self.membership.of(
            kind=str(unit.get("kind", "")) if own_kind else None,
            row_keys=tuple(unit.get("row_keys") or ()),
            edge_families=families,
        )


def node_key(path: str, unit: Mapping[str, Any]) -> str | None:
    """The graph node key of a unit view's occurrence, derived from the parser's coordinates when
    the view holds them rather than the key, because the key costs a hash."""
    from . import epistemic_graph

    occurrence = unit.get("occurrence")
    if occurrence is None:
        return None
    if not isinstance(occurrence, str):
        occurrence = semantic_units.occurrence_key(*occurrence)
    return epistemic_graph.unit_node_key(path, occurrence, str(unit.get("form", "")))
