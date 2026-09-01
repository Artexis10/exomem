"""L6-only pre-cap CLIP acquisition over fixed projection measurements."""

from __future__ import annotations

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import projected_retrieval, projection_store, projections
from exomem.governance.decisions import Decision


def _key() -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="d" * 64,
        projector_schema_version=1,
        catalog_generation=16,
    )


def _variant(
    item_identity: str,
    content_hash: str,
    *,
    level: int,
    text: str,
    media_type: str | None = "image",
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
        full_search_fields={
            "body": text,
            "title": item_identity,
            **({} if media_type is None else {"media_type": media_type}),
        },
    )
    assert variant is not None
    return variant


def _item(
    variant: projections.ProjectionVariant,
    *more: projections.ProjectionVariant,
) -> projection_store.ProjectionItemVariants:
    return projection_store.ProjectionItemVariants(
        item_identity=variant.item_identity,
        content_hash=variant.content_hash,
        variants=(variant, *more),
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


def _measurement(
    variant: projections.ProjectionVariant,
    vector: tuple[float, ...],
    *,
    lane: str = "clip",
) -> projected_retrieval.ProjectionClipMeasurement:
    return projected_retrieval.ProjectionClipMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane=lane,
            extractor_version="pixels-v1",
            model_version="clip-test-v1",
        ),
        vector=vector,
    )


def _video_measurement(
    variant: projections.ProjectionVariant,
    *samples: tuple[int, tuple[float, ...]],
) -> projected_retrieval.ProjectionClipMeasurement:
    return projected_retrieval.ProjectionClipMeasurement(
        measurement_key=projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="clip",
            extractor_version="pixels-v1",
            model_version="clip-test-v1",
        ),
        samples=tuple(
            projected_retrieval.ProjectionClipSample(
                frame_timestamp_ms=timestamp_ms,
                vector=vector,
            )
            for timestamp_ms, vector in samples
        ),
    )


def test_l0_pixel_match_is_equivalent_to_absence_before_clip_cap() -> None:
    visible = _variant("visible", "1" * 64, level=6, text="visible")
    hidden = _variant("hidden", "2" * 64, level=6, text="hidden")
    visible_item = _item(visible)
    hidden_item = _item(hidden)
    present = projected_retrieval.ProjectedClipIndex(
        _namespace(visible_item, hidden_item),
        (_measurement(visible, (0.1, 0.9)), _measurement(hidden, (1.0, 0.0))),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )
    absent = projected_retrieval.ProjectedClipIndex(
        _namespace(visible_item),
        (_measurement(visible, (0.1, 0.9)),),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )

    assert present.search_clip(
        _map((visible_item, visible), (hidden_item, None)),
        (1.0, 0.0),
        k=1,
    ) == absent.search_clip(
        _map((visible_item, visible)),
        (1.0, 0.0),
        k=1,
    )


def test_below_l6_is_excluded_from_binary_lane_even_if_measurement_exists() -> None:
    lower = _variant("media", "3" * 64, level=2, text="text companion")
    full = _variant("media", "3" * 64, level=6, text="full media")
    item = _item(lower, full)
    index = projected_retrieval.ProjectedClipIndex(
        _namespace(item),
        (_measurement(full, (1.0, 0.0)),),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )

    assert index.search_clip(_map((item, lower)), (1.0, 0.0), k=1) == ()
    assert index.search_clip(_map((item, full)), (1.0, 0.0), k=1)[
        0
    ].projection_variant_id == full.projection_variant_id


def test_missing_selected_l6_clip_measurement_disables_lane() -> None:
    full = _variant("media", "4" * 64, level=6, text="full media")
    item = _item(full)
    index = projected_retrieval.ProjectedClipIndex(
        _namespace(item),
        (),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )

    with pytest.raises(projected_retrieval.ProjectedLaneUnavailable, match="selected"):
        index.search_clip(_map((item, full)), (1.0, 0.0), k=1)


def test_l6_text_note_does_not_require_a_pixel_measurement() -> None:
    image = _variant("image", "5" * 64, level=6, text="image")
    note = _variant(
        "note",
        "6" * 64,
        level=6,
        text="ordinary note",
        media_type=None,
    )
    image_item, note_item = _item(image), _item(note)
    index = projected_retrieval.ProjectedClipIndex(
        _namespace(image_item, note_item),
        (_measurement(image, (1.0, 0.0)),),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )

    hits = index.search_clip(
        _map((image_item, image), (note_item, note)),
        (1.0, 0.0),
        k=2,
    )

    assert [hit.item_identity for hit in hits] == [image.item_identity]


def test_clip_measurement_lane_and_versions_are_closed_subkeys() -> None:
    full = _variant("media", "5" * 64, level=6, text="full media")
    item = _item(full)
    with pytest.raises(projections.ProjectionCanonicalizationError, match="lane"):
        _measurement(full, (1.0, 0.0), lane="vector")
    with pytest.raises(projections.ProjectionCanonicalizationError, match="model"):
        projected_retrieval.ProjectedClipIndex(
            _namespace(item),
            (_measurement(full, (1.0, 0.0)),),
            extractor_version="pixels-v1",
            model_version="different-model",
        )


def test_video_scores_every_keyframe_but_returns_the_parent_once() -> None:
    video = _variant(
        "video",
        "7" * 64,
        level=6,
        text="full video",
        media_type="video",
    )
    item = _item(video)
    index = projected_retrieval.ProjectedClipIndex(
        _namespace(item),
        (
            _video_measurement(
                video,
                (1_000, (0.0, 1.0)),
                (8_500, (1.0, 0.0)),
                (19_000, (2.0, 0.0)),
            ),
        ),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )

    hits = index.search_clip(_map((item, video)), (1.0, 0.0), k=5)

    assert len(hits) == 1
    assert hits[0].item_identity == "video"
    assert hits[0].clip_frame_timestamp_ms == 8_500
    assert hits[0].score == pytest.approx(1.0)


def test_hidden_video_keyframes_are_absent_before_parent_top_k() -> None:
    visible = _variant(
        "visible-video",
        "c" * 64,
        level=6,
        text="visible video",
        media_type="video",
    )
    hidden = _variant(
        "hidden-video",
        "d" * 64,
        level=6,
        text="hidden video",
        media_type="video",
    )
    visible_item, hidden_item = _item(visible), _item(hidden)
    visible_measurement = _video_measurement(
        visible,
        (1_000, (0.2, 0.8)),
        (8_500, (0.6, 0.4)),
    )
    present = projected_retrieval.ProjectedClipIndex(
        _namespace(visible_item, hidden_item),
        (
            visible_measurement,
            _video_measurement(hidden, (2_000, (1.0, 0.0))),
        ),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )
    absent = projected_retrieval.ProjectedClipIndex(
        _namespace(visible_item),
        (visible_measurement,),
        extractor_version="pixels-v1",
        model_version="clip-test-v1",
    )

    present_hits = present.search_clip(
        _map((visible_item, visible), (hidden_item, None)),
        (1.0, 0.0),
        k=1,
    )
    absent_hits = absent.search_clip(
        _map((visible_item, visible)),
        (1.0, 0.0),
        k=1,
    )

    assert present_hits == absent_hits
    assert present_hits[0].clip_frame_timestamp_ms == 8_500


def test_video_keyframes_require_a_strict_canonical_timestamp_order() -> None:
    video = _variant(
        "video",
        "8" * 64,
        level=6,
        text="full video",
        media_type="video",
    )

    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="timestamp order",
    ):
        _video_measurement(
            video,
            (8_500, (1.0, 0.0)),
            (1_000, (0.0, 1.0)),
        )


def test_projected_clip_keyframe_count_has_a_fixed_non_overridable_cap() -> None:
    video = _variant(
        "video",
        "9" * 64,
        level=6,
        text="full video",
        media_type="video",
    )

    with pytest.raises(
        projections.ProjectionCapacityExceeded,
        match="CLIP samples",
    ):
        _video_measurement(
            video,
            *((index, (1.0, 0.0)) for index in range(41)),
        )


def test_media_kind_and_clip_sample_shape_must_agree() -> None:
    image = _variant("image", "a" * 64, level=6, text="image")
    video = _variant(
        "video",
        "b" * 64,
        level=6,
        text="video",
        media_type="video",
    )

    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="image.*untimestamped",
    ):
        projected_retrieval.ProjectedClipIndex(
            _namespace(_item(image)),
            (_video_measurement(image, (1_000, (1.0, 0.0))),),
            extractor_version="pixels-v1",
            model_version="clip-test-v1",
        )
    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="video.*timestamps",
    ):
        projected_retrieval.ProjectedClipIndex(
            _namespace(_item(video)),
            (_measurement(video, (1.0, 0.0)),),
            extractor_version="pixels-v1",
            model_version="clip-test-v1",
        )
