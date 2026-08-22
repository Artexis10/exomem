from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from exomem.governance import authorization_custody, policy, schema_v4, store

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
