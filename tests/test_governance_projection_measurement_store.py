"""Immutable persistence for namespace-bound projected measurement families."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import (
    projected_graph,
    projected_retrieval,
    projection_measurement_store,
    projection_store,
    projections,
)
from exomem.governance.decisions import Decision


def _key() -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=7,
    )


def _variant(identity: str, level: int) -> projections.ProjectionVariant:
    content_hash = {"lower": "b" * 64, "full": "c" * 64}[identity]
    options = {"abstract": "safe projected abstract"} if level == 3 else {}
    variant = projections.build_projection_variant(
        item_identity=identity,
        content_hash=content_hash,
        decision=Decision(level=level, options=options),
        projector_schema_version=1,
        full_search_fields={"body": f"private body for {identity}"},
    )
    assert variant is not None
    return variant


def _namespace() -> tuple[
    projection_store.VerifiedProjectionNamespace,
    projections.ProjectionVariant,
    projections.ProjectionVariant,
]:
    lower = _variant("lower", 3)
    full = _variant("full", 6)
    items = (
        projection_store.ProjectionItemVariants(
            item_identity="lower",
            content_hash="b" * 64,
            variants=(lower,),
        ),
        projection_store.ProjectionItemVariants(
            item_identity="full",
            content_hash="c" * 64,
            variants=(full,),
        ),
    )
    return verified_namespace(_key(), items), lower, full


def _family(
    lane: str,
    *,
    model_version: str = "model-v1",
) -> projection_measurement_store.MeasurementFamilyKey:
    return projection_measurement_store.MeasurementFamilyKey(
        namespace_key=_key(),
        lane=lane,
        extractor_version="extractor-v1",
        model_version=model_version,
    )


def _vector(
    variant: projections.ProjectionVariant,
    values: tuple[float, ...],
) -> projected_retrieval.ProjectionVectorMeasurement:
    return projected_retrieval.ProjectionVectorMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="vector",
            extractor_version="extractor-v1",
            model_version="model-v1",
        ),
        vector=values,
    )


def _clip(
    variant: projections.ProjectionVariant,
) -> projected_retrieval.ProjectionClipMeasurement:
    return projected_retrieval.ProjectionClipMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="clip",
            extractor_version="extractor-v1",
            model_version="model-v1",
        ),
        vector=(0.25, 0.75),
    )


def _graph(
    variant: projections.ProjectionVariant,
    *edges: projected_graph.ProjectionGraphEdge,
) -> projected_graph.ProjectionGraphMeasurement:
    return projected_graph.ProjectionGraphMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="graph",
            extractor_version="extractor-v1",
            model_version="model-v1",
        ),
        edges=edges,
    )


def test_vector_family_stages_replays_and_loads_exact_rows(tmp_path: Path) -> None:
    namespace, lower, full = _namespace()
    family = _family("vector")
    rows = (_vector(lower, (1.0, 0.0)), _vector(full, (0.0, 1.0)))

    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=rows,
    )
    replay = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=rows,
    )
    loaded_manifest, loaded = projection_measurement_store.load_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        expected_rows_digest=manifest.rows_digest,
    )

    assert manifest == replay == loaded_manifest
    assert loaded == rows
    assert manifest.vector_dimension == 2
    assert manifest.graph_edge_count == 0
    assert projection_measurement_store.measurement_store_path(
        tmp_path,
        family,
    ).relative_to(tmp_path) == Path(
        "Knowledge Base/.authorization-projections",
        family.namespace_key.namespace_id,
        "measurements/vector",
        family.family_id,
        "rows.sqlite",
    )


def test_model_version_is_a_measurement_subkey_not_a_namespace_alias() -> None:
    first = _family("vector", model_version="model-v1")
    second = _family("vector", model_version="model-v2")

    assert first.namespace_key == second.namespace_key
    assert first.family_id == (
        "3827b459d2f12d5ac8619a081f151ddd796d22e7afea3205228b27e1c3e6bfd0"
    )
    assert first.family_id != second.family_id
    assert "model-v1" not in first.family_id
    assert "model-v2" not in second.family_id


@pytest.mark.parametrize("lane", ("vector", "clip", "graph"))
def test_empty_measurement_family_is_a_stable_ready_store(
    tmp_path: Path,
    lane: str,
) -> None:
    namespace, _lower, _full = _namespace()
    family = _family(lane)

    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=(),
    )
    replay = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=(),
    )
    loaded_manifest, loaded = projection_measurement_store.load_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        expected_rows_digest=manifest.rows_digest,
    )

    assert manifest == replay == loaded_manifest
    assert manifest.measurement_count == 0
    assert manifest.vector_dimension is None
    assert loaded == ()


def test_clip_family_accepts_only_l6_projection_rows(tmp_path: Path) -> None:
    namespace, lower, full = _namespace()
    family = _family("clip")

    with pytest.raises(projections.ProjectionCanonicalizationError, match="L6"):
        projection_measurement_store.stage_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            measurements=(_clip(lower),),
        )

    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=(_clip(full),),
    )
    _loaded_manifest, loaded = projection_measurement_store.load_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        expected_rows_digest=manifest.rows_digest,
    )
    assert loaded == (_clip(full),)


def test_graph_family_round_trips_canonical_edges(tmp_path: Path) -> None:
    namespace, lower, full = _namespace()
    family = _family("graph")
    edge = projected_graph.ProjectionGraphEdge("full", "lower", "supports")
    rows = (_graph(lower), _graph(full, edge))

    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=rows,
    )
    loaded_manifest, loaded = projection_measurement_store.load_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        expected_rows_digest=manifest.rows_digest,
    )

    assert loaded_manifest.graph_edge_count == 1
    assert loaded_manifest.vector_dimension is None
    assert loaded == rows


def test_typed_loaders_rebuild_only_the_bound_measurement_lane(tmp_path: Path) -> None:
    namespace, lower, full = _namespace()
    cases = (
        (
            _family("vector"),
            (_vector(lower, (1.0, -0.0)), _vector(full, (0.0, 1.0))),
            projection_measurement_store.load_vector_index,
            projected_retrieval.ProjectedVectorIndex,
        ),
        (
            _family("clip"),
            (_clip(full),),
            projection_measurement_store.load_clip_index,
            projected_retrieval.ProjectedClipIndex,
        ),
        (
            _family("graph"),
            (
                _graph(lower),
                _graph(
                    full,
                    projected_graph.ProjectionGraphEdge(
                        "full",
                        "lower",
                        "supports",
                    ),
                ),
            ),
            projection_measurement_store.load_graph_index,
            projected_graph.ProjectedGraphIndex,
        ),
    )

    for family, rows, loader, expected_type in cases:
        manifest = projection_measurement_store.stage_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            measurements=rows,
        )
        loaded_manifest, index = loader(
            tmp_path,
            namespace=namespace,
            family=family,
            expected_rows_digest=manifest.rows_digest,
        )
        assert loaded_manifest == manifest
        assert isinstance(index, expected_type)


def test_vector_family_has_fixed_binary_root_and_normalizes_negative_zero(
    tmp_path: Path,
) -> None:
    namespace, lower, full = _namespace()
    family = _family("vector")
    negative_zero = (_vector(lower, (1.0, -0.0)), _vector(full, (0.0, 1.0)))
    positive_zero = (_vector(lower, (1.0, 0.0)), _vector(full, (0.0, 1.0)))

    first = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=negative_zero,
    )
    replay = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=positive_zero,
    )

    assert first == replay
    assert first.rows_digest == (
        "83301337e33478636aab6ce859dc4544968b48faa706a29c507fad8acbf498af"
    )


@pytest.mark.parametrize("crash_at", ("before-commit", "after-commit"))
def test_staging_crash_recovers_without_a_partial_ready_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_at: str,
) -> None:
    namespace, lower, full = _namespace()
    family = _family("vector")
    rows = (_vector(lower, (1.0, 0.0)), _vector(full, (0.0, 1.0)))

    def crash(point: str) -> None:
        if point == crash_at:
            raise RuntimeError(f"injected {point}")

    monkeypatch.setattr(projection_measurement_store, "_crash_point", crash)
    with pytest.raises(RuntimeError, match=f"injected {crash_at}"):
        projection_measurement_store.stage_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            measurements=rows,
        )

    monkeypatch.setattr(
        projection_measurement_store,
        "_crash_point",
        lambda _point: None,
    )
    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=rows,
    )
    assert projection_measurement_store.verify_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        expected_rows_digest=manifest.rows_digest,
    ) == manifest


def test_loader_refuses_an_unbound_expected_root(tmp_path: Path) -> None:
    namespace, lower, _full = _namespace()
    family = _family("vector")
    projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=(_vector(lower, (1.0, 0.0)),),
    )

    with pytest.raises(
        projection_measurement_store.MeasurementStoreMismatch,
        match="expected root",
    ):
        projection_measurement_store.load_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            expected_rows_digest="f" * 64,
        )


def test_same_family_refuses_different_or_tampered_rows(tmp_path: Path) -> None:
    namespace, lower, full = _namespace()
    family = _family("vector")
    rows = (_vector(lower, (1.0, 0.0)), _vector(full, (0.0, 1.0)))
    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=rows,
    )

    with pytest.raises(projection_measurement_store.MeasurementStoreMismatch):
        projection_measurement_store.stage_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            measurements=(_vector(lower, (0.5, 0.5)), rows[1]),
        )

    database = projection_measurement_store.measurement_store_path(tmp_path, family)
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER measurement_rows_no_update")
    connection.execute(
        "UPDATE measurement_rows SET payload=? WHERE projection_variant_id=?",
        (b"tampered", lower.projection_variant_id),
    )
    connection.commit()
    connection.close()

    with pytest.raises(projection_measurement_store.MeasurementStoreMismatch):
        projection_measurement_store.load_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            expected_rows_digest=manifest.rows_digest,
        )


def test_malformed_sqlite_metadata_refuses_as_a_content_free_store_mismatch(
    tmp_path: Path,
) -> None:
    namespace, lower, _full = _namespace()
    family = _family("vector")
    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=namespace,
        family=family,
        measurements=(_vector(lower, (1.0, 0.0)),),
    )
    connection = sqlite3.connect(
        projection_measurement_store.measurement_store_path(tmp_path, family)
    )
    connection.execute("DROP TRIGGER measurement_meta_no_update")
    connection.execute(
        "UPDATE measurement_meta SET measurement_count='not-an-integer'"
    )
    connection.execute(
        "CREATE TRIGGER measurement_meta_no_update BEFORE UPDATE "
        "ON measurement_meta BEGIN SELECT RAISE(ABORT, "
        "'measurement_meta rows are immutable'); END"
    )
    connection.commit()
    connection.close()

    with pytest.raises(projection_measurement_store.MeasurementStoreMismatch):
        projection_measurement_store.load_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            expected_rows_digest=manifest.rows_digest,
        )


def test_family_refuses_a_different_namespace_or_measurement_version(
    tmp_path: Path,
) -> None:
    namespace, lower, _full = _namespace()
    family = _family("vector")
    other_key = projections.ProjectionNamespaceKey(
        policy_fingerprint="d" * 64,
        projector_schema_version=1,
        catalog_generation=7,
    )

    with pytest.raises(projection_measurement_store.MeasurementStoreMismatch):
        projection_measurement_store.stage_measurement_store(
            tmp_path,
            namespace=namespace,
            family=projection_measurement_store.MeasurementFamilyKey(
                namespace_key=other_key,
                lane="vector",
                extractor_version="extractor-v1",
                model_version="model-v1",
            ),
            measurements=(_vector(lower, (1.0, 0.0)),),
        )

    mismatched = projected_retrieval.ProjectionVectorMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=lower.projection_variant_id,
            lane="vector",
            extractor_version="extractor-v1",
            model_version="model-v2",
        ),
        vector=(1.0, 0.0),
    )
    with pytest.raises(projections.ProjectionCanonicalizationError, match="model"):
        projection_measurement_store.stage_measurement_store(
            tmp_path,
            namespace=namespace,
            family=family,
            measurements=(mismatched,),
        )
