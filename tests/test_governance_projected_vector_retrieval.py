"""Projected-corpus vector acquisition over fixed variant measurements."""

from __future__ import annotations

import pytest

from exomem.governance import projected_retrieval, projection_store, projections
from exomem.governance.decisions import Decision


def _key() -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="b" * 64,
        projector_schema_version=1,
        catalog_generation=12,
    )


def _variant(
    item_identity: str,
    content_hash: str,
    *,
    level: int,
    text: str,
) -> projections.ProjectionVariant:
    option_key = {1: "notice", 2: "constraint", 3: "abstract"}.get(level)
    variant = projections.build_projection_variant(
        item_identity=item_identity,
        content_hash=content_hash,
        decision=Decision(
            level=level,
            options={} if option_key is None else {option_key: text},
        ),
        projector_schema_version=1,
        full_search_fields={"body": text, "title": item_identity},
    )
    assert variant is not None
    return variant


def _item(
    item_identity: str,
    content_hash: str,
    *variants: projections.ProjectionVariant,
) -> projection_store.ProjectionItemVariants:
    return projection_store.ProjectionItemVariants(
        item_identity=item_identity,
        content_hash=content_hash,
        variants=variants,
    )


def _selection(
    item: projection_store.ProjectionItemVariants,
    variant: projections.ProjectionVariant | None,
) -> projected_retrieval.ProjectionSelection:
    return projected_retrieval.ProjectionSelection(
        item_identity=item.item_identity,
        content_hash=item.content_hash,
        projection_variant_id=(
            None if variant is None else variant.projection_variant_id
        ),
    )


def _map(
    *pairs: tuple[
        projection_store.ProjectionItemVariants,
        projections.ProjectionVariant | None,
    ],
) -> projected_retrieval.AuthorizationProjectionMap:
    return projected_retrieval.AuthorizationProjectionMap(
        _key(), tuple(_selection(item, variant) for item, variant in pairs)
    )


def _measurement(
    variant: projections.ProjectionVariant,
    vector: tuple[float, ...],
    *,
    lane: str = "vector",
    model_version: str = "test-model-v1",
) -> projected_retrieval.ProjectionVectorMeasurement:
    return projected_retrieval.ProjectionVectorMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane=lane,
            extractor_version="projected-text-v1",
            model_version=model_version,
        ),
        vector=vector,
    )


def test_hidden_present_and_absent_have_identical_vector_envelopes() -> None:
    first = _variant("first", "1" * 64, level=6, text="first")
    second = _variant("second", "2" * 64, level=6, text="second")
    hidden = _variant("hidden", "3" * 64, level=6, text="hidden")
    first_item = _item("first", "1" * 64, first)
    second_item = _item("second", "2" * 64, second)
    hidden_item = _item("hidden", "3" * 64, hidden)
    present = projected_retrieval.ProjectedVectorIndex(
        _key(),
        (first_item, second_item, hidden_item),
        (
            _measurement(first, (1.0, 0.0)),
            _measurement(second, (0.8, 0.2)),
            _measurement(hidden, (1.0, 0.0)),
        ),
        extractor_version="projected-text-v1",
        model_version="test-model-v1",
    )
    absent = projected_retrieval.ProjectedVectorIndex(
        _key(),
        (first_item, second_item),
        (
            _measurement(first, (1.0, 0.0)),
            _measurement(second, (0.8, 0.2)),
        ),
        extractor_version="projected-text-v1",
        model_version="test-model-v1",
    )

    assert present.search_vector(
        _map((first_item, first), (second_item, second), (hidden_item, None)),
        (1.0, 0.0),
        k=2,
    ) == absent.search_vector(
        _map((first_item, first), (second_item, second)),
        (1.0, 0.0),
        k=2,
    )


def test_selected_projection_vector_not_raw_body_controls_relevance() -> None:
    low = _variant("shared", "4" * 64, level=2, text="approved abstraction")
    full = _variant("shared", "4" * 64, level=6, text="raw body")
    item = _item("shared", "4" * 64, low, full)
    index = projected_retrieval.ProjectedVectorIndex(
        _key(),
        (item,),
        (_measurement(low, (1.0, 0.0)), _measurement(full, (0.0, 1.0))),
        extractor_version="projected-text-v1",
        model_version="test-model-v1",
    )

    low_hit = index.search_vector(_map((item, low)), (1.0, 0.0), k=1)[0]
    full_hit = index.search_vector(_map((item, full)), (1.0, 0.0), k=1)[0]

    assert low_hit.projection_variant_id == low.projection_variant_id
    assert low_hit.score == pytest.approx(1.0)
    assert full_hit.projection_variant_id == full.projection_variant_id
    assert full_hit.score == pytest.approx(0.0)


def test_hidden_high_scores_do_not_consume_the_vector_cap() -> None:
    visible = _variant("visible", "5" * 64, level=3, text="visible")
    visible_item = _item("visible", "5" * 64, visible)
    hidden_pairs = tuple(
        (
            _item(
                f"hidden-{index:03d}",
                f"{index + 20:064x}",
                hidden := _variant(
                    f"hidden-{index:03d}",
                    f"{index + 20:064x}",
                    level=6,
                    text="hidden",
                ),
            ),
            hidden,
        )
        for index in range(80)
    )
    index = projected_retrieval.ProjectedVectorIndex(
        _key(),
        (*(item for item, _variant_row in hidden_pairs), visible_item),
        (
            *(_measurement(variant, (1.0, 0.0)) for _item_row, variant in hidden_pairs),
            _measurement(visible, (0.1, 0.9)),
        ),
        extractor_version="projected-text-v1",
        model_version="test-model-v1",
    )
    authorization = _map(
        *((item, None) for item, _variant_row in hidden_pairs),
        (visible_item, visible),
    )

    assert [
        hit.item_identity
        for hit in index.search_vector(authorization, (1.0, 0.0), k=1)
    ] == ["visible"]


def test_missing_selected_vector_disables_the_visible_lane_without_fallback() -> None:
    projected = _variant("projected", "6" * 64, level=2, text="projected")
    raw = _variant("projected", "6" * 64, level=6, text="raw")
    item = _item("projected", "6" * 64, projected, raw)
    index = projected_retrieval.ProjectedVectorIndex(
        _key(),
        (item,),
        (_measurement(raw, (1.0, 0.0)),),
        extractor_version="projected-text-v1",
        model_version="test-model-v1",
    )

    with pytest.raises(projected_retrieval.ProjectedLaneUnavailable, match="selected"):
        index.search_vector(_map((item, projected)), (1.0, 0.0), k=1)


def test_measurement_versions_are_subkeys_not_namespace_aliases() -> None:
    variant = _variant("item", "7" * 64, level=6, text="item")
    item = _item("item", "7" * 64, variant)
    with pytest.raises(projections.ProjectionCanonicalizationError, match="model"):
        projected_retrieval.ProjectedVectorIndex(
            _key(),
            (item,),
            (_measurement(variant, (1.0, 0.0), model_version="other-model"),),
            extractor_version="projected-text-v1",
            model_version="test-model-v1",
        )

    with pytest.raises(projections.ProjectionCanonicalizationError, match="lane"):
        _measurement(variant, (1.0, 0.0), lane="graph")


def test_vector_dimension_and_finiteness_are_closed() -> None:
    variant = _variant("item", "8" * 64, level=6, text="item")
    item = _item("item", "8" * 64, variant)
    index = projected_retrieval.ProjectedVectorIndex(
        _key(),
        (item,),
        (_measurement(variant, (1.0, 0.0)),),
        extractor_version="projected-text-v1",
        model_version="test-model-v1",
    )

    with pytest.raises(projected_retrieval.ProjectedLaneUnavailable, match="dimension"):
        index.search_vector(_map((item, variant)), (1.0,), k=1)
    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="finite",
    ):
        _measurement(variant, (float("nan"), 0.0))
