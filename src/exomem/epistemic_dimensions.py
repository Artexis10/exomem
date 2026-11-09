"""Which epistemic dimensions a unit belongs to.

A dimension is registry data (`semantic_labels.DIMENSIONS`): a key with members, which are
relation families, unit kinds and governed unit-metadata keys. This module is the one place that
applies the membership rule. Nothing here names a dimension, a family, a kind or a key; every
word comes from the registries, so a vault dimension needs no code.

A unit belongs to a dimension when its kind is a member, when it carries a member key, or when it
is an endpoint of an edge whose relation family is a member. A family comes from the relation
registry entry: a core key's own, a vault extension's parent's, never a saved family.

The graph records a typed edge between pages, not between units, so a unit is an endpoint of
every edge its page is an endpoint of; the `relations` filter treats a unit the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import semantic_labels
from .relation_registry import RelationRegistry
from .semantic_language_registry import SemanticLanguageRegistry


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
    # The dimensions whose `kinds` holds the wildcard, and the kinds the page's registry has.
    every_kind: frozenset[str]
    known_kinds: frozenset[str]

    @property
    def needs_edges(self) -> bool:
        """Whether any dimension here has a family member, so a unit's edges can change its
        membership. A reader that has no edges to give skips the edge read when this is false."""
        return bool(self.by_family)

    def families_of(self, edge_relations: Iterable[str]) -> frozenset[str]:
        """The families of relation keys: the roll-up rule, kept in `RelationRegistry.family_of`."""
        families = (self.relations.family_of(relation) for relation in edge_relations)
        return frozenset(family for family in families if family is not None)

    def of(
        self,
        *,
        kind: str,
        row_keys: Collection[str] = (),
        edge_families: Iterable[str] = (),
    ) -> frozenset[str]:
        """The dimensions a unit belongs to.

        `kind` is the unit's resolved kind key, `row_keys` the keys of the rows it carries and
        `edge_families` the relation families of every edge that has the unit as an endpoint. A
        unit can belong to several dimensions, and to none.
        """
        found = set(self.by_kind.get(kind, ()))
        if kind in self.known_kinds:
            found |= self.every_kind
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
    for key, dimension in language.label_definitions(semantic_labels.DIMENSIONS).items():
        if only is not None and key not in only:
            continue
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
        known_kinds=frozenset((*language.core_kinds, *language.kinds)),
    )


def dimensions_of(
    unit: object,
    *,
    language: SemanticLanguageRegistry,
    relations: RelationRegistry,
    edge_relations: Iterable[str] = (),
) -> frozenset[str]:
    """`DimensionMembership.of` for one parsed unit, building the lookups for this call.

    A reader that tests many units builds `membership` once instead.
    """
    lookups = membership(language, relations)
    return lookups.of(
        kind=str(getattr(unit, "kind", "")),
        row_keys=tuple(getattr(unit, "metadata", None) or ()),
        edge_families=lookups.families_of(edge_relations),
    )


@dataclass(frozen=True, slots=True)
class FamilyPages:
    """The pages that an edge of each family touches, or why the graph could not say.

    `status` is the graph's: "available" means `pages` is authoritative, where an empty set is
    a real "no such edges"; any other value means the graph is missing, stale or off, and
    `pages` is empty without meaning that no edge exists.
    """

    status: str
    pages: Mapping[str, frozenset[str]]


def read_family_pages(
    vault_root: Path,
    relations: RelationRegistry,
    families: Collection[str],
    *,
    keep: Callable[[str], bool] | None = None,
) -> FamilyPages:
    """Read, in one graph query, the pages that an edge of each of `families` touches.

    A vault extension rolls up to its parent's key, so the query on the shipped keys of each
    family finds the extension's edges too. `keep` is the caller's admission: an edge counts only
    when the caller may read both of its pages. It never scans the corpus and never reports an
    unreadable graph as an empty one.
    """
    if not families:
        return FamilyPages("available", {})
    from . import epistemic_graph

    keys = sorted(
        key for key, entry in relations.core.items() if entry.family in set(families)
    )
    result = epistemic_graph.EpistemicGraphIndex(vault_root).relation_participants(
        keys, keep=keep
    )
    if result.status != "available":
        return FamilyPages(result.status, {})
    found: dict[str, set[str]] = {family: set() for family in families}
    for page, matched in result.matched_keys.items():
        for key in matched:
            # An edge authored with a deprecated key matched through its replacement, so it
            # belongs to the replacement's family.
            family = relations.family_of(relations.terminal_replacement(key) or key)
            if family in found:
                found[family].add(page)
    return FamilyPages("available", {family: frozenset(pages) for family, pages in found.items()})


@dataclass(frozen=True, slots=True)
class UnitDimensions:
    """Answers `unit.dimension` for one request: the dimensions a unit belongs to.

    `participants` maps each family some dimension names to the pages that are an endpoint of an
    edge of that family, read from the graph once for the whole candidate set.
    """

    membership: DimensionMembership
    participants: Mapping[str, frozenset[str]]

    def __call__(self, page: Mapping[str, Any], unit: Mapping[str, Any]) -> frozenset[str]:
        families: tuple[str, ...] = ()
        if self.participants:
            path = page.get("path")
            if path is None:
                raise ValueError("a page view that is evaluated for a dimension names its path")
            families = tuple(
                family for family, pages in self.participants.items() if path in pages
            )
        return self.membership.of(
            kind=str(unit.get("kind", "")),
            row_keys=tuple(unit.get("row_keys") or ()),
            edge_families=families,
        )
