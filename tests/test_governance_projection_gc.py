"""Exact-tuple retention and collection for authorization projections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import (
    projection_gc,
    projection_runtime,
    projection_store,
    projections,
    schema_v4,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy
from exomem.governance.principal import RequestPrincipal


def _key(generation: int) -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey("a" * 64, 1, generation)


def _items(key: projections.ProjectionNamespaceKey):
    variant = projections.build_projection_variant(
        item_identity="Knowledge Base/visible.md",
        content_hash=f"{key.catalog_generation:064x}",
        decision=Decision(level=6),
        projector_schema_version=key.projector_schema_version,
        full_search_fields={"body": "visible projected text"},
    )
    assert variant is not None
    return (
        projection_store.ProjectionItemVariants(
            item_identity=variant.item_identity,
            content_hash=variant.content_hash,
            variants=(variant,),
        ),
    )


def _runtime(key: projections.ProjectionNamespaceKey):
    items = _items(key)
    namespace = verified_namespace(key, items)
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            logical_vault_id="fixture-vault",
            activation_store_id="fixture-store",
            activation_epoch=key.catalog_generation,
            activation_state_digest=namespace.active_state_digest,
            policy_generation_id="fixture-policy",
            policy_fingerprint=key.policy_fingerprint,
            projector_schema_version=key.projector_schema_version,
            catalog_generation=key.catalog_generation,
            projection_namespace_id=key.namespace_id,
        ),
        policy=Policy(fingerprint=key.policy_fingerprint),
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, items),
        projection_namespace_evidence=(
            projection_store.projection_namespace_evidence_bytes(namespace.manifest)
        ),
    )
    return projection_runtime.ActiveProjectionRuntime(snapshot, namespace)


def _v4_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    schema_v4._create_v4_schema(connection)
    connection.execute(
        "CREATE TABLE governance_proposals ("
        "proposal_id TEXT PRIMARY KEY, proposal_json TEXT NOT NULL, status TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE governance_operation_journals ("
        "event_id TEXT PRIMARY KEY, phase TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE governance_operation_components ("
        "event_id TEXT NOT NULL, component_kind TEXT NOT NULL, value_json TEXT NOT NULL)"
    )
    connection.execute("PRAGMA user_version=4")
    return connection


def _insert_active_namespace(
    connection: sqlite3.Connection,
    key: projections.ProjectionNamespaceKey,
) -> None:
    connection.execute(
        "INSERT INTO governance_projection_namespaces "
        "(policy_fingerprint, projector_schema_version, catalog_generation, "
        "namespace_id, namespace_digest, evidence, ready_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            key.policy_fingerprint,
            key.projector_schema_version,
            key.catalog_generation,
            key.namespace_id,
            "b" * 64,
            b"{}",
            1,
        ),
    )
    connection.execute(
        "INSERT INTO active_governance_tuple "
        "(singleton, policy_generation_id, policy_fingerprint, "
        "projector_schema_version, catalog_generation) VALUES (1, ?, ?, ?, ?)",
        ("policy-active", *key.as_tuple()),
    )


def test_durable_pins_cover_active_pending_policy_and_open_catalog_journal() -> None:
    active = _key(7)
    policy_target = _key(8)
    catalog_target = _key(9)
    connection = _v4_connection()
    _insert_active_namespace(connection, active)
    proposal = {
        "authority_binding": {
            "reviewed_active_tuple": {
                "projection_namespace_id": active.namespace_id,
            },
            "target": {
                "projection_namespace": {"namespace_id": policy_target.namespace_id},
            },
        }
    }
    connection.execute(
        "INSERT INTO governance_proposals (proposal_id, proposal_json, status) "
        "VALUES (?, ?, 'pending')",
        ("proposal", json.dumps(proposal)),
    )
    connection.execute(
        "INSERT INTO governance_operation_journals (event_id, phase) "
        "VALUES (?, 'pending')",
        ("event",),
    )
    connection.execute(
        "INSERT INTO governance_operation_components "
        "(event_id, component_kind, value_json) VALUES (?, 'catalog', ?)",
        (
            "event",
            json.dumps({"projection_namespace_id": catalog_target.namespace_id}),
        ),
    )

    assert schema_v4.projection_namespace_pins(connection) == frozenset(
        {active.namespace_id, policy_target.namespace_id, catalog_target.namespace_id}
    )


def test_malformed_open_recovery_state_refuses_pin_snapshot() -> None:
    connection = _v4_connection()
    _insert_active_namespace(connection, _key(7))
    connection.execute(
        "INSERT INTO governance_proposals (proposal_id, proposal_json, status) "
        "VALUES (?, ?, 'pending')",
        ("proposal", "{}"),
    )

    with pytest.raises(schema_v4.SchemaV4Error, match="namespace pins"):
        schema_v4.projection_namespace_pins(connection)


def test_live_request_and_continuation_pin_their_exact_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(_key(7))
    projection_runtime._clear_projected_continuations_for_tests()
    projection_runtime._clear_projection_request_pins_for_tests()

    with projection_runtime._projection_runtime_request_scope(
        tmp_path,
        runtime,
        continuation=None,
    ) as (selected, record):
        assert selected is runtime
        assert record is None
        assert projection_runtime.projection_namespace_runtime_pins(tmp_path) == (
            frozenset({runtime.namespace.namespace_key.namespace_id})
        )

    assert projection_runtime.projection_namespace_runtime_pins(tmp_path) == frozenset()

    monkeypatch.setattr(projection_runtime.time, "monotonic", lambda: 100.0)
    projection_runtime._register_projected_continuation(
        tmp_path,
        runtime=runtime,
        principal_binding=("external", None),
        declared_purpose=None,
        request_digest="1" * 64,
        authorization_digest="2" * 64,
        visible_authorization_digest="3" * 64,
        visible_snapshot_digest="4" * 64,
        next_offset=1,
        candidate_depth=1,
        replace_existing=False,
    )
    assert projection_runtime.projection_namespace_runtime_pins(tmp_path) == frozenset(
        {runtime.namespace.namespace_key.namespace_id}
    )

    monkeypatch.setattr(
        projection_runtime.time,
        "monotonic",
        lambda: 100.0 + projection_runtime._PROJECTED_CONTINUATION_TTL_S,
    )
    assert projection_runtime.projection_namespace_runtime_pins(tmp_path) == frozenset()


def test_public_projected_find_holds_and_releases_the_request_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(_key(7))
    projection_runtime._clear_projected_continuations_for_tests()
    projection_runtime._clear_projection_request_pins_for_tests()
    observed: list[frozenset[str]] = []

    def fail_while_pinned(*_args, **_kwargs):
        observed.append(projection_runtime.projection_namespace_runtime_pins(tmp_path))
        raise RuntimeError("injected projected request failure")

    monkeypatch.setattr(
        projection_runtime,
        "_find_projected_hits_pinned",
        fail_while_pinned,
    )
    with pytest.raises(RuntimeError, match="injected projected request failure"):
        projection_runtime.find_projected_hits(
            tmp_path,
            runtime,
            query="visible",
            limit=10,
            mode="keyword",
            graph=False,
            rerank=False,
            principal=RequestPrincipal("external", resolved=True),
            purpose=None,
        )

    assert observed == [frozenset({runtime.namespace.namespace_key.namespace_id})]
    assert projection_runtime.projection_namespace_runtime_pins(tmp_path) == frozenset()


def test_exact_tuple_collector_retains_every_authoritative_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _key(7)
    cursor = _key(8)
    rollback = _key(9)
    stale = _key(10)
    staged = _key(11)
    for key in (active, cursor, rollback, stale, staged):
        projection_store.stage_variant_store(tmp_path, key=key, items=_items(key))
    stale_measurement = (
        projection_store.variant_store_path(tmp_path, stale).parent
        / "measurements"
        / "vector"
        / ("c" * 64)
        / "rows.sqlite"
    )
    stale_measurement.parent.mkdir(parents=True)
    stale_measurement.write_bytes(b"closed fixture")

    monkeypatch.setattr(
        projection_gc,
        "_durable_namespace_snapshot",
        lambda _root: (
            frozenset(key.namespace_id for key in (active, cursor, rollback, stale)),
            frozenset({active.namespace_id}),
        ),
    )
    monkeypatch.setattr(
        projection_runtime,
        "projection_namespace_runtime_pins",
        lambda _root: frozenset({cursor.namespace_id}),
    )

    collected = projection_gc.collect_unpinned_projection_namespaces(
        tmp_path,
        retained_namespace_ids=frozenset({rollback.namespace_id}),
    )

    assert collected == (stale.namespace_id,)
    for key in (active, cursor, rollback):
        assert projection_store.variant_store_path(tmp_path, key).is_file()
    assert projection_store.variant_store_path(tmp_path, staged).is_file()
    assert not projection_store.variant_store_path(tmp_path, stale).exists()
    assert not projection_store.variant_store_path(tmp_path, stale).parent.exists()
