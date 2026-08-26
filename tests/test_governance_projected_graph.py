"""Request-local graph reductions over the authorized projected corpus."""

from __future__ import annotations

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import projected_graph, projected_retrieval, projection_store, projections
from exomem.governance.decisions import Decision


def _key() -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="e" * 64,
        projector_schema_version=1,
        catalog_generation=18,
    )


def _variant(identity: str, content_hash: str, *, level: int = 6) -> projections.ProjectionVariant:
    option_key = {1: "notice", 2: "constraint", 3: "abstract"}.get(level)
    variant = projections.build_projection_variant(
        item_identity=identity,
        content_hash=content_hash,
        decision=Decision(
            level=level,
            options={} if option_key is None else {option_key: identity},
        ),
        projector_schema_version=1,
        full_search_fields={"body": identity, "title": identity},
    )
    assert variant is not None
    return variant


def _item(variant: projections.ProjectionVariant) -> projection_store.ProjectionItemVariants:
    return projection_store.ProjectionItemVariants(
        variant.item_identity,
        variant.content_hash,
        (variant,),
    )


def _namespace(
    *items: projection_store.ProjectionItemVariants,
) -> projection_store.VerifiedProjectionNamespace:
    return verified_namespace(_key(), items)


def _map(
    *pairs: tuple[
        projection_store.ProjectionItemVariants,
        projections.ProjectionVariant | None,
    ],
) -> projected_retrieval.AuthorizationProjectionMap:
    return projected_retrieval.AuthorizationProjectionMap(
        _key(),
        tuple(
            projected_retrieval.ProjectionSelection(
                item.item_identity,
                item.content_hash,
                None if variant is None else variant.projection_variant_id,
            )
            for item, variant in pairs
        ),
    )


def _edge(source: str, target: str, relation: str = "supports") -> projected_graph.ProjectionGraphEdge:
    return projected_graph.ProjectionGraphEdge(
        source_item_identity=source,
        target_item_identity=target,
        relation_type=relation,
    )


def _measurement(
    variant: projections.ProjectionVariant,
    *edges: projected_graph.ProjectionGraphEdge,
    extractor_version: str = "relations-v1",
    model_version: str = "graph-schema-v1",
) -> projected_graph.ProjectionGraphMeasurement:
    return projected_graph.ProjectionGraphMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="graph",
            extractor_version=extractor_version,
            model_version=model_version,
        ),
        edges=edges,
    )


def test_hidden_intermediary_is_equivalent_to_physical_absence() -> None:
    first = _variant("first", "1" * 64)
    hidden = _variant("hidden", "2" * 64)
    last = _variant("last", "3" * 64)
    first_item, hidden_item, last_item = _item(first), _item(hidden), _item(last)
    present = projected_graph.ProjectedGraphIndex(
        _namespace(first_item, hidden_item, last_item),
        (
            _measurement(first, _edge("first", "hidden")),
            _measurement(hidden, _edge("hidden", "last")),
            _measurement(last),
        ),
        extractor_version="relations-v1",
        model_version="graph-schema-v1",
    ).authorize(_map((first_item, first), (hidden_item, None), (last_item, last)))
    absent = projected_graph.ProjectedGraphIndex(
        _namespace(first_item, last_item),
        (_measurement(first), _measurement(last)),
        extractor_version="relations-v1",
        model_version="graph-schema-v1",
    ).authorize(_map((first_item, first), (last_item, last)))

    assert present == absent
    assert present.reachable("first", "last") is False
    assert present.shortest_path("first", "last") is None


def test_hidden_edges_do_not_change_degree_or_graph_rank() -> None:
    first = _variant("first", "4" * 64)
    hidden = _variant("hidden", "5" * 64)
    target = _variant("target", "6" * 64)
    first_item, hidden_item, target_item = _item(first), _item(hidden), _item(target)
    graph = projected_graph.ProjectedGraphIndex(
        _namespace(first_item, hidden_item, target_item),
        (
            _measurement(first, _edge("first", "target")),
            _measurement(
                hidden,
                _edge("hidden", "target"),
                _edge("hidden", "first"),
            ),
            _measurement(target),
        ),
        extractor_version="relations-v1",
        model_version="graph-schema-v1",
    ).authorize(_map((first_item, first), (hidden_item, None), (target_item, target)))

    assert graph.in_degree("target") == 1
    assert graph.out_degree("first") == 1
    assert graph.rank_by_in_degree(k=2) == ("target", "first")


def test_edges_to_hidden_targets_are_removed_before_relation_matching() -> None:
    source = _variant("source", "7" * 64)
    visible = _variant("visible", "8" * 64)
    hidden = _variant("hidden", "9" * 64)
    source_item, visible_item, hidden_item = _item(source), _item(visible), _item(hidden)
    graph = projected_graph.ProjectedGraphIndex(
        _namespace(source_item, visible_item, hidden_item),
        (
            _measurement(
                source,
                _edge("source", "visible", "supports"),
                _edge("source", "hidden", "contradicts"),
            ),
            _measurement(visible),
            _measurement(hidden),
        ),
        extractor_version="relations-v1",
        model_version="graph-schema-v1",
    ).authorize(
        _map((source_item, source), (visible_item, visible), (hidden_item, None))
    )

    assert graph.relation_matches("supports") == (
        _edge("source", "visible", "supports"),
    )
    assert graph.relation_matches("contradicts") == ()
    assert graph.neighbors("source") == ("visible",)


def test_index_compacts_measurements_before_runtime_publication() -> None:
    source = _variant("source", "d" * 64)
    target = _variant("target", "e" * 64)
    source_item, target_item = _item(source), _item(target)
    index = projected_graph.ProjectedGraphIndex(
        _namespace(source_item, target_item),
        (
            _measurement(source, _edge("source", "target", "supports")),
            _measurement(target),
        ),
        extractor_version="relations-v1",
        model_version="graph-schema-v1",
    )

    assert all(
        not isinstance(value, projected_graph.ProjectionGraphMeasurement)
        for value in index._measurements.values()
    )
    assert index.authorize(_map((source_item, source), (target_item, target))).edges == (
        _edge("source", "target", "supports"),
    )


def test_missing_selected_graph_measurement_disables_lane_without_raw_fallback() -> None:
    source = _variant("source", "a" * 64)
    source_item = _item(source)
    index = projected_graph.ProjectedGraphIndex(
        _namespace(source_item),
        (),
        extractor_version="relations-v1",
        model_version="graph-schema-v1",
    )

    with pytest.raises(projected_retrieval.ProjectedLaneUnavailable, match="selected"):
        index.authorize(_map((source_item, source)))


def test_lower_projection_edges_and_mismatched_subkeys_are_refused() -> None:
    lower = _variant("lower", "b" * 64, level=3)
    target = _variant("target", "c" * 64)
    lower_item, target_item = _item(lower), _item(target)
    with pytest.raises(projections.ProjectionCanonicalizationError, match="below L6"):
        projected_graph.ProjectedGraphIndex(
            _namespace(lower_item, target_item),
            (
                _measurement(lower, _edge("lower", "target")),
                _measurement(target),
            ),
            extractor_version="relations-v1",
            model_version="graph-schema-v1",
        )
    with pytest.raises(projections.ProjectionCanonicalizationError, match="model"):
        projected_graph.ProjectedGraphIndex(
            _namespace(target_item),
            (_measurement(target, model_version="other-graph"),),
            extractor_version="relations-v1",
            model_version="graph-schema-v1",
        )
