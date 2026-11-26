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
    check: Callable[[], None] | None = None,
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

    A write to a page the caller may not see leaves its read as it was: the edges are those of a
    vault that never had that write, or unknown when the graph cannot show that
    (`EpistemicGraphIndex.unit_edges`). The owner's read keeps the graph's own gating.
    """
    if not lookups.needs_edges and not behaviours:
        return DimensionEdges("available")
    from . import epistemic_graph
    from .collection_store.connection import checked_sql
    from .governance import egress
    from .vault_store import mode as store_mode
    from .vault_store import read as store_read

    if check is not None:
        check()
    if pages is not None and store_mode.store_mode_for_reads(vault_root):
        import sqlite3

        from .find import RetrievalIndexWarming
        from .vocabulary.registry import RegistryError

        conn = store_read.lend(vault_root)
        if conn is None:
            return DimensionEdges("unavailable")
        try:
            with checked_sql(conn, check):
                view = epistemic_graph.GraphView(
                    vault_root, conn, keep=egress.edge_view(vault_root, admission),
                    reads_placeholders=False, check=check,
                )
                edges = tuple(epistemic_graph.stored_family_edges(
                    vault_root, conn, pages, lookups, keep=admission, behaviours=behaviours, view=view,
                ))
            return DimensionEdges("available", edges)
        except (RetrievalIndexWarming, sqlite3.Error):
            return DimensionEdges("warming")
        except RegistryError:
            return DimensionEdges("unavailable")
        finally:
            conn.close()

    keys = sorted(
        key
        for key, entry in lookups.relations.core.items()
        if entry.family in lookups.by_family or not entry.behaviours.isdisjoint(behaviours)
    )
    keep = egress.edge_view(vault_root, admission)
    # Optional file reads share a cold proof instead of spending their request's guard reserve.
    result = epistemic_graph.EpistemicGraphIndex(
        vault_root,
        prove_cold_snapshots=epistemic_graph.SINGLE_FLIGHT if check is not None else True,
    ).unit_edges(
        keys,
        pages=pages,
        keep=keep,
        resolver=resolver,
        reader=egress.visible_page_filter(vault_root) if keep is not None else None,
        check=check,
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
    def from_edges(cls, lookups: DimensionMembership, edges: Iterable[UnitEdge], *,
                   paths: Collection[str] | None = None) -> UnitDimensions:
        units: dict[str, set[str]] = {}
        pages: dict[str, set[str]] = {}
        unit_pages: set[str] = set()
        for edge in edges:
            family = edge.family if edge.family is not None else lookups.family_of(edge.relation, edge.parent)
            if family not in lookups.by_family:
                continue
            for path, unit in (
                (edge.source_path, edge.source_unit),
                (edge.target_path, edge.target_unit),
            ):
                if paths is not None and path not in paths:
                    continue
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


class StoredUnitDimensions:
    """Selected family membership of explicitly prepared stored candidates, never ranking statistics."""

    def __init__(self, vault_root: Path, lookups: DimensionMembership, *, keep: Callable[[str], bool] | None,
                 needs_edges: bool):
        self.vault_root, self.lookups, self.keep = vault_root, lookups, keep
        self.needs_edges = needs_edges
        self.prepared: set[str] = set()
        self.view: Any = None
        self.snapshot: Any = None
        self.value = UnitDimensions.from_edges(lookups, ())

    def prepare(self, paths: Collection[str], *, conn: Any = None, interpretations: Any = None) -> None:
        if not self.needs_edges:
            return
        from . import call_spans, epistemic_graph
        from .governance import egress
        from .vault_store import read as store_read

        borrowed = store_read.lend(self.vault_root) if conn is None else None
        active = conn if conn is not None else borrowed
        try:
            identity = store_read.snapshot_for_connection(active)
            if identity is None or identity is not self.snapshot:
                self.snapshot = identity
                self.prepared.clear()
                self.value = UnitDimensions.from_edges(self.lookups, ())
                self.view = None
            missing = set(paths) - self.prepared
            if not missing:
                return
            view = self.view or epistemic_graph.GraphView(
                self.vault_root, active,
                keep=egress.edge_view(self.vault_root, self.keep), reads_placeholders=False,
                interpretations=interpretations,
            )
            # A caller-owned connection can be reused only while its pinned snapshot remains live.
            if conn is not None and store_read.snapshot_active(identity):
                self.view = view
            with call_spans.span("dimension.prepare", {"pages": len(missing)}):
                selected = UnitDimensions.from_edges(self.lookups, epistemic_graph.stored_family_edges(
                    self.vault_root, active, missing, self.lookups, keep=self.keep, view=view,
                ), paths=missing)
            self.value = UnitDimensions(
                self.lookups,
                {**self.value.unit_families, **selected.unit_families},
                {**self.value.page_families, **selected.page_families},
                self.value.unit_pages | selected.unit_pages,
            )
            self.prepared.update(missing)
        finally:
            if borrowed is not None:
                borrowed.close()

    def __call__(self, page: Mapping[str, Any], unit: Mapping[str, Any], *, own_kind: bool = True) -> frozenset[str]:
        if self.needs_edges:
            from .vault_store import read as store_read
            from .vocabulary.registry import RegistryError

            # Reusing facts after their snapshot ends could silently serve stale positive or negative membership.
            if not store_read.snapshot_active(self.snapshot):
                raise RegistryError("REGISTRY_UNAVAILABLE: selected dimension snapshot ended")
        return self.value(page, unit, own_kind=own_kind)


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
