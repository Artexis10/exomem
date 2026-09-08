from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import mutation_lock, state_migration, writer_lease
from exomem.governance import (
    authorization_custody,
    authorization_serving_membership,
    schema_migration,
    store,
)

POLICY_BYTES = b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntypes: [insight]\n"


@pytest.fixture(autouse=True)
def _offline_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    lease_state = tmp_path / "lease-state"
    lease_state.mkdir(mode=0o700)
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        sid = mutation_lock._windows_current_user_sid()
        mutation_lock._windows_apply_private_dacl(external, sid)
        mutation_lock._windows_apply_private_dacl(lease_state, sid)
    monkeypatch.setattr(
        authorization_custody,
        "_standalone_host_control_root",
        lambda: tmp_path / "host-control",
    )
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(external / "keyring.json"),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(external / "control.json"),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(external / "membership.json"),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "hosted-replica")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(lease_state))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    governance = vault / "Knowledge Base" / "_Governance" / "scopes"
    governance.mkdir(parents=True)
    (governance / "migration.yaml").write_bytes(POLICY_BYTES)
    state_migration.migrate_vault_state_offline(
        vault,
        authority=state_migration.assert_offline_migration_authority(
            source="hosted governance migration fixture",
        ),
    )
    return vault


def _write_hosted_custody(
    vault: Path,
    *,
    now: int,
    enrolled_target: object | None = None,
) -> None:
    """Create an authenticated Hosted custody fixture before opening v3."""

    keyring_path = Path(os.environ[authorization_custody.KEYRING_FILE_ENV])
    control_path = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    if enrolled_target is not None:
        custody = authorization_custody.load_authorization_custody(vault, now=now)
        enrolled = replace(
            custody.control,
            governance_enrolled=True,
            activation_store_id=enrolled_target.activation_store_id,
            activation_epoch=enrolled_target.activation_epoch,
            activation_state_digest=enrolled_target.activation_state_digest,
        )
        control_path.write_bytes(
            authorization_custody._signed_control_bytes(  # noqa: SLF001
                enrolled,
                signing_key=custody.keyring.active_key.key,
            ),
        )
        return
    if keyring_path.exists():
        keyring = authorization_custody.parse_keyring(keyring_path.read_bytes())
    else:
        keyring = authorization_custody._new_standalone_staging_keyring(  # noqa: SLF001
            attachment_id="attachment-v1-" + "b" * 64,
            current_time=now,
        )
        authorization_custody._publish_private_artifact(  # noqa: SLF001
            keyring_path,
            authorization_custody._keyring_bytes(keyring),  # noqa: SLF001
            maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
        )
    control = authorization_custody.AuthorizationControlRecord(
        version=1,
        keyring_id=keyring.keyring_id,
        cell_id=keyring.cell_id,
        logical_vault_id=keyring.logical_vault_id,
        registry_attachment_id="hosted-attachment-v1-" + "a" * 64,
        attachment_epoch=7,
        governance_enrolled=False,
        activation_store_id=None,
        activation_epoch=None,
        activation_state_digest=None,
        serving_membership_epoch=9,
        serving_membership_digest="0" * 64,
        issued_at=now,
        expires_at=now + 3600,
        signing_key_id=keyring.active_key_id,
    )
    target = enrolled_target
    provisional = replace(
        control,
        registry_attachment_id="hosted-attachment-v1-" + "a" * 64,
        attachment_epoch=7,
        governance_enrolled=target is not None,
        activation_store_id=None if target is None else target.activation_store_id,
        activation_epoch=None if target is None else target.activation_epoch,
        activation_state_digest=(None if target is None else target.activation_state_digest),
        serving_membership_epoch=9,
        serving_membership_digest="0" * 64,
    )
    membership = authorization_custody._standalone_membership_bytes(  # noqa: SLF001
        keyring=keyring,
        control=provisional,
        replica_id="hosted-replica",
        state="DRAINING",
        schema_version=3,
        issuance_stopped=True,
        no_in_flight=True,
        previous_epoch_digest="c" * 64,
    )
    hosted_control = replace(
        provisional,
        serving_membership_digest=(
            authorization_serving_membership.serving_membership_digest(membership)
        ),
    )
    signing_key = keyring.active_key.key
    control_path.write_bytes(
        authorization_custody._signed_control_bytes(  # noqa: SLF001
            hosted_control,
            signing_key=signing_key,
        )
    )
    membership_path.write_bytes(membership)
    if os.name != "nt":
        control_path.chmod(0o600)
        membership_path.chmod(0o600)


def _configure_fixed_hosted_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = {
        "HOSTED_KEYRING_FILE": Path(os.environ[authorization_custody.KEYRING_FILE_ENV]),
        "HOSTED_CONTROL_FILE": Path(os.environ[authorization_custody.CONTROL_FILE_ENV]),
        "HOSTED_MEMBERSHIP_FILE": Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]),
    }
    for name, path in paths.items():
        monkeypatch.setattr(authorization_custody, name, path)


def _open_v3(vault: Path) -> None:
    connection = store.open_connection(vault)
    connection.close()


def _schema_version(vault: Path) -> int:
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_hosted_drained_unenrolled_custody_prepares_without_standalone_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)

    def no_standalone(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Hosted preparation must not stage standalone custody")

    monkeypatch.setattr(
        authorization_custody,
        "stage_standalone_v3_custody",
        no_standalone,
    )

    plan = schema_migration.prepare_forward_migration(vault, now=now + 1)

    assert plan.target.logical_vault_id
    assert _schema_version(vault) == 3


def test_hosted_partial_fixed_custody_refuses_without_standalone_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    monkeypatch.delenv(authorization_custody.MEMBERSHIP_FILE_ENV)
    monkeypatch.setattr(
        authorization_custody,
        "stage_standalone_v3_custody",
        lambda *_args, **_kwargs: pytest.fail("must not stage standalone custody"),
    )

    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.prepare_forward_migration(vault, now=now + 1)


def test_hosted_backup_uses_the_explicit_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    plan = schema_migration.prepare_forward_migration(vault, now=now + 1)
    schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    backup_root = tmp_path / "private-backups"
    backup_root.mkdir(mode=0o700)

    backup = schema_migration.prepare_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
        backup_root=backup_root,
    )

    assert backup.plan_digest == plan.plan_digest
    assert (
        schema_migration.forward_migration_backup_path(
            vault,
            plan_digest=plan.plan_digest,
            backup_root=backup_root,
        ).parent
        == backup_root
    )


@pytest.mark.parametrize("enrollment_point", ["before", "during-publication"])
def test_hosted_backup_cannot_be_created_after_enrollment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enrollment_point: str,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    plan = schema_migration.prepare_forward_migration(vault, now=now + 1)
    schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    backup_root = tmp_path / "private-backups"
    backup_root.mkdir(mode=0o700)
    if enrollment_point == "before":
        _write_hosted_custody(vault, now=now + 2, enrolled_target=plan.target)
    else:
        publish = authorization_custody._publish_private_artifact  # noqa: SLF001

        def enroll_during_publication(path: Path, data: bytes, *, maximum_bytes: int) -> bytes:
            _write_hosted_custody(vault, now=now + 2, enrolled_target=plan.target)
            return publish(path, data, maximum_bytes=maximum_bytes)

        monkeypatch.setattr(
            authorization_custody, "_publish_private_artifact", enroll_during_publication
        )
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.prepare_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 3,
            backup_root=backup_root,
        )
    assert len(list(backup_root.iterdir())) == (0 if enrollment_point == "before" else 1)
    assert _schema_version(vault) == 3


@pytest.mark.parametrize(
    "successor_state",
    [
        None,
        "DRAINING",
        "SERVING",
        "renewed-SERVING",
        "same-epoch",
        "wrong-predecessor",
        "skipped-epoch",
        "control-only-window",
    ],
)
def test_hosted_commit_requires_enrollment_and_preserves_custody_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    successor_state: str | None,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    plan = schema_migration.prepare_forward_migration(vault, now=now + 1)
    schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    backup_root = tmp_path / "private-backups"
    backup_root.mkdir(mode=0o700)
    schema_migration.prepare_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
        backup_root=backup_root,
    )

    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.commit_enrolled_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
            backup_root=backup_root,
        )

    _write_hosted_custody(vault, now=now + 3, enrolled_target=plan.target)
    custody_paths = tuple(
        Path(os.environ[name])
        for name in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
            authorization_custody.MEMBERSHIP_FILE_ENV,
        )
    )
    before = tuple(path.read_bytes() for path in custody_paths)

    committed = schema_migration.commit_enrolled_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 4,
        backup_root=backup_root,
    )

    assert committed.target == plan.target
    assert committed.replayed is False
    assert _schema_version(vault) == 4
    assert tuple(path.read_bytes() for path in custody_paths) == before
    if successor_state is not None:
        custody = authorization_custody.load_authorization_custody(vault, now=now + 5)
        advance = {"same-epoch": 0, "skipped-epoch": 2}.get(successor_state, 1)
        control = replace(
            custody.control,
            serving_membership_epoch=custody.control.serving_membership_epoch + advance,
        )
        if successor_state == "renewed-SERVING":
            control = replace(
                control, issued_at=control.issued_at + 1, expires_at=control.expires_at + 1
            )
        membership = authorization_custody._standalone_membership_bytes(  # noqa: SLF001
            keyring=custody.keyring,
            control=control,
            replica_id="hosted-replica",
            state="DRAINING" if successor_state == "DRAINING" else "SERVING",
            schema_version=4,
            issuance_stopped=successor_state == "DRAINING",
            no_in_flight=successor_state == "DRAINING",
            previous_epoch_digest=(
                "d" * 64
                if successor_state == "wrong-predecessor"
                else custody.control.serving_membership_digest
            ),
        )
        control = replace(
            control,
            serving_membership_digest=authorization_serving_membership.serving_membership_digest(
                membership
            ),
        )
        if successor_state == "control-only-window":
            control = replace(control, issued_at=control.issued_at + 1)
        custody_paths[1].write_bytes(
            authorization_custody._signed_control_bytes(  # noqa: SLF001
                control,
                signing_key=custody.keyring.active_key.key,
            ),
        )
        custody_paths[2].write_bytes(membership)
        before = tuple(path.read_bytes() for path in custody_paths)
    if successor_state in {
        "same-epoch",
        "wrong-predecessor",
        "skipped-epoch",
        "control-only-window",
    }:
        assert (
            authorization_custody.load_authorization_custody(vault, now=now + 5).serving_membership
            is not None
        )
        with pytest.raises(schema_migration.ForwardMigrationUnavailable):
            schema_migration.commit_enrolled_forward_migration(
                vault,
                expected_plan_digest=plan.plan_digest,
                now=now + 5,
                backup_root=backup_root,
            )
        assert tuple(path.read_bytes() for path in custody_paths) == before
        assert _schema_version(vault) == 4
        return
    replay = schema_migration.commit_enrolled_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 5,
        backup_root=backup_root,
    )
    assert replay.replayed is True
    assert replay.target == committed.target
    assert replay.backup_reference == committed.backup_reference
    assert tuple(path.read_bytes() for path in custody_paths) == before


@pytest.mark.parametrize("phase", ["prepare", "replay"])
@pytest.mark.parametrize("field", ["control_digest", "keyring_digest", "accepted_key_ids"])
def test_hosted_migration_rejects_authenticated_but_inconsistent_attestations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    field: str,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    backup_root = tmp_path / "private-backups"
    backup_root.mkdir(mode=0o700)
    plan = schema_migration.prepare_forward_migration(vault, now=now + 1)
    if phase == "replay":
        schema_migration.stage_forward_migration(
            vault, expected_plan_digest=plan.plan_digest, now=now + 1
        )
        schema_migration.prepare_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 1,
            backup_root=backup_root,
        )
        _write_hosted_custody(vault, now=now + 1, enrolled_target=plan.target)
        schema_migration.commit_enrolled_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 1,
            backup_root=backup_root,
        )
    custody = authorization_custody.load_authorization_custody(vault, now=now + 2)
    record = custody.serving_membership
    assert record is not None
    replica = record.replicas[0]
    changed = (
        tuple(sorted((*replica.accepted_key_ids, "unavailable-key")))
        if field == "accepted_key_ids"
        else "a" * 64
    )
    record = replace(record, replicas=(replace(replica, **{field: changed}),))
    raw = authorization_serving_membership.encode_serving_membership(
        record,
        verifier_keys={key.key_id: key.key for key in custody.keyring.accepted_keys},
    )
    control = replace(
        custody.control,
        serving_membership_digest=authorization_serving_membership.serving_membership_digest(raw),
    )
    Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).write_bytes(raw)
    Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).write_bytes(
        authorization_custody._signed_control_bytes(  # noqa: SLF001
            control,
            signing_key=custody.keyring.active_key.key,
        ),
    )
    assert (
        authorization_custody.load_authorization_custody(vault, now=now + 2).serving_membership
        == record
    )
    before = store.sidecar_path(vault).read_bytes()
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        if phase == "prepare":
            schema_migration.prepare_forward_migration(vault, now=now + 2)
        else:
            schema_migration.commit_enrolled_forward_migration(
                vault,
                expected_plan_digest=plan.plan_digest,
                now=now + 2,
                backup_root=backup_root,
            )
    assert store.sidecar_path(vault).read_bytes() == before


def test_hosted_mode_missing_all_custody_never_initializes_standalone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _open_v3(vault)
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    for name in (
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
    ):
        monkeypatch.delenv(name)
    monkeypatch.setattr(
        authorization_custody,
        "stage_standalone_v3_custody",
        lambda *_args, **_kwargs: pytest.fail("Hosted mode must never create standalone custody"),
    )
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.prepare_forward_migration(vault, now=now + 1)


@pytest.mark.parametrize("kind", ["absent", "inside-vault", "public", "symlink"])
def test_hosted_backup_rejects_unsafe_or_implicit_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    backup_root = None
    if kind != "absent":
        backup_root = (vault if kind == "inside-vault" else tmp_path) / "backups"
        backup_root.mkdir(mode=0o755 if kind == "public" else 0o700)
        if kind == "public" and os.name == "nt":
            pytest.skip("POSIX permission fixture")
        if kind == "symlink":
            linked = tmp_path / "linked-backups"
            try:
                linked.symlink_to(backup_root, target_is_directory=True)
            except OSError:
                pytest.skip("symlink fixture unavailable")
            backup_root = linked
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.forward_migration_backup_path(
            vault,
            plan_digest="a" * 64,
            backup_root=backup_root,
        )


@pytest.mark.parametrize("aliased_inside_vault", [False, True])
def test_hosted_backup_rejects_ancestor_symlink_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    aliased_inside_vault: bool,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    plan = schema_migration.prepare_forward_migration(vault, now=now + 1)
    schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    target = (vault if aliased_inside_vault else tmp_path) / "private-target"
    backup_root = target / "a" / "b"
    backup_root.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "backup-alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink fixture unavailable")
    before = sorted(path.relative_to(target) for path in target.rglob("*"))

    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.prepare_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 1,
            backup_root=alias / "a" / "b",
        )

    assert sorted(path.relative_to(target) for path in target.rglob("*")) == before


@pytest.mark.parametrize(
    "drift",
    [
        "plan",
        "source-store",
        "policy",
        "enrolled-target",
        "attachment",
        "membership",
        "control-window",
    ],
)
def test_hosted_commit_rejects_drift_without_standalone_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    plan = schema_migration.prepare_forward_migration(vault, now=now + 1)
    schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    schema_migration.prepare_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
        backup_root=backup_root,
    )
    target = plan.target
    if drift == "enrolled-target":
        target = replace(target, activation_state_digest="f" * 64)
    _write_hosted_custody(vault, now=now + 2, enrolled_target=target)
    if drift == "source-store":
        connection = sqlite3.connect(store.sidecar_path(vault))
        try:
            connection.execute(
                "INSERT INTO governance_session_purpose "
                "(authorization_session, principal_id, purpose, status, "
                "prepared_event_id, created_at, expires_at) VALUES "
                "('racing-session', 'racing-principal', 'review', 'active', NULL, ?, ?)",
                (now, now + 60),
            )
            connection.commit()
        finally:
            connection.close()
    elif drift == "policy":
        (vault / "Knowledge Base/_Governance/scopes/migration.yaml").write_bytes(
            POLICY_BYTES + b"# changed after preparation\n",
        )
    elif drift in {"attachment", "membership", "control-window"}:
        custody = authorization_custody.load_authorization_custody(vault, now=now + 2)
        control = custody.control
        if drift == "attachment":
            control = replace(control, attachment_epoch=control.attachment_epoch + 1)
        elif drift == "control-window":
            control = replace(control, issued_at=control.issued_at + 1)
        else:
            control = replace(
                control, serving_membership_epoch=control.serving_membership_epoch + 1
            )
            membership = authorization_custody._standalone_membership_bytes(  # noqa: SLF001
                keyring=custody.keyring,
                control=control,
                replica_id="hosted-replica",
                state="DRAINING",
                schema_version=3,
                issuance_stopped=True,
                no_in_flight=True,
                previous_epoch_digest=custody.control.serving_membership_digest,
            )
            Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).write_bytes(membership)
            control = replace(
                control,
                serving_membership_digest=authorization_serving_membership.serving_membership_digest(
                    membership,
                ),
            )
        Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).write_bytes(
            authorization_custody._signed_control_bytes(  # noqa: SLF001
                control,
                signing_key=custody.keyring.active_key.key,
            ),
        )
        assert (
            authorization_custody.load_authorization_custody(vault, now=now + 3).control == control
        )
    for name in (
        "stage_standalone_v3_custody",
        "enroll_standalone_v3_migration",
        "complete_standalone_v4_migration",
    ):
        monkeypatch.setattr(
            authorization_custody,
            name,
            lambda *_args, **_kwargs: pytest.fail("no standalone effect is permitted"),
        )
    custody_before = tuple(
        Path(os.environ[name]).read_bytes()
        for name in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
            authorization_custody.MEMBERSHIP_FILE_ENV,
        )
    )
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.commit_enrolled_forward_migration(
            vault,
            expected_plan_digest="b" * 64 if drift == "plan" else plan.plan_digest,
            now=now + 3,
            backup_root=backup_root,
        )
    assert _schema_version(vault) == 3
    assert (
        tuple(
            Path(os.environ[name]).read_bytes()
            for name in (
                authorization_custody.KEYRING_FILE_ENV,
                authorization_custody.CONTROL_FILE_ENV,
                authorization_custody.MEMBERSHIP_FILE_ENV,
            )
        )
        == custody_before
    )
