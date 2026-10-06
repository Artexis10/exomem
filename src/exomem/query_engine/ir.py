"""Immutable logical query nodes, carrying no execution or release authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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


class BackendAdapter(Protocol):
    """Trusted capability advertisement, not an executor for logical queries."""

    @property
    def capabilities(self) -> frozenset[str]: ...
