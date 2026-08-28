from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exomem.governance import (
    authorization_custody,
    authorization_serving_membership,
    authorization_session_lifecycle,
    authorization_sessions,
    policy,
    schema_v4,
    scrubber,
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


def _connection() -> tuple[sqlite3.Connection, schema_v4.MigrationResult]:
    connection = sqlite3.connect(":memory:")
    store._migrate(connection)
    connection.commit()
    result = schema_v4.migrate_v3_connection(connection, _seed())
    return connection, result


def _file_connection(
    path: Path,
) -> tuple[sqlite3.Connection, schema_v4.MigrationResult]:
    connection = sqlite3.connect(path)
    store._migrate(connection)
    connection.commit()
    return connection, schema_v4.migrate_v3_connection(connection, _seed())


def _resume_in_fresh_process(
    database_path: Path,
    *,
    bearer: str,
    activation_state_digest: str,
) -> dict[str, object]:
    script = textwrap.dedent(
        """
        import json
        import sqlite3
        import sys
        from dataclasses import asdict
        from pathlib import Path

        from exomem.governance import (
            authorization_custody,
            authorization_session_lifecycle,
            authorization_serving_membership,
        )

        value = json.loads(sys.stdin.read())
        key = authorization_custody.AuthorizationVerifierKey(
            key_id="auth-key-old",
            key=b"o" * 32,
            not_before=value["now"] - 60,
            not_after=value["now"] + 86_400,
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
            keyring_id="keyring-7",
            cell_id="cell-7",
            logical_vault_id="logical-vault-7",
            registry_attachment_id="attachment-7",
            attachment_epoch=1,
            governance_enrolled=True,
            activation_store_id="activation-store-7",
            activation_epoch=1,
            activation_state_digest=value["activation_state_digest"],
            serving_membership_epoch=1,
            serving_membership_digest="a" * 64,
            issued_at=value["now"] - 60,
            expires_at=value["now"] + 86_400,
            signing_key_id=key.key_id,
        )
        attestation = authorization_serving_membership.ReplicaReadinessAttestation(
            version=1,
            epoch=1,
            replica_id="replica-7",
            state="SERVING",
            software_version=authorization_custody.runtime_software_version(),
            schema_version=4,
            cell_id="cell-7",
            active_key_id=key.key_id,
            accepted_key_ids=(key.key_id,),
            control_digest=authorization_custody.control_attestation_digest(control),
            keyring_digest=authorization_custody.keyring_attestation_digest(keyring),
            attested_at=value["now"] - 1,
            expires_at=value["now"] + 299,
            issuance_stopped=False,
            no_in_flight=False,
            signing_key_id=key.key_id,
        )
        custody = authorization_custody.AuthorizationCustody(
            keyring_path=Path("/external/keyring.json"),
            control_path=Path("/external/control.json"),
            keyring=keyring,
            control=control,
            serving_membership=authorization_serving_membership.ServingMembershipEpoch(
                version=1,
                epoch=1,
                cell_id="cell-7",
                logical_vault_id="logical-vault-7",
                previous_epoch_digest=None,
                issued_at=value["now"] - 1,
                expires_at=value["now"] + 299,
                replicas=(attestation,),
                signing_key_id=key.key_id,
            ),
            local_replica_id="replica-7",
        )
        with sqlite3.connect(value["database_path"]) as connection:
            context = authorization_session_lifecycle.resume_session(
                connection,
                custody=custody,
                bearer=value["bearer"],
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=value["now"] + 1,
            )
        print(json.dumps(asdict(context), sort_keys=True))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {
                "database_path": str(database_path),
                "bearer": bearer,
                "activation_state_digest": activation_state_digest,
                "now": NOW,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        cwd=Path(__file__).parents[1],
    )
    assert bearer not in completed.stdout
    assert bearer not in completed.stderr
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _key(
    key_id: str,
    value: bytes,
) -> authorization_custody.AuthorizationVerifierKey:
    return authorization_custody.AuthorizationVerifierKey(
        key_id=key_id,
        key=value,
        not_before=NOW - 60,
        not_after=NOW + 86_400,
    )


def _custody(
    activation_digest: str,
    *,
    active_key_id: str = "auth-key-old",
    keys: tuple[authorization_custody.AuthorizationVerifierKey, ...] | None = None,
    membership_expires_at: int = NOW + 299,
) -> authorization_custody.AuthorizationCustody:
    accepted = keys or (_key("auth-key-old", b"o" * 32),)
    keyring = authorization_custody.AuthorizationKeyring(
        version=1,
        keyring_id="keyring-7",
        cell_id="cell-7",
        logical_vault_id="logical-vault-7",
        active_key_id=active_key_id,
        accepted_keys=accepted,
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
        signing_key_id=active_key_id,
    )
    key_ids = tuple(sorted(key.key_id for key in accepted))
    serving_membership = authorization_serving_membership.ServingMembershipEpoch(
        version=1,
        epoch=control.serving_membership_epoch,
        cell_id=control.cell_id,
        logical_vault_id=control.logical_vault_id,
        previous_epoch_digest=None,
        issued_at=NOW - 1,
        expires_at=membership_expires_at,
        replicas=(
            authorization_serving_membership.ReplicaReadinessAttestation(
                version=1,
                epoch=control.serving_membership_epoch,
                replica_id="replica-7",
                state="SERVING",
                software_version=authorization_custody.runtime_software_version(),
                schema_version=4,
                cell_id=control.cell_id,
                active_key_id=active_key_id,
                accepted_key_ids=key_ids,
                control_digest=authorization_custody.control_attestation_digest(control),
                keyring_digest=authorization_custody.keyring_attestation_digest(keyring),
                attested_at=NOW - 1,
                expires_at=membership_expires_at,
                issuance_stopped=False,
                no_in_flight=False,
                signing_key_id=active_key_id,
            ),
        ),
        signing_key_id=active_key_id,
    )
    return authorization_custody.AuthorizationCustody(
        keyring_path=Path("/external/keyring.json"),
        control_path=Path("/external/control.json"),
        keyring=keyring,
        control=control,
        serving_membership=serving_membership,
        local_replica_id="replica-7",
    )


def _hosted_custody(
    activation_digest: str,
) -> authorization_custody.AuthorizationCustody:
    custody = _custody(activation_digest)
    control = replace(
        custody.control,
        registry_attachment_id="hosted-attachment-v1-" + "a" * 64,
    )
    assert custody.serving_membership is not None
    replica = replace(
        custody.serving_membership.replicas[0],
        control_digest=authorization_custody.control_attestation_digest(control),
    )
    return replace(
        custody,
        keyring_path=authorization_custody.HOSTED_KEYRING_FILE,
        control_path=authorization_custody.HOSTED_CONTROL_FILE,
        control=control,
        serving_membership=replace(
            custody.serving_membership,
            replicas=(replica,),
        ),
        membership_path=authorization_custody.HOSTED_MEMBERSHIP_FILE,
    )


def test_open_persists_only_a_bound_verifier_and_resume_returns_context() -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)

    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    resumed = authorization_session_lifecycle.resume_session(
        connection,
        custody=custody,
        bearer=issued.bearer,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW + 1,
    )

    assert authorization_sessions.parse_credential(issued.bearer) is not None
    assert resumed == issued.context
    assert resumed.credential_generation == 1
    assert resumed.cell_id == "cell-7"
    row = connection.execute(
        "SELECT session_id, verifier_key_id, principal_id, issuer_family, cell_id, "
        "logical_vault_id, keyring_id, status, expires_at "
        "FROM governance_authorization_sessions"
    ).fetchone()
    assert row == (
        resumed.session_id,
        "auth-key-old",
        resumed.principal_id,
        resumed.issuer_family,
        resumed.cell_id,
        resumed.logical_vault_id,
        resumed.keyring_id,
        "active",
        NOW + 600,
    )
    assert issued.bearer not in "\n".join(connection.iterdump())
    assert issued.bearer not in repr(issued)


@pytest.mark.parametrize("missing", [True, False])
def test_open_fails_closed_before_writing_when_membership_is_missing_or_stale(
    missing: bool,
) -> None:
    connection, migration = _connection()
    custody = _custody(
        migration.activation_state_digest,
        membership_expires_at=NOW if not missing else NOW + 299,
    )
    if missing:
        custody = replace(custody, serving_membership=None)

    with pytest.raises(
        authorization_session_lifecycle.AuthorizationSessionUnavailable
    ) as raised:
        authorization_session_lifecycle.open_session(
            connection,
            custody=custody,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW,
            ttl_seconds=600,
        )

    assert raised.value.code == "AUTHORIZATION_SESSION_UNAVAILABLE"
    assert connection.execute(
        "SELECT COUNT(*) FROM governance_authorization_sessions"
    ).fetchone() == (0,)


def test_hosted_readiness_binds_the_control_plane_cell_vault_and_replica(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "governance.sqlite"
    connection, migration = _file_connection(database_path)
    connection.close()
    custody = _hosted_custody(migration.activation_state_digest)
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(authorization_custody.HOSTED_KEYRING_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(authorization_custody.HOSTED_CONTROL_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(authorization_custody.HOSTED_MEMBERSHIP_FILE),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "replica-7")
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda _root, *, now: custody,
    )
    monkeypatch.setattr(
        store,
        "open_authorization_session_connection",
        lambda _root: sqlite3.connect(database_path),
    )

    ready = authorization_session_lifecycle.hosted_serving_membership_readiness(
        tmp_path,
        expected_cell_id="cell-7",
        expected_logical_vault_id="logical-vault-7",
        expected_replica_id="replica-7",
        now=NOW,
    )

    assert ready.ready is True
    for expected in (
        {"expected_cell_id": "other-cell"},
        {"expected_logical_vault_id": "other-vault"},
        {"expected_replica_id": "other-replica"},
    ):
        arguments = {
            "expected_cell_id": "cell-7",
            "expected_logical_vault_id": "logical-vault-7",
            "expected_replica_id": "replica-7",
            **expected,
        }
        refused = authorization_session_lifecycle.hosted_serving_membership_readiness(
            tmp_path,
            now=NOW,
            **arguments,
        )
        assert refused == authorization_serving_membership.unavailable_readiness()


def test_hosted_readiness_rejects_a_standalone_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "governance.sqlite"
    connection, migration = _file_connection(database_path)
    connection.close()
    custody = _custody(migration.activation_state_digest)
    custody = replace(
        custody,
        keyring_path=authorization_custody.HOSTED_KEYRING_FILE,
        control_path=authorization_custody.HOSTED_CONTROL_FILE,
        membership_path=authorization_custody.HOSTED_MEMBERSHIP_FILE,
    )
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(authorization_custody.HOSTED_KEYRING_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(authorization_custody.HOSTED_CONTROL_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(authorization_custody.HOSTED_MEMBERSHIP_FILE),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "replica-7")
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda _root, *, now: custody,
    )
    monkeypatch.setattr(
        store,
        "open_authorization_session_connection",
        lambda _root: sqlite3.connect(database_path),
    )

    refused = authorization_session_lifecycle.hosted_serving_membership_readiness(
        tmp_path,
        expected_cell_id="cell-7",
        expected_logical_vault_id="logical-vault-7",
        expected_replica_id="replica-7",
        now=NOW,
    )

    assert refused == authorization_serving_membership.unavailable_readiness()


@pytest.mark.parametrize(
    ("phase", "active_reads", "expected_state", "issuance_stopped", "no_in_flight"),
    [
        ("active", 7, "SERVING", False, False),
        ("quiesced", 0, "DRAINING", True, True),
    ],
)
def test_hosted_runtime_mints_only_state_derived_replica_attestations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    active_reads: int,
    expected_state: str,
    issuance_stopped: bool,
    no_in_flight: bool,
) -> None:
    custody = _hosted_custody("1" * 64)
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(authorization_custody.HOSTED_KEYRING_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(authorization_custody.HOSTED_CONTROL_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(authorization_custody.HOSTED_MEMBERSHIP_FILE),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "replica-7")
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda _root, *, now: custody,
    )

    raw = authorization_session_lifecycle.mint_hosted_replica_readiness_attestation(
        tmp_path,
        expected_cell_id="cell-7",
        expected_logical_vault_id="logical-vault-7",
        expected_replica_id="replica-7",
        lifecycle_phase=phase,
        active_reads=active_reads,
        active_mutations=0,
        active_transfers=0,
        target_epoch=2,
        previous_epoch_digest="a" * 64,
        ttl_seconds=300,
        now=NOW,
    )
    parsed = authorization_serving_membership.parse_replica_readiness_attestation(
        raw,
        verifier_keys={item.key_id: item.key for item in custody.keyring.accepted_keys},
        now=NOW,
        expected_epoch=2,
        expected_cell_id="cell-7",
    )

    assert parsed.replica_id == "replica-7"
    assert parsed.state == expected_state
    assert parsed.software_version == authorization_custody.runtime_software_version()
    assert parsed.schema_version == schema_v4.SCHEMA_USER_VERSION
    assert parsed.issuance_stopped is issuance_stopped
    assert parsed.no_in_flight is no_in_flight
    assert parsed.attested_at == NOW
    assert parsed.expires_at == NOW + 300


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_replica_id": "other-replica"},
        {"previous_epoch_digest": "b" * 64},
        {"target_epoch": 1},
        {"lifecycle_phase": "quiescing"},
        {"lifecycle_phase": "quiesced", "active_reads": 1},
        {"lifecycle_phase": "quiesced", "active_mutations": 1},
        {"lifecycle_phase": "quiesced", "active_transfers": 1},
    ],
)
def test_hosted_runtime_attestation_refuses_caller_claims_and_incomplete_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    custody = _hosted_custody("1" * 64)
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(authorization_custody.HOSTED_KEYRING_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(authorization_custody.HOSTED_CONTROL_FILE),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(authorization_custody.HOSTED_MEMBERSHIP_FILE),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "replica-7")
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda _root, *, now: custody,
    )
    arguments: dict[str, object] = {
        "expected_cell_id": "cell-7",
        "expected_logical_vault_id": "logical-vault-7",
        "expected_replica_id": "replica-7",
        "lifecycle_phase": "active",
        "active_reads": 0,
        "active_mutations": 0,
        "active_transfers": 0,
        "target_epoch": 2,
        "previous_epoch_digest": "a" * 64,
        "ttl_seconds": 300,
        "now": NOW,
    }
    arguments.update(overrides)

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authorization_session_lifecycle.mint_hosted_replica_readiness_attestation(
            tmp_path,
            **arguments,  # type: ignore[arg-type]
        )


def test_hosted_runtime_attestation_can_extend_the_next_control_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custody = _hosted_custody("1" * 64)
    assert custody.serving_membership is not None
    custody = replace(
        custody,
        control=replace(custody.control, expires_at=NOW + 300),
        serving_membership=replace(
            custody.serving_membership,
            expires_at=NOW + 300,
            replicas=(
                replace(
                    custody.serving_membership.replicas[0],
                    expires_at=NOW + 300,
                ),
            ),
        ),
    )
    for variable, path in {
        authorization_custody.KEYRING_FILE_ENV: authorization_custody.HOSTED_KEYRING_FILE,
        authorization_custody.CONTROL_FILE_ENV: authorization_custody.HOSTED_CONTROL_FILE,
        authorization_custody.MEMBERSHIP_FILE_ENV: authorization_custody.HOSTED_MEMBERSHIP_FILE,
    }.items():
        monkeypatch.setenv(variable, str(path))
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "replica-7")
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda _root, *, now: custody,
    )

    raw = authorization_session_lifecycle.mint_hosted_replica_readiness_attestation(
        tmp_path,
        expected_cell_id="cell-7",
        expected_logical_vault_id="logical-vault-7",
        expected_replica_id="replica-7",
        lifecycle_phase="active",
        active_reads=0,
        active_mutations=0,
        active_transfers=0,
        target_epoch=2,
        previous_epoch_digest="a" * 64,
        ttl_seconds=300,
        now=NOW + 200,
    )
    parsed = authorization_serving_membership.parse_replica_readiness_attestation(
        raw,
        verifier_keys={item.key_id: item.key for item in custody.keyring.accepted_keys},
        now=NOW + 200,
        expected_epoch=2,
        expected_cell_id="cell-7",
    )

    assert parsed.expires_at == NOW + 500


def test_resume_fails_when_a_live_row_key_drops_from_the_serving_intersection() -> None:
    connection, migration = _connection()
    old_key = _key("auth-key-old", b"o" * 32)
    new_key = _key("auth-key-new", b"n" * 32)
    initial = _custody(
        migration.activation_state_digest,
        keys=(old_key, new_key),
    )
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=initial,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    switched = _custody(
        migration.activation_state_digest,
        active_key_id="auth-key-new",
        keys=(new_key,),
    )

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authorization_session_lifecycle.resume_session(
            connection,
            custody=switched,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 1,
        )


def test_open_builds_the_exact_typed_issuance_terminal() -> None:
    connection, migration = _connection()
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=_custody(migration.activation_state_digest),
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )

    expected_expiry = (
        datetime.fromtimestamp(NOW + 600, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    response = issued.response()

    assert response == {
        "status": "ok",
        "issued_credential": {
            "kind": "authorization-session-bearer",
            "bearer": issued.bearer,
            "expires_at": expected_expiry,
        },
    }
    cleaned, blocked = scrubber._scrub_issuance_response(
        "open",
        response,
        scrubber._new_issuance_context("open", issued.bearer),
    )
    assert not blocked
    assert cleaned == response


def test_open_storage_failure_has_the_common_shape_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    monkeypatch.setattr(
        authorization_session_lifecycle.secrets,
        "token_hex",
        lambda _length: "f" * 32,
    )
    first = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable) as raised:
        authorization_session_lifecycle.open_session(
            connection,
            custody=custody,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 1,
            ttl_seconds=600,
        )

    assert str(raised.value) == "authorization session is unavailable"
    assert connection.execute(
        "SELECT COUNT(*) FROM governance_authorization_sessions"
    ).fetchone() == (1,)
    assert (
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=first.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 2,
        )
        == first.context
    )


def test_status_requires_a_live_credential_and_expiry_uses_the_common_shape() -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=60,
    )

    assert (
        authorization_session_lifecycle.status_session(
            connection,
            custody=custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 59,
        )
        == issued.context
    )
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable) as expired:
        authorization_session_lifecycle.status_session(
            connection,
            custody=custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 60,
        )
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable) as unknown:
        authorization_session_lifecycle.status_session(
            connection,
            custody=custody,
            bearer="as1." + "A" * 22 + "." + "A" * 43,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 60,
        )

    assert expired.value.args == unknown.value.args
    assert connection.execute(
        "SELECT status, expires_at FROM governance_authorization_sessions"
    ).fetchone() == ("active", NOW + 60)


def test_verified_request_context_can_status_rotate_and_close_without_bearer_forwarding() -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    opened = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )

    assert (
        authorization_session_lifecycle.status_verified_session(
            connection,
            custody=custody,
            context=opened.context,
            now=NOW + 1,
        )
        == opened.context
    )
    rotated = authorization_session_lifecycle.rotate_verified_session(
        connection,
        custody=custody,
        context=opened.context,
        now=NOW + 2,
        ttl_seconds=500,
    )
    assert rotated.context.session_id == opened.context.session_id
    assert rotated.context.credential_generation == 2
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=opened.bearer,
            principal_id=opened.context.principal_id,
            issuer_family=opened.context.issuer_family,
            now=NOW + 3,
        )
    assert (
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=rotated.bearer,
            principal_id=rotated.context.principal_id,
            issuer_family=rotated.context.issuer_family,
            now=NOW + 3,
        )
        == rotated.context
    )

    closed = authorization_session_lifecycle.close_verified_session(
        connection,
        custody=custody,
        context=rotated.context,
        now=NOW + 4,
    )
    assert closed == rotated.context
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=rotated.bearer,
            principal_id=rotated.context.principal_id,
            issuer_family=rotated.context.issuer_family,
            now=NOW + 5,
        )


def test_open_refuses_stale_activation_or_excessive_ttl_without_writing() -> None:
    connection, migration = _connection()

    for custody, ttl in (
        (_custody("b" * 64), 60),
        (
            _custody(migration.activation_state_digest),
            authorization_session_lifecycle.MAX_SESSION_TTL_SECONDS + 1,
        ),
    ):
        with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
            authorization_session_lifecycle.open_session(
                connection,
                custody=custody,
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=NOW,
                ttl_seconds=ttl,
            )

    assert connection.execute(
        "SELECT COUNT(*) FROM governance_authorization_sessions"
    ).fetchone() == (0,)


def test_session_resumes_after_database_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "governance.sqlite"
    first_connection, migration = _file_connection(database_path)
    custody = _custody(migration.activation_state_digest)
    issued = authorization_session_lifecycle.open_session(
        first_connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    first_connection.close()

    restarted = sqlite3.connect(database_path)
    try:
        assert (
            authorization_session_lifecycle.resume_session(
                restarted,
                custody=custody,
                bearer=issued.bearer,
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=NOW + 1,
            )
            == issued.context
        )
        assert restarted.execute("PRAGMA user_version").fetchone() == (4,)
    finally:
        restarted.close()


def test_two_fresh_v4_processes_resume_the_same_session(tmp_path: Path) -> None:
    database_path = tmp_path / "governance.sqlite"
    connection, migration = _file_connection(database_path)
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=_custody(migration.activation_state_digest),
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    connection.close()
    expected = {
        "session_id": issued.context.session_id,
        "principal_id": issued.context.principal_id,
        "issuer_family": issued.context.issuer_family,
        "cell_id": issued.context.cell_id,
        "logical_vault_id": issued.context.logical_vault_id,
        "keyring_id": issued.context.keyring_id,
        "credential_generation": issued.context.credential_generation,
        "expires_at": issued.context.expires_at,
    }

    assert (
        _resume_in_fresh_process(
            database_path,
            bearer=issued.bearer,
            activation_state_digest=migration.activation_state_digest,
        )
        == expected
    )
    assert (
        _resume_in_fresh_process(
            database_path,
            bearer=issued.bearer,
            activation_state_digest=migration.activation_state_digest,
        )
        == expected
    )


def test_two_v4_connections_share_rotation_state(tmp_path: Path) -> None:
    database_path = tmp_path / "governance.sqlite"
    first, migration = _file_connection(database_path)
    second = sqlite3.connect(database_path)
    old_key = _key("auth-key-old", b"o" * 32)
    new_key = _key("auth-key-new", b"n" * 32)
    old_custody = _custody(
        migration.activation_state_digest,
        keys=(old_key, new_key),
    )
    rotated_custody = _custody(
        migration.activation_state_digest,
        active_key_id="auth-key-new",
        keys=(old_key, new_key),
    )
    try:
        issued = authorization_session_lifecycle.open_session(
            first,
            custody=old_custody,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW,
            ttl_seconds=600,
        )
        rotated = authorization_session_lifecycle.rotate_session(
            second,
            custody=rotated_custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 10,
            ttl_seconds=900,
        )

        with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
            authorization_session_lifecycle.resume_session(
                first,
                custody=rotated_custody,
                bearer=issued.bearer,
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=NOW + 11,
            )
        assert (
            authorization_session_lifecycle.resume_session(
                first,
                custody=rotated_custody,
                bearer=rotated.bearer,
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=NOW + 11,
            )
            == rotated.context
        )
    finally:
        first.close()
        second.close()


def test_competing_rotations_have_one_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "governance.sqlite"
    setup, migration = _file_connection(database_path)
    old_key = _key("auth-key-old", b"o" * 32)
    new_key = _key("auth-key-new", b"n" * 32)
    old_custody = _custody(
        migration.activation_state_digest,
        keys=(old_key, new_key),
    )
    rotated_custody = _custody(
        migration.activation_state_digest,
        active_key_id="auth-key-new",
        keys=(old_key, new_key),
    )
    issued = authorization_session_lifecycle.open_session(
        setup,
        custody=old_custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    setup.close()
    barrier = threading.Barrier(2)
    original_begin = authorization_session_lifecycle._begin_immediate

    def synchronized_begin(connection: sqlite3.Connection) -> None:
        barrier.wait(timeout=5)
        original_begin(connection)

    monkeypatch.setattr(
        authorization_session_lifecycle,
        "_begin_immediate",
        synchronized_begin,
    )

    def rotate() -> object:
        connection = sqlite3.connect(database_path, timeout=5)
        try:
            return authorization_session_lifecycle.rotate_session(
                connection,
                custody=rotated_custody,
                bearer=issued.bearer,
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=NOW + 10,
                ttl_seconds=900,
            )
        except authorization_session_lifecycle.AuthorizationSessionUnavailable as error:
            return error
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: rotate(), range(2)))

    winners = [
        outcome
        for outcome in outcomes
        if isinstance(
            outcome,
            authorization_session_lifecycle.AuthorizationSessionIssuance,
        )
    ]
    refusals = [
        outcome
        for outcome in outcomes
        if isinstance(
            outcome,
            authorization_session_lifecycle.AuthorizationSessionUnavailable,
        )
    ]
    assert len(winners) == 1
    assert len(refusals) == 1
    assert refusals[0].args == ("authorization session is unavailable",)

    verification = sqlite3.connect(database_path)
    try:
        winner = winners[0]
        assert (
            authorization_session_lifecycle.resume_session(
                verification,
                custody=rotated_custody,
                bearer=winner.bearer,
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=NOW + 11,
            )
            == winner.context
        )
        with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
            authorization_session_lifecycle.resume_session(
                verification,
                custody=rotated_custody,
                bearer=issued.bearer,
                principal_id="principal:owner:1000",
                issuer_family="cli-local-owner",
                now=NOW + 11,
            )
    finally:
        verification.close()


@pytest.mark.parametrize(
    ("principal_id", "issuer_family", "bearer"),
    (
        ("principal:other", "cli-local-owner", None),
        ("principal:owner:1000", "rest-api-key", None),
        ("principal:owner:1000", "cli-local-owner", "as1.invalid"),
    ),
)
def test_resume_failures_have_one_content_free_shape(
    principal_id: str,
    issuer_family: str,
    bearer: str | None,
) -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable) as raised:
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=issued.bearer if bearer is None else bearer,
            principal_id=principal_id,
            issuer_family=issuer_family,
            now=NOW + 1,
        )

    assert raised.value.code == "AUTHORIZATION_SESSION_UNAVAILABLE"
    assert str(raised.value) == "authorization session is unavailable"
    assert issued.bearer not in str(raised.value)


def test_rotate_keeps_the_session_id_and_immediately_invalidates_the_old_bearer() -> None:
    connection, migration = _connection()
    old_key = _key("auth-key-old", b"o" * 32)
    new_key = _key("auth-key-new", b"n" * 32)
    old_custody = _custody(migration.activation_state_digest, keys=(old_key, new_key))
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=old_custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    rotated_custody = _custody(
        migration.activation_state_digest,
        active_key_id="auth-key-new",
        keys=(old_key, new_key),
    )

    rotated = authorization_session_lifecycle.rotate_session(
        connection,
        custody=rotated_custody,
        bearer=issued.bearer,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW + 10,
        ttl_seconds=900,
    )

    assert rotated.context.session_id == issued.context.session_id
    assert rotated.context.credential_generation == 2
    assert rotated.bearer != issued.bearer
    assert (
        authorization_session_lifecycle.resume_session(
            connection,
            custody=rotated_custody,
            bearer=rotated.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 11,
        )
        == rotated.context
    )
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authorization_session_lifecycle.resume_session(
            connection,
            custody=rotated_custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 11,
        )


def test_rotate_storage_failure_keeps_the_old_bearer_live_and_has_common_shape() -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    connection.execute(
        "CREATE TRIGGER reject_session_rotation BEFORE UPDATE OF locator_digest "
        "ON governance_authorization_sessions BEGIN "
        "SELECT RAISE(ABORT, 'private rotation failure'); END"
    )
    connection.commit()

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable) as raised:
        authorization_session_lifecycle.rotate_session(
            connection,
            custody=custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 10,
            ttl_seconds=900,
        )

    assert str(raised.value) == "authorization session is unavailable"
    assert (
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 11,
        )
        == issued.context
    )
    assert connection.execute(
        "SELECT credential_generation, rotated_at FROM governance_authorization_sessions"
    ).fetchone() == (1, None)


def test_close_dependent_failure_rolls_back_the_session_and_has_common_shape() -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    connection.execute(
        "INSERT INTO governance_session_grants "
        "(grant_id, authorization_session_id, principal_id, issuer_family, audience, "
        "purpose, ceiling, paths, fingerprints, scope_ids, membership_manifest, "
        "policy_fingerprint, token_jti, status, created_at, expires_at) "
        "VALUES ('grant-failure', ?, ?, ?, 'external', 'support', 5, '[]', '[]', "
        "'[]', '[]', ?, 'token-failure', 'active', ?, ?)",
        (
            issued.context.session_id,
            issued.context.principal_id,
            issued.context.issuer_family,
            POLICY_FINGERPRINT,
            NOW,
            NOW + 500,
        ),
    )
    connection.execute(
        "CREATE TRIGGER reject_grant_revocation BEFORE UPDATE OF status "
        "ON governance_session_grants WHEN NEW.status='revoked' BEGIN "
        "SELECT RAISE(ABORT, 'private close failure'); END"
    )
    connection.commit()

    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable) as raised:
        authorization_session_lifecycle.close_session(
            connection,
            custody=custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 20,
        )

    assert str(raised.value) == "authorization session is unavailable"
    assert connection.execute(
        "SELECT status, closed_at FROM governance_authorization_sessions"
    ).fetchone() == ("active", None)
    assert connection.execute(
        "SELECT status, revoked_at FROM governance_session_grants"
    ).fetchone() == ("active", None)
    assert (
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=issued.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 21,
        )
        == issued.context
    )


def test_close_revokes_only_the_bound_sessions_dependent_authority() -> None:
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    first = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    second = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    session_id = first.context.session_id
    common = (
        session_id,
        first.context.principal_id,
        first.context.issuer_family,
        "external",
    )
    connection.execute(
        "INSERT INTO governance_session_purpose "
        "(authorization_session_id, principal_id, issuer_family, audience, purpose, "
        "status, created_at, expires_at) VALUES (?, ?, ?, ?, 'support', 'active', ?, ?)",
        (*common, NOW, NOW + 500),
    )
    connection.execute(
        "INSERT INTO governance_session_grants "
        "(grant_id, authorization_session_id, principal_id, issuer_family, audience, "
        "purpose, ceiling, paths, fingerprints, scope_ids, membership_manifest, "
        "policy_fingerprint, token_jti, status, created_at, expires_at) "
        "VALUES ('grant-1', ?, ?, ?, ?, 'support', 5, '[]', '[]', '[]', '[]', ?, "
        "'token-1', 'active', ?, ?)",
        (*common, POLICY_FINGERPRINT, NOW, NOW + 500),
    )
    connection.execute(
        "INSERT INTO withhold_tokens "
        "(jti, authorization_session_id, principal_id, issuer_family, audience, max_level, "
        "fingerprints, paths, scope_ids, purpose, org_ceiling, status, expires_at, minted_at) "
        "VALUES ('token-1', ?, ?, ?, ?, 5, '[]', '[]', '[]', 'support', 6, 'active', ?, ?)",
        (*common, NOW + 500, NOW),
    )
    connection.commit()

    closed = authorization_session_lifecycle.close_session(
        connection,
        custody=custody,
        bearer=first.bearer,
        principal_id="principal:owner:1000",
        issuer_family="cli-local-owner",
        now=NOW + 20,
    )

    assert closed.session_id == session_id
    assert connection.execute(
        "SELECT status, closed_at FROM governance_authorization_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone() == ("closed", NOW + 20)
    assert connection.execute(
        "SELECT status FROM governance_session_purpose WHERE authorization_session_id=?",
        (session_id,),
    ).fetchone() == ("closed",)
    assert connection.execute(
        "SELECT status, revoked_at FROM governance_session_grants WHERE grant_id='grant-1'"
    ).fetchone() == ("revoked", NOW + 20)
    assert connection.execute(
        "SELECT status, consumed_at FROM withhold_tokens WHERE jti='token-1'"
    ).fetchone() == ("expired", None)
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=first.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 21,
        )
    assert (
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=second.bearer,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=NOW + 21,
        )
        == second.context
    )
