"""Immutable private persistence for principal-free projection variants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import reserved_paths, state_paths
from exomem.governance import projection_store, projections
from exomem.governance.decisions import Decision


def _key() -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=7,
    )


def _variant(
    item_identity: str,
    content_hash: str,
    *,
    notice: str,
) -> projections.ProjectionVariant:
    variant = projections.build_projection_variant(
        item_identity=item_identity,
        content_hash=content_hash,
        decision=Decision(level=1, options={"notice": notice}),
        projector_schema_version=1,
        full_search_fields={"body": "hidden"},
    )
    assert variant is not None
    return variant


def _items() -> tuple[projection_store.ProjectionItemVariants, ...]:
    first_hash = "b" * 64
    second_hash = "c" * 64
    return (
        projection_store.ProjectionItemVariants(
            item_identity="exomem://memory/first",
            content_hash=first_hash,
            variants=(
                _variant(
                    "exomem://memory/first",
                    first_hash,
                    notice="Restricted",
                ),
                _variant(
                    "exomem://memory/first",
                    first_hash,
                    notice="Client-confidential",
                ),
            ),
        ),
        projection_store.ProjectionItemVariants(
            item_identity="exomem://memory/second",
            content_hash=second_hash,
            variants=(),
        ),
    )


def test_stage_replay_verify_and_load_exact_projection_row(tmp_path: Path) -> None:
    key = _key()
    items = _items()

    manifest = projection_store.stage_variant_store(tmp_path, key=key, items=items)
    replay = projection_store.stage_variant_store(tmp_path, key=key, items=items)
    verified = projection_store.verify_variant_store(
        tmp_path,
        key=key,
        expected_rows_digest=manifest.rows_digest,
    )
    loaded = projection_store.load_projection_variant(
        tmp_path,
        key=key,
        expected_rows_digest=manifest.rows_digest,
        item_identity=items[0].item_identity,
        expected_content_hash=items[0].content_hash,
        projection_variant_id=items[0].variants[1].projection_variant_id,
    )

    assert manifest == replay == verified
    assert manifest.namespace_key == key
    assert manifest.namespace_id == key.namespace_id
    assert manifest.item_count == 2
    assert manifest.variant_count == 2
    assert len(manifest.rows_digest) == 64
    assert loaded == items[0].variants[1]
    assert projection_store.variant_store_path(tmp_path, key).relative_to(
        state_paths.vault_state_dir(tmp_path)
    ) == Path(
        ".authorization-projections",
        key.namespace_id,
        "rows.sqlite",
    )


def test_store_round_trips_exact_catalog_membership(tmp_path: Path) -> None:
    key = _key()
    variant = _variant("member", "9" * 64, notice="member")
    item = projection_store.ProjectionItemVariants(
        item_identity="member",
        content_hash="9" * 64,
        variants=(variant,),
        scope_ids=("scope-b", "scope-a"),
    )
    manifest = projection_store.stage_variant_store(tmp_path, key=key, items=(item,))

    loaded_manifest, loaded_items = projection_store.load_projection_catalog(
        tmp_path,
        key=key,
        expected_rows_digest=manifest.rows_digest,
    )

    assert loaded_manifest == manifest
    assert loaded_items[0].scope_ids == ("scope-a", "scope-b")


def test_store_refuses_same_namespace_with_different_rows(tmp_path: Path) -> None:
    key = _key()
    items = _items()
    projection_store.stage_variant_store(tmp_path, key=key, items=items)
    changed = (
        projection_store.ProjectionItemVariants(
            item_identity=items[0].item_identity,
            content_hash=items[0].content_hash,
            variants=(
                _variant(
                    items[0].item_identity,
                    items[0].content_hash,
                    notice="Different",
                ),
            ),
        ),
        items[1],
    )

    with pytest.raises(projection_store.ProjectionStoreMismatch):
        projection_store.stage_variant_store(tmp_path, key=key, items=changed)


def test_projection_catalog_refuses_identity_count_above_repository_capacity() -> None:
    items = tuple(
        projection_store.ProjectionItemVariants(
            item_identity=f"item-{index:05d}",
            content_hash=f"{index:064x}",
            variants=(),
        )
        for index in range(projections.MAX_GOVERNED_CATALOG_ITEMS + 1)
    )

    with pytest.raises(projections.ProjectionCapacityExceeded, match="catalog"):
        projection_store._materialize(_key(), items)


def test_item_bundle_refuses_duplicate_or_cross_item_variants() -> None:
    variant = _variant("item-a", "d" * 64, notice="Restricted")
    with pytest.raises(projections.ProjectionCanonicalizationError, match="duplicate"):
        projection_store.ProjectionItemVariants(
            item_identity="item-a",
            content_hash="d" * 64,
            variants=(variant, variant),
        )
    with pytest.raises(projections.ProjectionCanonicalizationError, match="item identity"):
        projection_store.ProjectionItemVariants(
            item_identity="item-b",
            content_hash="d" * 64,
            variants=(variant,),
        )


def test_store_refuses_variant_from_a_different_projector_schema(tmp_path: Path) -> None:
    variant = projections.build_projection_variant(
        item_identity="item-schema",
        content_hash="d" * 64,
        decision=Decision(level=1, options={"notice": "Restricted"}),
        projector_schema_version=2,
        full_search_fields={"body": "hidden"},
    )
    assert variant is not None
    item = projection_store.ProjectionItemVariants(
        item_identity="item-schema",
        content_hash="d" * 64,
        variants=(variant,),
    )

    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="projector schema",
    ):
        projection_store.stage_variant_store(tmp_path, key=_key(), items=(item,))


@pytest.mark.parametrize("crash_at", ("before-commit", "after-commit"))
def test_staging_crash_has_no_partial_ready_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_at: str,
) -> None:
    key = _key()
    items = _items()

    def crash(point: str) -> None:
        if point == crash_at:
            raise RuntimeError(f"injected {point}")

    monkeypatch.setattr(projection_store, "_crash_point", crash)
    with pytest.raises(RuntimeError, match=f"injected {crash_at}"):
        projection_store.stage_variant_store(tmp_path, key=key, items=items)

    monkeypatch.setattr(projection_store, "_crash_point", lambda _point: None)
    manifest = projection_store.stage_variant_store(tmp_path, key=key, items=items)
    assert projection_store.verify_variant_store(
        tmp_path,
        key=key,
        expected_rows_digest=manifest.rows_digest,
    ) == manifest


def test_full_verifier_and_selected_loader_refuse_tamper(tmp_path: Path) -> None:
    key = _key()
    items = _items()
    manifest = projection_store.stage_variant_store(tmp_path, key=key, items=items)
    database = projection_store.variant_store_path(tmp_path, key)
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER projection_variants_no_update")
    connection.execute(
        "UPDATE projection_variants SET search_fields_jcs=? WHERE projection_variant_id=?",
        (b'{"notice":"Tampered"}', items[0].variants[0].projection_variant_id),
    )
    connection.commit()
    connection.close()

    with pytest.raises(projection_store.ProjectionStoreMismatch):
        projection_store.verify_variant_store(
            tmp_path,
            key=key,
            expected_rows_digest=manifest.rows_digest,
        )


def test_selected_loader_refuses_a_valid_row_inserted_after_commit(tmp_path: Path) -> None:
    key = _key()
    items = _items()
    manifest = projection_store.stage_variant_store(tmp_path, key=key, items=items)
    injected = _variant(
        items[0].item_identity,
        items[0].content_hash,
        notice="Injected after commit",
    )
    row = projection_store._row_material(injected)
    database = projection_store.variant_store_path(tmp_path, key)
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER projection_variants_no_insert")
    connection.execute(
        "INSERT INTO projection_variants "
        "(item_identity, projection_variant_id, decision_level, value_jcs, "
        "search_fields_jcs, row_digest) VALUES (?, ?, ?, ?, ?, ?)",
        (
            injected.item_identity,
            injected.projection_variant_id,
            injected.decision_level,
            injected.value_jcs,
            row.search_fields_jcs,
            row.row_digest,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(projection_store.ProjectionStoreMismatch):
        projection_store.load_projection_variant(
            tmp_path,
            key=key,
            expected_rows_digest=manifest.rows_digest,
            item_identity=injected.item_identity,
            expected_content_hash=injected.content_hash,
            projection_variant_id=injected.projection_variant_id,
        )


def test_store_schema_rejects_post_commit_insert(tmp_path: Path) -> None:
    key = _key()
    items = _items()
    projection_store.stage_variant_store(tmp_path, key=key, items=items)
    injected = _variant(
        items[0].item_identity,
        items[0].content_hash,
        notice="Injected after commit",
    )
    row = projection_store._row_material(injected)
    connection = sqlite3.connect(projection_store.variant_store_path(tmp_path, key))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "INSERT INTO projection_variants "
                "(item_identity, projection_variant_id, decision_level, value_jcs, "
                "search_fields_jcs, row_digest) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    injected.item_identity,
                    injected.projection_variant_id,
                    injected.decision_level,
                    injected.value_jcs,
                    row.search_fields_jcs,
                    row.row_digest,
                ),
            )
    finally:
        connection.close()


def test_loader_refuses_stale_tuple_or_catalog_identity(tmp_path: Path) -> None:
    key = _key()
    items = _items()
    manifest = projection_store.stage_variant_store(tmp_path, key=key, items=items)

    with pytest.raises(projection_store.ProjectionStoreMismatch, match="digest"):
        projection_store.load_projection_variant(
            tmp_path,
            key=key,
            expected_rows_digest="f" * 64,
            item_identity=items[0].item_identity,
            expected_content_hash=items[0].content_hash,
            projection_variant_id=items[0].variants[0].projection_variant_id,
        )
    with pytest.raises(projection_store.ProjectionStoreMismatch, match="content"):
        projection_store.load_projection_variant(
            tmp_path,
            key=key,
            expected_rows_digest=manifest.rows_digest,
            item_identity=items[0].item_identity,
            expected_content_hash="e" * 64,
            projection_variant_id=items[0].variants[0].projection_variant_id,
        )


def test_publishing_a_next_namespace_keeps_prior_namespace_identities(tmp_path: Path) -> None:
    first_key = _key()
    second_key = projections.ProjectionNamespaceKey(
        policy_fingerprint=first_key.policy_fingerprint,
        projector_schema_version=first_key.projector_schema_version,
        catalog_generation=first_key.catalog_generation + 1,
    )
    items = _items()
    first = projection_store.stage_variant_store(tmp_path, key=first_key, items=items)
    second = projection_store.stage_variant_store(tmp_path, key=second_key, items=items)

    assert projection_store.verify_variant_store(
        tmp_path,
        key=first_key,
        expected_rows_digest=first.rows_digest,
    ) == first
    assert projection_store.verify_variant_store(
        tmp_path,
        key=second_key,
        expected_rows_digest=second.rows_digest,
    ) == second
    assert projection_store.load_projection_variant(
        tmp_path,
        key=first_key,
        expected_rows_digest=first.rows_digest,
        item_identity=items[0].item_identity,
        expected_content_hash=items[0].content_hash,
        projection_variant_id=items[0].variants[0].projection_variant_id,
    ) == items[0].variants[0]
    assert projection_store.load_projection_variant(
        tmp_path,
        key=second_key,
        expected_rows_digest=second.rows_digest,
        item_identity=items[0].item_identity,
        expected_content_hash=items[0].content_hash,
        projection_variant_id=items[0].variants[0].projection_variant_id,
    ) == items[0].variants[0]

    with reserved_paths._subsystem_authority_scope("governance.projections"):
        with reserved_paths._identity_coordination_scope(
            tmp_path,
            descriptor_ids=("authorization-projections",),
        ):
            published = reserved_paths._reachable_owner_publications(
                tmp_path,
                "authorization-projections",
            )

    assert published == {}
