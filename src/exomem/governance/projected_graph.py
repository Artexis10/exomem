"""Graph measurement rows and request-local projected graph reductions.

Edges are persisted beneath principal-free projection variants.  Authorization
first selects one variant or L0 per catalog item; only then is the request graph
materialized.  Every degree, expansion, path, relation match, and rank therefore
runs over the same graph the caller could observe if hidden items were absent.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from . import projected_retrieval, projection_store, projections

_MAX_GRAPH_RESULTS = 1_000


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be bounded non-empty text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise projections.ProjectionCanonicalizationError(
            f"{name} contains an invalid Unicode scalar"
        ) from error
    return value


def _sort_key(value: str) -> bytes:
    return value.encode("utf-16-be")


@dataclass(frozen=True, slots=True)
class ProjectionGraphEdge:
    """One directed typed edge derived from a fixed source projection."""

    source_item_identity: str
    target_item_identity: str
    relation_type: str

    def __post_init__(self) -> None:
        _text(self.source_item_identity, "graph source identity")
        _text(self.target_item_identity, "graph target identity")
        _text(self.relation_type, "graph relation type", maximum=256)


def _edge_sort_key(edge: ProjectionGraphEdge) -> tuple[bytes, bytes, bytes]:
    return (
        _sort_key(edge.source_item_identity),
        _sort_key(edge.target_item_identity),
        _sort_key(edge.relation_type),
    )


@dataclass(frozen=True, slots=True)
class ProjectionGraphMeasurement:
    """Complete outgoing graph measurement for one projection variant."""

    measurement_key: projections.MeasurementKey
    edges: tuple[ProjectionGraphEdge, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.measurement_key, projections.MeasurementKey):
            raise projections.ProjectionCanonicalizationError(
                "graph measurement key is invalid"
            )
        if self.measurement_key.lane != "graph":
            raise projections.ProjectionCanonicalizationError(
                "graph measurement key has an invalid lane"
            )
        if not isinstance(self.edges, tuple):
            raise projections.ProjectionCanonicalizationError(
                "graph measurement edges must be an immutable tuple"
            )
        projections.require_supported_capacity(graph_edges=len(self.edges))
        seen: set[ProjectionGraphEdge] = set()
        for edge in self.edges:
            if not isinstance(edge, ProjectionGraphEdge):
                raise projections.ProjectionCanonicalizationError(
                    "graph measurement edge has an invalid type"
                )
            if edge in seen:
                raise projections.ProjectionCanonicalizationError(
                    "graph measurement contains a duplicate edge"
                )
            seen.add(edge)
        object.__setattr__(self, "edges", tuple(sorted(seen, key=_edge_sort_key)))


@dataclass(frozen=True, slots=True)
class AuthorizedProjectedGraph:
    """One request-local graph after variant and edge admission."""

    namespace_key: projections.ProjectionNamespaceKey
    selected_variants: tuple[tuple[str, str], ...]
    vertices: tuple[str, ...]
    edges: tuple[ProjectionGraphEdge, ...]
    _vertex_set: frozenset[str] = field(init=False, repr=False, compare=False)
    _neighbors: Mapping[str, tuple[str, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _in_degrees: Mapping[str, int] = field(init=False, repr=False, compare=False)
    _out_degrees: Mapping[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        vertex_set = frozenset(self.vertices)
        if len(vertex_set) != len(self.vertices):
            raise projections.ProjectionCanonicalizationError(
                "authorized projected graph contains duplicate vertices"
            )
        neighbors: dict[str, set[str]] = {vertex: set() for vertex in self.vertices}
        in_degrees = dict.fromkeys(self.vertices, 0)
        out_degrees = dict.fromkeys(self.vertices, 0)
        for edge in self.edges:
            if (
                edge.source_item_identity not in vertex_set
                or edge.target_item_identity not in vertex_set
            ):
                raise projections.ProjectionCanonicalizationError(
                    "authorized projected graph edge names an unavailable vertex"
                )
            neighbors[edge.source_item_identity].add(edge.target_item_identity)
            in_degrees[edge.target_item_identity] += 1
            out_degrees[edge.source_item_identity] += 1
        object.__setattr__(self, "_vertex_set", vertex_set)
        object.__setattr__(
            self,
            "_neighbors",
            MappingProxyType(
                {
                    vertex: tuple(sorted(targets, key=_sort_key))
                    for vertex, targets in neighbors.items()
                }
            ),
        )
        object.__setattr__(self, "_in_degrees", MappingProxyType(in_degrees))
        object.__setattr__(self, "_out_degrees", MappingProxyType(out_degrees))

    def _require_vertex(self, item_identity: str) -> None:
        if item_identity not in self._vertex_set:
            raise projected_retrieval.ProjectedRetrievalUnavailable(
                "projected graph vertex is unavailable"
            )

    def in_degree(self, item_identity: str) -> int:
        """Return degree from admitted edges only."""

        self._require_vertex(item_identity)
        return self._in_degrees[item_identity]

    def out_degree(self, item_identity: str) -> int:
        """Return degree from admitted edges only."""

        self._require_vertex(item_identity)
        return self._out_degrees[item_identity]

    def neighbors(self, item_identity: str) -> tuple[str, ...]:
        """Return deterministic outbound neighbors in the authorized graph."""

        self._require_vertex(item_identity)
        return self._neighbors[item_identity]

    def relation_matches(self, relation_type: str) -> tuple[ProjectionGraphEdge, ...]:
        """Match one relation only after edge admission."""

        relation = _text(relation_type, "graph relation type", maximum=256)
        return tuple(edge for edge in self.edges if edge.relation_type == relation)

    def reachable(self, source: str, target: str) -> bool:
        """Test directed reachability over the authorized graph."""

        self._require_vertex(source)
        self._require_vertex(target)
        if source == target:
            return True
        seen = {source}
        pending = deque([source])
        while pending:
            current = pending.popleft()
            for neighbor in self.neighbors(current):
                if neighbor == target:
                    return True
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        return False

    def shortest_path(self, source: str, target: str) -> tuple[str, ...] | None:
        """Return the deterministic shortest directed path, if one exists."""

        self._require_vertex(source)
        self._require_vertex(target)
        if source == target:
            return (source,)
        parent: dict[str, str | None] = {source: None}
        pending = deque([source])
        while pending:
            current = pending.popleft()
            for neighbor in self.neighbors(current):
                if neighbor in parent:
                    continue
                parent[neighbor] = current
                if neighbor == target:
                    path = [target]
                    cursor = current
                    while cursor is not None:
                        path.append(cursor)
                        cursor = parent[cursor]
                    return tuple(reversed(path))
                pending.append(neighbor)
        return None

    def rank_by_in_degree(
        self,
        *,
        k: int,
        exclude: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Rank projected vertices after graph admission and before the cap."""

        if type(k) is not int or not 1 <= k <= _MAX_GRAPH_RESULTS:
            raise ValueError(
                f"k must be an integer from 1 through {_MAX_GRAPH_RESULTS}"
            )
        if not isinstance(exclude, tuple):
            raise ValueError("exclude must be an immutable tuple")
        excluded = frozenset(exclude)
        ranked = sorted(
            (vertex for vertex in self.vertices if vertex not in excluded),
            key=lambda vertex: (-self.in_degree(vertex), _sort_key(vertex)),
        )
        return tuple(ranked[:k])


class ProjectedGraphIndex:
    """Principal-free graph measurements projected into one request-local view."""

    def __init__(
        self,
        namespace: projection_store.VerifiedProjectionNamespace,
        measurements: tuple[ProjectionGraphMeasurement, ...],
        *,
        extractor_version: str,
        model_version: str,
    ) -> None:
        self.catalog = projected_retrieval.ProjectionCatalog(namespace)
        self.extractor_version = _text(
            extractor_version,
            "graph extractor version",
            maximum=256,
        )
        self.model_version = _text(
            model_version,
            "graph model version",
            maximum=256,
        )
        variants = {
            variant.projection_variant_id: variant
            for item in self.catalog.items.values()
            for variant in item.variants
        }
        identities = frozenset(self.catalog.items)
        by_variant: dict[str, ProjectionGraphMeasurement] = {}
        graph_edge_count = 0
        for measurement in measurements:
            if not isinstance(measurement, ProjectionGraphMeasurement):
                raise projections.ProjectionCanonicalizationError(
                    "projected graph measurement has an invalid type"
                )
            graph_edge_count += len(measurement.edges)
            projections.require_supported_capacity(graph_edges=graph_edge_count)
            key = measurement.measurement_key
            if key.extractor_version != self.extractor_version:
                raise projections.ProjectionCanonicalizationError(
                    "graph measurement extractor version does not match index"
                )
            if key.model_version != self.model_version:
                raise projections.ProjectionCanonicalizationError(
                    "graph measurement model version does not match index"
                )
            variant = variants.get(key.projection_variant_id)
            if variant is None:
                raise projections.ProjectionCanonicalizationError(
                    "graph measurement variant is outside the projection catalog"
                )
            if key.projection_variant_id in by_variant:
                raise projections.ProjectionCanonicalizationError(
                    "projected graph index contains a duplicate variant measurement"
                )
            if variant.decision_level < 6 and measurement.edges:
                raise projections.ProjectionCanonicalizationError(
                    "graph edges below L6 are not registered in this projector"
                )
            for edge in measurement.edges:
                if edge.source_item_identity != variant.item_identity:
                    raise projections.ProjectionCanonicalizationError(
                        "graph edge source does not match its projection variant"
                    )
                if edge.target_item_identity not in identities:
                    raise projections.ProjectionCanonicalizationError(
                        "graph edge target is outside the projection catalog"
                    )
            by_variant[key.projection_variant_id] = measurement
        self._measurements = MappingProxyType(by_variant)

    def authorize(
        self,
        authorization: projected_retrieval.AuthorizationProjectionMap,
    ) -> AuthorizedProjectedGraph:
        """Admit vertices and edges before any graph computation."""

        selected = self.catalog.select(authorization)
        selected_identities = frozenset(variant.item_identity for variant in selected)
        admitted_edges: list[ProjectionGraphEdge] = []
        for variant in selected:
            measurement = self._measurements.get(variant.projection_variant_id)
            if measurement is None:
                raise projected_retrieval.ProjectedLaneUnavailable(
                    "selected projection graph measurement is unavailable"
                )
            admitted_edges.extend(
                edge
                for edge in measurement.edges
                if edge.target_item_identity in selected_identities
            )
        vertices = tuple(sorted(selected_identities, key=_sort_key))
        selected_variants = tuple(
            sorted(
                (
                    (variant.item_identity, variant.projection_variant_id)
                    for variant in selected
                ),
                key=lambda item: (_sort_key(item[0]), item[1]),
            )
        )
        return AuthorizedProjectedGraph(
            namespace_key=self.catalog.namespace_key,
            selected_variants=selected_variants,
            vertices=vertices,
            edges=tuple(sorted(admitted_edges, key=_edge_sort_key)),
        )


__all__ = [
    "AuthorizedProjectedGraph",
    "ProjectedGraphIndex",
    "ProjectionGraphEdge",
    "ProjectionGraphMeasurement",
]
