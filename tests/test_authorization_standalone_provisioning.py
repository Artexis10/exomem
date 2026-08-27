from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import mutation_lock, sidecar_store, writer_lease
from exomem.governance import (
    authorization_custody,
    authorization_session_lifecycle,
    policy,
    schema_v4,
    store,
)

_PRODUCTION_HOST_CONTROL_ROOT = authorization_custody._standalone_host_control_root


@pytest.fixture(autouse=True)
def _custody_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    host_control = tmp_path / "host-control"
    lease_state = tmp_path / "lease-state"
    lease_state.mkdir(mode=0o700)
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        sid = mutation_lock._windows_current_user_sid()
        mutation_lock._windows_apply_private_dacl(external, sid)
        mutation_lock._windows_apply_private_dacl(lease_state, sid)
    monkeypatch.setattr(
        authorization_custody,
        "_standalone_host_control_root",
        lambda: host_control,
    )
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(external / "authorization-keyring.json"),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(external / "authorization-control.json"),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(external / "authorization-serving-membership.json"),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "standalone")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(lease_state))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    vault = tmp_path / name
    (vault / "Knowledge Base").mkdir(parents=True)
    return vault


def test_host_control_root_ignores_portable_runtime_path_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _PRODUCTION_HOST_CONTROL_ROOT()
    monkeypatch.setenv("HOME", str(tmp_path / "copied-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "copied-state"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "copied-program-data"))
    monkeypatch.setenv("ALLUSERSPROFILE", str(tmp_path / "copied-profile"))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "copied-lease"))

    assert _PRODUCTION_HOST_CONTROL_ROOT() == before
    assert before.is_absolute()


def _target(
    custody: authorization_custody.AuthorizationCustody,
    *,
    digest: str = "d" * 64,
) -> schema_v4.VerifiedActiveGovernanceState:
    return schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=custody.control.logical_vault_id,
        activation_store_id="activation-store-initial",
        activation_epoch=1,
        activation_state_digest=digest,
        policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=1,
        projection_namespace_id="projection-namespace-initial",
    )


def _migration_seed(
    *,
    logical_vault_id: str,
    now: int,
) -> schema_v4.MigrationSeed:
    documents = (
        (
            "scopes/transfer.yaml",
            b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
            b"paths:\n  - Notes/**\n",
        ),
    )
    compiled = policy.compile_documents(dict(documents))
    assert not compiled.empty and not compiled.blocked
    return schema_v4.MigrationSeed(
        activation_store_id="activation-store-transfer",
        logical_vault_id=logical_vault_id,
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source_documents=documents,
            source_fingerprint=compiled.fingerprint,
            conflict_digest="2" * 64,
            compiled_policy=policy.canonical_compiled_bytes(compiled),
            policy_fingerprint=compiled.fingerprint,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-transfer-policy",
            receipt_event_id="receipt-transfer-policy",
            created_at=now,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=now,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="projection-namespace-transfer",
            evidence=b'{"ready":true}',
            ready_at=now,
        ),
        migrated_at=now,
    )


def _enrolled_v4(
    vault: Path,
    *,
    now: int,
) -> tuple[
    authorization_custody.AuthorizationCustody,
    schema_v4.VerifiedActiveGovernanceState,
]:
    authorization_custody.provision_standalone_custody(vault, now=now)
    registered = authorization_custody.load_authorization_custody(vault, now=now + 1)
    seed = _migration_seed(
        logical_vault_id=registered.control.logical_vault_id,
        now=now,
    )
    target = schema_v4.migration_target(seed)
    authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=registered.control,
        target=target,
        now=now + 1,
    )
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        store._migrate(connection)
        sidecar_store.ensure_meta_table(
            connection,
            store.DATA_TABLE,
            "governance-attachment-transfer-test",
        )
        connection.commit()
        schema_v4.migrate_v3_connection(connection, seed)
    finally:
        connection.close()
    return (
        authorization_custody.load_authorization_custody(vault, now=now + 2),
        target,
    )


def _exact_v3(vault: Path) -> Path:
    connection = store.open_connection(vault)
    connection.close()
    return store.sidecar_path(vault)


def _staged_target(
    staged: authorization_custody.StandaloneV3StagingResult,
    *,
    digest: str = "d" * 64,
) -> schema_v4.VerifiedActiveGovernanceState:
    return schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=staged.logical_vault_id,
        activation_store_id="activation-store-initial",
        activation_epoch=1,
        activation_state_digest=digest,
        policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=1,
        projection_namespace_id="projection-namespace-initial",
    )


def test_attachment_identity_is_stable_for_one_root_and_changes_for_a_copy(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = _vault(tmp_path, "copied-vault")

    first = authorization_custody.standalone_attachment_id(vault)
    second = authorization_custody.standalone_attachment_id(vault)

    assert first == second
    assert first.startswith("attachment-v1-")
    assert authorization_custody.standalone_attachment_id(copied) != first


def test_explicit_standalone_provisioning_is_private_idempotent_and_copy_bound(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000

    first = authorization_custody.provision_standalone_custody(vault, now=now)
    keyring_before = first.keyring_path.read_bytes()
    control_before = first.control_path.read_bytes()
    membership_before = first.membership_path.read_bytes()
    replay = authorization_custody.provision_standalone_custody(vault, now=now)
    loaded = authorization_custody.load_authorization_custody(vault, now=now + 1)

    assert replay == first
    assert loaded.control.governance_enrolled is False
    assert loaded.control.activation_store_id is None
    assert loaded.control.registry_attachment_id == first.registry_attachment_id
    assert first.keyring_path.read_bytes() == keyring_before
    assert first.control_path.read_bytes() == control_before
    assert first.membership_path.read_bytes() == membership_before
    assert loaded.serving_membership is not None
    assert loaded.serving_membership.epoch == 1
    assert loaded.local_replica_id == "standalone"
    assert first.cell_id == loaded.keyring.cell_id
    assert first.logical_vault_id == loaded.keyring.logical_vault_id
    assert first.keyring_id == loaded.keyring.keyring_id
    active_secret = loaded.keyring.active_key.key.hex()
    assert active_secret not in repr(first)
    if os.name != "nt":
        assert stat.S_IMODE(first.keyring_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.control_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.membership_path.stat().st_mode) == 0o600

    copied = _vault(tmp_path, "copied-vault")
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(copied, now=now + 1)


def test_host_registry_rejects_record_forged_with_portable_vault_key(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    forged = authorization_custody._host_registry_record(
        custody.control,
        state="SERVING",
        no_in_flight=False,
        updated_at=now + 1,
        host_key_id=custody.keyring.active_key_id,
    )
    registry_path = authorization_custody._host_registry_path(
        custody.control.logical_vault_id
    )
    registry_path.write_bytes(
        authorization_custody._signed_host_registry_bytes(
            forged,
            signing_key=custody.keyring.active_key.key,
        )
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(vault, now=now + 1)


def test_standalone_provisioning_refuses_opaque_hosted_attachment(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    provisioned = authorization_custody.provision_standalone_custody(vault, now=now)
    custody = authorization_custody.load_authorization_custody(vault, now=now + 1)
    opaque_control = replace(
        custody.control,
        registry_attachment_id="hosted-opaque-attachment",
    )
    provisioned.control_path.write_bytes(
        authorization_custody._signed_control_bytes(
            opaque_control,
            signing_key=custody.keyring.active_key.key,
        )
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.provision_standalone_custody(vault, now=now + 1)


def test_existing_exact_v3_stages_identity_without_registering_or_serving(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _exact_v3(vault)
    now = 1_800_000_000

    first = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    keyring_before = first.keyring_path.read_bytes()
    replay = authorization_custody.stage_standalone_v3_custody(vault, now=now + 1)

    assert replay == first
    assert first.keyring_path.read_bytes() == keyring_before
    assert first.registry_attachment_id == authorization_custody.standalone_attachment_id(
        vault
    )
    keyring = authorization_custody.parse_keyring(keyring_before)
    assert first.keyring_id == keyring.keyring_id
    assert first.cell_id == keyring.cell_id
    assert first.logical_vault_id == keyring.logical_vault_id
    assert keyring.active_key.key.hex() not in repr(first)
    if os.name != "nt":
        assert stat.S_IMODE(first.keyring_path.stat().st_mode) == 0o600
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(vault, now=now + 1)
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.provision_standalone_custody(vault, now=now + 1)


@pytest.mark.parametrize(
    "source",
    [
        "absent",
        "corrupt",
        "future",
        "unknown-table",
        "future-column",
        "unknown-index",
        "unknown-trigger",
    ],
)
def test_v3_staging_refuses_nonexact_authority_without_external_registration(
    tmp_path: Path,
    source: str,
) -> None:
    vault = _vault(tmp_path)
    if source != "absent":
        sidecar = _exact_v3(vault)
        if source == "corrupt":
            sidecar.write_bytes(b"not a governance database")
        else:
            connection = sqlite3.connect(sidecar)
            try:
                if source == "future":
                    connection.execute("PRAGMA user_version=4")
                elif source == "unknown-table":
                    connection.execute("CREATE TABLE migration_smuggled_state (value TEXT)")
                elif source == "future-column":
                    connection.execute(
                        "ALTER TABLE governance_session_grants ADD COLUMN future_state TEXT"
                    )
                elif source == "unknown-index":
                    connection.execute(
                        "CREATE INDEX migration_smuggled_index "
                        "ON compiled_policy(compiled_at)"
                    )
                else:
                    connection.execute(
                        "CREATE TRIGGER migration_smuggled_trigger "
                        "AFTER INSERT ON compiled_policy BEGIN SELECT 1; END"
                    )
                connection.commit()
            finally:
                connection.close()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.stage_standalone_v3_custody(
            vault,
            now=1_800_000_000,
        )

    assert not Path(os.environ[authorization_custody.KEYRING_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()


def test_v3_staging_keyring_cannot_be_claimed_by_a_copied_attachment(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = _vault(tmp_path, "copied-vault")
    original_sidecar = _exact_v3(vault)
    copied_sidecar = store.sidecar_path(copied)
    copied_sidecar.write_bytes(original_sidecar.read_bytes())
    now = 1_800_000_000
    first = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    keyring_before = first.keyring_path.read_bytes()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.stage_standalone_v3_custody(copied, now=now + 1)

    assert first.keyring_path.read_bytes() == keyring_before
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()


def test_v3_staging_schema_drift_after_keyring_publish_remains_inert_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    sidecar = _exact_v3(vault)
    now = 1_800_000_000
    original = authorization_custody._publish_private_file
    mutated = False

    def publish_then_drift(path: Path, data: bytes) -> bytes:
        nonlocal mutated
        published = original(path, data)
        if path == Path(os.environ[authorization_custody.KEYRING_FILE_ENV]) and not mutated:
            mutated = True
            connection = sqlite3.connect(sidecar)
            try:
                connection.execute("CREATE TABLE concurrent_schema_state (value TEXT)")
                connection.commit()
            finally:
                connection.close()
        return published

    monkeypatch.setattr(
        authorization_custody,
        "_publish_private_file",
        publish_then_drift,
    )
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.stage_standalone_v3_custody(vault, now=now)

    keyring_path = Path(os.environ[authorization_custody.KEYRING_FILE_ENV])
    keyring_before = keyring_path.read_bytes()
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()

    connection = sqlite3.connect(sidecar)
    try:
        connection.execute("DROP TABLE concurrent_schema_state")
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(authorization_custody, "_publish_private_file", original)

    result = authorization_custody.stage_standalone_v3_custody(vault, now=now + 1)

    assert result.keyring_path.read_bytes() == keyring_before


def test_v3_staging_never_reclassifies_an_existing_registration(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    provisioned = authorization_custody.provision_standalone_custody(vault, now=now)
    control_before = provisioned.control_path.read_bytes()
    membership_before = provisioned.membership_path.read_bytes()
    _exact_v3(vault)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.stage_standalone_v3_custody(vault, now=now + 1)

    assert provisioned.control_path.read_bytes() == control_before
    assert provisioned.membership_path.read_bytes() == membership_before
    assert (
        authorization_custody.load_authorization_custody(
            vault,
            now=now + 1,
        ).control.governance_enrolled
        is False
    )


def test_standalone_control_remains_valid_past_bootstrap_hour(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    initial = authorization_custody.load_authorization_custody(vault, now=now + 1)

    loaded = authorization_custody.load_authorization_custody(
        vault,
        now=now + 24 * 60 * 60,
    )

    assert initial.control.expires_at == initial.keyring.active_key.not_after
    assert loaded.control == initial.control


@pytest.mark.parametrize(
    "relative",
    (
        "Knowledge Base/_Governance",
        "Knowledge Base/.governance.sqlite",
        "Knowledge Base/.governance.sqlite-wal",
        "Knowledge Base/.governance.sqlite-shm",
        "Knowledge Base/.governance.sqlite-journal",
    ),
)
def test_never_enrolled_provisioning_refuses_existing_governance_authority(
    tmp_path: Path,
    relative: str,
) -> None:
    vault = _vault(tmp_path)
    target = vault / relative
    if target.suffix:
        target.write_bytes(b"authority")
    else:
        target.mkdir()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.provision_standalone_custody(
            vault,
            now=1_800_000_000,
        )

    assert not Path(os.environ[authorization_custody.KEYRING_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()


def test_initial_enrollment_is_irreversible_exact_and_idempotent(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    prior = authorization_custody.load_authorization_custody(vault, now=now + 1)
    target = _target(prior)

    first = authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=prior.control,
        target=target,
        now=now + 1,
    )
    replay = authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=prior.control,
        target=target,
        now=now + 1,
    )
    enrolled = authorization_custody.load_authorization_custody(vault, now=now + 1)

    assert first == replay == schema_v4.ActivationRegistryAcknowledgement(
        activation_store_id=target.activation_store_id,
        activation_epoch=1,
        activation_state_digest=target.activation_state_digest,
    )
    assert enrolled.control.governance_enrolled is True
    assert enrolled.control.activation_store_id == target.activation_store_id
    assert enrolled.control.activation_epoch == 1
    assert enrolled.control.activation_state_digest == target.activation_state_digest

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.enroll_initial_activation_tuple(
            vault,
            expected_control=prior.control,
            target=_target(prior, digest="e" * 64),
            now=now + 1,
        )


def test_initial_enrollment_exact_retry_survives_store_creation(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    prior = authorization_custody.load_authorization_custody(vault, now=now + 1)
    target = _target(prior)
    first = authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=prior.control,
        target=target,
        now=now + 1,
    )
    control_before = prior.control_path.read_bytes()
    (vault / "Knowledge Base" / ".governance.sqlite").write_bytes(b"authority")

    replay = authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=prior.control,
        target=target,
        now=now + 1,
    )

    assert replay == first
    assert prior.control_path.read_bytes() == control_before


def test_bound_attachment_refuses_successor_cas_from_a_copied_root(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = _vault(tmp_path, "copied-vault")
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    prior = authorization_custody.load_authorization_custody(vault, now=now + 1)
    authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=prior.control,
        target=_target(prior),
        now=now + 1,
    )
    enrolled = authorization_custody.load_authorization_custody(vault, now=now + 1)
    control_before = enrolled.control_path.read_bytes()
    successor = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=enrolled.control.logical_vault_id,
        activation_store_id="activation-store-initial",
        activation_epoch=2,
        activation_state_digest="e" * 64,
        policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        policy_fingerprint="b" * 64,
        projector_schema_version=1,
        catalog_generation=2,
        projection_namespace_id="projection-namespace-successor",
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.acknowledge_activation_tuple(
            copied,
            expected_control=enrolled.control,
            target=successor,
            now=now + 1,
        )

    assert enrolled.control_path.read_bytes() == control_before


def test_enrollment_before_store_creation_blocks_instead_of_reopening(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    prior = authorization_custody.load_authorization_custody(vault, now=now + 1)
    authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=prior.control,
        target=_target(prior),
        now=now + 1,
    )

    assert not (vault / "Knowledge Base" / ".governance.sqlite").exists()
    loaded = policy.load(vault)
    assert loaded.blocked
    assert not loaded.empty


def test_initial_enrollment_refuses_governance_state_created_after_registration(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    prior = authorization_custody.load_authorization_custody(vault, now=now + 1)
    control_before = prior.control_path.read_bytes()
    (vault / "Knowledge Base" / "_Governance").mkdir()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.enroll_initial_activation_tuple(
            vault,
            expected_control=prior.control,
            target=_target(prior),
            now=now + 1,
        )

    assert prior.control_path.read_bytes() == control_before


def test_keyring_only_interruption_retries_without_rotating_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    original = authorization_custody._publish_private_file
    failed = False

    def interrupt_control(path: Path, data: bytes) -> bytes:
        nonlocal failed
        if path.name == "authorization-control.json" and not failed:
            failed = True
            raise authorization_custody.AuthorizationCustodyUnavailable
        return original(path, data)

    monkeypatch.setattr(
        authorization_custody,
        "_publish_private_file",
        interrupt_control,
    )
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.provision_standalone_custody(vault, now=now)

    keyring_path = Path(os.environ[authorization_custody.KEYRING_FILE_ENV])
    control_path = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
    assert keyring_path.exists()
    assert not control_path.exists()
    keyring_before = keyring_path.read_bytes()

    monkeypatch.setattr(authorization_custody, "_publish_private_file", original)
    copied = _vault(tmp_path, "copied-vault")
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.provision_standalone_custody(copied, now=now)
    assert not control_path.exists()
    assert keyring_path.read_bytes() == keyring_before

    result = authorization_custody.provision_standalone_custody(vault, now=now)

    assert keyring_path.read_bytes() == keyring_before
    assert result.control_path == control_path
    assert authorization_custody.load_authorization_custody(
        vault,
        now=now + 1,
    ).control.governance_enrolled is False


def test_control_before_membership_interruption_retries_without_session_fail_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    original = authorization_custody._publish_private_file
    failed = False

    def interrupt_membership(path: Path, data: bytes) -> bytes:
        nonlocal failed
        if path.name == "authorization-serving-membership.json" and not failed:
            failed = True
            raise authorization_custody.AuthorizationCustodyUnavailable
        return original(path, data)

    monkeypatch.setattr(
        authorization_custody,
        "_publish_private_file",
        interrupt_membership,
    )
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.provision_standalone_custody(vault, now=now)

    control_path = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    control_before = control_path.read_bytes()
    assert not membership_path.exists()
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(
            vault,
            now=now + 1,
        )

    monkeypatch.setattr(authorization_custody, "_publish_private_file", original)
    result = authorization_custody.provision_standalone_custody(vault, now=now)

    assert control_path.read_bytes() == control_before
    assert result.membership_path == membership_path
    assert authorization_custody.load_authorization_custody(
        vault,
        now=now + 1,
    ).serving_membership is not None


def test_standalone_attachment_transfer_requires_drain_ack_and_moves_exclusively(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    shutil.copytree(vault, copied)
    serving = authorization_custody.load_authorization_custody(vault, now=now + 1)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.prepare_standalone_attachment_transfer(
            vault,
            copied,
            expected_control=serving.control,
            now=now + 1,
        )

    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=serving.control,
        now=now + 2,
    )
    assert draining.serving_membership is not None
    assert draining.serving_membership.replicas[0].state == "DRAINING"
    assert draining.serving_membership.replicas[0].issuance_stopped is True
    assert draining.serving_membership.replicas[0].no_in_flight is False

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.prepare_standalone_attachment_transfer(
            vault,
            copied,
            expected_control=draining.control,
            now=now + 2,
        )

    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 3,
    )
    assert drained.serving_membership is not None
    assert drained.serving_membership.replicas[0].no_in_flight is True

    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 3,
    )
    moved = authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 4,
    )
    replay = authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 4,
    )

    assert replay == moved
    assert moved.control.attachment_epoch == drained.control.attachment_epoch + 1
    assert moved.control.registry_attachment_id == (
        authorization_custody.standalone_attachment_id(copied)
    )
    assert moved.keyring.cell_id == drained.keyring.cell_id
    assert moved.keyring.logical_vault_id == drained.keyring.logical_vault_id
    assert moved.keyring.keyring_id == drained.keyring.keyring_id
    assert moved.serving_membership is not None
    assert moved.serving_membership.epoch == drained.serving_membership.epoch + 1
    assert moved.serving_membership.replicas[0].state == "SERVING"

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(vault, now=now + 4)


def test_standalone_attachment_transfer_recovers_membership_first_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    shutil.copytree(vault, copied)
    serving = authorization_custody.load_authorization_custody(vault, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 3,
    )
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 3,
    )
    control_path = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    control_before = control_path.read_bytes()
    membership_before = membership_path.read_bytes()
    original = authorization_custody._replace_control_bytes
    interrupted = False

    def stop_before_control(
        path: Path,
        *,
        expected: bytes,
        target: bytes,
    ) -> None:
        nonlocal interrupted
        if path == control_path and not interrupted:
            interrupted = True
            raise authorization_custody.AuthorizationCustodyUnavailable
        original(path, expected=expected, target=target)

    monkeypatch.setattr(
        authorization_custody,
        "_replace_control_bytes",
        stop_before_control,
    )
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.complete_standalone_attachment_transfer(
            copied,
            acknowledgement=acknowledgement,
            now=now + 4,
        )

    assert control_path.read_bytes() == control_before
    assert membership_path.read_bytes() != membership_before
    assert authorization_custody.load_authorization_custody(
        vault,
        now=now + 4,
    ).serving_membership is None
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(copied, now=now + 4)

    monkeypatch.setattr(
        authorization_custody,
        "_replace_control_bytes",
        original,
    )
    recovered = authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 4,
    )

    assert recovered.control.attachment_epoch == drained.control.attachment_epoch + 1
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(vault, now=now + 4)


def test_standalone_attachment_transfer_rejects_tampered_acknowledgement(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    shutil.copytree(vault, copied)
    serving = authorization_custody.load_authorization_custody(vault, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 3,
    )
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 3,
    )
    tampered = bytearray(acknowledgement)
    tampered[-2] = ord("A") if tampered[-2] != ord("A") else ord("B")

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.complete_standalone_attachment_transfer(
            copied,
            acknowledgement=bytes(tampered),
            now=now + 4,
        )

    assert authorization_custody.load_authorization_custody(
        vault,
        now=now + 4,
    ).control == drained.control


def test_stale_external_bundle_cannot_reattach_source_after_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    copied_external = tmp_path / "copied-external"
    copied_external.mkdir(mode=0o700)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    shutil.copytree(vault, copied)
    for variable in (
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
    ):
        source = Path(os.environ[variable])
        shutil.copy2(source, copied_external / source.name)
    serving = authorization_custody.load_authorization_custody(vault, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 3,
    )
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 3,
    )
    authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 4,
    )

    for variable in (
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
    ):
        source = Path(os.environ[variable])
        monkeypatch.setenv(variable, str(copied_external / source.name))

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(vault, now=now + 4)


def test_predrain_writer_state_snapshot_cannot_revive_source_after_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    stale_external = tmp_path / "stale-external"
    stale_writer_state = tmp_path / "stale-writer-state"
    stale_external.mkdir(mode=0o700)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    shutil.copytree(vault, copied)
    for variable in (
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
    ):
        source = Path(os.environ[variable])
        shutil.copy2(source, stale_external / source.name)
    shutil.copytree(
        writer_lease.get_manager().config.state_dir,
        stale_writer_state,
    )

    serving = authorization_custody.load_authorization_custody(vault, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 3,
    )
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 3,
    )
    moved = authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 4,
    )
    assert moved.control.attachment_epoch == serving.control.attachment_epoch + 1

    for variable in (
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
    ):
        source = Path(os.environ[variable])
        monkeypatch.setenv(variable, str(stale_external / source.name))
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(stale_writer_state),
    )
    writer_lease.reset_managers_for_tests()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(vault, now=now + 4)


def test_stale_predrain_custody_cannot_issue_after_drain_begins(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    serving, _target_state = _enrolled_v4(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        authorization_custody.begin_standalone_attachment_drain(
            vault,
            expected_control=serving.control,
            now=now + 3,
        )

        with pytest.raises(
            authorization_session_lifecycle.AuthorizationSessionUnavailable
        ):
            authorization_session_lifecycle.open_session(
                connection,
                custody=serving,
                principal_id="principal-owner",
                issuer_family="cli",
                now=now + 4,
                ttl_seconds=60,
            )
    finally:
        connection.close()


def test_paused_session_issuance_rechecks_host_registry_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    serving, _target_state = _enrolled_v4(vault, now=now)
    connection = store.open_authorization_session_connection(
        vault,
        check_same_thread=False,
    )
    entered = threading.Event()
    release = threading.Event()
    original = authorization_session_lifecycle.authorization_sessions.issue_credential

    def pause_after_first_readiness(*args: object, **kwargs: object) -> object:
        issued = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return issued

    monkeypatch.setattr(
        authorization_session_lifecycle.authorization_sessions,
        "issue_credential",
        pause_after_first_readiness,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                authorization_session_lifecycle.open_session,
                connection,
                custody=serving,
                principal_id="principal-owner",
                issuer_family="cli",
                now=now + 3,
                ttl_seconds=60,
            )
            assert entered.wait(5)
            authorization_custody.begin_standalone_attachment_drain(
                vault,
                expected_control=serving.control,
                now=now + 3,
            )
            release.set()
            with pytest.raises(
                authorization_session_lifecycle.AuthorizationSessionUnavailable
            ):
                future.result(timeout=5)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_authorization_sessions"
        ).fetchone() == (0,)
    finally:
        release.set()
        connection.close()


def test_drain_waits_for_mutation_boundary_and_blocks_new_mutations(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    serving = authorization_custody.load_authorization_custody(vault, now=now + 1)
    manager = writer_lease.get_manager()
    entered = threading.Event()
    release = threading.Event()

    def hold_mutation() -> None:
        with manager.mutation_guard(vault, attachment_now=now + 1):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        mutation = executor.submit(hold_mutation)
        assert entered.wait(5)
        drain = executor.submit(
            authorization_custody.begin_standalone_attachment_drain,
            vault,
            expected_control=serving.control,
            now=now + 2,
        )
        assert not drain.done()
        release.set()
        mutation.result(timeout=5)
        draining = drain.result(timeout=5)

    assert draining.serving_membership is not None
    assert draining.serving_membership.replicas[0].state == "DRAINING"
    with pytest.raises(writer_lease.OpError, match="registered vault attachment") as error:
        with manager.mutation_guard(vault, attachment_now=now + 3):
            pass
    assert error.value.code == "ATTACHMENT_DRAINING"


def test_attachment_transfer_invalidates_sessions_from_stale_copy(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    now = 1_800_000_000
    serving, target_state = _enrolled_v4(vault, now=now)
    source_connection = store.open_authorization_session_connection(vault)
    try:
        issued = authorization_session_lifecycle.open_session(
            source_connection,
            custody=serving,
            principal_id="principal-owner",
            issuer_family="cli",
            now=now + 3,
            ttl_seconds=120,
        )
        common = (
            issued.context.session_id,
            issued.context.principal_id,
            issued.context.issuer_family,
            "external",
        )
        source_connection.execute(
            "INSERT INTO governance_session_purpose "
            "(authorization_session_id, principal_id, issuer_family, audience, purpose, "
            "status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, 'support', 'active', ?, ?)",
            (*common, now + 3, now + 120),
        )
        source_connection.execute(
            "INSERT INTO governance_session_grants "
            "(grant_id, authorization_session_id, principal_id, issuer_family, audience, "
            "purpose, ceiling, paths, fingerprints, scope_ids, membership_manifest, "
            "policy_fingerprint, token_jti, status, created_at, expires_at) "
            "VALUES ('grant-stale-copy', ?, ?, ?, ?, 'support', 5, '[]', '[]', '[]', "
            "'[]', ?, 'token-stale-copy', 'active', ?, ?)",
            (*common, target_state.policy_fingerprint, now + 3, now + 120),
        )
        source_connection.execute(
            "INSERT INTO withhold_tokens "
            "(jti, authorization_session_id, principal_id, issuer_family, audience, "
            "max_level, fingerprints, paths, scope_ids, purpose, org_ceiling, status, "
            "expires_at, minted_at) "
            "VALUES ('token-stale-copy', ?, ?, ?, ?, 5, '[]', '[]', '[]', 'support', "
            "6, 'active', ?, ?)",
            (*common, now + 120, now + 3),
        )
        source_connection.commit()
    finally:
        source_connection.close()
    shutil.copytree(vault, copied)
    source_connection = store.open_authorization_session_connection(vault)
    try:
        authorization_session_lifecycle.close_session(
            source_connection,
            custody=serving,
            bearer=issued.bearer,
            principal_id="principal-owner",
            issuer_family="cli",
            now=now + 4,
        )
    finally:
        source_connection.close()
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=serving.control,
        now=now + 5,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 6,
    )
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 6,
    )
    moved = authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 7,
    )
    target_connection = store.open_authorization_session_connection(copied)
    try:
        with pytest.raises(
            authorization_session_lifecycle.AuthorizationSessionUnavailable
        ):
            authorization_session_lifecycle.resume_session(
                target_connection,
                custody=moved,
                bearer=issued.bearer,
                principal_id="principal-owner",
                issuer_family="cli",
                now=now + 7,
            )
        assert target_connection.execute(
            "SELECT status FROM governance_authorization_sessions WHERE session_id=?",
            (issued.context.session_id,),
        ).fetchone() == ("closed",)
        assert target_connection.execute(
            "SELECT status FROM governance_session_purpose "
            "WHERE authorization_session_id=?",
            (issued.context.session_id,),
        ).fetchone() == ("revoked",)
        assert target_connection.execute(
            "SELECT status FROM governance_session_grants "
            "WHERE grant_id='grant-stale-copy'"
        ).fetchone() == ("revoked",)
        assert target_connection.execute(
            "SELECT status FROM withhold_tokens WHERE jti='token-stale-copy'"
        ).fetchone() == ("expired",)
    finally:
        target_connection.close()


def test_attachment_transfer_replay_preserves_new_target_session(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    now = 1_800_000_000
    serving, _target_state = _enrolled_v4(vault, now=now)
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=serving.control,
        now=now + 3,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 4,
    )
    shutil.copytree(vault, copied)
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 4,
    )
    moved = authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 5,
    )
    connection = store.open_authorization_session_connection(copied)
    try:
        issued = authorization_session_lifecycle.open_session(
            connection,
            custody=moved,
            principal_id="principal-owner",
            issuer_family="cli",
            now=now + 6,
            ttl_seconds=60,
        )
        replay = authorization_custody.complete_standalone_attachment_transfer(
            copied,
            acknowledgement=acknowledgement,
            now=now + 7,
        )
        resumed = authorization_session_lifecycle.resume_session(
            connection,
            custody=replay,
            bearer=issued.bearer,
            principal_id="principal-owner",
            issuer_family="cli",
            now=now + 7,
        )
    finally:
        connection.close()

    assert resumed.session_id == issued.context.session_id


def test_enrolled_attachment_transfer_requires_exact_target_activation_store(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    now = 1_800_000_000
    authorization_custody.provision_standalone_custody(vault, now=now)
    registered = authorization_custody.load_authorization_custody(vault, now=now + 1)
    authorization_custody.enroll_initial_activation_tuple(
        vault,
        expected_control=registered.control,
        target=_target(registered),
        now=now + 1,
    )
    enrolled = authorization_custody.load_authorization_custody(vault, now=now + 2)
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=enrolled.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 3,
    )
    shutil.copytree(vault, copied)
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 3,
    )
    control_path = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    control_before = control_path.read_bytes()
    membership_before = membership_path.read_bytes()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.complete_standalone_attachment_transfer(
            copied,
            acknowledgement=acknowledgement,
            now=now + 4,
        )

    assert control_path.read_bytes() == control_before
    assert membership_path.read_bytes() == membership_before
    assert authorization_custody.load_authorization_custody(
        vault,
        now=now + 4,
    ).control == drained.control


def test_enrolled_attachment_transfer_preserves_exact_active_store(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = tmp_path / "copied-vault"
    now = 1_800_000_000
    enrolled, target = _enrolled_v4(vault, now=now)
    draining = authorization_custody.begin_standalone_attachment_drain(
        vault,
        expected_control=enrolled.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        vault,
        expected_control=draining.control,
        now=now + 3,
    )
    shutil.copytree(vault, copied)
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        vault,
        copied,
        expected_control=drained.control,
        now=now + 3,
    )

    moved = authorization_custody.complete_standalone_attachment_transfer(
        copied,
        acknowledgement=acknowledgement,
        now=now + 4,
    )
    copied_connection = store.open_active_governance_read_connection(copied)
    try:
        active = schema_v4.load_active_state(
            copied_connection,
            expected_logical_vault_id=moved.control.logical_vault_id,
            expected_activation_store_id=str(moved.control.activation_store_id),
            expected_activation_epoch=int(moved.control.activation_epoch or 0),
            expected_activation_state_digest=str(
                moved.control.activation_state_digest
            ),
        )
    finally:
        copied_connection.close()

    assert active == target
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(vault, now=now + 4)


def test_staged_v3_enrollment_is_irreversible_but_remains_fail_closed(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    sidecar = _exact_v3(vault)
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    target = _staged_target(staged)

    acknowledgement = authorization_custody.enroll_standalone_v3_migration(
        vault,
        target=target,
        now=now + 1,
    )
    custody = authorization_custody.load_authorization_custody(
        vault,
        now=now + 2,
    )

    assert acknowledgement == schema_v4.ActivationRegistryAcknowledgement(
        activation_store_id=target.activation_store_id,
        activation_epoch=1,
        activation_state_digest=target.activation_state_digest,
    )
    assert custody.control.governance_enrolled is True
    assert custody.control.activation_store_id == target.activation_store_id
    assert custody.control.activation_epoch == target.activation_epoch
    assert custody.control.activation_state_digest == target.activation_state_digest
    assert custody.serving_membership is not None
    assert custody.serving_membership.epoch == 1
    assert custody.local_replica_id == "standalone"
    assert sidecar.exists()
    connection = sqlite3.connect(sidecar)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    finally:
        connection.close()
    assert policy.load(vault).blocked
    assert not authorization_session_lifecycle.serving_membership_readiness(
        vault,
        now=now + 2,
    ).ready


def test_staged_v3_enrollment_replay_recovers_missing_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _exact_v3(vault)
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    target = _staged_target(staged)
    original = authorization_custody._publish_private_file
    failed = False

    def interrupt_membership(path: Path, data: bytes) -> bytes:
        nonlocal failed
        if path.name == "authorization-serving-membership.json" and not failed:
            failed = True
            raise authorization_custody.AuthorizationCustodyUnavailable
        return original(path, data)

    monkeypatch.setattr(
        authorization_custody,
        "_publish_private_file",
        interrupt_membership,
    )
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.enroll_standalone_v3_migration(
            vault,
            target=target,
            now=now + 1,
        )

    control_path = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    control_before = control_path.read_bytes()
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(
            vault,
            now=now + 2,
        )
    assert not membership_path.exists()
    assert policy.load(vault).blocked

    monkeypatch.setattr(authorization_custody, "_publish_private_file", original)
    replay = authorization_custody.enroll_standalone_v3_migration(
        vault,
        target=target,
        now=now + 3,
    )

    assert replay.activation_state_digest == target.activation_state_digest
    assert control_path.read_bytes() == control_before
    assert membership_path.exists()
    assert authorization_custody.load_authorization_custody(
        vault,
        now=now + 4,
    ).serving_membership is not None


@pytest.mark.parametrize(
    ("change", "target_change"),
    [
        (None, {"logical_vault_id": "wrong-vault"}),
        (None, {"activation_epoch": 2}),
        ("schema", {}),
    ],
)
def test_staged_v3_enrollment_refuses_wrong_target_or_changed_schema(
    tmp_path: Path,
    change: str | None,
    target_change: dict[str, object],
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    sidecar = _exact_v3(vault)
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    target = replace(_staged_target(staged), **target_change)
    if change == "schema":
        connection = sqlite3.connect(sidecar)
        try:
            connection.execute("CREATE TABLE unexpected_enrollment_state(value TEXT)")
            connection.commit()
        finally:
            connection.close()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.enroll_standalone_v3_migration(
            vault,
            target=target,
            now=now + 1,
        )

    assert not Path(
        os.environ[authorization_custody.CONTROL_FILE_ENV]
    ).exists()
    assert not Path(
        os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]
    ).exists()


def test_staged_v3_enrollment_refuses_copied_attachment_and_target_rewrite(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = _vault(tmp_path, "copied-vault")
    now = 1_800_000_000
    _exact_v3(vault)
    _exact_v3(copied)
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    target = _staged_target(staged)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.enroll_standalone_v3_migration(
            copied,
            target=target,
            now=now + 1,
        )

    authorization_custody.enroll_standalone_v3_migration(
        vault,
        target=target,
        now=now + 1,
    )
    control_path = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    control_before = control_path.read_bytes()
    membership_before = membership_path.read_bytes()

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.enroll_standalone_v3_migration(
            vault,
            target=_staged_target(staged, digest="e" * 64),
            now=now + 2,
        )

    assert control_path.read_bytes() == control_before
    assert membership_path.read_bytes() == membership_before


def test_staged_v3_enrollment_refuses_corrupt_existing_membership(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = 1_800_000_000
    _exact_v3(vault)
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    target = _staged_target(staged)
    authorization_custody.enroll_standalone_v3_migration(
        vault,
        target=target,
        now=now + 1,
    )
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    membership_path.write_bytes(b"{}")

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.enroll_standalone_v3_migration(
            vault,
            target=target,
            now=now + 2,
        )
