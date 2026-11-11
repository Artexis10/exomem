"""Immutable logical query nodes, carrying no execution or release authority."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

# The predicate operator groups every evaluator reads; the grammar fixes each group.
# nosemgrep: ep-word-set -- closed query operators fixed by the query grammar
TEXT_OPS = frozenset({"contains", "icontains", "startswith"})
NULL_STATE_OPS = frozenset({"is_null", "is_missing", "is_not_null"})
PRESENCE_OPS = frozenset({"exists", "missing"})


@dataclass(frozen=True, slots=True)
class Source:
    domain: str
    ref: str
    type_id: str


@dataclass(frozen=True, slots=True)
class Field:
    source: Source
    path: str
    value_type: str
    alias: str | None = None
    enum: tuple[str | int | float | bool, ...] = ()


@dataclass(frozen=True, slots=True)
class RelativeDate:
    amount: int
    unit: str
    relative_to: str = "as_of"


@dataclass(frozen=True, slots=True)
class Range:
    lower: object
    upper: object
    include_lower: bool = True
    include_upper: bool = True


@dataclass(frozen=True, slots=True)
class Filter:
    op: str
    field: Field | None = None
    value: object = None
    children: tuple[Filter, ...] = ()


@dataclass(frozen=True, slots=True)
class Project:
    fields: tuple[Field, ...]


@dataclass(frozen=True, slots=True)
class Join:
    source: Source
    relation: str
    source_field: Field
    target: Source
    alias: str
    kind: str
    cardinality: str


@dataclass(frozen=True, slots=True)
class GroupKey:
    field: Field
    bucket: str | None = None
    #: Inclusive source-local day window on a time bucket's basis (ISO dates).
    window_from: str | None = None
    window_to: str | None = None


@dataclass(frozen=True, slots=True)
class NamedAggregate:
    name: str
    op: str
    field: Field | None = None
    p: float | None = None
    where: Filter | None = None


@dataclass(frozen=True, slots=True)
class Aggregate:
    groups: tuple[GroupKey, ...]
    values: tuple[NamedAggregate, ...]


@dataclass(frozen=True, slots=True)
class SortKey:
    field: Field
    direction: str = "asc"
    nulls: str = "last"


@dataclass(frozen=True, slots=True)
class Sort:
    keys: tuple[SortKey, ...]


@dataclass(frozen=True, slots=True)
class Page:
    limit: int = 50
    after: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentMatch:
    op: str
    terms: tuple[str, ...] = ()
    children: tuple[DocumentMatch, ...] = ()


@dataclass(frozen=True, slots=True)
class Traverse:
    anchors: tuple[str, ...]
    relations: tuple[str, ...]
    min_hops: int = 1
    max_hops: int = 3
    direction: str = "outbound"
    subtypes: bool = True
    output: str = "nodes"


@dataclass(frozen=True, slots=True)
class Path:
    kind: str
    start: str
    end: str
    max_hops: int = 3


@dataclass(frozen=True, slots=True)
class PatternNode:
    name: str
    anchor: str | None = None


@dataclass(frozen=True, slots=True)
class PatternEdge:
    start: str
    end: str
    relation: str
    direction: str
    min_hops: int
    max_hops: int
    subtypes: bool = True


@dataclass(frozen=True, slots=True)
class Pattern:
    nodes: tuple[PatternNode, ...]
    edges: tuple[PatternEdge, ...]


@dataclass(frozen=True, slots=True)
class Related:
    path: str
    limit: int = 10
    #: ``(source collection id, declaration dotted name)`` for an inverse entry, else None.
    inverse: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Query:
    source: Source
    select: Project
    where: Filter | None
    joins: tuple[Join, ...]
    aggregate: Aggregate | None
    having: Filter | None
    order_by: Sort
    page: Page
    graph: Traverse | Path | Pattern | None = None
    text: DocumentMatch | None = None
    as_of: str | None = None
    mode: str = "execute"
    execution_profile: str = "interactive"
    version: int = 1
    related: tuple[Related, ...] = ()


def fields(query: Query) -> Iterator[Field]:
    """Every field a query names: its select, filter, order, group keys, aggregates and having."""

    def leaves(node: Filter | None) -> Iterator[Field]:
        if node is None:
            return
        for child in node.children:
            yield from leaves(child)
        if node.field is not None:
            yield node.field

    yield from query.select.fields
    yield from leaves(query.where)
    for key in query.order_by.keys:
        yield key.field
    if query.aggregate is not None:
        for group in query.aggregate.groups:
            yield group.field
        for value in query.aggregate.values:
            if value.field is not None:
                yield value.field
            yield from leaves(value.where)
    yield from leaves(query.having)
