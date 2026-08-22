from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from exomem.governance import authorization_sessions, schema_v4, store


def _v3_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    store._migrate(connection)
    connection.commit()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    return connection


def _seed(**changes: object) -> schema_v4.MigrationSeed:
    policy = schema_v4.PolicyGenerationSeed(
        generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        source_documents=(
            (
                "rules/release.yaml",
                b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n",
            ),
        ),
        source_fingerprint="1" * 64,
        conflict_digest="2" * 64,
        compiled_policy=b'{"rules":[]}',
        policy_fingerprint="3" * 64,
        compiler_schema_version=1,
        projector_schema_version=1,
        predecessor_generation_id=None,
        authoring_event_id="event-migration-policy",
        receipt_event_id="receipt-migration-policy",
        created_at=1_800_000_000,
    )
    catalog = schema_v4.CatalogGenerationSeed(
        catalog_generation=7,
        descriptor=b'{"artifacts":[]}',
        artifact_count=0,
        created_at=1_800_000_000,
    )
    namespace = schema_v4.ProjectionNamespaceSeed(
        namespace_id="projection-namespace-7",
        evidence=b'{"ready":true}',
        ready_at=1_800_000_000,
    )
    values: dict[str, object] = {
        "activation_store_id": "activation-store-7",
        "logical_vault_id": "logical-vault-7",
        "activation_epoch": 1,
        "policy": policy,
        "catalog": catalog,
        "namespace": namespace,
        "migrated_at": 1_800_000_000,
    }
    values.update(changes)
    return schema_v4.MigrationSeed(**values)  # type: ignore[arg-type]


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _insert_legacy_authority(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO governance_session_grants "
        "(grant_id, authorization_session, audience, purpose, ceiling, paths, "
        "fingerprints, token_jti, status, created_at, expires_at, "
        "membership_manifest, policy_fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "grant-v3",
            "legacy-session-handle",
            "external",
            "support",
            5,
            '["Notes/a.md"]',
            '["4"]',
            "token-v3",
            "active",
            1_700_000_000,
            1_900_000_000,
            '[{"path":"Notes/a.md","scope_ids":["scope-a"]}]',
            "5" * 64,
        ),
    )
    connection.execute(
        "INSERT INTO governance_session_purpose "
        "(authorization_session, principal_id, purpose, status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "legacy-session-handle",
            "external",
            "support",
            "active",
            1_700_000_000,
            1_900_000_000,
        ),
    )
    connection.execute(
        "INSERT INTO withhold_tokens "
        "(jti, audience, max_level, fingerprints, paths, expires_at, minted_at, "
        "authorization_session, purpose, org_ceiling, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "token-v3",
            "external",
            5,
            '["4"]',
            '["Notes/a.md"]',
            1_900_000_000,
            1_700_000_000,
            "legacy-session-handle",
            "support",
            6,
            "active",
        ),
    )
    connection.commit()


def test_exact_v3_to_v4_migration_is_atomic_and_expires_legacy_authority() -> None:
    connection = _v3_connection()
    _insert_legacy_authority(connection)

    result = schema_v4.migrate_v3_connection(connection, _seed())

    assert result.schema_version == 4
    assert result.activation_store_id == "activation-store-7"
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _columns(connection, "governance_authorization_sessions") == (
        "session_id",
        "locator_digest",
        "verifier",
        "verifier_key_id",
        "credential_generation",
        "principal_id",
        "issuer_family",
        "cell_id",
        "logical_vault_id",
        "keyring_id",
        "status",
        "created_at",
        "rotated_at",
        "expires_at",
        "closed_at",
    )
    assert _columns(connection, "compiled_policy_generations") == (
        "generation_id",
        "source_documents",
        "source_fingerprint",
        "conflict_digest",
        "compiled_policy",
        "policy_fingerprint",
        "compiler_schema_version",
        "projector_schema_version",
        "predecessor_generation_id",
        "authoring_event_id",
        "receipt_event_id",
        "immutable_row_digest",
        "created_at",
    )
    assert _columns(connection, "catalog_generation_descriptors") == (
        "catalog_generation",
        "descriptor",
        "descriptor_digest",
        "artifact_count",
        "created_at",
    )
    assert _columns(connection, "governance_projection_namespaces") == (
        "policy_fingerprint",
        "projector_schema_version",
        "catalog_generation",
        "namespace_id",
        "namespace_digest",
        "evidence",
        "ready_at",
    )
    assert _columns(connection, "active_governance_tuple") == (
        "singleton",
        "policy_generation_id",
        "policy_fingerprint",
        "projector_schema_version",
        "catalog_generation",
    )
    assert _columns(connection, "governance_activation_store") == (
        "singleton",
        "activation_store_id",
        "logical_vault_id",
        "activation_epoch",
        "activation_state_digest",
    )
    assert _columns(connection, "governance_session_grants") == (
        "grant_id",
        "authorization_session_id",
        "principal_id",
        "issuer_family",
        "audience",
        "purpose",
        "ceiling",
        "paths",
        "fingerprints",
        "scope_ids",
        "membership_manifest",
        "policy_fingerprint",
        "token_jti",
        "status",
        "prepared_event_id",
        "created_at",
        "expires_at",
        "revoked_at",
    )
    assert _columns(connection, "governance_session_purpose") == (
        "authorization_session_id",
        "principal_id",
        "issuer_family",
        "audience",
        "purpose",
        "status",
        "prepared_event_id",
        "created_at",
        "expires_at",
    )
    assert _columns(connection, "withhold_tokens") == (
        "jti",
        "authorization_session_id",
        "principal_id",
        "issuer_family",
        "audience",
        "max_level",
        "fingerprints",
        "paths",
        "scope_ids",
        "purpose",
        "org_ceiling",
        "status",
        "prepared_event_id",
        "expires_at",
        "minted_at",
        "consumed_at",
    )
    for table in (
        "governance_authorization_sessions",
        "governance_session_grants",
        "governance_session_purpose",
        "governance_session_purpose_staging",
        "withhold_tokens",
    ):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    archived = connection.execute(
        "SELECT source_table, source_key, reason FROM governance_legacy_authority "
        "ORDER BY source_table"
    ).fetchall()
    assert archived == [
        ("governance_session_grants", "grant-v3", "v3-unbound-authorization"),
        (
            "governance_session_purpose",
            "legacy-session-handle",
            "v3-unbound-authorization",
        ),
        ("withhold_tokens", "token-v3", "v3-unbound-authorization"),
    ]


def test_v4_seed_publishes_one_complete_tuple_and_activation_digest() -> None:
    connection = _v3_connection()

    result = schema_v4.migrate_v3_connection(connection, _seed())

    policy = connection.execute(
        "SELECT generation_id, policy_fingerprint, immutable_row_digest "
        "FROM compiled_policy_generations"
    ).fetchone()
    catalog = connection.execute(
        "SELECT catalog_generation, descriptor_digest FROM catalog_generation_descriptors"
    ).fetchone()
    namespace = connection.execute(
        "SELECT namespace_id, namespace_digest FROM governance_projection_namespaces"
    ).fetchone()
    active = connection.execute("SELECT * FROM active_governance_tuple").fetchone()
    activation = connection.execute("SELECT * FROM governance_activation_store").fetchone()

    assert policy is not None and len(policy[2]) == 64
    assert catalog is not None and len(catalog[1]) == 64
    assert namespace is not None and len(namespace[1]) == 64
    assert active == (1, policy[0], policy[1], 1, catalog[0])
    assert activation == (
        1,
        "activation-store-7",
        "logical-vault-7",
        1,
        result.activation_state_digest,
    )
    assert result.activation_state_digest == schema_v4.activation_state_digest(
        logical_vault_id="logical-vault-7",
        activation_store_id="activation-store-7",
        activation_epoch=1,
        policy_generation_id=str(policy[0]),
        policy_fingerprint=str(policy[1]),
        policy_row_digest=str(policy[2]),
        projector_schema_version=1,
        catalog_generation=int(catalog[0]),
        catalog_descriptor_digest=str(catalog[1]),
        projection_namespace_identity=str(namespace[0]),
    )


def test_activation_state_digest_has_a_fixed_cross_runtime_vector() -> None:
    assert (
        schema_v4.activation_state_digest(
            logical_vault_id="logical-vault-7",
            activation_store_id="activation-store-7",
            activation_epoch=1,
            policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            policy_fingerprint="3" * 64,
            policy_row_digest="4" * 64,
            projector_schema_version=1,
            catalog_generation=7,
            catalog_descriptor_digest="5" * 64,
            projection_namespace_identity="projection-namespace-7",
        )
        == "d6f146e6d87c02e0ee1e093e908f516dd9d8bd652603730b1a80017c1a0c5bad"
    )


def test_v4_active_state_verifies_the_complete_external_tuple() -> None:
    connection = _v3_connection()
    result = schema_v4.migrate_v3_connection(connection, _seed())

    active = schema_v4.load_active_state(
        connection,
        expected_logical_vault_id="logical-vault-7",
        expected_activation_store_id="activation-store-7",
        expected_activation_epoch=1,
        expected_activation_state_digest=result.activation_state_digest,
    )

    assert active == schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="logical-vault-7",
        activation_store_id="activation-store-7",
        activation_epoch=1,
        activation_state_digest=result.activation_state_digest,
        policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        policy_fingerprint="3" * 64,
        projector_schema_version=1,
        catalog_generation=7,
        projection_namespace_id="projection-namespace-7",
    )


@pytest.mark.parametrize(
    "corruption",
    ("policy-bytes", "namespace-missing", "external-digest"),
)
def test_v4_active_state_refuses_internal_or_external_tuple_drift(
    corruption: str,
) -> None:
    connection = _v3_connection()
    result = schema_v4.migrate_v3_connection(connection, _seed())
    expected_digest = result.activation_state_digest
    if corruption == "policy-bytes":
        connection.execute("DROP TRIGGER compiled_policy_generations_no_update")
        connection.execute(
            "UPDATE compiled_policy_generations SET compiled_policy=?",
            (b'{"rules":["tampered"]}',),
        )
    elif corruption == "namespace-missing":
        connection.execute("DROP TRIGGER governance_projection_namespaces_no_delete")
        connection.execute("DELETE FROM governance_projection_namespaces")
    else:
        expected_digest = "f" * 64
    connection.commit()

    with pytest.raises(schema_v4.SchemaV4Error, match="activation state"):
        schema_v4.load_active_state(
            connection,
            expected_logical_vault_id="logical-vault-7",
            expected_activation_store_id="activation-store-7",
            expected_activation_epoch=1,
            expected_activation_state_digest=expected_digest,
        )


@pytest.mark.parametrize(
    "table",
    (
        "compiled_policy_generations",
        "catalog_generation_descriptors",
        "governance_projection_namespaces",
        "governance_schema_migrations",
    ),
)
def test_v4_generation_rows_are_append_only(table: str) -> None:
    connection = _v3_connection()
    schema_v4.migrate_v3_connection(connection, _seed())

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(f"UPDATE {table} SET rowid=rowid")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(f"DELETE FROM {table}")


def test_v4_migration_retry_requires_the_exact_same_seed() -> None:
    connection = _v3_connection()
    first = schema_v4.migrate_v3_connection(connection, _seed())

    replay = schema_v4.migrate_v3_connection(connection, _seed())

    assert replay == first
    with pytest.raises(schema_v4.SchemaV4Error, match="migration seed"):
        schema_v4.migrate_v3_connection(
            connection,
            _seed(activation_store_id="different-store"),
        )


def test_v4_migration_retry_binds_the_original_migration_timestamp() -> None:
    connection = _v3_connection()
    schema_v4.migrate_v3_connection(connection, _seed())

    with pytest.raises(schema_v4.SchemaV4Error, match="migration seed"):
        schema_v4.migrate_v3_connection(
            connection,
            _seed(migrated_at=1_800_000_001),
        )


def test_v4_migration_archives_staged_legacy_purpose_authority() -> None:
    connection = _v3_connection()
    connection.execute(
        "INSERT INTO governance_session_purpose_staging "
        "(event_id, authorization_session, principal_id, purpose, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "event-v3-purpose",
            "legacy-session-handle",
            "external",
            "support",
            1_700_000_000,
            1_900_000_000,
        ),
    )
    connection.commit()

    schema_v4.migrate_v3_connection(connection, _seed())

    assert connection.execute(
        "SELECT source_key, reason FROM governance_legacy_authority "
        "WHERE source_table='governance_session_purpose_staging'"
    ).fetchall() == [
        ("event-v3-purpose", "v3-unbound-authorization"),
    ]


def test_v4_migration_crash_before_commit_leaves_exact_v3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _v3_connection()
    _insert_legacy_authority(connection)
    before = tuple(connection.iterdump())

    def crash(point: str) -> None:
        if point == "before-commit":
            raise RuntimeError("injected migration crash")

    monkeypatch.setattr(schema_v4, "_crash_point", crash)
    with pytest.raises(RuntimeError, match="injected migration crash"):
        schema_v4.migrate_v3_connection(connection, _seed())

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize(
    "crash_at",
    ("after-legacy-archive", "after-schema", "after-seed", "before-commit"),
)
def test_every_v4_migration_crash_barrier_restores_exact_v3(
    crash_at: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _v3_connection()
    _insert_legacy_authority(connection)
    before = tuple(connection.iterdump())

    def crash(point: str) -> None:
        if point == crash_at:
            raise RuntimeError(f"injected {point}")

    monkeypatch.setattr(schema_v4, "_crash_point", crash)
    with pytest.raises(RuntimeError, match=f"injected {crash_at}"):
        schema_v4.migrate_v3_connection(connection, _seed())

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert tuple(connection.iterdump()) == before


def test_invalid_v4_seed_refuses_before_opening_a_write_transaction() -> None:
    connection = _v3_connection()
    before = tuple(connection.iterdump())

    with pytest.raises(schema_v4.SchemaV4Error, match="activation_store_id"):
        schema_v4.migrate_v3_connection(
            connection,
            _seed(activation_store_id=""),
        )

    assert not connection.in_transaction
    assert tuple(connection.iterdump()) == before


def test_v4_migration_blocks_pending_journals_bound_to_legacy_sessions() -> None:
    connection = _v3_connection()
    connection.execute(
        "INSERT INTO governance_operation_journals "
        "(event_id, operation, causation_id, authorization_session, principal_id, "
        "phase, direction, prior_digest, prepared_digest, final_digest, affected_ids, "
        "required_child_intents, required_child_terminals, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "event-pending-v3-session",
            "declare",
            "cause-pending-v3-session",
            "legacy-session-handle",
            "external",
            "pending",
            "widening",
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "[]",
            "[]",
            "[]",
            1_700_000_000,
            1_700_000_001,
        ),
    )
    connection.commit()

    schema_v4.migrate_v3_connection(connection, _seed())

    assert connection.execute(
        "SELECT phase, blocked_reason FROM governance_operation_journals "
        "WHERE event_id='event-pending-v3-session'"
    ).fetchone() == ("pending", "v3-unbound-authorization")


@pytest.mark.parametrize("version", (0, 1, 2, 5))
def test_v4_migration_refuses_every_non_v3_source_without_writes(version: int) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(f"PRAGMA user_version={version}")
    connection.commit()
    before = tuple(connection.iterdump())

    with pytest.raises(schema_v4.SchemaV4Error, match="schema v3"):
        schema_v4.migrate_v3_connection(connection, _seed())

    assert connection.execute("PRAGMA user_version").fetchone()[0] == version
    assert tuple(connection.iterdump()) == before


def test_v4_session_row_stores_bound_verifiers_and_never_the_bearer() -> None:
    connection = _v3_connection()
    schema_v4.migrate_v3_connection(connection, _seed())
    binding = authorization_sessions.AuthorizationSessionBinding(
        session_id="session-018f621c",
        principal_id="principal:local-owner:1000",
        issuer_family="cli-local-owner",
        cell_id="cell-7bd27031",
        logical_vault_id="logical-vault-7",
        keyring_id="keyring-e7901e43",
        credential_generation=3,
        expires_at=1_800_003_600,
    )
    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        binding=binding,
    )

    schema_v4.insert_authorization_session(
        connection,
        issued.record,
        created_at=1_800_000_000,
    )

    row = connection.execute(
        "SELECT session_id, locator_digest, verifier, verifier_key_id, "
        "credential_generation, principal_id, issuer_family, cell_id, "
        "logical_vault_id, keyring_id, status, created_at, rotated_at, "
        "expires_at, closed_at FROM governance_authorization_sessions"
    ).fetchone()
    assert row == (
        binding.session_id,
        issued.record.locator_digest,
        issued.record.verifier,
        "auth-key-2026-08",
        3,
        binding.principal_id,
        binding.issuer_family,
        binding.cell_id,
        binding.logical_vault_id,
        binding.keyring_id,
        "active",
        1_800_000_000,
        None,
        binding.expires_at,
        None,
    )
    dump = "\n".join(connection.iterdump())
    assert issued.bearer not in dump
    assert issued.bearer not in repr(row)
    assert issued.record.binding == binding


def test_v4_session_insert_rejects_identity_or_lifecycle_mismatch() -> None:
    connection = _v3_connection()
    schema_v4.migrate_v3_connection(connection, _seed())
    binding = authorization_sessions.AuthorizationSessionBinding(
        session_id="session-018f621c",
        principal_id="principal:local-owner:1000",
        issuer_family="cli-local-owner",
        cell_id="cell-7bd27031",
        logical_vault_id="wrong-vault",
        keyring_id="keyring-e7901e43",
        credential_generation=3,
        expires_at=1_800_003_600,
    )
    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        binding=binding,
    )

    with pytest.raises(schema_v4.SchemaV4Error, match="logical vault"):
        schema_v4.insert_authorization_session(
            connection,
            issued.record,
            created_at=1_800_000_000,
        )
    with pytest.raises(schema_v4.SchemaV4Error, match="lifecycle"):
        schema_v4.insert_authorization_session(
            connection,
            dataclasses.replace(issued.record, status="closed"),
            created_at=1_800_000_000,
        )
