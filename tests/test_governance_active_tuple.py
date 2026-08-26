from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

from exomem import (
    create_file as create_file_module,
)
from exomem import (
    delete_directory as delete_directory_module,
)
from exomem import (
    delete_file as delete_file_module,
)
from exomem import (
    embeddings,
    find_corpus,
    graph_sync,
    media_jobs,
    media_processing,
    preserve,
    reserved_paths,
    scene_frames,
    semantic_contract,
    semantic_writes,
    writer_lease,
)
from exomem import (
    move_file as move_file_module,
)
from exomem import (
    recover_from_trash as recover_module,
)
from exomem import (
    vault as vault_module,
)
from exomem.governance import (
    authorization_custody,
    catalog_publication,
    companions,
    membership,
    policy,
    projected_graph,
    projected_retrieval,
    projection_measurement_store,
    projection_store,
    projections,
    receipts,
    schema_v4,
    store,
)
from exomem.governance.principal import owner_principal

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
FIRST_GENERATION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
SECOND_GENERATION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
LOSING_GENERATION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAZ"
LOGICAL_VAULT_ID = "vault-active-tuple"
ACTIVATION_STORE_ID = "activation-active-tuple"
KEYRING_ID = "keyring-active-tuple"
CELL_ID = "cell-active-tuple"
KEY_ID = "key-active-tuple"
SIGNING_KEY = b"k" * 32


def _documents(*, ceiling: int) -> tuple[tuple[str, bytes], ...]:
    return (
        (
            "rules/external.yaml",
            (
                "governance_version: 1\n"
                f"id: {RULE_ID}\n"
                "scope_ids:\n"
                f"  - {SCOPE_ID}\n"
                "audience: external\n"
                f"ceiling: {ceiling}\n"
            ).encode(),
        ),
        (
            "scopes/private.yaml",
            (
                "governance_version: 1\n"
                f"id: {SCOPE_ID}\n"
                "name: private\n"
                "paths:\n"
                "  - Notes/**\n"
                "default_deny: true\n"
            ).encode(),
        ),
    )


def _compiled(documents: tuple[tuple[str, bytes], ...]) -> policy.Policy:
    compiled = policy.compile_documents(dict(documents))
    assert not compiled.empty and not compiled.blocked
    return compiled


def _policy_seed(
    *,
    generation_id: str,
    documents: tuple[tuple[str, bytes], ...],
    predecessor_generation_id: str | None,
    event_suffix: str,
    now: int,
) -> schema_v4.PolicyGenerationSeed:
    compiled = _compiled(documents)
    return schema_v4.PolicyGenerationSeed(
        generation_id=generation_id,
        source_documents=documents,
        source_fingerprint=compiled.fingerprint,
        conflict_digest="0" * 64,
        compiled_policy=policy.canonical_compiled_bytes(compiled),
        policy_fingerprint=compiled.fingerprint,
        compiler_schema_version=1,
        projector_schema_version=1,
        predecessor_generation_id=predecessor_generation_id,
        authoring_event_id=f"authoring-{event_suffix}",
        receipt_event_id=f"receipt-{event_suffix}",
        created_at=now,
    )


def _migration_seed(*, now: int) -> schema_v4.MigrationSeed:
    return schema_v4.MigrationSeed(
        activation_store_id=ACTIVATION_STORE_ID,
        logical_vault_id=LOGICAL_VAULT_ID,
        activation_epoch=1,
        policy=_policy_seed(
            generation_id=FIRST_GENERATION_ID,
            documents=_documents(ceiling=2),
            predecessor_generation_id=None,
            event_suffix="first",
            now=now,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=now,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="namespace-first",
            evidence=b'{"ready":true}',
            ready_at=now,
        ),
        migrated_at=now,
    )


def _write_workspace(vault: Path, documents: tuple[tuple[str, bytes], ...]) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    for relative, content in documents:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _protected_file(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if os.name == "nt":
        from exomem import mutation_lock

        mutation_lock._windows_apply_private_dacl(
            path, mutation_lock._windows_current_user_sid()
        )
    else:
        path.chmod(0o600)


def _framed(domain: bytes, fields: list[bytes]) -> bytes:
    result = bytearray(domain)
    result.append(0)
    for field in fields:
        result.extend(len(field).to_bytes(4, "big"))
        result.extend(field)
    return bytes(result)


def _configure_custody(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    activation_epoch: int | None,
    activation_state_digest: str | None,
    now: int,
    governance_enrolled: bool = True,
) -> None:
    keyring = {
        "version": 1,
        "keyring_id": KEYRING_ID,
        "cell_id": CELL_ID,
        "logical_vault_id": LOGICAL_VAULT_ID,
        "active_key_id": KEY_ID,
        "accepted_keys": [
            {
                "key_id": KEY_ID,
                "key": base64.urlsafe_b64encode(SIGNING_KEY)
                .rstrip(b"=")
                .decode("ascii"),
                "not_before": now - 60,
                "not_after": now + 7_200,
            }
        ],
    }
    control: dict[str, object] = {
        "version": 1,
        "keyring_id": KEYRING_ID,
        "cell_id": CELL_ID,
        "logical_vault_id": LOGICAL_VAULT_ID,
        "registry_attachment_id": "attachment-active-tuple",
        "attachment_epoch": 1,
        "governance_enrolled": governance_enrolled,
        "activation_store_id": ACTIVATION_STORE_ID if governance_enrolled else None,
        "activation_epoch": activation_epoch,
        "activation_state_digest": activation_state_digest,
        "serving_membership_epoch": 1,
        "serving_membership_digest": "a" * 64,
        "issued_at": now - 30,
        "expires_at": now + 3_600,
        "signing_key_id": KEY_ID,
    }
    fields = [
        str(control["version"]).encode(),
        str(control["keyring_id"]).encode(),
        str(control["cell_id"]).encode(),
        str(control["logical_vault_id"]).encode(),
        str(control["registry_attachment_id"]).encode(),
        str(control["attachment_epoch"]).encode(),
        b"true" if governance_enrolled else b"false",
        (
            b""
            if control["activation_store_id"] is None
            else str(control["activation_store_id"]).encode()
        ),
        (
            b""
            if control["activation_epoch"] is None
            else str(control["activation_epoch"]).encode()
        ),
        (
            b""
            if control["activation_state_digest"] is None
            else str(control["activation_state_digest"]).encode()
        ),
        str(control["serving_membership_epoch"]).encode(),
        str(control["serving_membership_digest"]).encode(),
        str(control["issued_at"]).encode(),
        str(control["expires_at"]).encode(),
        str(control["signing_key_id"]).encode(),
    ]
    control["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(
                SIGNING_KEY,
                _framed(b"exomem.authorization-session.control/v1", fields),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    keyring_path = root / "keyring.json"
    control_path = root / "control.json"
    _protected_file(keyring_path, json.dumps(keyring, separators=(",", ":")).encode())
    _protected_file(control_path, json.dumps(control, separators=(",", ":")).encode())
    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(keyring_path))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(control_path))


def _migrate(vault: Path, *, now: int) -> schema_v4.MigrationResult:
    connection = store.open_connection(vault)
    try:
        result = schema_v4.migrate_v3_connection(connection, _migration_seed(now=now))
    finally:
        connection.close()
    return result


def _migrate_with_empty_projection_catalog(
    vault: Path,
    *,
    now: int,
) -> schema_v4.MigrationResult:
    documents = _documents(ceiling=2)
    compiled = _compiled(documents)
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=compiled.fingerprint,
        projector_schema_version=1,
        catalog_generation=1,
    )
    manifest = projection_store.stage_variant_store(vault, key=key, items=())
    connection = store.open_connection(vault)
    try:
        result = schema_v4.migrate_v3_connection(
            connection,
            schema_v4.MigrationSeed(
                activation_store_id=ACTIVATION_STORE_ID,
                logical_vault_id=LOGICAL_VAULT_ID,
                activation_epoch=1,
                policy=_policy_seed(
                    generation_id=FIRST_GENERATION_ID,
                    documents=documents,
                    predecessor_generation_id=None,
                    event_suffix="first",
                    now=now,
                ),
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=1,
                    descriptor=projection_store.catalog_descriptor_bytes(key, ()),
                    artifact_count=0,
                    created_at=now,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id=key.namespace_id,
                    evidence=projection_store.projection_namespace_evidence_bytes(
                        manifest
                    ),
                    ready_at=now,
                ),
                migrated_at=now,
            ),
        )
    finally:
        connection.close()
    return result


def _projection_item(
    *,
    vault: Path,
    compiled: policy.Policy,
    path: str,
    source: str,
    catalog_generation: int,
) -> projection_store.ProjectionItemVariants:
    content = source.encode("utf-8")
    parsed = find_corpus.parse_page(
        vault / path,
        0.0,
        vault,
        content=content,
        resolved_relative=path,
    )
    assert parsed is not None
    content_hash = hashlib.sha256(content).hexdigest()
    scope_ids = tuple(
        sorted(membership.evaluate_snapshot(parsed, compiled, content_hash=content_hash))
    )
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=compiled.fingerprint,
        projector_schema_version=1,
        catalog_generation=catalog_generation,
    )
    search_fields = {
        "body": parsed.body,
        "title": parsed.title,
    }
    for name, value in (
        ("status", parsed.frontmatter.get("status")),
        ("type", parsed.page_type),
        ("updated", parsed.updated),
        ("media_type", parsed.frontmatter.get("media_type")),
        ("parent_media", parsed.frontmatter.get("parent_media")),
    ):
        if value:
            search_fields[name] = str(value)
    variants = projections.enumerate_projection_variants(
        item_identity=path,
        content_hash=content_hash,
        scope_ids=scope_ids,
        policy=compiled,
        projector_schema_version=key.projector_schema_version,
        full_search_fields=search_fields,
    )
    return projection_store.ProjectionItemVariants(
        item_identity=path,
        content_hash=content_hash,
        scope_ids=scope_ids,
        variants=variants,
    )


def _migrate_with_projection_item(
    vault: Path,
    *,
    path: str,
    source: str,
    now: int,
) -> schema_v4.MigrationResult:
    return _migrate_with_projection_items(
        vault,
        items=((path, source),),
        now=now,
    )


def _migrate_with_projection_items(
    vault: Path,
    *,
    items: tuple[tuple[str, str], ...],
    ceiling: int = 2,
    graph_edges: tuple[projected_graph.ProjectionGraphEdge, ...] | None = None,
    now: int,
) -> schema_v4.MigrationResult:
    documents = _documents(ceiling=ceiling)
    compiled = _compiled(documents)
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=compiled.fingerprint,
        projector_schema_version=1,
        catalog_generation=1,
    )
    projected_items = tuple(
        _projection_item(
            vault=vault,
            compiled=compiled,
            path=path,
            source=source,
            catalog_generation=1,
        )
        for path, source in items
    )
    manifest = projection_store.stage_variant_store(
        vault,
        key=key,
        items=projected_items,
    )
    measurement_roots: tuple[projection_store.ProjectionMeasurementRoot, ...] = ()
    if graph_edges is not None:
        namespace = projection_store.prepare_projection_namespace(
            key=key,
            manifest=manifest,
            items=projected_items,
        )
        family = projection_measurement_store.MeasurementFamilyKey(
            namespace_key=key,
            lane="graph",
            extractor_version="projected-graph-v1",
            model_version="graph-schema-v1",
        )
        graph_manifest = projection_measurement_store.stage_measurement_store(
            vault,
            namespace=namespace,
            family=family,
            measurements=tuple(
                projected_graph.ProjectionGraphMeasurement(
                    measurement_key=projections.MeasurementKey(
                        projection_variant_id=variant.projection_variant_id,
                        lane=family.lane,
                        extractor_version=family.extractor_version,
                        model_version=family.model_version,
                    ),
                    edges=(
                        tuple(
                            edge
                            for edge in graph_edges
                            if edge.source_item_identity == item.item_identity
                        )
                        if variant.decision_level == 6
                        else ()
                    ),
                )
                for item in projected_items
                for variant in item.variants
            ),
        )
        measurement_roots = (
            projection_measurement_store.measurement_root(graph_manifest),
        )
    connection = store.open_connection(vault)
    try:
        return schema_v4.migrate_v3_connection(
            connection,
            schema_v4.MigrationSeed(
                activation_store_id=ACTIVATION_STORE_ID,
                logical_vault_id=LOGICAL_VAULT_ID,
                activation_epoch=1,
                policy=_policy_seed(
                    generation_id=FIRST_GENERATION_ID,
                    documents=documents,
                    predecessor_generation_id=None,
                    event_suffix="first",
                    now=now,
                ),
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=1,
                    descriptor=projection_store.catalog_descriptor_bytes(
                        key,
                        projected_items,
                    ),
                    artifact_count=len(projected_items),
                    created_at=now,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id=key.namespace_id,
                    evidence=projection_store.projection_namespace_evidence_bytes(
                        manifest,
                        required_measurement_roots=measurement_roots,
                    ),
                    ready_at=now,
                ),
                migrated_at=now,
            ),
        )
    finally:
        connection.close()


def _migrate_with_vector_projection_items(
    vault: Path,
    *,
    items: tuple[tuple[str, str], ...],
    clip_samples_by_path: dict[
        str,
        tuple[projected_retrieval.ProjectionClipSample, ...],
    ]
    | None = None,
    now: int,
) -> tuple[
    schema_v4.MigrationResult,
    tuple[projected_retrieval.ProjectionVectorMeasurement, ...],
]:
    documents = _documents(ceiling=2)
    compiled = _compiled(documents)
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=compiled.fingerprint,
        projector_schema_version=1,
        catalog_generation=1,
    )
    projected_items = tuple(
        _projection_item(
            vault=vault,
            compiled=compiled,
            path=path,
            source=source,
            catalog_generation=1,
        )
        for path, source in items
    )
    manifest = projection_store.stage_variant_store(
        vault,
        key=key,
        items=projected_items,
    )
    namespace = projection_store.prepare_projection_namespace(
        key=key,
        manifest=manifest,
        items=projected_items,
    )
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=key,
        lane="vector",
        extractor_version="projected-text-v1",
        model_version=embeddings.MODEL_NAME,
    )
    measurements = tuple(
        projected_retrieval.ProjectionVectorMeasurement(
            measurement_key=projections.MeasurementKey(
                projection_variant_id=variant.projection_variant_id,
                lane="vector",
                extractor_version=family.extractor_version,
                model_version=family.model_version,
            ),
            vector=(float(index + 1), 1.0),
        )
        for index, variant in enumerate(
            variant
            for item in projected_items
            for variant in item.variants
        )
    )
    measurement_manifest = projection_measurement_store.stage_measurement_store(
        vault,
        namespace=namespace,
        family=family,
        measurements=measurements,
    )
    measurement_roots = [
        projection_measurement_store.measurement_root(measurement_manifest)
    ]
    if clip_samples_by_path is not None:
        clip_family = projection_measurement_store.MeasurementFamilyKey(
            namespace_key=key,
            lane="clip",
            extractor_version="pixels-v1",
            model_version=embeddings.CLIP_MODEL_NAME,
        )
        clip_measurements = tuple(
            projected_retrieval.ProjectionClipMeasurement(
                measurement_key=projections.MeasurementKey(
                    projection_variant_id=variant.projection_variant_id,
                    lane=clip_family.lane,
                    extractor_version=clip_family.extractor_version,
                    model_version=clip_family.model_version,
                ),
                samples=clip_samples_by_path[item.item_identity],
            )
            for item in projected_items
            for variant in item.variants
            if projected_retrieval.clip_variant_applicable(variant)
        )
        expected_clip_items = {
            item.item_identity
            for item in projected_items
            if any(
                projected_retrieval.clip_variant_applicable(variant)
                for variant in item.variants
            )
        }
        assert set(clip_samples_by_path) == expected_clip_items
        clip_manifest = projection_measurement_store.stage_measurement_store(
            vault,
            namespace=namespace,
            family=clip_family,
            measurements=clip_measurements,
        )
        measurement_roots.append(
            projection_measurement_store.measurement_root(clip_manifest)
        )
    connection = store.open_connection(vault)
    try:
        migration = schema_v4.migrate_v3_connection(
            connection,
            schema_v4.MigrationSeed(
                activation_store_id=ACTIVATION_STORE_ID,
                logical_vault_id=LOGICAL_VAULT_ID,
                activation_epoch=1,
                policy=_policy_seed(
                    generation_id=FIRST_GENERATION_ID,
                    documents=documents,
                    predecessor_generation_id=None,
                    event_suffix="first",
                    now=now,
                ),
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=1,
                    descriptor=projection_store.catalog_descriptor_bytes(
                        key,
                        projected_items,
                    ),
                    artifact_count=len(projected_items),
                    created_at=now,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id=key.namespace_id,
                    evidence=projection_store.projection_namespace_evidence_bytes(
                        manifest,
                        required_measurement_roots=tuple(measurement_roots),
                    ),
                    ready_at=now,
                ),
                migrated_at=now,
            ),
        )
    finally:
        connection.close()
    return migration, measurements


def _load_active_projection_items(
    vault: Path,
    *,
    activation_epoch: int,
    activation_state_digest: str,
) -> tuple[
    schema_v4.VerifiedActiveGovernanceState,
    projection_store.VariantStoreManifest,
    tuple[projection_store.ProjectionItemVariants, ...],
]:
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=activation_epoch,
            expected_activation_state_digest=activation_state_digest,
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    return active, manifest, items


def _configure_media_v4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sidecar_source: str | None,
    now: int,
) -> tuple[Path, Path, schema_v4.MigrationResult]:
    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "interview.m4a"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x00\x00\x00\x18ftypM4A governed audio")
    sidecar = binary.with_name(f"{binary.name}.md")
    if sidecar_source is not None:
        sidecar.write_text(sidecar_source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    if sidecar_source is None:
        migration = _migrate_with_empty_projection_catalog(vault, now=now)
    else:
        migration = _migrate_with_projection_item(
            vault,
            path=sidecar.relative_to(vault).as_posix(),
            source=sidecar_source,
            now=now,
        )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    media_processing.set_media_runtime_available(vault)
    return vault, sidecar, migration


def _legacy_companion_backfill_input(
    vault: Path,
) -> tuple[str, str, str, dict[str, object]]:
    artifact_path = "Knowledge Base/Notes/private.bin"
    companion_path = f"{artifact_path}.md"
    artifact = b"\x00private-bytes\xff"
    companion = (
        "---\n"
        "title: Private binary\n"
        "type: source\n"
        "status: draft\n"
        "---\n\n"
        "# Private binary\n\n"
        "Legacy companion.\n"
    )
    artifact_target = vault / artifact_path
    artifact_target.parent.mkdir(parents=True, exist_ok=True)
    artifact_target.write_bytes(artifact)
    (vault / companion_path).write_text(companion, encoding="utf-8")
    payload: dict[str, object] = {
        "version": 1,
        "artifact_class": "binary",
        "artifact_path": artifact_path,
        "expected_artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "expected_artifact_size": len(artifact),
        "expected_companion_path": companion_path,
        "expected_companion_sha256": hashlib.sha256(companion.encode()).hexdigest(),
        "semantics": {
            "projects": ["private"],
            "tags": ["confidential"],
            "types": ["source"],
            "classes": ["pii"],
        },
    }
    return artifact_path, companion_path, companion, payload


def _preview_companion_backfill(
    vault: Path,
    payload: dict[str, object],
    *,
    now: int,
) -> dict[str, object]:
    from exomem.governance.tool import op_govern_memory

    with reserved_paths._owner_authority_scope("govern_memory"):
        return op_govern_memory(
            vault,
            operation="backfill_companion",
            backfill_action="preview",
            companion_input=payload,
            principal=owner_principal(),
            now=now,
        )


def _commit_companion_backfill(
    vault: Path,
    payload: dict[str, object],
    *,
    proposal_id: str,
    now: int,
    crash_at: str | None = None,
) -> dict[str, object]:
    from exomem.governance.tool import op_govern_memory

    with reserved_paths._owner_authority_scope("govern_memory"):
        return op_govern_memory(
            vault,
            operation="backfill_companion",
            backfill_action="commit",
            proposal_id=proposal_id,
            companion_input=payload,
            principal=owner_principal(),
            now=now,
            crash_at=crash_at,
        )


def test_v4_companion_backfill_publishes_catalog_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _artifact_path, companion_path, _companion, payload = (
        _legacy_companion_backfill_input(vault)
    )
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((companion_path, (vault / companion_path).read_text(encoding="utf-8")),),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    preview = _preview_companion_backfill(vault, payload, now=now + 1)

    committed = _commit_companion_backfill(
        vault,
        payload,
        proposal_id=str(preview["proposal_id"]),
        now=now + 2,
    )

    assert committed["status"] == "committed"
    companion_after = (vault / companion_path).read_text(encoding="utf-8")
    assert "governance_companion:" in companion_after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert custody.control.activation_epoch == 2
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert active.active.catalog_generation == 2
    assert manifest.item_count == 1
    assert {item.item_identity: item.content_hash for item in items} == {
        companion_path: vault_module.content_hash(companion_after)
    }
    connection = store.open_connection(vault)
    try:
        component_kinds = {
            str(row[0])
            for row in connection.execute(
                "SELECT component_kind FROM governance_operation_components "
                "WHERE event_id=?",
                (str(committed["event_id"]),),
            ).fetchall()
        }
        catalog_target = connection.execute(
            "SELECT value_json FROM governance_operation_components "
            "WHERE event_id=? AND phase='final' AND component_kind='catalog'",
            (str(committed["event_id"]),),
        ).fetchone()
    finally:
        connection.close()
    assert component_kinds == {"catalog", "companion", "proposal"}
    assert catalog_target is not None
    assert json.loads(str(catalog_target[0]))["activation_state_digest"] == (
        custody.control.activation_state_digest
    )


@pytest.mark.parametrize("crash_at", ["after_catalog", "after_terminal"])
def test_v4_companion_backfill_recovers_after_catalog_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_at: str,
) -> None:
    from exomem.governance.recovery import reconcile_governance_operations
    from exomem.governance.tool import GovernanceCrash

    now = int(time.time())
    vault = tmp_path / "vault"
    _artifact_path, companion_path, _companion, payload = (
        _legacy_companion_backfill_input(vault)
    )
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((companion_path, (vault / companion_path).read_text(encoding="utf-8")),),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    preview = _preview_companion_backfill(vault, payload, now=now + 1)

    with pytest.raises(GovernanceCrash, match=crash_at):
        _commit_companion_backfill(
            vault,
            payload,
            proposal_id=str(preview["proposal_id"]),
            now=now + 2,
            crash_at=crash_at,
        )

    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert custody.control.activation_epoch == 2
    recovered = reconcile_governance_operations(vault)
    assert recovered["blocked"] is False
    assert recovered["activated"] == 1
    replayed = _commit_companion_backfill(
        vault,
        payload,
        proposal_id=str(preview["proposal_id"]),
        now=now + 4,
    )
    assert replayed["status"] == "committed"


def test_v4_companion_backfill_recovers_catalog_after_companion_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.recovery import reconcile_governance_operations
    from exomem.governance.tool import GovernanceCrash

    now = int(time.time())
    vault = tmp_path / "vault"
    _artifact_path, companion_path, _companion, payload = (
        _legacy_companion_backfill_input(vault)
    )
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((companion_path, (vault / companion_path).read_text(encoding="utf-8")),),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    preview = _preview_companion_backfill(vault, payload, now=now + 1)

    with pytest.raises(GovernanceCrash, match="after_publish"):
        _commit_companion_backfill(
            vault,
            payload,
            proposal_id=str(preview["proposal_id"]),
            now=now + 2,
            crash_at="after_publish",
        )

    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert custody.control.activation_epoch == 1
    assert "governance_companion:" in (vault / companion_path).read_text(encoding="utf-8")
    recovered = reconcile_governance_operations(vault)
    assert recovered["blocked"] is False
    assert recovered["activated"] == 1
    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert custody.control.activation_epoch == 2
    replayed = _commit_companion_backfill(
        vault,
        payload,
        proposal_id=str(preview["proposal_id"]),
        now=now + 4,
    )
    assert replayed["status"] == "committed"


def test_v4_companion_backfill_publication_uncertainty_keeps_pending_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.recovery import reconcile_governance_operations
    from exomem.governance.tool import GovernanceError

    now = int(time.time())
    vault = tmp_path / "vault"
    _artifact_path, companion_path, _companion, payload = (
        _legacy_companion_backfill_input(vault)
    )
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((companion_path, (vault / companion_path).read_text(encoding="utf-8")),),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    preview = _preview_companion_backfill(vault, payload, now=now + 1)

    def lose_catalog_terminal(_prepared) -> None:
        raise catalog_publication.CatalogPublicationError("lost catalog terminal")

    monkeypatch.setattr(
        catalog_publication,
        "publish_markdown_batch",
        lose_catalog_terminal,
    )
    with pytest.raises(GovernanceError) as uncertain:
        _commit_companion_backfill(
            vault,
            payload,
            proposal_id=str(preview["proposal_id"]),
            now=now + 2,
        )

    assert uncertain.value.code == "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN"
    assert "governance_companion:" in (vault / companion_path).read_text(encoding="utf-8")
    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert custody.control.activation_epoch == 1
    recovered = reconcile_governance_operations(vault)
    assert recovered["blocked"] is True
    assert recovered["activated"] == 0


def test_govern_memory_v4_proposal_persists_exact_authority_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    target_documents = {
        relative: content.decode("utf-8")
        for relative, content in _documents(ceiling=1)
    }

    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents=target_documents,
            target_ceiling=1,
            now=now + 1,
        )

    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        proposal_json, membership_manifest = connection.execute(
            "SELECT proposal_json, membership_manifest FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone()
    payload = json.loads(proposal_json)
    binding = payload["authority_binding"]
    target = binding["target"]
    reviewed = binding["reviewed_active_tuple"]
    snapshot = binding["authoring_snapshot"]
    target_policy = _compiled(_documents(ceiling=1))
    target_key = projections.ProjectionNamespaceKey(
        policy_fingerprint=target_policy.fingerprint,
        projector_schema_version=1,
        catalog_generation=1,
    )

    assert binding["schema"] == "exomem.governance-policy-proposal/v3"
    assert reviewed == {
        "activation_epoch": 1,
        "activation_state_digest": migration.activation_state_digest,
        "activation_store_id": ACTIVATION_STORE_ID,
        "catalog_generation": 1,
        "logical_vault_id": LOGICAL_VAULT_ID,
        "policy_fingerprint": _compiled(_documents(ceiling=2)).fingerprint,
        "policy_generation_id": FIRST_GENERATION_ID,
        "projection_namespace_id": projections.ProjectionNamespaceKey(
            policy_fingerprint=_compiled(_documents(ceiling=2)).fingerprint,
            projector_schema_version=1,
            catalog_generation=1,
        ).namespace_id,
        "projector_schema_version": 1,
    }
    assert snapshot["source_fingerprint"] == reviewed["policy_fingerprint"]
    assert snapshot["conflict_set_digest"] == hashlib.sha256(
        b"exomem.governance-conflict-set.v1\0"
    ).hexdigest()
    assert snapshot["guard_generation"]
    assert len(snapshot["documents"]) == 2
    assert len(snapshot["file_identities"]) == 2
    assert [item["path"] for item in snapshot["directory_identities"]] == [
        "rules",
        "scopes",
    ]
    assert target["policy_fingerprint"] == target_policy.fingerprint
    assert target["source_fingerprint"] == target_policy.fingerprint
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", target["generation_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", target["authoring_event_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", target["receipt_event_id"])
    assert target["compiled_policy"] == base64.b64encode(
        policy.canonical_compiled_bytes(target_policy)
    ).decode("ascii")
    assert target["projection_namespace"] == {
        "catalog_generation": 1,
        "evidence": base64.b64encode(
            projection_store.projection_namespace_evidence_bytes(
                projection_store.verify_variant_store(
                    vault,
                    key=target_key,
                    expected_rows_digest=target["projection_rows_digest"],
                )
            )
        ).decode("ascii"),
        "namespace_id": target_key.namespace_id,
        "projector_schema_version": 1,
        "ready_at": now + 1,
    }
    assert json.loads(membership_manifest) == binding["membership_manifest"]


def test_govern_memory_v4_proposal_refuses_unprepared_model_measurements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    load_evidence = projection_store.namespace_evidence_from_snapshot

    def model_backed_evidence(
        snapshot: schema_v4.ActivePolicySnapshot,
    ) -> projection_store.ProjectionNamespaceEvidence:
        evidence = load_evidence(snapshot)
        return projection_store.ProjectionNamespaceEvidence(
            manifest=evidence.manifest,
            required_measurement_roots=(object(),),
        )

    monkeypatch.setattr(
        projection_store,
        "namespace_evidence_from_snapshot",
        model_backed_evidence,
    )

    with reserved_paths._owner_authority_scope("govern_memory"):
        with pytest.raises(GovernanceError) as error:
            op_govern_memory(
                vault,
                operation="propose",
                principal=owner_principal(),
                intent="Lower the external ceiling",
                documents={
                    relative: content.decode("utf-8")
                    for relative, content in _documents(ceiling=1)
                },
                target_ceiling=1,
                now=now + 1,
            )
    assert error.value.code == "GOVERNANCE_PROJECTION_REBUILD_REQUIRED"
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_proposals"
        ).fetchone() == (0,)


def test_govern_memory_v4_commit_publishes_the_exact_reviewed_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    served = policy.load(vault)
    custody = authorization_custody.load_authorization_custody(vault, now=now + 2)

    assert committed == {
        "status": "committed",
        "event_id": committed["event_id"],
        "proposal_id": proposed["proposal_id"],
        "direction": "narrowing",
        "mirror_status": "complete",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", committed["event_id"])
    assert served.fingerprint == _compiled(_documents(ceiling=1)).fingerprint
    assert served.rules[0].ceiling == 1
    assert custody.control.activation_epoch == 2
    assert custody.control.activation_state_digest is not None
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == dict(_documents(ceiling=1))["rules/external.yaml"]

    mirror_records = [
        record
        for record in receipts.event_records(vault)
        if record.get("operation") == "governance_policy_workspace_mirror"
        or record.get("outcome") == "complete"
    ]
    assert [record["phase"] for record in mirror_records] == ["intent", "committed"]
    assert mirror_records[0]["parent_causation_id"] == committed["event_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", mirror_records[0]["prior"])
    assert re.fullmatch(r"[0-9a-f]{64}", mirror_records[0]["prepared"])
    assert re.fullmatch(r"[0-9a-f]{64}", mirror_records[0]["target"])

    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status, reserved_event_id, spent_at FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent", None, now + 2)
        active = connection.execute(
            "SELECT policy_generation_id, policy_fingerprint, "
            "projector_schema_version, catalog_generation "
            "FROM active_governance_tuple WHERE singleton=1"
        ).fetchone()
        assert active[0] != FIRST_GENERATION_ID
        assert active[1:] == (_compiled(_documents(ceiling=1)).fingerprint, 1, 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_recovers_mirror_after_lost_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    target_documents = dict(_documents(ceiling=1))
    target_documents["scopes/private.yaml"] = target_documents[
        "scopes/private.yaml"
    ].replace(b"name: private\n", b"name: sensitive\n")
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in target_documents.items()
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(GovernanceCrash, match="v4_after_mirror_write:1"):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                crash_at="v4_after_mirror_write:1",
                now=now + 2,
            )

    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == target_documents["rules/external.yaml"]
    assert (
        vault / "Knowledge Base" / "_Governance" / "scopes" / "private.yaml"
    ).read_bytes() == dict(_documents(ceiling=2))["scopes/private.yaml"]
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent",)

    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["mirror_status"] == "complete"
    assert (
        vault / "Knowledge Base" / "_Governance" / "scopes" / "private.yaml"
    ).read_bytes() == target_documents["scopes/private.yaml"]
    records = receipts.event_records(vault)
    intents = [
        record
        for record in records
        if record.get("operation") == "governance_policy_workspace_mirror"
    ]
    terminals = [
        record
        for record in records
        if record.get("causation_id") == intents[0]["event_id"]
    ]
    assert len(intents) == 1
    assert [(record["phase"], record["outcome"]) for record in terminals] == [
        ("committed", "complete")
    ]
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_retries_transient_mirror_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import held_fs
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    original_acquire = held_fs.acquire
    armed = False
    mirror_acquires = 0

    def arm_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal armed
        if phase == "after_intent":
            armed = True

    def refuse_effect_acquire(root: Path):
        nonlocal mirror_acquires
        if armed:
            mirror_acquires += 1
            if mirror_acquires == 4:
                return held_fs.HeldResult(
                    error=held_fs.HeldFsError(
                        "CAPABILITY_UNAVAILABLE",
                        "test-only transient capability refusal",
                    )
                )
        return original_acquire(root)

    monkeypatch.setattr(tool, "_v4_workspace_mirror_barrier", arm_after_intent)
    monkeypatch.setattr(held_fs, "acquire", refuse_effect_acquire)
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(tool.GovernanceError) as refused:
            tool.op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                now=now + 2,
            )

    assert refused.value.code == "GOVERNANCE_BLOCKED"
    assert policy.load(vault).rules[0].ceiling == 1
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == dict(_documents(ceiling=2))["rules/external.yaml"]

    monkeypatch.setattr(held_fs, "acquire", original_acquire)
    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["mirror_status"] == "complete"
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == dict(_documents(ceiling=1))["rules/external.yaml"]


def test_govern_memory_v4_commit_replays_after_mirror_terminal_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(GovernanceCrash, match="v4_after_mirror_terminal"):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                crash_at="v4_after_mirror_terminal",
                now=now + 2,
            )
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["mirror_status"] == "complete"
    records = receipts.event_records(vault)
    intents = [
        record
        for record in records
        if record.get("operation") == "governance_policy_workspace_mirror"
    ]
    assert len(intents) == 1
    assert sum(
        record.get("causation_id") == intents[0]["event_id"] for record in records
    ) == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_preserves_observed_workspace_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    drift = dict(_documents(ceiling=3))["rules/external.yaml"]
    changed = False

    def mutate_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase == "after_intent" and not changed:
            changed = True
            (
                vault
                / "Knowledge Base"
                / "_Governance"
                / "rules"
                / "external.yaml"
            ).write_bytes(drift)

    monkeypatch.setattr(tool, "_v4_workspace_mirror_barrier", mutate_after_intent)
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["mirror_status"] == "diverged"
    assert (
        vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"
    ).read_bytes() == drift
    assert policy.load(vault).rules[0].ceiling == 1
    mirror_terminal = next(
        record
        for record in receipts.event_records(vault)
        if record.get("outcome") == "diverged"
    )
    assert mirror_terminal["phase"] == "committed"


def test_govern_memory_v4_commit_refuses_swapped_reviewed_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    changed = False
    rules = vault / "Knowledge Base" / "_Governance" / "rules"
    displaced = tmp_path / "reviewed-rules"

    def exchange_parent_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase == "after_intent" and not changed:
            changed = True
            rules.rename(displaced)
            rules.mkdir()
            (displaced / "external.yaml").rename(rules / "external.yaml")
            displaced.rmdir()

    monkeypatch.setattr(
        tool,
        "_v4_workspace_mirror_barrier",
        exchange_parent_after_intent,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["mirror_status"] == "diverged"
    assert (rules / "external.yaml").read_bytes() == dict(_documents(ceiling=2))[
        "rules/external.yaml"
    ]
    assert policy.load(vault).rules[0].ceiling == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink race fixture")
def test_govern_memory_v4_commit_refuses_symlinked_mirror_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    changed = False
    leaf = vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml"

    def alias_after_intent(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase == "after_intent" and not changed:
            changed = True
            leaf.unlink()
            leaf.symlink_to("../scopes/private.yaml")

    monkeypatch.setattr(tool, "_v4_workspace_mirror_barrier", alias_after_intent)
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = tool.op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        committed = tool.op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["mirror_status"] == "diverged"
    assert leaf.is_symlink()
    assert policy.load(vault).blocked


def test_govern_memory_v4_commit_recovers_lost_registry_ack_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )

    def crash(point: str) -> None:
        if point == "policy-publication-after-commit-before-registry":
            raise GovernanceCrash(point)

    monkeypatch.setattr(schema_v4, "_crash_point", crash)
    with reserved_paths._owner_authority_scope("govern_memory"):
        with pytest.raises(GovernanceCrash):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                now=now + 2,
            )

    assert policy.load(vault).blocked
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)

    monkeypatch.setattr(schema_v4, "_crash_point", lambda _point: None)
    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 1_000,
        )

    assert recovered["status"] == "committed"
    assert recovered["proposal_id"] == proposed["proposal_id"]
    assert policy.load(vault).rules[0].ceiling == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status, spent_at FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent", now + 1_000)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_recovers_after_registry_ack_before_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        with pytest.raises(GovernanceCrash, match="v4_after_registry_ack"):
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                crash_at="v4_after_registry_ack",
                now=now + 2,
            )

    assert policy.load(vault).rules[0].ceiling == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("pending",)

    with reserved_paths._owner_authority_scope("govern_memory"):
        recovered = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 3,
        )

    assert recovered["status"] == "committed"
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status, spent_at FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent", now + 3)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_adopts_its_exact_concurrent_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )

    publish = schema_v4.publish_policy_generation

    def concurrent_winner(*args: object, **kwargs: object) -> object:
        publish(*args, **kwargs)  # type: ignore[arg-type]
        raise schema_v4.ActiveTupleStale("concurrent retry lost the CAS")

    monkeypatch.setattr(schema_v4, "publish_policy_generation", concurrent_winner)
    with reserved_paths._owner_authority_scope("govern_memory"):
        committed = op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposed["proposal_id"],
            now=now + 2,
        )

    assert committed["status"] == "committed"
    assert policy.load(vault).rules[0].ceiling == 1
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT status FROM governance_proposals WHERE proposal_id=?",
            (proposed["proposal_id"],),
        ).fetchone() == ("spent",)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def test_govern_memory_v4_commit_refuses_reviewed_tuple_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    with reserved_paths._owner_authority_scope("govern_memory"):
        proposed = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Lower the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=1)
            },
            target_ceiling=1,
            now=now + 1,
        )
        winner = op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Close the external ceiling",
            documents={
                relative: content.decode("utf-8")
                for relative, content in _documents(ceiling=0)
            },
            target_ceiling=0,
            now=now + 1,
        )
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=winner["proposal_id"],
            now=now + 2,
        )

    with reserved_paths._owner_authority_scope("govern_memory"):
        with pytest.raises(GovernanceError) as error:
            op_govern_memory(
                vault,
                operation="commit",
                principal=owner_principal(),
                proposal_id=proposed["proposal_id"],
                now=now + 3,
            )
    assert error.value.code == "STALE_GOVERNANCE_POLICY"
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)


def _acknowledge(
    active: schema_v4.VerifiedActiveGovernanceState,
) -> schema_v4.ActivationRegistryAcknowledgement:
    return schema_v4.ActivationRegistryAcknowledgement(
        activation_store_id=active.activation_store_id,
        activation_epoch=active.activation_epoch,
        activation_state_digest=active.activation_state_digest,
    )


def test_compiled_policy_authority_bytes_have_a_fixed_vector() -> None:
    compiled = _compiled(_documents(ceiling=2))

    assert (
        hashlib.sha256(policy.canonical_compiled_bytes(compiled)).hexdigest()
        == "b10ad7307c6c63f0cc732e5bc03462997e59be1fea03c19038945da3e2944ed2"
    )


def test_activation_state_digest_has_a_cross_runtime_fixed_vector() -> None:
    assert schema_v4.activation_state_digest(
        logical_vault_id=LOGICAL_VAULT_ID,
        activation_store_id=ACTIVATION_STORE_ID,
        activation_epoch=7,
        policy_generation_id=FIRST_GENERATION_ID,
        policy_fingerprint="1" * 64,
        policy_row_digest="2" * 64,
        projector_schema_version=3,
        catalog_generation=11,
        catalog_descriptor_digest="4" * 64,
        projection_namespace_identity="5" * 64,
    ) == "07a35c70829d9486f876aed26c650e3aeb3eaf064a676ba842e7dbc97ebb878b"


def test_bounded_pointer_exposes_sqlite_cas_before_registry_ack(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        predecessor = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        forbidden_columns = {
            "source_documents",
            "compiled_policy",
            "descriptor",
            "evidence",
        }

        def bounded_authorizer(
            action: int,
            _table: str | None,
            column: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and column in forbidden_columns:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(bounded_authorizer)
        assert schema_v4.load_active_tuple_pointer(connection) == predecessor
        connection.set_authorizer(None)

        def unavailable_registry(
            _active: schema_v4.VerifiedActiveGovernanceState,
        ) -> schema_v4.ActivationRegistryAcknowledgement:
            raise RuntimeError("registry acknowledgement unavailable")

        with pytest.raises(RuntimeError, match="registry acknowledgement unavailable"):
            schema_v4.publish_policy_generation(
                connection,
                expected=predecessor,
                policy=_policy_seed(
                    generation_id=SECOND_GENERATION_ID,
                    documents=_documents(ceiling=1),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="bounded-pointer",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-bounded-pointer",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=unavailable_registry,
            )

        successor = schema_v4.load_active_tuple_pointer(connection)
        assert successor.activation_epoch == predecessor.activation_epoch + 1
        assert successor != predecessor
    finally:
        connection.close()


def test_tuple_publication_schema_is_closed_and_append_only(tmp_path: Path) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        columns = tuple(
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(governance_tuple_publications)"
            )
        )
        assert columns == (
            "event_id",
            "publication_kind",
            "predecessor_activation_state_digest",
            "target_activation_state_digest",
            "policy_generation_id",
            "policy_fingerprint",
            "projector_schema_version",
            "catalog_generation",
            "activation_epoch",
            "status",
            "activated_at",
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE governance_tuple_publications SET status='committed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM governance_tuple_publications")
    finally:
        connection.close()


def test_migration_refuses_noncanonical_compiled_seed_before_schema_write(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    connection = store.open_connection(vault)
    invalid = dataclasses.replace(
        _migration_seed(now=now),
        policy=dataclasses.replace(
            _migration_seed(now=now).policy,
            compiled_policy=b'{"schema":"caller-selected"}',
        ),
    )
    try:
        with pytest.raises(schema_v4.SchemaV4Error, match="source parity"):
            schema_v4.migrate_v3_connection(connection, invalid)

        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='compiled_policy_generations'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_v4_policy_load_uses_the_verified_immutable_generation_not_live_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    result = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=result.activation_state_digest,
        now=now,
    )

    initial = policy.load(vault)
    _write_workspace(vault, _documents(ceiling=0))
    pending = policy.load(vault)

    assert initial.fingerprint == _compiled(_documents(ceiling=2)).fingerprint
    assert pending == initial
    assert pending.rules[0].ceiling == 2


def test_v4_policy_load_blocks_registry_tuple_mismatch_and_workspace_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    result = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest="f" * 64,
        now=now,
    )

    assert policy.load(vault).blocked

    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=result.activation_state_digest,
        now=now,
    )
    for path in sorted(
        (vault / "Knowledge Base" / "_Governance").rglob("*"), reverse=True
    ):
        path.rmdir() if path.is_dir() else path.unlink()
    (vault / "Knowledge Base" / "_Governance").rmdir()

    assert policy.load(vault).blocked


def test_external_enrollment_proof_controls_open_and_missing_store_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=None,
        activation_state_digest=None,
        governance_enrolled=False,
        now=now,
    )

    assert policy.load(vault).empty

    _write_workspace(vault, _documents(ceiling=2))
    assert policy.load(vault).blocked

    for path in sorted(
        (vault / "Knowledge Base" / "_Governance").rglob("*"), reverse=True
    ):
        path.rmdir() if path.is_dir() else path.unlink()
    (vault / "Knowledge Base" / "_Governance").rmdir()
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest="f" * 64,
        governance_enrolled=True,
        now=now,
    )

    assert policy.load(vault).blocked


@pytest.mark.skipif(os.name == "nt", reason="requires an unprivileged symlink fixture")
def test_never_enrolled_refuses_broken_activation_or_workspace_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=None,
        activation_state_digest=None,
        governance_enrolled=False,
        now=now,
    )
    (kb / ".governance.sqlite").symlink_to(tmp_path / "missing-store")

    assert policy.load(vault).blocked

    (kb / ".governance.sqlite").unlink()
    (kb / "_Governance").symlink_to(tmp_path / "missing-workspace", target_is_directory=True)

    assert policy.load(vault).blocked


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_never_enrolled_refuses_orphaned_activation_store_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=None,
        activation_state_digest=None,
        governance_enrolled=False,
        now=now,
    )
    (kb / f".governance.sqlite{suffix}").write_bytes(b"orphaned activation state")

    assert policy.load(vault).blocked


def test_policy_publication_cas_has_one_winner_and_no_losing_rows(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    result = _migrate(vault, now=now)
    first = store.open_authorization_session_connection(vault)
    second = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            first,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=result.activation_state_digest,
        )
        winner = schema_v4.publish_policy_generation(
            first,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="winner",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-winner",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )

        with pytest.raises(schema_v4.ActiveTupleStale):
            schema_v4.publish_policy_generation(
                second,
                expected=expected,
                policy=_policy_seed(
                    generation_id=LOSING_GENERATION_ID,
                    documents=_documents(ceiling=0),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="loser",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-loser",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert winner.active.policy_generation_id == SECOND_GENERATION_ID
        assert winner.active.activation_epoch == 2
        rows = first.execute(
            "SELECT generation_id FROM compiled_policy_generations ORDER BY generation_id"
        ).fetchall()
        assert rows == [(FIRST_GENERATION_ID,), (SECOND_GENERATION_ID,)]
        assert first.execute(
            "SELECT COUNT(*) FROM governance_projection_namespaces "
            "WHERE namespace_id='namespace-loser'"
        ).fetchone() == (0,)
    finally:
        second.close()
        first.close()


def test_registry_ack_is_required_before_the_new_policy_can_serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    prior_custody = authorization_custody.load_authorization_custody(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_policy_generation(
            connection,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="second",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-second",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=lambda active: authorization_custody.acknowledge_activation_tuple(
                vault,
                expected_control=prior_custody.control,
                target=active,
                now=now + 1,
            ),
        )
    finally:
        connection.close()

    served = policy.load(vault)

    assert served.fingerprint == publication.active.policy_fingerprint
    assert served.rules[0].ceiling == 1


def test_crash_after_tuple_commit_stays_blocked_until_exact_registry_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    prior_custody = authorization_custody.load_authorization_custody(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )

        def crash_after_commit(_active: schema_v4.VerifiedActiveGovernanceState):
            raise RuntimeError("lost registry acknowledgement")

        with pytest.raises(RuntimeError, match="lost registry acknowledgement"):
            schema_v4.publish_policy_generation(
                connection,
                expected=expected,
                policy=_policy_seed(
                    generation_id=SECOND_GENERATION_ID,
                    documents=_documents(ceiling=1),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="ack-crash",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-ack-crash",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=crash_after_commit,
            )

        assert policy.load(vault).blocked
        recovered = schema_v4.recover_registry_acknowledgement(
            connection,
            expected=expected,
            acknowledge_registry=lambda active: authorization_custody.acknowledge_activation_tuple(
                vault,
                expected_control=prior_custody.control,
                target=active,
                now=now + 1,
            ),
        )
        served = policy.load(vault)

        assert recovered.active.activation_epoch == 2
        assert served.rules[0].ceiling == 1
    finally:
        connection.close()


def test_active_reader_pins_one_sqlite_snapshot_across_publication(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    reader = sqlite3.connect(store.sidecar_path(vault))
    writer = store.open_authorization_session_connection(vault)
    try:
        reader.execute("BEGIN")
        predecessor = schema_v4.load_active_policy(
            reader,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_policy_generation(
            writer,
            expected=predecessor.active,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="snapshot",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-snapshot",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )
        still_predecessor = schema_v4.load_active_policy(
            reader,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        reader.commit()
        successor = schema_v4.load_active_policy(
            reader,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=publication.active.activation_epoch,
            expected_activation_state_digest=publication.active.activation_state_digest,
        )

        assert predecessor.policy.rules[0].ceiling == 2
        assert still_predecessor == predecessor
        assert successor.policy.rules[0].ceiling == 1
    finally:
        writer.close()
        reader.close()


def test_policy_publication_crash_before_commit_restores_exact_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )

        def crash(point: str) -> None:
            if point == "policy-publication-before-commit":
                raise RuntimeError("injected tuple publication crash")

        monkeypatch.setattr(schema_v4, "_crash_point", crash)
        with pytest.raises(RuntimeError, match="injected tuple publication crash"):
            schema_v4.publish_policy_generation(
                connection,
                expected=expected,
                policy=_policy_seed(
                    generation_id=SECOND_GENERATION_ID,
                    documents=_documents(ceiling=1),
                    predecessor_generation_id=FIRST_GENERATION_ID,
                    event_suffix="crash",
                    now=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-crash",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        ) == expected
        assert connection.execute(
            "SELECT COUNT(*) FROM compiled_policy_generations "
            "WHERE generation_id=?",
            (SECOND_GENERATION_ID,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE event_id='receipt-crash'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_policy_publication_refuses_noncanonical_compiled_target_before_write(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        invalid = dataclasses.replace(
            _policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="invalid",
                now=now + 1,
            ),
            compiled_policy=b'{"schema":"caller-selected"}',
        )

        with pytest.raises(schema_v4.SchemaV4Error, match="source parity"):
            schema_v4.publish_policy_generation(
                connection,
                expected=expected,
                policy=invalid,
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-invalid",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert not connection.in_transaction
        assert schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        ) == expected
    finally:
        connection.close()


def test_catalog_publication_keeps_the_reviewed_policy_and_advances_one_tuple(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_catalog_generation(
            connection,
            expected=expected,
            catalog=schema_v4.CatalogGenerationSeed(
                catalog_generation=2,
                descriptor=b'{"artifacts":["Notes/new.md"]}',
                artifact_count=1,
                created_at=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-catalog-2",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            receipt_event_id="receipt-catalog-2",
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )
        loaded = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=publication.active.activation_epoch,
            expected_activation_state_digest=publication.active.activation_state_digest,
        )

        assert publication.active.policy_generation_id == FIRST_GENERATION_ID
        assert publication.active.catalog_generation == 2
        assert publication.active.activation_epoch == 2
        assert loaded.policy.rules[0].ceiling == 2
        assert loaded.catalog_descriptor == b'{"artifacts":["Notes/new.md"]}'
    finally:
        connection.close()


def test_semantic_edit_publishes_the_next_v4_catalog_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )
    committed = semantic_writes.commit_existing(vault, preflight=preflight)

    assert committed.mutated is True
    assert target.read_text(encoding="utf-8") == after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    assert active.active.catalog_generation == 2
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=active.policy.fingerprint,
        projector_schema_version=active.active.projector_schema_version,
        catalog_generation=2,
    )
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=key,
        expected_rows_digest=(
            projection_store.namespace_evidence_from_snapshot(active).manifest.rows_digest
        ),
    )
    assert manifest.item_count == 1
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (relative, vault_module.content_hash(after))
    ]


def test_semantic_creation_publishes_the_next_v4_catalog_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/new.md"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    created = create_file_module.create_file(
        vault,
        path=relative,
        content="New governed note.\n",
        frontmatter={"title": "New", "status": "draft"},
        today=dt.date(2026, 8, 25),
    )

    assert created.creation is not None
    source = (vault / relative).read_text(encoding="utf-8")
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    _manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (relative, vault_module.content_hash(source))
    ]


def test_semantic_creation_publishes_index_and_log_auxiliaries_in_one_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/new.md"
    index_relative = "Knowledge Base/index.md"
    log_relative = "Knowledge Base/log.md"
    index_before = "# Knowledge Base\n\n## Recent activity\n"
    log_before = "# Log\n\n---\n"
    for existing, source in (
        (index_relative, index_before),
        (log_relative, log_before),
    ):
        target = vault / existing
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((index_relative, index_before), (log_relative, log_before)),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    created = create_file_module.create_file(
        vault,
        path=relative,
        content="New governed note.\n",
        frontmatter={"title": "New", "status": "draft"},
        today=dt.date(2026, 8, 25),
    )

    assert created.creation is not None
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    expected = {
        path: vault_module.content_hash((vault / path).read_text(encoding="utf-8"))
        for path in (relative, index_relative, log_relative)
    }
    assert active.active.catalog_generation == 2
    assert manifest.item_count == 3
    assert {item.item_identity: item.content_hash for item in items} == expected


def test_markdown_removal_and_log_write_publish_one_v4_catalog_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    log_relative = "Knowledge Base/log.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    log_before = "# Log\n\n---\n"
    for existing, source in ((relative, before), (log_relative, log_before)):
        target = vault / existing
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((relative, before), (log_relative, log_before)),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    def reject_graph_corpus(*_args, **_kwargs):
        raise AssertionError("lexical-only trash must not invoke the graph producer")

    monkeypatch.setattr(
        semantic_contract,
        "build_corpus_context",
        reject_graph_corpus,
    )

    removed = delete_file_module.delete_file(
        vault,
        path=relative,
        confirm=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )

    assert not (vault / relative).exists()
    assert (vault / removed.trash_path).is_file()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    assert active.active.catalog_generation == 2
    assert manifest.item_count == 1
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (
            log_relative,
            vault_module.content_hash((vault / log_relative).read_text(encoding="utf-8")),
        )
    ]


def test_v4_file_trash_removes_inbound_graph_edge_in_one_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    removed_relative = "Knowledge Base/Notes/private.md"
    referrer_relative = "Knowledge Base/Notes/referrer.md"
    log_relative = "Knowledge Base/log.md"
    removed_source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate.\n"
    referrer_source = (
        "---\ntitle: Referrer\nstatus: draft\n---\n\n"
        "See [[Knowledge Base/Notes/private]].\n"
    )
    log_before = "# Log\n\n---\n"
    for path, source in (
        (removed_relative, removed_source),
        (referrer_relative, referrer_source),
        (log_relative, log_before),
    ):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=6))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (removed_relative, removed_source),
            (referrer_relative, referrer_source),
            (log_relative, log_before),
        ),
        ceiling=6,
        graph_edges=(
            projected_graph.ProjectionGraphEdge(
                referrer_relative,
                removed_relative,
                "links_to",
            ),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    removed = delete_file_module.delete_file(
        vault,
        path=removed_relative,
        confirm=True,
        force_orphan=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )

    assert not (vault / removed_relative).exists()
    assert (vault / removed.trash_path).is_file()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    graph_root = next(
        root for root in evidence.required_measurement_roots if root.lane == "graph"
    )
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=evidence.manifest.namespace_key,
        lane=graph_root.lane,
        extractor_version=graph_root.extractor_version,
        model_version=graph_root.model_version,
    )
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    _graph_manifest, graph_rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=namespace,
        family=family,
        expected_rows_digest=graph_root.rows_digest,
    )
    assert all(
        edge.target_item_identity != removed_relative
        for row in graph_rows
        for edge in row.edges
    )


def test_v4_trash_refuses_unsupported_non_markdown_before_moving_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.bin"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"private bytes")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    with pytest.raises(delete_file_module.DeleteFileError) as blocked:
        delete_file_module.delete_file(
            vault,
            path=relative,
            confirm=True,
            today=dt.date(2026, 8, 25),
            now=dt.datetime.fromtimestamp(now),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert blocked.value.reason == "non-Markdown content publication is not available"
    assert target.read_bytes() == b"private bytes"
    assert not (vault / "Knowledge Base/_trash").exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_v4_directory_trash_retires_all_rows_and_publishes_log_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    directory = "Knowledge Base/Notes/private-tree"
    first_relative = f"{directory}/first.md"
    second_relative = f"{directory}/nested/second.md"
    log_relative = "Knowledge Base/log.md"
    first = "---\ntitle: First\nstatus: draft\n---\n\nFirst.\n"
    second = "---\ntitle: Second\nstatus: draft\n---\n\nSecond.\n"
    log_before = "# Log\n\n---\n"
    for relative, source in (
        (first_relative, first),
        (second_relative, second),
        (log_relative, log_before),
    ):
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (first_relative, first),
            (second_relative, second),
            (log_relative, log_before),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    removed = delete_directory_module.delete_directory(
        vault,
        path=directory,
        confirm=True,
        recursive=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )

    assert not (vault / directory).exists()
    assert (vault / removed.trash_path).is_dir()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    assert active.active.catalog_generation == 2
    assert manifest.item_count == 1
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (
            log_relative,
            vault_module.content_hash((vault / log_relative).read_text(encoding="utf-8")),
        )
    ]


def test_v4_directory_trash_removes_inbound_graph_edge_in_one_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    directory = "Knowledge Base/Notes/private-tree"
    removed_relative = f"{directory}/private.md"
    referrer_relative = "Knowledge Base/Notes/referrer.md"
    log_relative = "Knowledge Base/log.md"
    removed_source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate.\n"
    referrer_source = (
        "---\ntitle: Referrer\nstatus: draft\n---\n\n"
        "See [[Knowledge Base/Notes/private-tree/private]].\n"
    )
    log_before = "# Log\n\n---\n"
    for path, source in (
        (removed_relative, removed_source),
        (referrer_relative, referrer_source),
        (log_relative, log_before),
    ):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=6))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (removed_relative, removed_source),
            (referrer_relative, referrer_source),
            (log_relative, log_before),
        ),
        ceiling=6,
        graph_edges=(
            projected_graph.ProjectionGraphEdge(
                referrer_relative,
                removed_relative,
                "links_to",
            ),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    removed = delete_directory_module.delete_directory(
        vault,
        path=directory,
        confirm=True,
        recursive=True,
        force_orphan=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )

    assert not (vault / directory).exists()
    assert (vault / removed.trash_path).is_dir()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    graph_root = next(
        root for root in evidence.required_measurement_roots if root.lane == "graph"
    )
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=evidence.manifest.namespace_key,
        lane=graph_root.lane,
        extractor_version=graph_root.extractor_version,
        model_version=graph_root.model_version,
    )
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    _graph_manifest, graph_rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=namespace,
        family=family,
        expected_rows_digest=graph_root.rows_digest,
    )
    assert all(
        edge.target_item_identity != removed_relative
        for row in graph_rows
        for edge in row.edges
    )


def test_v4_directory_trash_refuses_non_markdown_child_before_moving_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    directory = "Knowledge Base/Notes/private-tree"
    binary = vault / directory / "private.bin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"private bytes")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    with pytest.raises(delete_directory_module.DeleteDirectoryError) as blocked:
        delete_directory_module.delete_directory(
            vault,
            path=directory,
            confirm=True,
            recursive=True,
            today=dt.date(2026, 8, 25),
            now=dt.datetime.fromtimestamp(now),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert blocked.value.reason == "non-Markdown content publication is not available"
    assert binary.read_bytes() == b"private bytes"
    assert not (vault / "Knowledge Base/_trash").exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_v4_directory_trash_does_not_restore_tree_after_publication_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    today = dt.datetime.fromtimestamp(now).date()
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    directory = "Knowledge Base/Notes/private-tree"
    relative = f"{directory}/private.md"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate.\n"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    def lose_publication_terminal(_prepared) -> None:
        raise catalog_publication.CatalogPublicationError("lost terminal")

    monkeypatch.setattr(
        catalog_publication,
        "publish_markdown_batch",
        lose_publication_terminal,
    )

    with pytest.raises(delete_directory_module.DeleteDirectoryError) as uncertain:
        delete_directory_module.delete_directory(
            vault,
            path=directory,
            confirm=True,
            recursive=True,
            today=today,
            now=dt.datetime.fromtimestamp(now),
        )

    assert uncertain.value.code == "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN"
    assert not (vault / directory).exists()
    trash_day = vault / f"Knowledge Base/_trash/{today.isoformat()}"
    assert any(path.is_dir() for path in trash_day.iterdir())


def test_v4_directory_trash_refuses_tree_drift_before_catalog_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    directory = "Knowledge Base/Notes/private-tree"
    relative = f"{directory}/private.md"
    added_relative = f"{directory}/added.md"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate.\n"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    real_begin = graph_sync.begin_deletion_transition

    def drift_then_begin(*args, **kwargs):
        (vault / added_relative).write_text("added concurrently\n", encoding="utf-8")
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(graph_sync, "begin_deletion_transition", drift_then_begin)

    with pytest.raises(delete_directory_module.DeleteDirectoryError) as blocked:
        delete_directory_module.delete_directory(
            vault,
            path=directory,
            confirm=True,
            recursive=True,
            today=dt.date(2026, 8, 25),
            now=dt.datetime.fromtimestamp(now),
        )

    assert blocked.value.code == "PATH_GUARD_CHANGED"
    assert target.read_text(encoding="utf-8") == source
    assert (vault / added_relative).read_text(encoding="utf-8") == "added concurrently\n"
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_v4_file_recovery_inserts_row_and_publishes_log_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    log_relative = "Knowledge Base/log.md"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate.\n"
    log_before = "# Log\n\n---\n"
    for path, content in ((relative, source), (log_relative, log_before)):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((relative, source), (log_relative, log_before)),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    trashed = delete_file_module.delete_file(
        vault,
        path=relative,
        confirm=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )

    recovered = recover_module.recover_from_trash(
        vault,
        trash_path=trashed.trash_path,
        today=dt.date(2026, 8, 25),
    )

    assert recovered.restored_path == relative
    assert (vault / relative).read_text(encoding="utf-8") == source
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 3
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=3,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    expected = {
        path: vault_module.content_hash((vault / path).read_text(encoding="utf-8"))
        for path in (relative, log_relative)
    }
    assert active.active.catalog_generation == 3
    assert manifest.item_count == 2
    assert {item.item_identity: item.content_hash for item in items} == expected


def test_v4_file_recovery_publishes_resolved_graph_successors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    restored_relative = "Knowledge Base/Notes/private.md"
    referrer_relative = "Knowledge Base/Notes/referrer.md"
    log_relative = "Knowledge Base/log.md"
    trash_relative = "Knowledge Base/_trash/2026-08-25/120000-private.md"
    restored_source = "---\ntitle: Private\nstatus: draft\n---\n\nRestored.\n"
    referrer_source = (
        "---\ntitle: Referrer\nstatus: draft\n---\n\n"
        "See [[Knowledge Base/Notes/private]].\n"
    )
    log_before = "# Log\n\n---\n"
    for path, source in (
        (referrer_relative, referrer_source),
        (log_relative, log_before),
        (trash_relative, restored_source),
    ):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    trash = vault / trash_relative
    trash.with_name(f"{trash.name}.meta.json").write_text(
        json.dumps({"original_path": restored_relative}),
        encoding="utf-8",
    )
    _write_workspace(vault, _documents(ceiling=6))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (referrer_relative, referrer_source),
            (log_relative, log_before),
        ),
        ceiling=6,
        graph_edges=(),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    recovered = recover_module.recover_from_trash(
        vault,
        trash_path=trash_relative,
        today=dt.date(2026, 8, 25),
    )

    assert recovered.restored_path == restored_relative
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    graph_root = next(
        root for root in evidence.required_measurement_roots if root.lane == "graph"
    )
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=evidence.manifest.namespace_key,
        lane=graph_root.lane,
        extractor_version=graph_root.extractor_version,
        model_version=graph_root.model_version,
    )
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    _graph_manifest, graph_rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=namespace,
        family=family,
        expected_rows_digest=graph_root.rows_digest,
    )
    assert tuple(
        edge
        for row in graph_rows
        for edge in row.edges
        if edge.source_item_identity == referrer_relative
    ) == (
        projected_graph.ProjectionGraphEdge(
            referrer_relative,
            restored_relative,
            "links_to",
        ),
    )


def test_v4_directory_recovery_inserts_every_row_in_one_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    directory = "Knowledge Base/Notes/private-tree"
    first_relative = f"{directory}/first.md"
    second_relative = f"{directory}/nested/second.md"
    log_relative = "Knowledge Base/log.md"
    first = "---\ntitle: First\nstatus: draft\n---\n\nFirst.\n"
    second = "---\ntitle: Second\nstatus: draft\n---\n\nSecond.\n"
    log_before = "# Log\n\n---\n"
    for path, content in (
        (first_relative, first),
        (second_relative, second),
        (log_relative, log_before),
    ):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (first_relative, first),
            (second_relative, second),
            (log_relative, log_before),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    trashed = delete_directory_module.delete_directory(
        vault,
        path=directory,
        confirm=True,
        recursive=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )

    recovered = recover_module.recover_from_trash(
        vault,
        trash_path=trashed.trash_path,
        today=dt.date(2026, 8, 25),
    )

    assert recovered.restored_path == directory
    assert (vault / first_relative).read_text(encoding="utf-8") == first
    assert (vault / second_relative).read_text(encoding="utf-8") == second
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 3
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=3,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    expected = {
        path: vault_module.content_hash((vault / path).read_text(encoding="utf-8"))
        for path in (first_relative, second_relative, log_relative)
    }
    assert active.active.catalog_generation == 3
    assert manifest.item_count == 3
    assert {item.item_identity: item.content_hash for item in items} == expected


def test_v4_recovery_refuses_non_markdown_before_moving_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    original = "Knowledge Base/Notes/private.bin"
    trash_relative = "Knowledge Base/_trash/2026-08-25/120000-private.bin"
    trash = vault / trash_relative
    trash.parent.mkdir(parents=True, exist_ok=True)
    trash.write_bytes(b"private bytes")
    trash.with_name(f"{trash.name}.meta.json").write_text(
        json.dumps({"original_path": original}),
        encoding="utf-8",
    )
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    with pytest.raises(recover_module.RecoverError) as blocked:
        recover_module.recover_from_trash(
            vault,
            trash_path=trash_relative,
            today=dt.date(2026, 8, 25),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert blocked.value.reason == "non-Markdown content publication is not available"
    assert trash.read_bytes() == b"private bytes"
    assert not (vault / original).exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_v4_recovery_does_not_inverse_rename_after_publication_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    log_relative = "Knowledge Base/log.md"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate.\n"
    log_before = "# Log\n\n---\n"
    for path, content in ((relative, source), (log_relative, log_before)):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((relative, source), (log_relative, log_before)),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    trashed = delete_file_module.delete_file(
        vault,
        path=relative,
        confirm=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )

    def lose_publication_terminal(_prepared) -> None:
        raise catalog_publication.CatalogPublicationError("lost terminal")

    monkeypatch.setattr(
        catalog_publication,
        "publish_markdown_batch",
        lose_publication_terminal,
    )

    with pytest.raises(recover_module.RecoverError) as uncertain:
        recover_module.recover_from_trash(
            vault,
            trash_path=trashed.trash_path,
            today=dt.date(2026, 8, 25),
        )

    assert uncertain.value.code == "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN"
    assert (vault / relative).read_text(encoding="utf-8") == source
    assert not (vault / trashed.trash_path).exists()


def test_v4_directory_recovery_refuses_tree_drift_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    directory = "Knowledge Base/Notes/private-tree"
    relative = f"{directory}/private.md"
    log_relative = "Knowledge Base/log.md"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate.\n"
    log_before = "# Log\n\n---\n"
    for path, content in ((relative, source), (log_relative, log_before)):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=((relative, source), (log_relative, log_before)),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    trashed = delete_directory_module.delete_directory(
        vault,
        path=directory,
        confirm=True,
        recursive=True,
        today=dt.date(2026, 8, 25),
        now=dt.datetime.fromtimestamp(now),
    )
    real_begin = graph_sync.begin_recovery_transition
    added = vault / trashed.trash_path / "added.md"

    def drift_then_begin(*args, **kwargs):
        added.write_text("added concurrently\n", encoding="utf-8")
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(graph_sync, "begin_recovery_transition", drift_then_begin)

    with pytest.raises(recover_module.RecoverError) as blocked:
        recover_module.recover_from_trash(
            vault,
            trash_path=trashed.trash_path,
            today=dt.date(2026, 8, 25),
        )

    assert blocked.value.code == "PATH_GUARD_CHANGED"
    assert not (vault / directory).exists()
    assert (vault / trashed.trash_path / "private.md").read_text(encoding="utf-8") == source
    assert added.read_text(encoding="utf-8") == "added concurrently\n"
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2


def test_v4_move_replaces_membership_and_publishes_auxiliaries_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    old_relative = "Knowledge Base/Notes/private.md"
    new_relative = "Knowledge Base/Notes/renamed-private.md"
    inbound_relative = "Knowledge Base/Notes/referrer.md"
    log_relative = "Knowledge Base/log.md"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nMove me.\n"
    inbound_before = (
        "---\ntitle: Referrer\nstatus: draft\n---\n\n"
        "See [[Knowledge Base/Notes/private]].\n"
    )
    log_before = "# Log\n\n---\n"
    for existing, content in (
        (old_relative, source),
        (inbound_relative, inbound_before),
        (log_relative, log_before),
    ):
        target = vault / existing
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=6))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (old_relative, source),
            (inbound_relative, inbound_before),
            (log_relative, log_before),
        ),
        ceiling=6,
        graph_edges=(
            projected_graph.ProjectionGraphEdge(
                inbound_relative,
                old_relative,
                "links_to",
            ),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    real_prepare_membership = catalog_publication.prepare_catalog_membership_batch
    graph_providers: list[object] = []

    def capture_graph_provider(*args, **kwargs):
        graph_providers.append(kwargs.get("graph_replacement_provider"))
        return real_prepare_membership(*args, **kwargs)

    monkeypatch.setattr(
        catalog_publication,
        "prepare_catalog_membership_batch",
        capture_graph_provider,
    )

    moved = move_file_module.move_file(
        vault,
        old_path=old_relative,
        new_path=new_relative,
        today=dt.date(2026, 8, 25),
    )

    assert moved.wikilinks_updated == 1
    assert graph_providers and callable(graph_providers[-1])
    assert not (vault / old_relative).exists()
    assert (vault / new_relative).read_text(encoding="utf-8") == source
    assert "renamed-private" in (vault / inbound_relative).read_text(encoding="utf-8")
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    expected_paths = (new_relative, inbound_relative, log_relative)
    expected = {
        path: vault_module.content_hash((vault / path).read_text(encoding="utf-8"))
        for path in expected_paths
    }
    assert active.active.catalog_generation == 2
    assert manifest.item_count == len(expected_paths)
    assert {item.item_identity: item.content_hash for item in items} == expected
    graph_root = next(
        root for root in evidence.required_measurement_roots if root.lane == "graph"
    )
    graph_family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=evidence.manifest.namespace_key,
        lane=graph_root.lane,
        extractor_version=graph_root.extractor_version,
        model_version=graph_root.model_version,
    )
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    _graph_manifest, graph_rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=namespace,
        family=graph_family,
        expected_rows_digest=graph_root.rows_digest,
    )
    referrer_edges = tuple(
        edge
        for row in graph_rows
        for edge in row.edges
        if edge.source_item_identity == inbound_relative
    )
    assert referrer_edges == (
        projected_graph.ProjectionGraphEdge(
            inbound_relative,
            new_relative,
            "links_to",
        ),
    )


def test_v4_move_refuses_unsupported_non_markdown_before_moving_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    old_relative = "Knowledge Base/Notes/private.bin"
    new_relative = "Knowledge Base/Notes/renamed-private.bin"
    source = vault / old_relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"private bytes")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    with pytest.raises(move_file_module.MoveFileError) as blocked:
        move_file_module.move_file(
            vault,
            old_path=old_relative,
            new_path=new_relative,
            today=dt.date(2026, 8, 25),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert blocked.value.reason == "non-Markdown content publication is not available"
    assert source.read_bytes() == b"private bytes"
    assert not (vault / new_relative).exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_v4_move_refuses_markdown_to_non_markdown_before_moving_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    old_relative = "Knowledge Base/Notes/private.md"
    new_relative = "Knowledge Base/Notes/private.txt"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nPrivate bytes.\n"
    target = vault / old_relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=old_relative,
        source=source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    with pytest.raises(move_file_module.MoveFileError) as blocked:
        move_file_module.move_file(
            vault,
            old_path=old_relative,
            new_path=new_relative,
            today=dt.date(2026, 8, 25),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert blocked.value.reason == "non-Markdown content publication is not available"
    assert target.read_text(encoding="utf-8") == source
    assert not (vault / new_relative).exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_v4_move_refuses_paired_artifact_until_companion_publication_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    old_binary = "Knowledge Base/Notes/private.bin"
    old_page = f"{old_binary}.md"
    new_binary = "Knowledge Base/Notes/renamed-private.bin"
    new_page = f"{new_binary}.md"
    page_source = (
        "---\ntitle: Private artifact\nstatus: draft\n"
        f"evidence_file: {old_binary}\n---\n\nPrivate artifact.\n"
    )
    binary = vault / old_binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"private bytes")
    (vault / old_page).write_text(page_source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=old_page,
        source=page_source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    with pytest.raises(move_file_module.MoveFileError) as blocked:
        move_file_module.move_file(
            vault,
            old_path=old_page,
            new_path=new_page,
            today=dt.date(2026, 8, 25),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert blocked.value.reason == "non-Markdown content publication is not available"
    assert binary.read_bytes() == b"private bytes"
    assert (vault / old_page).read_text(encoding="utf-8") == page_source
    assert not (vault / new_binary).exists()
    assert not (vault / new_page).exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_v4_move_does_not_undo_bytes_after_catalog_outcome_becomes_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    old_relative = "Knowledge Base/Notes/private.md"
    new_relative = "Knowledge Base/Notes/renamed-private.md"
    source = "---\ntitle: Private\nstatus: draft\n---\n\nMove me.\n"
    target = vault / old_relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=old_relative,
        source=source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    def lose_publication_terminal(_prepared) -> None:
        raise catalog_publication.CatalogPublicationError("lost terminal")

    monkeypatch.setattr(
        catalog_publication,
        "publish_markdown_batch",
        lose_publication_terminal,
    )

    with pytest.raises(move_file_module.MoveFileError) as uncertain:
        move_file_module.move_file(
            vault,
            old_path=old_relative,
            new_path=new_relative,
            today=dt.date(2026, 8, 25),
        )

    assert uncertain.value.code == "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN"
    assert not (vault / old_relative).exists()
    assert (vault / new_relative).read_text(encoding="utf-8") == source


def test_open_vault_skips_v4_only_auxiliary_predecessor_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/legacy.md"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("legacy\n", encoding="utf-8")

    prepared = catalog_publication.prepare_planned_markdown_batch(
        vault,
        writes=(vault_module.PlannedWrite(target, "updated\n"),),
    )

    assert prepared is None


def test_v4_batch_refuses_existing_markdown_without_exact_predecessor_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    with pytest.raises(catalog_publication.CatalogPublicationError) as blocked:
        catalog_publication.prepare_planned_markdown_batch(
            vault,
            writes=(vault_module.PlannedWrite(target, before.replace("before", "after")),),
            now=now + 1,
        )

    assert "exact predecessor" in str(blocked.value)


def test_media_reconciliation_publishes_new_sidecar_in_next_v4_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault, sidecar, _migration = _configure_media_v4(
        tmp_path,
        monkeypatch,
        sidecar_source=None,
        now=now,
    )

    result = media_processing.reconcile_media(vault, sidecar.with_suffix(""))

    assert result is not None
    assert result.sidecar_path == sidecar
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    active, _manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert active.active.catalog_generation == 2
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (
            sidecar.relative_to(vault).as_posix(),
            vault_module.content_hash(sidecar.read_text(encoding="utf-8")),
        )
    ]


def test_scene_frame_bytes_and_companion_publish_one_v4_catalog_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Image:
        size = (1920, 1080)

        def resize(self, _size):
            return self

        def convert(self, _mode):
            return self

        def save(self, target, **_kwargs) -> None:
            target.write(b"\xff\xd8governed-scene")

    now = int(time.time())
    vault = tmp_path / "vault"
    video = vault / "Knowledge Base/Evidence/Video/demo.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"governed video")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    pairs = scene_frames.write_scene_frames(
        vault,
        video,
        [
            (
                embeddings.Scene(
                    start_ts=8.0,
                    end_ts=12.0,
                    rep_ts=10.0,
                    boundary_score=0.5,
                ),
                Image(),
            )
        ],
    )

    assert len(pairs) == 1
    jpg, sidecar = pairs[0]
    assert jpg.read_bytes() == b"\xff\xd8governed-scene"
    companions.classify(vault, jpg.relative_to(vault).as_posix())
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, _manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert active.active.catalog_generation == 2
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (
            sidecar.relative_to(vault).as_posix(),
            vault_module.content_hash(sidecar.read_text(encoding="utf-8")),
        )
    ]

    before = {path: path.read_bytes() for path in (jpg, sidecar)}
    with pytest.raises(catalog_publication.CatalogCommitError) as blocked:
        scene_frames.write_scene_frames(
            vault,
            video,
            [
                (
                    embeddings.Scene(
                        start_ts=38.0,
                        end_ts=42.0,
                        rep_ts=40.0,
                        boundary_score=0.5,
                    ),
                    Image(),
                )
            ],
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert {path: path.read_bytes() for path in (jpg, sidecar)} == before
    assert not list(jpg.parent.glob("*t40000ms.jpg"))
    custody_after = authorization_custody.load_authorization_custody(vault, now=now + 2)
    assert custody_after.control.activation_epoch == 2


def test_scene_frame_batch_replaces_parent_clip_samples_in_same_v4_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Image:
        size = (1920, 1080)

        def resize(self, _size):
            return self

        def convert(self, _mode):
            return self

        def save(self, target, **_kwargs) -> None:
            target.write(b"\xff\xd8governed-scene")

    now = int(time.time())
    vault = tmp_path / "vault"
    video = vault / "Knowledge Base/Evidence/Video/demo.mp4"
    video_path = "Knowledge Base/Evidence/Video/demo.mp4.md"
    video_source = (
        "---\ntitle: Demo video\ntype: source\nstatus: active\n"
        "media_type: video\n---\n\nVideo evidence.\n"
    )
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"governed video")
    (vault / video_path).write_text(video_source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    old_samples = (
        projected_retrieval.ProjectionClipSample(1_000, (1.0, 0.0)),
    )
    new_samples = (
        projected_retrieval.ProjectionClipSample(10_000, (0.0, 1.0)),
    )
    migration, _prior_vectors = _migrate_with_vector_projection_items(
        vault,
        items=((video_path, video_source),),
        clip_samples_by_path={video_path: old_samples},
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [(9.0, 1.0) for _text in texts],
    )

    pairs = scene_frames.write_scene_frames(
        vault,
        video,
        [
            (
                embeddings.Scene(
                    start_ts=8.0,
                    end_ts=12.0,
                    rep_ts=10.0,
                    boundary_score=0.5,
                ),
                Image(),
            )
        ],
        parent_clip_samples=new_samples,
    )

    assert len(pairs) == 1
    _jpg, frame_sidecar = pairs[0]
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert {item.item_identity for item in items} == {
        video_path,
        frame_sidecar.relative_to(vault).as_posix(),
    }
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    assert tuple(root.lane for root in evidence.required_measurement_roots) == (
        "vector",
        "clip",
    )
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    clip_root = next(
        root for root in evidence.required_measurement_roots if root.lane == "clip"
    )
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=namespace.namespace_key,
        lane=clip_root.lane,
        extractor_version=clip_root.extractor_version,
        model_version=clip_root.model_version,
    )
    _clip_manifest, rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=namespace,
        family=family,
        expected_rows_digest=clip_root.rows_digest,
    )
    assert len(rows) == 1
    assert isinstance(rows[0], projected_retrieval.ProjectionClipMeasurement)
    assert rows[0].samples == new_samples


def test_media_extraction_update_publishes_exact_v4_catalog_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    pending = (
        "---\ntitle: Interview\ntype: source\nstatus: draft\n"
        "media_type: audio\nextracted_by: pending\n"
        "processing_state: pending\n---\n\n## Extracted text\n\nPending.\n"
    )
    vault, sidecar, _migration = _configure_media_v4(
        tmp_path,
        monkeypatch,
        sidecar_source=pending,
        now=now,
    )

    preserve.update_sidecar_extraction(
        vault,
        sidecar,
        text="Governed transcript.",
        engine="test-engine",
    )

    after = sidecar.read_text(encoding="utf-8")
    assert "Governed transcript." in after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    active, _manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert active.active.catalog_generation == 2
    assert [(item.item_identity, item.content_hash) for item in items] == [
        (
            sidecar.relative_to(vault).as_posix(),
            vault_module.content_hash(after),
        )
    ]


def test_media_reconciliation_refuses_model_bound_v4_before_sidecar_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault, sidecar, _migration = _configure_media_v4(
        tmp_path,
        monkeypatch,
        sidecar_source=None,
        now=now,
    )
    load_evidence = projection_store.namespace_evidence_from_snapshot

    def model_bound(snapshot):
        return dataclasses.replace(
            load_evidence(snapshot),
            required_measurement_roots=(object(),),
        )

    monkeypatch.setattr(
        projection_store,
        "namespace_evidence_from_snapshot",
        model_bound,
    )

    with pytest.raises(media_processing.MediaProcessingError) as blocked:
        media_processing.reconcile_media(vault, sidecar.with_suffix(""))

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert not sidecar.exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_media_catalog_uncertainty_keeps_committed_sidecar_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    pending = (
        "---\ntitle: Interview\ntype: source\nstatus: draft\n"
        "media_type: audio\nextracted_by: pending\n"
        "processing_state: pending\n---\n\n## Extracted text\n\nPending.\n"
    )
    vault, sidecar, _migration = _configure_media_v4(
        tmp_path,
        monkeypatch,
        sidecar_source=pending,
        now=now,
    )

    def lose_publication_terminal(_prepared) -> None:
        raise catalog_publication.CatalogPublicationError("lost terminal")

    monkeypatch.setattr(
        catalog_publication,
        "publish_markdown_batch",
        lose_publication_terminal,
    )

    with pytest.raises(catalog_publication.CatalogPublicationError, match="lost terminal"):
        preserve.update_sidecar_extraction(
            vault,
            sidecar,
            text="Committed before the terminal was lost.",
            engine="test-engine",
        )

    assert "Committed before the terminal was lost." in sidecar.read_text(encoding="utf-8")
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_media_failure_and_pending_updates_each_publish_a_v4_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    pending = (
        "---\ntitle: Interview\ntype: source\nstatus: draft\n"
        "media_type: audio\nextracted_by: pending\n"
        "processing_state: pending\n---\n\n## Extracted text\n\nPending.\n"
    )
    vault, sidecar, _migration = _configure_media_v4(
        tmp_path,
        monkeypatch,
        sidecar_source=pending,
        now=now,
    )

    preserve.update_sidecar_processing_failure(
        vault,
        sidecar,
        state=media_jobs.BLOCKED,
        attempts=1,
        error="runtime unavailable",
        retryable=True,
        next_action="restore the runtime",
    )
    blocked = sidecar.read_text(encoding="utf-8")
    first = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert first.control.activation_epoch == 2

    assert preserve.update_sidecar_processing_pending(
        vault,
        sidecar,
        attempts=2,
        expected_hash=vault_module.content_hash(blocked),
    )
    after = sidecar.read_text(encoding="utf-8")
    second = authorization_custody.load_authorization_custody(vault, now=now + 2)
    assert second.control.activation_epoch == 3
    active, _manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=3,
        activation_state_digest=second.control.activation_state_digest or "",
    )
    assert active.active.catalog_generation == 3
    assert items[0].content_hash == vault_module.content_hash(after)
    assert "processing_attempts: 2" in after


def test_mark_processing_unavailable_publishes_sidecar_before_job_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    pending = (
        "---\ntitle: Interview\ntype: source\nstatus: draft\n"
        "media_type: audio\nextracted_by: pending\n"
        "processing_state: pending\n---\n\n## Extracted text\n\nPending.\n"
    )
    vault, sidecar, _migration = _configure_media_v4(
        tmp_path,
        monkeypatch,
        sidecar_source=pending,
        now=now,
    )
    binary = sidecar.with_suffix("")
    store_instance = media_jobs.MediaJobStore(vault)
    job_id = store_instance.enqueue(
        media_jobs.MediaJob(
            binary_path=binary,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    changed = media_processing.mark_processing_unavailable(
        vault,
        reason="runtime unavailable",
        next_action="restore the runtime",
    )

    assert changed == 1
    assert store_instance.get(job_id).state == media_jobs.BLOCKED
    after = sidecar.read_text(encoding="utf-8")
    assert "processing_state: blocked" in after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    _active, _manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert items[0].content_hash == vault_module.content_hash(after)


def test_existing_binary_backfill_pages_publish_v4_catalog_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault, media_sidecar, _migration = _configure_media_v4(
        tmp_path,
        monkeypatch,
        sidecar_source=None,
        now=now,
    )

    created_media, media_created = preserve.ensure_media_sidecar(
        vault,
        media_sidecar.with_suffix(""),
        today=dt.date(2026, 8, 25),
    )

    assert media_created and created_media == media_sidecar
    first = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert first.control.activation_epoch == 2

    artifact = vault / "Knowledge Base" / "Evidence" / "Files" / "sample.bin"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"existing artifact")
    artifact_page, page_created = preserve.ensure_artifact_page(
        vault,
        artifact,
        today=dt.date(2026, 8, 25),
    )

    assert page_created
    second = authorization_custody.load_authorization_custody(vault, now=now + 2)
    assert second.control.activation_epoch == 3
    active, _manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=3,
        activation_state_digest=second.control.activation_state_digest or "",
    )
    assert active.active.catalog_generation == 3
    assert {item.item_identity for item in items} == {
        media_sidecar.relative_to(vault).as_posix(),
        artifact_page.relative_to(vault).as_posix(),
    }


def test_preserve_binary_publishes_bound_companion_and_v4_catalog_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    result = preserve.preserve_bytes(
        vault,
        scope="Private",
        category="files",
        filename="payload.bin",
        data=b"governed binary payload",
        today=dt.date(2026, 8, 26),
    )

    bound = companions.classify(vault, result.path)
    assert bound.projects == ()
    assert bound.tags == ()
    assert bound.types == ()
    assert bound.classes == ()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert active.active.catalog_generation == 2
    assert manifest.item_count == 1
    assert {item.item_identity for item in items} == {result.sidecar_path}


def test_preserve_v4_updates_navigation_in_the_same_projection_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    index = kb / "index.md"
    log = kb / "log.md"
    index.write_text("# Knowledge Base\n\n## Recent activity\n\n", encoding="utf-8")
    log.write_text("# Activity log\n\n---\n", encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            ("Knowledge Base/index.md", index.read_text(encoding="utf-8")),
            ("Knowledge Base/log.md", log.read_text(encoding="utf-8")),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    result = preserve.preserve_bytes(
        vault,
        scope="Private",
        category="files",
        filename="with-navigation.bin",
        data=b"governed payload",
        today=dt.date(2026, 8, 26),
    )

    assert "with-navigation.bin" in index.read_text(encoding="utf-8")
    assert "with-navigation.bin" in log.read_text(encoding="utf-8")
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    _active, _manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    assert {item.item_identity for item in items} == {
        result.sidecar_path,
        "Knowledge Base/index.md",
        "Knowledge Base/log.md",
    }


def test_preserve_binary_rebuilds_vector_measurements_before_v4_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration, _prior = _migrate_with_vector_projection_items(
        vault,
        items=(),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    embedded: list[str] = []

    def embed_target(texts: list[str], *, is_query: bool = False):
        assert is_query is False
        embedded.extend(texts)
        return tuple(
            tuple(float(index + 1) for _component in range(embeddings.VECTOR_DIM))
            for index, _text in enumerate(texts)
        )

    monkeypatch.setattr(embeddings, "embed_texts", embed_target)

    result = preserve.preserve_bytes(
        vault,
        scope="Private",
        category="files",
        filename="vector.bin",
        data=b"vector-backed payload",
        text="projection-only searchable text",
        today=dt.date(2026, 8, 26),
    )

    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    root = evidence.required_measurement_roots[0]
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=namespace.namespace_key,
        lane=root.lane,
        extractor_version=root.extractor_version,
        model_version=root.model_version,
    )
    _measurement_manifest, measurements = (
        projection_measurement_store.load_measurement_store(
            vault,
            namespace=namespace,
            family=family,
            expected_rows_digest=root.rows_digest,
        )
    )
    assert {item.item_identity for item in items} == {result.sidecar_path}
    assert len(measurements) == sum(len(item.variants) for item in items)
    assert len(embedded) == len(measurements)


def test_preserve_binary_v4_preflight_failure_leaves_no_canonical_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    def refuse(
        _vault_root: Path,
        *,
        writes,  # noqa: ANN001, ARG001
        graph_replacement_provider,
        now=None,  # noqa: ANN001, ARG001
    ):
        assert callable(graph_replacement_provider)
        raise catalog_publication.CatalogPublicationError("preflight refused")

    monkeypatch.setattr(
        catalog_publication,
        "prepare_planned_markdown_batch",
        refuse,
    )
    with pytest.raises(catalog_publication.CatalogCommitError) as blocked:
        preserve.preserve_bytes(
            vault,
            scope="Private",
            category="files",
            filename="blocked.bin",
            data=b"must not commit",
            today=dt.date(2026, 8, 26),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    folder = vault / "Knowledge Base" / "Evidence" / "Private" / "files"
    assert not (folder / "blocked.bin").exists()
    assert not (folder / "blocked.bin.md").exists()


def test_preserve_binary_v4_catalog_terminal_loss_is_explicitly_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_empty_projection_catalog(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    def lose_terminal(_prepared) -> None:  # noqa: ANN001
        raise catalog_publication.CatalogPublicationError("lost catalog terminal")

    monkeypatch.setattr(
        catalog_publication,
        "publish_markdown_batch",
        lose_terminal,
    )
    with pytest.raises(catalog_publication.CatalogCommitError) as uncertain:
        preserve.preserve_bytes(
            vault,
            scope="Private",
            category="files",
            filename="uncertain.bin",
            data=b"canonical bytes committed",
            today=dt.date(2026, 8, 26),
        )

    assert uncertain.value.code == "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN"
    folder = vault / "Knowledge Base" / "Evidence" / "Private" / "files"
    assert (folder / "uncertain.bin").read_bytes() == b"canonical bytes committed"
    assert (folder / "uncertain.bin.md").exists()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_semantic_edit_publishes_auxiliary_markdown_in_the_same_v4_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    primary_relative = "Knowledge Base/Notes/private.md"
    auxiliary_relative = "Knowledge Base/Notes/backlinks.md"
    primary_before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    primary_after = primary_before.replace("before", "after")
    auxiliary_before = "---\ntitle: Backlinks\nstatus: draft\n---\n\nold link\n"
    auxiliary_after = auxiliary_before.replace("old link", "new link")
    for relative, source in (
        (primary_relative, primary_before),
        (auxiliary_relative, auxiliary_before),
    ):
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (primary_relative, primary_before),
            (auxiliary_relative, auxiliary_before),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    _auxiliary_source, auxiliary_guard = vault_module.read_guarded_text(
        vault,
        vault / auxiliary_relative,
    )
    preflight = semantic_writes.preflight_existing(
        vault,
        path=primary_relative,
        after_source=primary_after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(primary_before),
    )

    committed = semantic_writes.commit_existing(
        vault,
        preflight=preflight,
        auxiliary_writes=(
            vault_module.PlannedWrite(
                vault / auxiliary_relative,
                auxiliary_after,
                guard=auxiliary_guard,
            ),
        ),
    )

    assert committed.mutated is True
    assert (vault / primary_relative).read_text(encoding="utf-8") == primary_after
    assert (vault / auxiliary_relative).read_text(encoding="utf-8") == auxiliary_after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2
    connection = store.open_authorization_session_connection(vault)
    try:
        active = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=2,
            expected_activation_state_digest=custody.control.activation_state_digest or "",
        )
    finally:
        connection.close()
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    manifest, items = projection_store.load_projection_catalog(
        vault,
        key=evidence.manifest.namespace_key,
        expected_rows_digest=evidence.manifest.rows_digest,
    )
    assert active.active.catalog_generation == 2
    assert manifest.item_count == 2
    assert sorted(
        (item.item_identity, item.content_hash) for item in items
    ) == sorted(
        (
            (primary_relative, vault_module.content_hash(primary_after)),
            (auxiliary_relative, vault_module.content_hash(auxiliary_after)),
        )
    )


def test_semantic_edit_rebuilds_only_changed_vectors_before_v4_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    vault = tmp_path / "vault"
    changed_path = "Knowledge Base/Notes/changed.md"
    stable_path = "Knowledge Base/Notes/stable.md"
    changed_before = "---\ntitle: Changed\nstatus: draft\n---\n\nbefore\n"
    changed_after = changed_before.replace("before", "after")
    stable_source = "---\ntitle: Stable\nstatus: draft\n---\n\nstable\n"
    for relative, source in (
        (changed_path, changed_before),
        (stable_path, stable_source),
    ):
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration, prior_measurements = _migrate_with_vector_projection_items(
        vault,
        items=((changed_path, changed_before), (stable_path, stable_source)),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    embedded: list[list[str]] = []

    def embed_target(texts: list[str], *, is_query: bool = False):
        assert is_query is False
        embedded.append(list(texts))
        return tuple((9.0, float(index + 1)) for index, _text in enumerate(texts))

    monkeypatch.setattr(embeddings, "embed_texts", embed_target)
    preflight = semantic_writes.preflight_existing(
        vault,
        path=changed_path,
        after_source=changed_after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(changed_before),
    )

    committed = semantic_writes.commit_existing(vault, preflight=preflight)

    assert committed.mutated is True
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    assert tuple(root.lane for root in evidence.required_measurement_roots) == (
        "vector",
    )
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    root = evidence.required_measurement_roots[0]
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=namespace.namespace_key,
        lane=root.lane,
        extractor_version=root.extractor_version,
        model_version=root.model_version,
    )
    _measurement_manifest, target_measurements = (
        projection_measurement_store.load_measurement_store(
            vault,
            namespace=namespace,
            family=family,
            expected_rows_digest=root.rows_digest,
        )
    )
    stable_variant_ids = {
        variant.projection_variant_id
        for item in items
        if item.item_identity == stable_path
        for variant in item.variants
    }
    prior_by_id = {
        row.measurement_key.projection_variant_id: row.vector
        for row in prior_measurements
    }
    target_by_id = {
        row.measurement_key.projection_variant_id: row.vector
        for row in target_measurements
    }
    assert embedded and sum(len(batch) for batch in embedded) == (
        len(target_measurements) - len(stable_variant_ids)
    )
    assert {
        variant_id: target_by_id[variant_id] for variant_id in stable_variant_ids
    } == {
        variant_id: prior_by_id[variant_id] for variant_id in stable_variant_ids
    }
    assert set(target_by_id) == {
        variant.projection_variant_id
        for item in items
        for variant in item.variants
    }


def test_semantic_edit_carries_complete_clip_family_into_the_same_v4_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    vault = tmp_path / "vault"
    note_path = "Knowledge Base/Notes/changed.md"
    video_path = "Knowledge Base/Evidence/Video/demo.mp4.md"
    note_before = "---\ntitle: Changed\nstatus: draft\n---\n\nbefore\n"
    note_after = note_before.replace("before", "after")
    video_source = (
        "---\ntitle: Demo video\ntype: source\nstatus: active\n"
        "media_type: video\n---\n\nVideo evidence.\n"
    )
    for relative, source in (
        (note_path, note_before),
        (video_path, video_source),
    ):
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    samples = (
        projected_retrieval.ProjectionClipSample(1_000, (1.0, 0.0)),
        projected_retrieval.ProjectionClipSample(8_500, (0.0, 1.0)),
    )
    migration, _prior_vectors = _migrate_with_vector_projection_items(
        vault,
        items=((note_path, note_before), (video_path, video_source)),
        clip_samples_by_path={video_path: samples},
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [(9.0, 1.0) for _text in texts],
    )
    preflight = semantic_writes.preflight_existing(
        vault,
        path=note_path,
        after_source=note_after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(note_before),
    )

    committed = semantic_writes.commit_existing(vault, preflight=preflight)

    assert committed.mutated is True
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    assert tuple(root.lane for root in evidence.required_measurement_roots) == (
        "vector",
        "clip",
    )
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    clip_root = next(
        root for root in evidence.required_measurement_roots if root.lane == "clip"
    )
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=namespace.namespace_key,
        lane=clip_root.lane,
        extractor_version=clip_root.extractor_version,
        model_version=clip_root.model_version,
    )
    _clip_manifest, rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=namespace,
        family=family,
        expected_rows_digest=clip_root.rows_digest,
    )
    assert len(rows) == 1
    assert isinstance(rows[0], projected_retrieval.ProjectionClipMeasurement)
    assert rows[0].samples == samples


def test_video_edit_replaces_clip_samples_in_the_published_v4_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    video_path = "Knowledge Base/Evidence/Video/demo.mp4.md"
    before = (
        "---\ntitle: Demo video\ntype: source\nstatus: active\n"
        "media_type: video\n---\n\nBefore.\n"
    )
    after = before.replace("Before.", "After.")
    target = vault / video_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    old_samples = (
        projected_retrieval.ProjectionClipSample(1_000, (1.0, 0.0)),
    )
    new_samples = (
        projected_retrieval.ProjectionClipSample(1_000, (0.0, 1.0)),
        projected_retrieval.ProjectionClipSample(8_500, (1.0, 0.0)),
    )
    migration, _prior_vectors = _migrate_with_vector_projection_items(
        vault,
        items=((video_path, before),),
        clip_samples_by_path={video_path: old_samples},
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [(9.0, 1.0) for _text in texts],
    )
    prepared = catalog_publication.prepare_markdown_batch(
        vault,
        mutations=(
            catalog_publication.MarkdownCatalogMutation(
                video_path,
                after,
                vault_module.content_hash(before),
            ),
        ),
        clip_replacements=(
            catalog_publication.ClipMeasurementReplacement(
                item_identity=video_path,
                content_hash=vault_module.content_hash(after),
                samples=new_samples,
            ),
        ),
        now=now + 1,
        activated_at=now + 1,
    )
    assert prepared is not None

    target.write_text(after, encoding="utf-8")
    published = catalog_publication.publish_markdown_batch(prepared)

    assert published is not None
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    active, manifest, items = _load_active_projection_items(
        vault,
        activation_epoch=2,
        activation_state_digest=custody.control.activation_state_digest or "",
    )
    evidence = projection_store.namespace_evidence_from_snapshot(active)
    namespace = projection_store.bind_active_projection_namespace(
        active,
        manifest=manifest,
        items=items,
    )
    clip_root = next(
        root for root in evidence.required_measurement_roots if root.lane == "clip"
    )
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=namespace.namespace_key,
        lane=clip_root.lane,
        extractor_version=clip_root.extractor_version,
        model_version=clip_root.model_version,
    )
    _clip_manifest, rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=namespace,
        family=family,
        expected_rows_digest=clip_root.rows_digest,
    )
    assert len(rows) == 1
    assert isinstance(rows[0], projected_retrieval.ProjectionClipMeasurement)
    assert rows[0].samples == new_samples


def test_vector_catalog_preflight_refuses_unknown_measurement_family_before_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration, _measurements = _migrate_with_vector_projection_items(
        vault,
        items=((relative, before),),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    original = projection_store.namespace_evidence_from_snapshot

    def unsupported(snapshot: schema_v4.ActivePolicySnapshot):
        evidence = original(snapshot)
        root = evidence.required_measurement_roots[0]
        return projection_store.ProjectionNamespaceEvidence(
            manifest=evidence.manifest,
            required_measurement_roots=(
                dataclasses.replace(root, model_version="unsupported-model"),
            ),
        )

    monkeypatch.setattr(
        projection_store,
        "namespace_evidence_from_snapshot",
        unsupported,
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as blocked:
        semantic_writes.commit_existing(
            vault,
            preflight=semantic_writes.preflight_existing(
                vault,
                path=relative,
                after_source=after,
                operation="edit",
                expected_before_hash=vault_module.content_hash(before),
            ),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert target.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("failure_mode", ["hard-off", "model-error"])
def test_vector_rebuild_failure_refuses_catalog_mutation_before_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration, _measurements = _migrate_with_vector_projection_items(
        vault,
        items=((relative, before),),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    def fail_rebuild(_texts: list[str], *, is_query: bool = False):
        assert is_query is False
        raise RuntimeError("model unavailable")

    if failure_mode == "model-error":
        monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        monkeypatch.setattr(embeddings, "embed_texts", fail_rebuild)
    else:
        monkeypatch.setattr(
            embeddings,
            "embed_texts",
            lambda *_args, **_kwargs: pytest.fail(
                "hard-off publication must not call the vector model"
            ),
        )

    with pytest.raises(semantic_writes.SemanticWriteError) as blocked:
        semantic_writes.commit_existing(
            vault,
            preflight=semantic_writes.preflight_existing(
                vault,
                path=relative,
                after_source=after,
                operation="edit",
                expected_before_hash=vault_module.content_hash(before),
            ),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert target.read_text(encoding="utf-8") == before
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1
    assert custody.control.activation_state_digest == migration.activation_state_digest


def test_vector_catalog_preparation_does_not_publish_an_abandoned_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration, _measurements = _migrate_with_vector_projection_items(
        vault,
        items=((relative, before),),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query=False: tuple(
            (9.0, float(index + 1)) for index, _text in enumerate(texts)
        ),
    )

    prepared = catalog_publication.prepare_markdown_upsert(
        vault,
        path=relative,
        source=after,
        expected_before_hash=vault_module.content_hash(before),
        now=now + 1,
    )

    assert prepared is not None
    assert len(prepared.target_measurements) == 1
    family = prepared.target_measurements[0].family
    assert not projection_measurement_store.measurement_store_path(
        vault,
        family,
    ).exists()
    assert target.read_text(encoding="utf-8") == before


def test_semantic_edit_refuses_auxiliary_catalog_drift_before_changing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    primary_relative = "Knowledge Base/Notes/private.md"
    auxiliary_relative = "Knowledge Base/Notes/backlinks.md"
    primary_before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    primary_after = primary_before.replace("before", "after")
    auxiliary_active = "---\ntitle: Backlinks\nstatus: draft\n---\n\nactive\n"
    auxiliary_drifted = auxiliary_active.replace("active", "out-of-band")
    auxiliary_requested = auxiliary_drifted.replace("out-of-band", "requested")
    for relative, source in (
        (primary_relative, primary_before),
        (auxiliary_relative, auxiliary_active),
    ):
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_items(
        vault,
        items=(
            (primary_relative, primary_before),
            (auxiliary_relative, auxiliary_active),
        ),
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    (vault / auxiliary_relative).write_text(auxiliary_drifted, encoding="utf-8")
    _auxiliary_source, auxiliary_guard = vault_module.read_guarded_text(
        vault,
        vault / auxiliary_relative,
    )
    preflight = semantic_writes.preflight_existing(
        vault,
        path=primary_relative,
        after_source=primary_after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(primary_before),
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as blocked:
        semantic_writes.commit_existing(
            vault,
            preflight=preflight,
            auxiliary_writes=(
                vault_module.PlannedWrite(
                    vault / auxiliary_relative,
                    auxiliary_requested,
                    guard=auxiliary_guard,
                ),
            ),
        )

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert "content identity" in blocked.value.reason
    assert (vault / primary_relative).read_text(encoding="utf-8") == primary_before
    assert (vault / auxiliary_relative).read_text(encoding="utf-8") == auxiliary_drifted
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_semantic_edit_refuses_catalog_drift_before_changing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    active_source = "---\ntitle: Private\nstatus: draft\n---\n\nactive\n"
    drifted_source = active_source.replace("active", "out-of-band")
    requested_source = drifted_source.replace("out-of-band", "requested")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(active_source, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=active_source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    target.write_text(drifted_source, encoding="utf-8")
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=requested_source,
        operation="edit",
        expected_before_hash=vault_module.content_hash(drifted_source),
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as blocked:
        semantic_writes.commit_existing(vault, preflight=preflight)

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert target.read_text(encoding="utf-8") == drifted_source
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_semantic_edit_recovers_lost_catalog_registry_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    real_crash_point = schema_v4._crash_point
    crashed = False

    def lose_first_ack(point: str) -> None:
        nonlocal crashed
        if point == "catalog-publication-after-commit-before-registry" and not crashed:
            crashed = True
            raise RuntimeError("lost catalogue acknowledgement")
        real_crash_point(point)

    monkeypatch.setattr(schema_v4, "_crash_point", lose_first_ack)
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )

    committed = semantic_writes.commit_existing(vault, preflight=preflight)

    assert crashed is True
    assert committed.mutated is True
    assert target.read_text(encoding="utf-8") == after
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 2


def test_semantic_edit_refuses_model_namespace_before_changing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    after = before.replace("before", "after")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    real_evidence = projection_store.namespace_evidence_from_snapshot

    def model_bound(snapshot):
        evidence = real_evidence(snapshot)
        return dataclasses.replace(
            evidence,
            required_measurement_roots=(object(),),
        )

    monkeypatch.setattr(
        projection_store,
        "namespace_evidence_from_snapshot",
        model_bound,
    )
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=after,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as blocked:
        semantic_writes.commit_existing(vault, preflight=preflight)

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert target.read_text(encoding="utf-8") == before
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    assert custody.control.activation_epoch == 1


def test_abandoned_content_preparation_does_not_poison_next_catalog_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state")
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    relative = "Knowledge Base/Notes/private.md"
    before = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
    first_candidate = before.replace("before", "abandoned")
    selected_candidate = before.replace("before", "selected")
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path=relative,
        source=before,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )

    abandoned = catalog_publication.prepare_markdown_upsert(
        vault,
        path=relative,
        source=first_candidate,
        expected_before_hash=vault_module.content_hash(before),
        now=now + 1,
    )
    selected = catalog_publication.prepare_markdown_upsert(
        vault,
        path=relative,
        source=selected_candidate,
        expected_before_hash=vault_module.content_hash(before),
        now=now + 1,
    )

    assert abandoned is not None and selected is not None
    assert abandoned.target_key == selected.target_key
    assert not projection_store.variant_store_path(
        vault, selected.target_key
    ).exists()
    preflight = semantic_writes.preflight_existing(
        vault,
        path=relative,
        after_source=selected_candidate,
        operation="edit",
        expected_before_hash=vault_module.content_hash(before),
    )
    committed = semantic_writes.commit_existing(vault, preflight=preflight)
    assert committed.mutated is True
    assert target.read_text(encoding="utf-8") == selected_candidate


def test_policy_and_catalog_publications_from_one_predecessor_have_one_winner(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    policy_writer = store.open_authorization_session_connection(vault)
    catalog_writer = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            policy_writer,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        schema_v4.publish_policy_generation(
            policy_writer,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="policy-race",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-policy-race",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )

        with pytest.raises(schema_v4.ActiveTupleStale):
            schema_v4.publish_catalog_generation(
                catalog_writer,
                expected=expected,
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=2,
                    descriptor=b'{"artifacts":["Notes/loser.md"]}',
                    artifact_count=1,
                    created_at=now + 1,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="namespace-catalog-loser",
                    evidence=b'{"ready":true}',
                    ready_at=now + 1,
                ),
                receipt_event_id="receipt-catalog-loser",
                activated_at=now + 1,
                acknowledge_registry=_acknowledge,
            )

        assert policy_writer.execute(
            "SELECT COUNT(*) FROM catalog_generation_descriptors "
            "WHERE catalog_generation=2"
        ).fetchone() == (0,)
        assert policy_writer.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE event_id='receipt-catalog-loser'"
        ).fetchone() == (0,)
    finally:
        catalog_writer.close()
        policy_writer.close()


def test_active_reader_refuses_corrupt_publication_predecessor(tmp_path: Path) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        expected = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=LOGICAL_VAULT_ID,
            expected_activation_store_id=ACTIVATION_STORE_ID,
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
        publication = schema_v4.publish_policy_generation(
            connection,
            expected=expected,
            policy=_policy_seed(
                generation_id=SECOND_GENERATION_ID,
                documents=_documents(ceiling=1),
                predecessor_generation_id=FIRST_GENERATION_ID,
                event_suffix="corrupt-predecessor",
                now=now + 1,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id="namespace-corrupt-predecessor",
                evidence=b'{"ready":true}',
                ready_at=now + 1,
            ),
            activated_at=now + 1,
            acknowledge_registry=_acknowledge,
        )
        connection.execute("DROP TRIGGER governance_tuple_publications_no_update")
        connection.execute(
            "UPDATE governance_tuple_publications "
            "SET predecessor_activation_state_digest=? WHERE activation_epoch=2",
            ("f" * 64,),
        )
        connection.commit()

        with pytest.raises(schema_v4.SchemaV4Error, match="activation state"):
            schema_v4.load_active_state(
                connection,
                expected_logical_vault_id=LOGICAL_VAULT_ID,
                expected_activation_store_id=ACTIVATION_STORE_ID,
                expected_activation_epoch=2,
                expected_activation_state_digest=(
                    publication.active.activation_state_digest
                ),
            )
    finally:
        connection.close()


def test_external_activation_digest_binds_projection_namespace_bytes(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        connection.execute(
            "DROP TRIGGER governance_projection_namespaces_no_update"
        )
        evidence = b'{"ready":false,"tampered":true}'
        namespace_digest = schema_v4._framed_digest(
            b"exomem.authorization-projection-namespace.v1",
            _compiled(_documents(ceiling=2)).fingerprint.encode("ascii"),
            b"1",
            b"1",
            b"namespace-first",
            evidence,
            str(now).encode("ascii"),
        )
        connection.execute(
            "UPDATE governance_projection_namespaces "
            "SET evidence=?, namespace_digest=? WHERE namespace_id='namespace-first'",
            (evidence, namespace_digest),
        )
        connection.commit()

        with pytest.raises(schema_v4.SchemaV4Error, match="activation state"):
            schema_v4.load_active_state(
                connection,
                expected_logical_vault_id=LOGICAL_VAULT_ID,
                expected_activation_store_id=ACTIVATION_STORE_ID,
                expected_activation_epoch=1,
                expected_activation_state_digest=migration.activation_state_digest,
            )
    finally:
        connection.close()


def test_v4_policy_loader_reuses_only_exact_pinned_source_compiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate(vault, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    policy._compile_pinned_documents.cache_clear()
    original = policy._compile_document_bytes
    calls = 0

    def counting_compile(documents: dict[str, bytes]):
        nonlocal calls
        calls += 1
        return original(documents)

    monkeypatch.setattr(policy, "_compile_document_bytes", counting_compile)

    assert policy.load(vault).rules[0].ceiling == 2
    assert policy.load(vault).rules[0].ceiling == 2
    assert calls == 1

    _write_workspace(vault, _documents(ceiling=1))

    assert policy.load(vault).rules[0].ceiling == 2
    assert calls == 2
