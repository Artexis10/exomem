"""Exact catalog successors for principal-free projected graph measurements."""

from __future__ import annotations

from pathlib import Path

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import (
    catalog_publication,
    projected_graph,
    projection_measurement_store,
    projection_store,
    projections,
)
from exomem.governance.decisions import Decision

_GRAPH_EXTRACTOR = "projected-graph-v1"
_GRAPH_MODEL = "graph-schema-v1"


def _key(generation: int) -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="b" * 64,
        projector_schema_version=1,
        catalog_generation=generation,
    )


def _variant(
    identity: str,
    content_hash: str,
    *,
    level: int = 6,
) -> projections.ProjectionVariant:
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


def _item(
    identity: str,
    content_hash: str,
    *variants: projections.ProjectionVariant,
) -> projection_store.ProjectionItemVariants:
    return projection_store.ProjectionItemVariants(
        item_identity=identity,
        content_hash=content_hash,
        variants=variants,
    )


def _family(
    key: projections.ProjectionNamespaceKey,
) -> projection_measurement_store.MeasurementFamilyKey:
    return projection_measurement_store.MeasurementFamilyKey(
        namespace_key=key,
        lane="graph",
        extractor_version=_GRAPH_EXTRACTOR,
        model_version=_GRAPH_MODEL,
    )


def _edge(
    source: str,
    target: str,
    relation: str = "supports",
) -> projected_graph.ProjectionGraphEdge:
    return projected_graph.ProjectionGraphEdge(
        source_item_identity=source,
        target_item_identity=target,
        relation_type=relation,
    )


def _measurement(
    family: projection_measurement_store.MeasurementFamilyKey,
    variant: projections.ProjectionVariant,
    *edges: projected_graph.ProjectionGraphEdge,
) -> projected_graph.ProjectionGraphMeasurement:
    return projected_graph.ProjectionGraphMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane=family.lane,
            extractor_version=family.extractor_version,
            model_version=family.model_version,
        ),
        edges=edges,
    )


def _prepared_namespace(
    key: projections.ProjectionNamespaceKey,
    items: tuple[projection_store.ProjectionItemVariants, ...],
) -> projection_store.PreparedProjectionNamespace:
    return projection_store.prepare_projection_namespace(
        key=key,
        manifest=projection_store.preview_variant_store(key=key, items=items),
        items=items,
    )


def test_graph_successor_carries_unchanged_rows_and_replaces_changed_source(
    tmp_path: Path,
) -> None:
    source_full = _variant("source", "1" * 64)
    source_lower = _variant("source", "1" * 64, level=3)
    target = _variant("target", "2" * 64)
    active_items = (
        _item("source", "1" * 64, source_lower, source_full),
        _item("target", "2" * 64, target),
    )
    active_key = _key(1)
    active_namespace = verified_namespace(active_key, active_items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(
            _measurement(active_family, source_lower),
            _measurement(active_family, source_full, _edge("source", "target")),
            _measurement(active_family, target),
        ),
    )

    changed_source = _variant("source", "3" * 64)
    changed_source_lower = _variant("source", "3" * 64, level=3)
    target_namespace = _prepared_namespace(
        _key(2),
        (
            _item("source", "3" * 64, changed_source_lower, changed_source),
            active_items[1],
        ),
    )

    prepared = catalog_publication._prepare_target_measurements(
        tmp_path,
        active_namespace=active_namespace,
        active_roots=(projection_measurement_store.measurement_root(active_manifest),),
        target_namespace=target_namespace,
        graph_replacements=(
            catalog_publication.GraphMeasurementReplacement(
                item_identity="source",
                content_hash="3" * 64,
                edges=(_edge("source", "target", "refines"),),
            ),
        ),
    )

    assert len(prepared) == 1
    successor = prepared[0]
    assert successor.family.namespace_key == target_namespace.namespace_key
    assert successor.manifest.measurement_count == 3
    assert successor.manifest.graph_edge_count == 1
    by_variant = {row.measurement_key.projection_variant_id: row for row in successor.measurements}
    assert by_variant[changed_source_lower.projection_variant_id].edges == ()
    assert by_variant[changed_source.projection_variant_id].edges == (
        _edge("source", "target", "refines"),
    )
    assert by_variant[target.projection_variant_id].edges == ()


def test_prepare_markdown_batch_forwards_graph_replacements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = catalog_publication.GraphMeasurementReplacement(
        item_identity="source",
        content_hash="4" * 64,
        edges=(),
    )
    captured: dict[str, object] = {}

    def fake_prepare(vault_root: Path, **kwargs: object) -> None:
        captured["vault_root"] = vault_root
        captured.update(kwargs)

    monkeypatch.setattr(
        catalog_publication,
        "_prepare_markdown_batch",
        fake_prepare,
    )

    result = catalog_publication.prepare_markdown_batch(
        tmp_path,
        mutations=(
            catalog_publication.MarkdownCatalogMutation(
                path="Knowledge Base/source.md",
                source="---\ntitle: Source\n---\n",
                expected_before_hash=None,
            ),
        ),
        graph_replacements=(replacement,),
    )

    assert result is None
    assert captured["graph_replacements"] == (replacement,)


def test_graph_successor_refuses_an_incomplete_active_family(tmp_path: Path) -> None:
    source = _variant("source", "5" * 64)
    target = _variant("target", "6" * 64)
    items = (
        _item("source", "5" * 64, source),
        _item("target", "6" * 64, target),
    )
    active_key = _key(3)
    active_namespace = verified_namespace(active_key, items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(_measurement(active_family, source, _edge("source", "target")),),
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError,
        match="active graph measurement family is incomplete",
    ):
        catalog_publication._prepare_target_measurements(
            tmp_path,
            active_namespace=active_namespace,
            active_roots=(projection_measurement_store.measurement_root(active_manifest),),
            target_namespace=_prepared_namespace(_key(4), items),
        )


def test_graph_successor_requires_a_replacement_for_changed_l6_content(
    tmp_path: Path,
) -> None:
    source = _variant("source", "7" * 64)
    target = _variant("target", "8" * 64)
    active_items = (
        _item("source", "7" * 64, source),
        _item("target", "8" * 64, target),
    )
    active_key = _key(5)
    active_namespace = verified_namespace(active_key, active_items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(
            _measurement(active_family, source, _edge("source", "target")),
            _measurement(active_family, target),
        ),
    )
    changed = _variant("source", "9" * 64)

    with pytest.raises(
        catalog_publication.CatalogPublicationError,
        match="target graph measurement family is incomplete",
    ):
        catalog_publication._prepare_target_measurements(
            tmp_path,
            active_namespace=active_namespace,
            active_roots=(projection_measurement_store.measurement_root(active_manifest),),
            target_namespace=_prepared_namespace(
                _key(6),
                (
                    _item("source", "9" * 64, changed),
                    active_items[1],
                ),
            ),
        )


def test_graph_successor_refuses_a_carried_edge_to_a_removed_target(
    tmp_path: Path,
) -> None:
    source = _variant("source", "a" * 64)
    target = _variant("target", "b" * 64)
    active_items = (
        _item("source", "a" * 64, source),
        _item("target", "b" * 64, target),
    )
    active_key = _key(7)
    active_namespace = verified_namespace(active_key, active_items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(
            _measurement(active_family, source, _edge("source", "target")),
            _measurement(active_family, target),
        ),
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError,
        match="target graph measurement family cannot be prepared",
    ):
        catalog_publication._prepare_target_measurements(
            tmp_path,
            active_namespace=active_namespace,
            active_roots=(projection_measurement_store.measurement_root(active_manifest),),
            target_namespace=_prepared_namespace(_key(8), (active_items[0],)),
        )


def test_graph_successor_refuses_a_replacement_not_bound_to_target_content(
    tmp_path: Path,
) -> None:
    source = _variant("source", "c" * 64)
    active_item = _item("source", "c" * 64, source)
    active_key = _key(9)
    active_namespace = verified_namespace(active_key, (active_item,))
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(_measurement(active_family, source),),
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError,
        match="replacement does not match target content",
    ):
        catalog_publication._prepare_target_measurements(
            tmp_path,
            active_namespace=active_namespace,
            active_roots=(projection_measurement_store.measurement_root(active_manifest),),
            target_namespace=_prepared_namespace(_key(10), (active_item,)),
            graph_replacements=(
                catalog_publication.GraphMeasurementReplacement(
                    item_identity="source",
                    content_hash="d" * 64,
                    edges=(),
                ),
            ),
        )
