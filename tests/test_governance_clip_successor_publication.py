"""Exact catalog successors for projected image/video CLIP measurements."""

from __future__ import annotations

from pathlib import Path

import pytest
from governance_projection_support import verified_namespace

from exomem import embeddings, find_corpus
from exomem.governance import (
    catalog_publication,
    projected_retrieval,
    projection_measurement_store,
    projection_store,
    projections,
)
from exomem.governance.decisions import Decision


def _key(generation: int) -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=generation,
    )


def _variant(
    identity: str,
    content_hash: str,
    *,
    media_type: str,
    parent_media: str | None = None,
) -> projections.ProjectionVariant:
    variant = projections.build_projection_variant(
        item_identity=identity,
        content_hash=content_hash,
        decision=Decision(level=6, options={}),
        projector_schema_version=1,
        full_search_fields={
            "body": identity,
            "media_type": media_type,
            **({} if parent_media is None else {"parent_media": parent_media}),
        },
    )
    assert variant is not None
    return variant


def _item(
    variant: projections.ProjectionVariant,
) -> projection_store.ProjectionItemVariants:
    return projection_store.ProjectionItemVariants(
        item_identity=variant.item_identity,
        content_hash=variant.content_hash,
        variants=(variant,),
    )


def _family(
    key: projections.ProjectionNamespaceKey,
) -> projection_measurement_store.MeasurementFamilyKey:
    return projection_measurement_store.MeasurementFamilyKey(
        namespace_key=key,
        lane="clip",
        extractor_version="pixels-v1",
        model_version="clip-ViT-B-32",
    )


def _vector_family(
    key: projections.ProjectionNamespaceKey,
) -> projection_measurement_store.MeasurementFamilyKey:
    return projection_measurement_store.MeasurementFamilyKey(
        namespace_key=key,
        lane="vector",
        extractor_version="projected-text-v1",
        model_version=embeddings.MODEL_NAME,
    )


def _vector_measurements(
    family: projection_measurement_store.MeasurementFamilyKey,
    *items: projection_store.ProjectionItemVariants,
) -> tuple[projected_retrieval.ProjectionVectorMeasurement, ...]:
    return tuple(
        projected_retrieval.ProjectionVectorMeasurement(
            projections.MeasurementKey(
                projection_variant_id=variant.projection_variant_id,
                lane=family.lane,
                extractor_version=family.extractor_version,
                model_version=family.model_version,
            ),
            (float(index + 1), 1.0),
        )
        for index, variant in enumerate(
            variant for item in items for variant in item.variants
        )
    )


def _image_measurement(
    family: projection_measurement_store.MeasurementFamilyKey,
    variant: projections.ProjectionVariant,
) -> projected_retrieval.ProjectionClipMeasurement:
    return projected_retrieval.ProjectionClipMeasurement(
        projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane=family.lane,
            extractor_version=family.extractor_version,
            model_version=family.model_version,
        ),
        (0.0, 1.0),
    )


def _video_measurement(
    family: projection_measurement_store.MeasurementFamilyKey,
    variant: projections.ProjectionVariant,
    *samples: tuple[int, tuple[float, ...]],
) -> projected_retrieval.ProjectionClipMeasurement:
    return projected_retrieval.ProjectionClipMeasurement(
        projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane=family.lane,
            extractor_version=family.extractor_version,
            model_version=family.model_version,
        ),
        samples=tuple(
            projected_retrieval.ProjectionClipSample(timestamp_ms, vector)
            for timestamp_ms, vector in samples
        ),
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


def test_clip_successor_carries_images_and_replaces_one_video_row(
    tmp_path: Path,
) -> None:
    active_key = _key(1)
    video = _variant("Knowledge Base/video.mp4.md", "1" * 64, media_type="video")
    image = _variant("Knowledge Base/image.jpg.md", "2" * 64, media_type="image")
    active_items = (_item(video), _item(image))
    active_namespace = verified_namespace(active_key, active_items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(
            _video_measurement(active_family, video, (1_000, (1.0, 0.0))),
            _image_measurement(active_family, image),
        ),
    )
    frame = _variant(
        "Knowledge Base/video.mp4.frames/scene-000-t8500ms.jpg.md",
        "3" * 64,
        media_type="image",
        parent_media="Knowledge Base/video.mp4",
    )
    target_namespace = _prepared_namespace(
        _key(2),
        (*active_items, _item(frame)),
    )

    prepared = catalog_publication._prepare_target_measurements(
        tmp_path,
        active_namespace=active_namespace,
        active_roots=(
            projection_measurement_store.measurement_root(active_manifest),
        ),
        target_namespace=target_namespace,
        clip_replacements=(
            catalog_publication.ClipMeasurementReplacement(
                item_identity=video.item_identity,
                content_hash=video.content_hash,
                samples=(
                    projected_retrieval.ProjectionClipSample(1_000, (0.0, 1.0)),
                    projected_retrieval.ProjectionClipSample(8_500, (1.0, 0.0)),
                ),
            ),
        ),
    )

    assert len(prepared) == 1
    target = prepared[0]
    assert target.family.namespace_key == target_namespace.namespace_key
    assert target.manifest.measurement_count == 2
    by_variant = {
        row.measurement_key.projection_variant_id: row
        for row in target.measurements
    }
    assert by_variant[video.projection_variant_id].samples[1].frame_timestamp_ms == 8_500
    assert by_variant[image.projection_variant_id].samples[0].frame_timestamp_ms is None
    assert frame.projection_variant_id not in by_variant


def test_clip_successor_refuses_an_incomplete_active_family(tmp_path: Path) -> None:
    active_key = _key(1)
    video = _variant("Knowledge Base/video.mp4.md", "4" * 64, media_type="video")
    image = _variant("Knowledge Base/image.jpg.md", "5" * 64, media_type="image")
    active_items = (_item(video), _item(image))
    active_namespace = verified_namespace(active_key, active_items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(
            _video_measurement(active_family, video, (1_000, (1.0, 0.0))),
        ),
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError,
        match="CLIP measurement family is incomplete",
    ):
        catalog_publication._prepare_target_measurements(
            tmp_path,
            active_namespace=active_namespace,
            active_roots=(
                projection_measurement_store.measurement_root(active_manifest),
            ),
            target_namespace=_prepared_namespace(_key(2), active_items),
            clip_replacements=(),
        )


def test_clip_successor_refuses_a_replacement_not_bound_to_target_content(
    tmp_path: Path,
) -> None:
    active_key = _key(1)
    image = _variant("Knowledge Base/image.jpg.md", "6" * 64, media_type="image")
    active_items = (_item(image),)
    active_namespace = verified_namespace(active_key, active_items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(_image_measurement(active_family, image),),
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError,
        match="replacement does not match target content",
    ):
        catalog_publication._prepare_target_measurements(
            tmp_path,
            active_namespace=active_namespace,
            active_roots=(
                projection_measurement_store.measurement_root(active_manifest),
            ),
            target_namespace=_prepared_namespace(_key(2), active_items),
            clip_replacements=(
                catalog_publication.ClipMeasurementReplacement(
                    item_identity=image.item_identity,
                    content_hash="f" * 64,
                    samples=(
                        projected_retrieval.ProjectionClipSample(None, (1.0, 0.0)),
                    ),
                ),
            ),
        )


def test_clip_successor_normalizes_invalid_media_samples_to_publication_refusal(
    tmp_path: Path,
) -> None:
    active_key = _key(1)
    image = _variant("Knowledge Base/image.jpg.md", "6" * 64, media_type="image")
    active_items = (_item(image),)
    active_namespace = verified_namespace(active_key, active_items)
    active_family = _family(active_key)
    active_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=active_family,
        measurements=(_image_measurement(active_family, image),),
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError,
        match="target CLIP measurement family cannot be prepared",
    ):
        catalog_publication._prepare_target_measurements(
            tmp_path,
            active_namespace=active_namespace,
            active_roots=(
                projection_measurement_store.measurement_root(active_manifest),
            ),
            target_namespace=_prepared_namespace(_key(2), active_items),
            clip_replacements=(
                catalog_publication.ClipMeasurementReplacement(
                    item_identity=image.item_identity,
                    content_hash=image.content_hash,
                    samples=(
                        projected_retrieval.ProjectionClipSample(
                            1_000,
                            (1.0, 0.0),
                        ),
                    ),
                ),
            ),
        )


def test_vector_and_clip_successors_share_one_target_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_key = _key(1)
    video = _variant("Knowledge Base/video.mp4.md", "7" * 64, media_type="video")
    active_items = (_item(video),)
    active_namespace = verified_namespace(active_key, active_items)
    vector_family = _vector_family(active_key)
    clip_family = _family(active_key)
    vector_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=vector_family,
        measurements=_vector_measurements(vector_family, *active_items),
    )
    clip_manifest = projection_measurement_store.stage_measurement_store(
        tmp_path,
        namespace=active_namespace,
        family=clip_family,
        measurements=(
            _video_measurement(clip_family, video, (1_000, (1.0, 0.0))),
        ),
    )
    frame = _variant(
        "Knowledge Base/video.mp4.frames/scene-000-t8500ms.jpg.md",
        "8" * 64,
        media_type="image",
        parent_media="Knowledge Base/video.mp4",
    )
    target_namespace = _prepared_namespace(
        _key(2),
        (*active_items, _item(frame)),
    )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [[1.0, 1.0] for _text in texts],
    )

    prepared = catalog_publication._prepare_target_measurements(
        tmp_path,
        active_namespace=active_namespace,
        active_roots=(
            projection_measurement_store.measurement_root(vector_manifest),
            projection_measurement_store.measurement_root(clip_manifest),
        ),
        target_namespace=target_namespace,
        clip_replacements=(
            catalog_publication.ClipMeasurementReplacement(
                item_identity=video.item_identity,
                content_hash=video.content_hash,
                samples=(
                    projected_retrieval.ProjectionClipSample(1_000, (0.0, 1.0)),
                    projected_retrieval.ProjectionClipSample(8_500, (1.0, 0.0)),
                ),
            ),
        ),
    )

    assert [target.family.lane for target in prepared] == ["vector", "clip"]
    assert all(
        target.family.namespace_key == target_namespace.namespace_key
        for target in prepared
    )
    vector_target, clip_target = prepared
    assert vector_target.manifest.measurement_count == 2
    assert clip_target.manifest.measurement_count == 1


def test_catalog_projection_fields_distinguish_parent_media_from_frame_children(
    tmp_path: Path,
) -> None:
    video_source = (
        "---\ntitle: Demo\ntype: source\nmedia_type: video\n---\n\nVideo body.\n"
    )
    frame_source = (
        "---\ntitle: Frame\ntype: evidence\nmedia_type: image\n"
        "parent_media: Knowledge Base/demo.mp4\n---\n\nFrame body.\n"
    )

    def fields(path: str, source: str) -> dict[str, str]:
        parsed = find_corpus.parse_page(
            tmp_path / path,
            0.0,
            tmp_path,
            content=source.encode(),
            resolved_relative=path,
        )
        assert parsed is not None
        return catalog_publication._search_fields(parsed)

    video_fields = fields("Knowledge Base/demo.mp4.md", video_source)
    frame_fields = fields(
        "Knowledge Base/demo.mp4.frames/scene-000-t1000ms.jpg.md",
        frame_source,
    )
    video = _variant(
        "Knowledge Base/demo.mp4.md",
        "9" * 64,
        media_type=video_fields["media_type"],
    )
    frame = _variant(
        "Knowledge Base/demo.mp4.frames/scene-000-t1000ms.jpg.md",
        "0" * 64,
        media_type=frame_fields["media_type"],
        parent_media=frame_fields["parent_media"],
    )

    assert projected_retrieval.clip_variant_applicable(video)
    assert not projected_retrieval.clip_variant_applicable(frame)
