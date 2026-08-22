from __future__ import annotations

import importlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from exomem.governance import (
    authorization_custody,
    authorization_session_lifecycle,
    policy,
    schema_v4,
    store,
)

NOW = 1_800_000_000
POLICY_DOCUMENTS = (
    (
        "scopes/migration.yaml",
        b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths:\n  - Notes/**\n",
    ),
)
COMPILED_POLICY = policy.compile_documents(dict(POLICY_DOCUMENTS))
assert not COMPILED_POLICY.empty and not COMPILED_POLICY.blocked
POLICY_FINGERPRINT = COMPILED_POLICY.fingerprint
COMPILED_POLICY_BYTES = policy.canonical_compiled_bytes(COMPILED_POLICY)


def _seed() -> schema_v4.MigrationSeed:
    return schema_v4.MigrationSeed(
        activation_store_id="activation-store-7",
        logical_vault_id="logical-vault-7",
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source_documents=POLICY_DOCUMENTS,
            source_fingerprint=POLICY_FINGERPRINT,
            conflict_digest="2" * 64,
            compiled_policy=COMPILED_POLICY_BYTES,
            policy_fingerprint=POLICY_FINGERPRINT,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-migration-policy",
            receipt_event_id="receipt-migration-policy",
            created_at=NOW,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=7,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=NOW,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="projection-namespace-7",
            evidence=b'{"ready":true}',
            ready_at=NOW,
        ),
        migrated_at=NOW,
    )


def _connection() -> tuple[sqlite3.Connection, str]:
    connection = sqlite3.connect(":memory:")
    store._migrate(connection)
    connection.commit()
    migration = schema_v4.migrate_v3_connection(connection, _seed())
    return connection, migration.activation_state_digest


def _custody(activation_digest: str) -> authorization_custody.AuthorizationCustody:
    key = authorization_custody.AuthorizationVerifierKey(
        key_id="auth-key-old",
        key=b"o" * 32,
        not_before=NOW - 60,
        not_after=NOW + 86_400,
    )
    keyring = authorization_custody.AuthorizationKeyring(
        version=1,
        keyring_id="keyring-7",
        cell_id="cell-7",
        logical_vault_id="logical-vault-7",
        active_key_id=key.key_id,
        accepted_keys=(key,),
    )
    control = authorization_custody.AuthorizationControlRecord(
        version=1,
        keyring_id=keyring.keyring_id,
        cell_id=keyring.cell_id,
        logical_vault_id=keyring.logical_vault_id,
        registry_attachment_id="attachment-7",
        attachment_epoch=1,
        governance_enrolled=True,
        activation_store_id="activation-store-7",
        activation_epoch=1,
        activation_state_digest=activation_digest,
        serving_membership_epoch=1,
        serving_membership_digest="a" * 64,
        issued_at=NOW - 60,
        expires_at=NOW + 86_400,
        signing_key_id=key.key_id,
    )
    return authorization_custody.AuthorizationCustody(
        keyring_path=Path("/external/keyring.json"),
        control_path=Path("/external/control.json"),
        keyring=keyring,
        control=control,
    )


def _open(
    connection: sqlite3.Connection,
    custody: authorization_custody.AuthorizationCustody,
) -> authorization_session_lifecycle.AuthorizationSessionIssuance:
    return authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:person-1",
        issuer_family="mcp-oauth",
        now=NOW,
        ttl_seconds=600,
    )


def _mint_and_redeem(
    authority,
    connection: sqlite3.Connection,
    session: authorization_session_lifecycle.AuthorizationSessionIssuance,
    *,
    purpose: str,
    path: str,
    fingerprint: str,
    reviewed_scope_ids: tuple[str, ...],
    current_scope_ids: tuple[str, ...],
):
    token = authority.mint_escalation_token(
        connection,
        context=session.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose=purpose,
        max_level=5,
        org_ceiling=6,
        paths=(path,),
        fingerprints=(fingerprint,),
        scope_ids=reviewed_scope_ids,
        now=NOW + 1,
        expires_at=NOW + 300,
    )
    membership = (
        authority.SessionMembership(
            path=path,
            fingerprint=fingerprint,
            scope_ids=current_scope_ids,
        ),
    )
    return authority.redeem_escalation_token(
        connection,
        token=token,
        context=session.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose=purpose,
        membership=membership,
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 2,
    )


def _authority_module():
    return importlib.import_module("exomem.governance.authorization_session_authority")


def test_token_redemption_and_grant_are_bound_to_one_internal_session_and_reviewed_scope() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    custody = _custody(activation_digest)
    session_a = _open(connection, custody)
    session_b = _open(connection, custody)
    membership = (
        authority.SessionMembership(
            path="Notes/shared.md",
            fingerprint="4" * 64,
            scope_ids=("scope-a", "scope-b"),
        ),
    )

    token = authority.mint_escalation_token(
        connection,
        context=session_a.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose="support",
        max_level=5,
        org_ceiling=6,
        paths=("Notes/shared.md",),
        fingerprints=("4" * 64,),
        scope_ids=("scope-a",),
        now=NOW + 1,
        expires_at=NOW + 300,
    )

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.redeem_escalation_token(
            connection,
            token=token,
            context=session_b.context,
            signing_key=b"t" * 32,
            audience="principal:person-1",
            purpose="support",
            membership=membership,
            policy_fingerprint=POLICY_FINGERPRINT,
            now=NOW + 2,
        )
    assert connection.execute("SELECT consumed_at FROM withhold_tokens").fetchone() == (None,)
    assert connection.execute("SELECT COUNT(*) FROM governance_session_grants").fetchone() == (0,)

    grant = authority.redeem_escalation_token(
        connection,
        token=token,
        context=session_a.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose="support",
        membership=membership,
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 2,
    )

    assert grant.authorization_session_id == session_a.context.session_id
    assert grant.scope_ids == ("scope-a",)
    assert grant.membership == membership
    active, identity = authority.active_session_grants(
        connection,
        context=session_a.context,
        audience="principal:person-1",
        purpose="support",
        path="Notes/shared.md",
        fingerprint="4" * 64,
        scope_ids=("scope-a", "scope-b"),
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 3,
    )
    assert tuple(item.scope_ids for item in active) == (("scope-a",),)
    assert identity != "no-session-grants"
    other, _identity = authority.active_session_grants(
        connection,
        context=session_b.context,
        audience="principal:person-1",
        purpose="support",
        path="Notes/shared.md",
        fingerprint="4" * 64,
        scope_ids=("scope-a", "scope-b"),
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 3,
    )
    assert other == ()

    database_text = "\n".join(connection.iterdump())
    assert session_a.bearer not in database_text
    assert session_b.bearer not in database_text


def test_projection_catalog_grants_are_loaded_once_and_stay_item_exact() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    session = _open(connection, _custody(activation_digest))
    _mint_and_redeem(
        authority,
        connection,
        session,
        purpose="support",
        path="Notes/reviewed.md",
        fingerprint="4" * 64,
        reviewed_scope_ids=("scope-a",),
        current_scope_ids=("scope-a",),
    )

    pairs = authority.active_session_grants_for_projection_catalog(
        connection,
        context=session.context,
        audience="principal:person-1",
        purpose="support",
        catalog=(
            authority.SessionMembership(
                path="Notes/reviewed.md",
                fingerprint="4" * 64,
                scope_ids=("scope-a",),
            ),
            authority.SessionMembership(
                path="Notes/sibling.md",
                fingerprint="5" * 64,
                scope_ids=("scope-a",),
            ),
        ),
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 3,
    )

    assert [(path, grant.grant_id) for path, grant in pairs] == [
        ("Notes/reviewed.md", pairs[0][1].grant_id)
    ]


def test_purpose_lookup_and_revoke_are_exact_for_same_principal_sessions() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    custody = _custody(activation_digest)
    session_a = _open(connection, custody)
    session_b = _open(connection, custody)
    grant_a = _mint_and_redeem(
        authority,
        connection,
        session_a,
        purpose="support",
        path="Notes/a.md",
        fingerprint="4" * 64,
        reviewed_scope_ids=("scope-a",),
        current_scope_ids=("scope-a",),
    )
    grant_b = _mint_and_redeem(
        authority,
        connection,
        session_b,
        purpose="audit",
        path="Notes/b.md",
        fingerprint="5" * 64,
        reviewed_scope_ids=("scope-b",),
        current_scope_ids=("scope-b",),
    )

    authority.declare_purpose(
        connection,
        context=session_a.context,
        audience="principal:person-1",
        purpose="support",
        now=NOW + 3,
        expires_at=NOW + 200,
    )
    authority.declare_purpose(
        connection,
        context=session_b.context,
        audience="principal:person-1",
        purpose="audit",
        now=NOW + 3,
        expires_at=NOW + 200,
    )
    assert (
        authority.active_session_purpose(
            connection,
            context=session_a.context,
            audience="principal:person-1",
            now=NOW + 4,
        )
        == "support"
    )
    assert (
        authority.active_session_purpose(
            connection,
            context=session_b.context,
            audience="principal:person-1",
            now=NOW + 4,
        )
        == "audit"
    )

    assert (
        authority.revoke_session_grants(
            connection,
            context=session_a.context,
            audience="principal:person-1",
            now=NOW + 5,
        )
        == 1
    )
    assert connection.execute(
        "SELECT grant_id, status FROM governance_session_grants ORDER BY grant_id"
    ).fetchall() == sorted([(grant_a.grant_id, "revoked"), (grant_b.grant_id, "active")])
    assert (
        authority.revoke_session_grants(
            connection,
            context=session_a.context,
            audience="principal:person-1",
            now=NOW + 6,
        )
        == 0
    )


def test_fabricated_context_and_policy_or_membership_drift_fail_closed_without_broadening() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    custody = _custody(activation_digest)
    session = _open(connection, custody)
    fabricated = replace(session.context, session_id="caller-selected-session")

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.declare_purpose(
            connection,
            context=fabricated,
            audience="principal:person-1",
            purpose="support",
            now=NOW + 1,
            expires_at=NOW + 200,
        )
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.mint_escalation_token(
            connection,
            context=None,
            signing_key=b"t" * 32,
            audience="principal:person-1",
            purpose="support",
            max_level=5,
            org_ceiling=6,
            paths=("Notes/shared.md",),
            fingerprints=("4" * 64,),
            scope_ids=("scope-a",),
            now=NOW + 1,
            expires_at=NOW + 300,
        )
    assert connection.execute("SELECT COUNT(*) FROM governance_session_purpose").fetchone() == (0,)
    assert connection.execute("SELECT COUNT(*) FROM withhold_tokens").fetchone() == (0,)

    _mint_and_redeem(
        authority,
        connection,
        session,
        purpose="support",
        path="Notes/shared.md",
        fingerprint="4" * 64,
        reviewed_scope_ids=("scope-a",),
        current_scope_ids=("scope-a", "scope-b"),
    )
    common = {
        "connection": connection,
        "context": session.context,
        "audience": "principal:person-1",
        "purpose": "support",
        "path": "Notes/shared.md",
        "fingerprint": "4" * 64,
        "scope_ids": ("scope-a", "scope-b"),
        "policy_fingerprint": POLICY_FINGERPRINT,
        "now": NOW + 3,
    }
    current, _identity = authority.active_session_grants(**common)
    assert len(current) == 1

    changed_membership, _identity = authority.active_session_grants(
        **{**common, "scope_ids": ("scope-a", "scope-b", "scope-c")}
    )
    changed_bytes, _identity = authority.active_session_grants(
        **{**common, "fingerprint": "6" * 64}
    )
    connection.execute(
        "UPDATE active_governance_tuple SET policy_fingerprint=? WHERE singleton=1",
        ("7" * 64,),
    )
    connection.commit()
    changed_policy, _identity = authority.active_session_grants(**common)

    assert changed_membership == ()
    assert changed_bytes == ()
    assert changed_policy == ()


def test_multi_item_token_preserves_each_path_fingerprint_pair() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    session = _open(connection, _custody(activation_digest))
    membership = (
        authority.SessionMembership(
            path="Notes/a.md",
            fingerprint="f" * 64,
            scope_ids=("scope-a",),
        ),
        authority.SessionMembership(
            path="Notes/z.md",
            fingerprint="1" * 64,
            scope_ids=("scope-z",),
        ),
    )

    token = authority.mint_escalation_token(
        connection,
        context=session.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose="support",
        max_level=5,
        org_ceiling=6,
        paths=("Notes/a.md", "Notes/z.md"),
        fingerprints=("f" * 64, "1" * 64),
        scope_ids=("scope-a", "scope-z"),
        now=NOW + 1,
        expires_at=NOW + 300,
    )
    grant = authority.redeem_escalation_token(
        connection,
        token=token,
        context=session.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose="support",
        membership=membership,
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 2,
    )

    assert tuple(zip(grant.paths, grant.fingerprints, strict=True)) == (
        ("Notes/a.md", "f" * 64),
        ("Notes/z.md", "1" * 64),
    )
    active, _identity = authority.active_session_grants(
        connection,
        context=session.context,
        audience="principal:person-1",
        purpose="support",
        path="Notes/z.md",
        fingerprint="1" * 64,
        scope_ids=("scope-z",),
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 3,
    )
    assert tuple(item.grant_id for item in active) == (grant.grant_id,)


def test_grant_insert_failure_rolls_back_token_consumption_and_replay_is_single_use() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    session = _open(connection, _custody(activation_digest))
    membership = (
        authority.SessionMembership(
            path="Notes/a.md",
            fingerprint="4" * 64,
            scope_ids=("scope-a",),
        ),
    )
    token = authority.mint_escalation_token(
        connection,
        context=session.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose="support",
        max_level=5,
        org_ceiling=6,
        paths=("Notes/a.md",),
        fingerprints=("4" * 64,),
        scope_ids=("scope-a",),
        now=NOW + 1,
        expires_at=NOW + 300,
    )
    connection.execute(
        "CREATE TRIGGER reject_session_grant BEFORE INSERT ON governance_session_grants "
        "BEGIN SELECT RAISE(ABORT, 'reject grant'); END"
    )

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.redeem_escalation_token(
            connection,
            token=token,
            context=session.context,
            signing_key=b"t" * 32,
            audience="principal:person-1",
            purpose="support",
            membership=membership,
            policy_fingerprint=POLICY_FINGERPRINT,
            now=NOW + 2,
        )
    assert connection.execute("SELECT status, consumed_at FROM withhold_tokens").fetchone() == (
        "active",
        None,
    )
    connection.execute("DROP TRIGGER reject_session_grant")

    first = authority.redeem_escalation_token(
        connection,
        token=token,
        context=session.context,
        signing_key=b"t" * 32,
        audience="principal:person-1",
        purpose="support",
        membership=membership,
        policy_fingerprint=POLICY_FINGERPRINT,
        now=NOW + 3,
    )
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.redeem_escalation_token(
            connection,
            token=token,
            context=session.context,
            signing_key=b"t" * 32,
            audience="principal:person-1",
            purpose="support",
            membership=membership,
            policy_fingerprint=POLICY_FINGERPRINT,
            now=NOW + 4,
        )
    assert connection.execute("SELECT grant_id FROM governance_session_grants").fetchall() == [
        (first.grant_id,)
    ]


def test_close_invalidates_only_the_bound_sessions_real_authority_rows() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    custody = _custody(activation_digest)
    session_a = _open(connection, custody)
    session_b = _open(connection, custody)
    _mint_and_redeem(
        authority,
        connection,
        session_a,
        purpose="support",
        path="Notes/a.md",
        fingerprint="4" * 64,
        reviewed_scope_ids=("scope-a",),
        current_scope_ids=("scope-a",),
    )
    grant_b = _mint_and_redeem(
        authority,
        connection,
        session_b,
        purpose="audit",
        path="Notes/b.md",
        fingerprint="5" * 64,
        reviewed_scope_ids=("scope-b",),
        current_scope_ids=("scope-b",),
    )
    authority.declare_purpose(
        connection,
        context=session_a.context,
        audience="principal:person-1",
        purpose="support",
        now=NOW + 3,
        expires_at=NOW + 200,
    )
    authority.declare_purpose(
        connection,
        context=session_b.context,
        audience="principal:person-1",
        purpose="audit",
        now=NOW + 3,
        expires_at=NOW + 200,
    )

    authorization_session_lifecycle.close_session(
        connection,
        custody=custody,
        bearer=session_a.bearer,
        principal_id="principal:person-1",
        issuer_family="mcp-oauth",
        now=NOW + 4,
    )

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.active_session_purpose(
            connection,
            context=session_a.context,
            audience="principal:person-1",
            now=NOW + 5,
        )
    assert (
        authority.active_session_purpose(
            connection,
            context=session_b.context,
            audience="principal:person-1",
            now=NOW + 5,
        )
        == "audit"
    )
    assert connection.execute(
        "SELECT status FROM governance_session_grants WHERE grant_id=?",
        (grant_b.grant_id,),
    ).fetchone() == ("active",)


def test_token_mint_refuses_to_commit_a_callers_existing_transaction() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    session = _open(connection, _custody(activation_digest))
    connection.execute("CREATE TABLE caller_state (value TEXT NOT NULL)")
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state (value) VALUES ('uncommitted')")

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.mint_escalation_token(
            connection,
            context=session.context,
            signing_key=b"t" * 32,
            audience="principal:person-1",
            purpose="support",
            max_level=5,
            org_ceiling=6,
            paths=("Notes/a.md",),
            fingerprints=("4" * 64,),
            scope_ids=("scope-a",),
            now=NOW + 1,
            expires_at=NOW + 300,
        )

    assert connection.in_transaction is True
    assert connection.execute("SELECT COUNT(*) FROM withhold_tokens").fetchone() == (0,)
    connection.rollback()
    assert connection.execute("SELECT COUNT(*) FROM caller_state").fetchone() == (0,)


def test_malformed_active_sibling_grant_blocks_the_complete_session_lookup() -> None:
    authority = _authority_module()
    connection, activation_digest = _connection()
    session = _open(connection, _custody(activation_digest))
    _mint_and_redeem(
        authority,
        connection,
        session,
        purpose="support",
        path="Notes/a.md",
        fingerprint="4" * 64,
        reviewed_scope_ids=("scope-a",),
        current_scope_ids=("scope-a",),
    )
    connection.execute(
        "INSERT INTO governance_session_grants "
        "(grant_id, authorization_session_id, principal_id, issuer_family, audience, "
        "purpose, ceiling, paths, fingerprints, scope_ids, membership_manifest, "
        "policy_fingerprint, token_jti, status, prepared_event_id, created_at, "
        "expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, 5, ?, ?, ?, ?, ?, ?, "
        "'active', NULL, ?, ?, NULL)",
        (
            "8" * 64,
            session.context.session_id,
            session.context.principal_id,
            session.context.issuer_family,
            "principal:person-1",
            "support",
            '["Notes/a.md"]',
            f'["{"4" * 64}"]',
            "not-json",
            '[{"fingerprint":"4","path":"Notes/a.md","scope_ids":[]}]',
            POLICY_FINGERPRINT,
            "malformed-token-row",
            NOW + 2,
            NOW + 200,
        ),
    )
    connection.commit()

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authority.active_session_grants(
            connection,
            context=session.context,
            audience="principal:person-1",
            purpose="support",
            path="Notes/a.md",
            fingerprint="4" * 64,
            scope_ids=("scope-a",),
            policy_fingerprint=POLICY_FINGERPRINT,
            now=NOW + 3,
        )
