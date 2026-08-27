from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import mutation_lock, writer_lease
from exomem.governance import authorization_custody, policy, schema_v4, store


@pytest.fixture(autouse=True)
def _custody_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    lease_state = tmp_path / "lease-state"
    lease_state.mkdir(mode=0o700)
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        sid = mutation_lock._windows_current_user_sid()
        mutation_lock._windows_apply_private_dacl(external, sid)
        mutation_lock._windows_apply_private_dacl(lease_state, sid)
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


def _exact_v3(vault: Path) -> Path:
    connection = store.open_connection(vault)
    connection.close()
    return store.sidecar_path(vault)


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
    interrupted = authorization_custody.load_authorization_custody(
        vault,
        now=now + 1,
    )
    assert interrupted.serving_membership is None

    monkeypatch.setattr(authorization_custody, "_publish_private_file", original)
    result = authorization_custody.provision_standalone_custody(vault, now=now)

    assert control_path.read_bytes() == control_before
    assert result.membership_path == membership_path
    assert authorization_custody.load_authorization_custody(
        vault,
        now=now + 1,
    ).serving_membership is not None
