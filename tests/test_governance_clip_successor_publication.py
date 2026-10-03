"""Exact catalog successors for projected image/video CLIP measurements."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from governance_projection_support import verified_namespace

from exomem import embeddings, find_corpus
from exomem.governance import (
    catalog_publication,
    projected_graph,
    projected_retrieval,
    projection_measurement_store,
    projection_store,
    projections,
    schema_v4,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy, Scope


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


def test_clip_refresh_rekeys_unchanged_pixel_inputs(
    tmp_path: Path,
) -> None:
    active_key = _key(8)
    image = _variant("Knowledge Base/image.md", "1" * 64, media_type="image")
    active_items = (_item(image),)
    active_namespace = verified_namespace(active_key, active_items)
    family = _family(active_key)
    row = _image_measurement(family, image)
    manifest = projection_measurement_store.stage_measurement_store(
        tmp_path, namespace=active_namespace, family=family, measurements=(row,)
    )
    target_key = projections.ProjectionNamespaceKey(
        active_key.policy_fingerprint,
        projections.PROJECTOR_SCHEMA_VERSION,
        active_key.catalog_generation,
    )
    refreshed = projections.build_projection_variant(
        item_identity=image.item_identity,
        content_hash=image.content_hash,
        decision=Decision(level=6),
        projector_schema_version=target_key.projector_schema_version,
        full_search_fields={"body": "current sanitized text", "media_type": "image"},
    )
    assert refreshed is not None
    target_namespace = _prepared_namespace(target_key, (_item(refreshed),))
    prepared = catalog_publication._prepare_target_measurements(
        tmp_path,
        active_namespace=active_namespace,
        active_roots=(projection_measurement_store.measurement_root(manifest),),
        target_namespace=target_namespace,
    )

    assert len(prepared) == 1
    assert prepared[0].measurements[0].samples == row.samples
    assert (
        prepared[0].measurements[0].measurement_key.projection_variant_id
        == refreshed.projection_variant_id
    )


def _canonical_refresh_snapshot(tmp_path: Path) -> schema_v4.ActivePolicySnapshot:
    active_key = _key(18)
    items = []
    for title, level in (("Image", 6), ("Low", 3), ("Zero", 0)):
        path = f"Knowledge Base/Notes/Insights/{title}.md"
        source = (
            f"---\ntitle: {title}\ntype: insight\n"
            + ("media_type: image\n" if title == "Image" else "")
            + "---\n\n## Observations\n\n"
            + f"- [decision] Canonical {title} prose.\n"
            + (
                "\n[[Low]]\n<!-- exomem-origin:future\n## Relations\n"
                "- supports [[Zero]]\n-->\n"
                if title == "Image"
                else ""
            )
        )
        if title == "Zero":
            source = source.replace("\n", "\r\n")
        canonical = tmp_path / path
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(source.encode("utf-8"))
        content_hash = hashlib.sha256(source.encode()).hexdigest()
        variant = projections.build_projection_variant(
            item_identity=path,
            content_hash=content_hash,
            decision=Decision(
                level=level, options={"abstract": "Old summary"} if level == 3 else {}
            ),
            projector_schema_version=1,
            full_search_fields={
                "body": "obsolete derived excerpt",
                **({"media_type": "image"} if title == "Image" else {}),
            },
        )
        items.append(
            projection_store.ProjectionItemVariants(
                path, content_hash, () if variant is None else (variant,)
            )
        )
    active_items = tuple(items)
    namespace = verified_namespace(active_key, active_items)
    manifest = projection_store.stage_variant_store(tmp_path, key=active_key, items=active_items)
    image = active_items[0].variants[0]
    graph_family = projection_measurement_store.MeasurementFamilyKey(
        active_key, "graph", "projected-graph-v1", "graph-schema-v1"
    )
    families = (_vector_family(active_key), _family(active_key), graph_family)
    measurements = (
        _vector_measurements(families[0], *active_items),
        (_image_measurement(families[1], image),),
        tuple(
            projected_graph.ProjectionGraphMeasurement(
                projections.MeasurementKey(
                    variant.projection_variant_id,
                    "graph",
                    graph_family.extractor_version,
                    graph_family.model_version,
                ),
                edges=(),
            )
            for item in active_items
            for variant in item.variants
        ),
    )
    roots = tuple(
        projection_measurement_store.measurement_root(
            projection_measurement_store.stage_measurement_store(
                tmp_path, namespace=namespace, family=family, measurements=rows
            )
        )
        for family, rows in zip(families, measurements, strict=True)
    )
    return schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            "fixture-vault",
            "fixture-store",
            1,
            "e" * 64,
            "fixture-policy",
            active_key.policy_fingerprint,
            1,
            active_key.catalog_generation,
            active_key.namespace_id,
        ),
        policy=Policy(fingerprint=active_key.policy_fingerprint),
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(active_key, active_items),
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            manifest, required_measurement_roots=roots
        ),
    )


def test_policy_refresh_derives_all_canonical_fields_and_reuses_staged_proofs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _canonical_refresh_snapshot(tmp_path)
    calls = []

    def embed(texts, *, is_query):
        assert not is_query
        calls.extend(texts)
        return [(0.0, 1.0) for _text in texts]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_texts", embed)
    prepared = catalog_publication.prepare_policy_projection(
        tmp_path, active_snapshot=snapshot, target_policy=snapshot.policy, ready_at=19
    )

    assert prepared.catalog is None
    assert prepared.manifest.namespace_key.catalog_generation == 18
    assert (
        prepared.manifest.namespace_key.projector_schema_version
        == projections.PROJECTOR_SCHEMA_VERSION
    )
    assert len(prepared.items) == 3
    assert {len(item.variants) for item in prepared.predecessor_items} == {0, 1}
    assert len(calls) == prepared.manifest.variant_count == 3
    assert all(
        "obsolete derived excerpt" not in text and "exomem-origin" not in text for text in calls
    )
    for item in prepared.items:
        assert all(
            f"Canonical {Path(item.item_identity).stem} prose." in variant.search_fields["body"]
            for variant in item.variants
        )
    evidence = projection_store.decode_projection_namespace_evidence(
        prepared.evidence, expected_key=prepared.manifest.namespace_key
    )
    assert {root.lane for root in evidence.required_measurement_roots} == {
        "vector",
        "clip",
        "graph",
    }
    target_namespace = projection_store.prepare_projection_namespace(
        key=prepared.manifest.namespace_key, manifest=prepared.manifest, items=prepared.items
    )
    graph_root = next(root for root in evidence.required_measurement_roots if root.lane == "graph")
    _manifest, graph_rows = projection_measurement_store.load_measurement_store(
        tmp_path,
        namespace=target_namespace,
        family=projection_measurement_store.MeasurementFamilyKey(
            graph_root.namespace_key,
            graph_root.lane,
            graph_root.extractor_version,
            graph_root.model_version,
        ),
        expected_rows_digest=graph_root.rows_digest,
    )
    assert {edge.target_item_identity for row in graph_rows for edge in row.edges} == {
        "Knowledge Base/Notes/Insights/Low.md"
    }
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda *_args, **_kwargs: pytest.fail("refresh reran the model")
    )
    retry = catalog_publication.prepare_policy_projection(
        tmp_path, active_snapshot=snapshot, target_policy=snapshot.policy, ready_at=19
    )
    committed = catalog_publication.prepare_policy_projection(
        tmp_path,
        active_snapshot=snapshot,
        target_policy=snapshot.policy,
        ready_at=19,
        prepared_evidence=prepared.evidence,
    )
    assert retry == committed == prepared

    path = tmp_path / prepared.items[0].item_identity
    original = path.read_bytes()
    path.write_bytes(original + b"\nChanged canonical prose.\n")
    with pytest.raises(catalog_publication.CatalogPublicationError, match="canonical corpus"):
        catalog_publication.prepare_policy_projection(
            tmp_path,
            active_snapshot=snapshot,
            target_policy=snapshot.policy,
            ready_at=19,
            prepared_evidence=prepared.evidence,
        )
    path.write_bytes(original)

    wire = json.loads(prepared.evidence)
    del wire["required_lane_roots"]["clip"]
    with pytest.raises(catalog_publication.CatalogPublicationError, match="required lane"):
        catalog_publication.prepare_policy_projection(
            tmp_path,
            active_snapshot=snapshot,
            target_policy=snapshot.policy,
            ready_at=19,
            prepared_evidence=projections.canonical_jcs(wire),
        )


def test_policy_refresh_advances_catalog_only_for_changed_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _canonical_refresh_snapshot(tmp_path)
    scope = Scope(id="classified", source="scope.yaml", paths=("Knowledge Base/**",))
    target = replace(snapshot.policy, fingerprint="b" * 64, scopes={scope.id: scope})
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda texts, **_kwargs: [(0.0, 1.0) for _text in texts]
    )

    prepared = catalog_publication.prepare_policy_projection(
        tmp_path, active_snapshot=snapshot, target_policy=target, ready_at=19
    )

    assert prepared.catalog is not None
    assert prepared.catalog.catalog_generation == 19
    assert all(item.scope_ids == (scope.id,) for item in prepared.items)


def test_policy_refresh_commit_refuses_a_missing_staged_vector_without_a_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _canonical_refresh_snapshot(tmp_path)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda texts, **_kwargs: [(0.0, 1.0) for _text in texts]
    )
    prepared = catalog_publication.prepare_policy_projection(
        tmp_path, active_snapshot=snapshot, target_policy=snapshot.policy, ready_at=19
    )
    evidence = projection_store.decode_projection_namespace_evidence(
        prepared.evidence, expected_key=prepared.manifest.namespace_key
    )
    vector = next(root for root in evidence.required_measurement_roots if root.lane == "vector")
    family = projection_measurement_store.MeasurementFamilyKey(
        vector.namespace_key, vector.lane, vector.extractor_version, vector.model_version
    )
    database = projection_measurement_store.measurement_store_path(tmp_path, family)
    database.rename(database.with_suffix(".missing"))
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda *_args, **_kwargs: pytest.fail("commit reran the model")
    )

    with pytest.raises(catalog_publication.CatalogPublicationError):
        catalog_publication.prepare_policy_projection(
            tmp_path,
            active_snapshot=snapshot,
            target_policy=snapshot.policy,
            ready_at=19,
            prepared_evidence=prepared.evidence,
        )


def test_policy_refresh_refuses_a_catalog_item_without_a_canonical_field_owner(
    tmp_path: Path,
) -> None:
    snapshot = _canonical_refresh_snapshot(tmp_path)
    key = projections.ProjectionNamespaceKey(snapshot.policy.fingerprint, 1, 27)
    item = projection_store.ProjectionItemVariants("arbitrary.bin", "f" * 64, ())
    manifest = projection_store.stage_variant_store(tmp_path, key=key, items=(item,))
    snapshot = replace(
        snapshot,
        active=replace(
            snapshot.active, catalog_generation=27, projection_namespace_id=key.namespace_id
        ),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, (item,)),
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            manifest
        ),
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError, match="canonical projection field owner"
    ) as refused:
        catalog_publication.prepare_policy_projection(
            tmp_path, active_snapshot=snapshot, target_policy=snapshot.policy, ready_at=19
        )

    assert "arbitrary.bin" not in str(refused.value)


def test_policy_refresh_refuses_an_unsupported_canonical_graph_producer_family(
    tmp_path: Path,
) -> None:
    snapshot = _canonical_refresh_snapshot(tmp_path)
    evidence = projection_store.namespace_evidence_from_snapshot(snapshot)
    graph = next(root for root in evidence.required_measurement_roots if root.lane == "graph")
    unsupported = replace(
        graph,
        extractor_version="unknown-graph-v9",
        family_id=projection_store.projection_measurement_family_id(
            graph.namespace_key,
            lane="graph",
            extractor_version="unknown-graph-v9",
            model_version=graph.model_version,
        ),
    )
    snapshot = replace(
        snapshot,
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            evidence.manifest,
            required_measurement_roots=tuple(
                unsupported if root.lane == "graph" else root
                for root in evidence.required_measurement_roots
            ),
        ),
    )
    assert (
        unsupported
        in projection_store.namespace_evidence_from_snapshot(snapshot).required_measurement_roots
    )

    with pytest.raises(
        catalog_publication.CatalogPublicationError, match="supported canonical graph producer"
    ):
        catalog_publication.prepare_policy_projection(
            tmp_path, active_snapshot=snapshot, target_policy=snapshot.policy, ready_at=19
        )


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
