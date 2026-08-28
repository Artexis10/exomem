from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import mutation_lock, state_migration, state_paths, writer_lease
from exomem.governance import (
    authorization_custody,
    authorization_session_lifecycle,
    legacy_v3_placement,
    projection_store,
    projections,
    receipts,
    schema_migration,
    schema_v4,
    store,
)

POLICY_BYTES = (
    b"governance_version: 1\n"
    b"id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
    b"types: [insight]\n"
)
NOTE_BYTES = b"---\ntype: insight\n---\n\n# Governed note\n\nVisible text.\n"


@pytest.fixture(autouse=True)
def _offline_state(
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


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    governance = vault / "Knowledge Base" / "_Governance" / "scopes"
    governance.mkdir(parents=True)
    (governance / "migration.yaml").write_bytes(POLICY_BYTES)
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir()
    (notes / "governed.md").write_bytes(NOTE_BYTES)
    connection = store.open_connection(vault)
    connection.close()
    state_migration.migrate_vault_state_offline(
        vault,
        authority=state_migration.assert_offline_migration_authority(
            source="governance schema migration fixture",
        ),
    )
    return vault


def _schema_version(vault: Path) -> int:
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_forward_migration_plan_is_replayable_and_inert(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())

    first = schema_migration.prepare_forward_migration(vault, now=now)
    replay = schema_migration.prepare_forward_migration(vault, now=now + 1)

    assert replay == first
    assert first.target.activation_epoch == 1
    assert first.target.catalog_generation == 1
    assert first.item_count == 1
    assert first.seed.policy.source_documents == (
        ("scopes/migration.yaml", POLICY_BYTES),
    )
    assert schema_v4.migration_target(first.seed) == first.target
    assert _schema_version(vault) == 3
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()

    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=first.target.policy_fingerprint,
        projector_schema_version=first.target.projector_schema_version,
        catalog_generation=first.target.catalog_generation,
    )
    assert not projection_store.variant_store_path(vault, key).exists()


def test_forward_migration_plan_binds_current_canonical_content(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    first = schema_migration.prepare_forward_migration(vault, now=now)

    (vault / "Knowledge Base" / "Notes" / "governed.md").write_bytes(
        NOTE_BYTES.replace(b"Visible text.", b"Changed text.")
    )
    second = schema_migration.prepare_forward_migration(vault, now=now + 1)

    assert second.target.activation_state_digest != first.target.activation_state_digest
    assert second.projection_rows_digest != first.projection_rows_digest
    assert _schema_version(vault) == 3


def test_forward_migration_plan_binds_the_wal_consistent_v3_store(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    first = schema_migration.prepare_forward_migration(vault, now=now)

    connection = store.open_connection(vault)
    try:
        connection.execute(
            "INSERT INTO governance_session_purpose "
            "(authorization_session, principal_id, purpose, status, prepared_event_id, "
            "created_at, expires_at) VALUES "
            "('legacy-session', 'legacy-principal', 'review', 'active', NULL, ?, ?)",
            (now, now + 60),
        )
        connection.commit()
    finally:
        connection.close()

    second = schema_migration.prepare_forward_migration(vault, now=now + 1)

    assert second.target == first.target
    assert second.source_store_digest != first.source_store_digest
    assert second.plan_digest != first.plan_digest


def test_forward_migration_plan_refuses_ambiguous_markdown(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "Knowledge Base" / "Notes" / "governed.md").write_text(
        "---\ntype: [unterminated\n---\n# Ambiguous\n",
        encoding="utf-8",
    )

    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.prepare_forward_migration(vault, now=int(time.time()))

    assert _schema_version(vault) == 3
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()


def test_forward_migration_plan_summary_is_content_free(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plan = schema_migration.prepare_forward_migration(vault, now=int(time.time()))

    encoded = json.dumps(schema_migration.plan_summary(plan), sort_keys=True)

    assert "Visible text" not in encoded
    assert "governed.md" not in encoded
    assert set(json.loads(encoded)) == {
        "activation_epoch",
        "activation_state_digest",
        "activation_store_id",
        "catalog_generation",
        "item_count",
        "logical_vault_id",
        "policy_fingerprint",
        "policy_generation_id",
        "plan_digest",
        "projection_namespace_id",
        "projection_rows_digest",
        "projector_schema_version",
        "schema_version",
        "source_store_digest",
    }


def test_forward_migration_stage_publishes_only_the_reviewed_namespace(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = schema_migration.prepare_forward_migration(vault, now=now)

    result = schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )

    assert result.plan_digest == plan.plan_digest
    assert result.projection_namespace_id == plan.target.projection_namespace_id
    assert result.projection_rows_digest == plan.projection_rows_digest
    assert result.item_count == plan.item_count
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=plan.target.policy_fingerprint,
        projector_schema_version=plan.target.projector_schema_version,
        catalog_generation=plan.target.catalog_generation,
    )
    assert projection_store.verify_variant_store(
        vault,
        key=key,
        expected_rows_digest=plan.projection_rows_digest,
    ).item_count == plan.item_count
    assert _schema_version(vault) == 3
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()


def test_forward_migration_stage_replays_without_changing_the_terminal(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = schema_migration.prepare_forward_migration(vault, now=now)

    first = schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    replay = schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )

    assert replay == first
    assert _schema_version(vault) == 3


def test_forward_migration_stage_refuses_stale_digest_before_publication(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = schema_migration.prepare_forward_migration(vault, now=now)
    (vault / "Knowledge Base" / "Notes" / "governed.md").write_bytes(
        NOTE_BYTES.replace(b"Visible text.", b"Changed text.")
    )

    with pytest.raises(schema_migration.ForwardMigrationPlanMismatch):
        schema_migration.stage_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 1,
        )

    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=plan.target.policy_fingerprint,
        projector_schema_version=plan.target.projector_schema_version,
        catalog_generation=plan.target.catalog_generation,
    )
    assert not projection_store.variant_store_path(vault, key).exists()
    assert _schema_version(vault) == 3


def test_forward_migration_stage_refuses_drift_after_inert_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = schema_migration.prepare_forward_migration(vault, now=now)
    original = projection_store.stage_variant_store

    def stage_then_drift(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        manifest = original(*args, **kwargs)
        (vault / "Knowledge Base" / "Notes" / "governed.md").write_bytes(
            NOTE_BYTES.replace(b"Visible text.", b"Changed during staging.")
        )
        return manifest

    monkeypatch.setattr(projection_store, "stage_variant_store", stage_then_drift)

    with pytest.raises(schema_migration.ForwardMigrationPlanMismatch):
        schema_migration.stage_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 1,
        )

    assert _schema_version(vault) == 3
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()


def _stage_reviewed_migration(vault: Path, *, now: int):  # noqa: ANN202
    plan = schema_migration.prepare_forward_migration(vault, now=now)
    schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    return plan


def _drain_verified_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    original = authorization_custody.load_authorization_custody

    def load(
        vault_root: Path,
        *,
        now: int,
    ) -> authorization_custody.AuthorizationCustody:
        custody = original(vault_root, now=now)
        membership_record = custody.serving_membership
        assert membership_record is not None
        replicas = tuple(
            replace(
                replica,
                state="DRAINING",
                issuance_stopped=True,
                no_in_flight=True,
            )
            for replica in membership_record.replicas
        )
        return replace(
            custody,
            serving_membership=replace(membership_record, replicas=replicas),
        )

    monkeypatch.setattr(authorization_custody, "load_authorization_custody", load)


def test_forward_migration_cutover_backs_up_v3_before_enrollment_and_replays(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)

    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )

    assert committed.schema_version == 4
    assert committed.target == plan.target
    assert committed.plan_digest == plan.plan_digest
    assert committed.source_store_digest == plan.source_store_digest
    assert committed.backup_reference.startswith(
        "exomem-governance-v3-backup://sha256/"
    )
    assert committed.replayed is False
    assert _schema_version(vault) == 4
    backup_path = schema_migration.forward_migration_backup_path(
        vault,
        plan_digest=plan.plan_digest,
    )
    if os.name != "nt":
        assert backup_path.stat().st_mode & 0o777 == 0o600
    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert custody.control.governance_enrolled is True
    assert custody.control.serving_membership_epoch == 2
    assert custody.serving_membership is not None
    assert custody.serving_membership.epoch == 2
    assert [
        (replica.state, replica.schema_version)
        for replica in custody.serving_membership.replicas
    ] == [("SERVING", 4)]

    backup = schema_migration.verify_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
    )
    assert backup.plan_digest == plan.plan_digest
    assert backup.source_store_digest == plan.source_store_digest
    assert backup.target == plan.target
    restored = sqlite3.connect(":memory:")
    try:
        restored.deserialize(backup.serialized_v3)
        schema_v4.require_exact_v3_connection(restored)
    finally:
        restored.close()

    replay = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 4,
    )
    assert replay == replace(committed, replayed=True)


def test_forward_migration_cutover_refuses_source_drift_before_enrollment(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    connection = store.open_connection(vault)
    try:
        connection.execute(
            "INSERT INTO governance_session_purpose "
            "(authorization_session, principal_id, purpose, status, prepared_event_id, "
            "created_at, expires_at) VALUES "
            "('late-session', 'late-principal', 'review', 'active', NULL, ?, ?)",
            (now, now + 60),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(schema_migration.ForwardMigrationPlanMismatch):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
        )

    assert _schema_version(vault) == 3
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert not Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]).exists()


def test_forward_migration_cutover_rechecks_v3_after_backup_before_enrollment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)

    def drift(point: str) -> None:
        if point != "after_backup":
            return
        connection = store.open_connection(vault)
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

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", drift)

    with pytest.raises(schema_migration.ForwardMigrationPlanMismatch):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
        )

    assert _schema_version(vault) == 3
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()
    assert schema_migration.forward_migration_backup_path(
        vault,
        plan_digest=plan.plan_digest,
    ).exists()


def test_forward_migration_cutover_recovers_after_irreversible_enrollment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    crashed = False

    def crash(point: str) -> None:
        nonlocal crashed
        if point == "after_enrollment" and not crashed:
            crashed = True
            raise schema_migration._ForwardMigrationCrash("enrollment crash")

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash, match="enrollment crash"):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
        )

    assert _schema_version(vault) == 3
    interrupted = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert interrupted.control.governance_enrolled is True
    assert interrupted.serving_membership is not None
    assert interrupted.serving_membership.epoch == 1
    assert interrupted.serving_membership.replicas[0].state == "DRAINING"

    monkeypatch.setattr(
        schema_migration,
        "_forward_migration_barrier",
        lambda _point: None,
    )
    recovered = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 4,
    )
    assert recovered.schema_version == 4
    assert recovered.target == plan.target
    assert recovered.replayed is False


def test_forward_migration_cutover_fails_closed_on_store_drift_after_enrollment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    enroll = authorization_custody.enroll_standalone_v3_migration

    def enroll_then_drift(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        acknowledgement = enroll(*args, **kwargs)
        connection = sqlite3.connect(store.sidecar_path(vault))
        try:
            connection.execute(
                "INSERT INTO governance_session_purpose "
                "(authorization_session, principal_id, purpose, status, "
                "prepared_event_id, created_at, expires_at) VALUES "
                "('post-enroll', 'post-enroll-principal', 'review', 'active', NULL, ?, ?)",
                (now, now + 60),
            )
            connection.commit()
        finally:
            connection.close()
        return acknowledgement

    monkeypatch.setattr(
        authorization_custody,
        "enroll_standalone_v3_migration",
        enroll_then_drift,
    )

    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
        )

    assert _schema_version(vault) == 3
    blocked = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert blocked.control.governance_enrolled is True
    assert blocked.serving_membership is not None
    assert blocked.serving_membership.replicas[0].state == "DRAINING"


def test_forward_migration_cutover_recovers_store_commit_before_membership_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    crashed = False

    def crash(point: str) -> None:
        nonlocal crashed
        if point == "after_store_commit" and not crashed:
            crashed = True
            raise schema_migration._ForwardMigrationCrash("store commit crash")

    monkeypatch.setattr(store, "_schema_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash, match="store commit"):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
        )

    assert _schema_version(vault) == 4
    interrupted = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert interrupted.control.serving_membership_epoch == 1
    assert interrupted.serving_membership is not None
    assert interrupted.serving_membership.epoch == 1

    monkeypatch.setattr(store, "_schema_migration_barrier", lambda _point: None)
    recovered = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 4,
    )
    assert recovered.schema_version == 4
    assert recovered.target == plan.target
    assert recovered.replayed is True


def test_forward_migration_cutover_refuses_catalog_drift_before_serving_v4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    note = vault / "Knowledge Base" / "Notes" / "governed.md"
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    drifted = False

    def drift(point: str) -> None:
        nonlocal drifted
        if point == "after_store_commit" and not drifted:
            drifted = True
            note.write_bytes(NOTE_BYTES.replace(b"Visible text.", b"Late direct edit."))

    monkeypatch.setattr(store, "_schema_migration_barrier", drift)
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
        )

    assert _schema_version(vault) == 4
    blocked = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert blocked.control.serving_membership_epoch == 1
    assert blocked.serving_membership is not None
    assert blocked.serving_membership.replicas[0].state == "DRAINING"

    note.write_bytes(NOTE_BYTES)
    monkeypatch.setattr(store, "_schema_migration_barrier", lambda _point: None)
    recovered = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 4,
    )
    assert recovered.schema_version == 4
    assert recovered.replayed is True


def test_forward_migration_cutover_refuses_a_tampered_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)

    def crash(point: str) -> None:
        if point == "after_backup":
            raise schema_migration._ForwardMigrationCrash("stop after backup")

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash, match="stop after backup"):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 2,
        )
    backup_path = schema_migration.forward_migration_backup_path(
        vault,
        plan_digest=plan.plan_digest,
    )
    raw = bytearray(backup_path.read_bytes())
    raw[-1] ^= 1
    backup_path.write_bytes(raw)

    monkeypatch.setattr(
        schema_migration,
        "_forward_migration_barrier",
        lambda _point: None,
    )
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.commit_forward_migration(
            vault,
            expected_plan_digest=plan.plan_digest,
            now=now + 3,
        )

    assert _schema_version(vault) == 3
    assert not Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).exists()


def test_pre_migration_backup_restore_is_drained_receipt_first_and_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)

    restored = schema_migration.restore_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        expected_backup_reference=committed.backup_reference,
        now=now + 3,
    )

    assert restored.schema_version == 3
    assert restored.plan_digest == plan.plan_digest
    assert restored.source_store_digest == plan.source_store_digest
    assert restored.backup_reference == committed.backup_reference
    assert restored.replayed is False
    assert _schema_version(vault) == 3
    manifest = state_migration._load_manifest(  # noqa: SLF001 - durable marker contract
        state_paths.vault_state_dir(vault),
        vault_root=vault,
    )
    assert manifest is not None
    marker = manifest["governance_rollback"]
    assert marker is not None
    assert marker["operation"] == "governance_schema_v3_backup_restore"
    assert marker["event_id"] == restored.recovery_event_id
    assert marker["phase"] == "complete"
    assert marker["plan_digest"] == restored.recovery_plan_digest
    assert marker["backup_reference"] == committed.backup_reference
    assert marker["backup_plan_digest"] == committed.plan_digest
    assert marker["source_store_digest"] == committed.source_store_digest
    assert marker["schema_fence_generation"] is None
    assert marker["timestamp"] == now + 3
    assert marker["legacy_path"] == "Knowledge Base/.governance.sqlite"
    assert marker["stage_leaf"] == (
        f".governance-v3-rollback-{restored.recovery_event_id}.sqlite"
    )
    connection = store.open_readonly_connection(vault)
    assert connection is not None
    try:
        schema_v4.require_exact_v3_connection(connection)
    finally:
        connection.close()
    custody = authorization_custody.load_authorization_custody(vault, now=now + 4)
    assert custody.control.governance_enrolled is True
    records = receipts.event_records(vault)
    assert [
        (item["phase"], item.get("causation_id"))
        for item in records
        if item.get("operation") == "governance_schema_v3_backup_restore"
        or item.get("causation_id") == restored.recovery_event_id
    ] == [
        ("intent", None),
        ("committed", restored.recovery_event_id),
    ]

    replay = schema_migration.restore_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        expected_backup_reference=committed.backup_reference,
        now=now + 5,
    )
    assert replay == replace(restored, replayed=True)


@pytest.mark.parametrize(
    ("barrier", "crashed_schema_version"),
    [
        ("after_restore_receipt_intent", 4),
        ("after_store_restore", 3),
        ("after_legacy_v3_publication", 3),
        ("after_restore_terminal_durable", 3),
        ("after_restore_receipt_commit", 3),
        ("after_restore_legacy_aligned", 3),
        ("after_restore_schema_fence", 3),
        ("after_restore_complete_marker", 3),
    ],
)
def test_pre_migration_backup_restore_replays_every_durable_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
    crashed_schema_version: int,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)

    def crash(point: str) -> None:
        if point == barrier:
            raise schema_migration._ForwardMigrationCrash(barrier)

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash, match=barrier):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )
    assert _schema_version(vault) == crashed_schema_version

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", lambda _point: None)
    replay = schema_migration.restore_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        expected_backup_reference=committed.backup_reference,
        now=now + 4,
    )

    assert replay.replayed is True
    assert _schema_version(vault) == 3
    records = receipts.event_records(vault)
    assert len(
        [item for item in records if item.get("event_id") == replay.recovery_event_id]
    ) == 1
    assert len(
        [item for item in records if item.get("causation_id") == replay.recovery_event_id]
    ) == 1


def test_pre_migration_backup_restore_refuses_until_v4_membership_is_drained(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )

    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )

    assert _schema_version(vault) == 4
    assert not receipts.event_records(vault)


def test_pre_migration_backup_restore_refuses_a_different_backup_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)

    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=(
                "exomem-governance-v3-backup://sha256/" + "0" * 64
            ),
            now=now + 3,
        )

    assert _schema_version(vault) == 4
    assert not receipts.event_records(vault)


def test_pre_migration_backup_restore_refuses_later_source_or_receipt_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    note = vault / "Knowledge Base" / "Notes" / "governed.md"
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)

    note.write_bytes(NOTE_BYTES.replace(b"Visible text.", b"Later content."))
    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )
    assert _schema_version(vault) == 4
    assert not receipts.event_records(vault)

    note.write_bytes(NOTE_BYTES)
    event_id = receipts.critical_event_id({"later": "durable-evidence"})
    receipts.begin_event(
        vault,
        operation="test_later_durable_evidence",
        prior="a" * 64,
        target="b" * 64,
        event_id=event_id,
    )
    receipts.commit_event(vault, event_id, outcome="recorded")

    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 4,
        )
    assert _schema_version(vault) == 4


def test_pre_migration_backup_restore_refuses_post_cutover_session_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    connection = store.open_authorization_session_connection(vault)
    try:
        issued = authorization_session_lifecycle.open_session(
            connection,
            custody=custody,
            principal_id="principal:owner:1000",
            issuer_family="cli-local-owner",
            now=now + 3,
            ttl_seconds=60,
        )
    finally:
        connection.close()
    _drain_verified_membership(monkeypatch)
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        before_journal_mode = str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).casefold()
    finally:
        connection.close()

    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 4,
        )

    assert _schema_version(vault) == 4
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        assert (
            str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            == before_journal_mode
        )
    finally:
        connection.close()
    connection = store.open_authorization_session_connection(vault)
    try:
        assert connection.execute(
            "SELECT status FROM governance_authorization_sessions WHERE session_id=?",
            (issued.context.session_id,),
        ).fetchone() == ("active",)
    finally:
        connection.close()
    assert not receipts.event_records(vault)


def test_pre_migration_backup_restore_refuses_before_effect_with_live_v4_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)
    reader = sqlite3.connect(store.sidecar_path(vault))
    reader.execute("BEGIN")
    assert reader.execute("PRAGMA user_version").fetchone() == (4,)

    try:
        with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
            schema_migration.restore_forward_migration_backup(
                vault,
                expected_plan_digest=plan.plan_digest,
                expected_backup_reference=committed.backup_reference,
                now=now + 3,
            )
        assert _schema_version(vault) == 4
        assert reader.execute("PRAGMA user_version").fetchone() == (4,)
        assert not receipts.event_records(vault)
    finally:
        reader.close()


def test_pre_migration_backup_restore_refuses_racing_v4_state_after_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)
    later_id = receipts.critical_event_id({"later": "racing-v4-state"})

    def race(point: str) -> None:
        if point != "after_restore_receipt_intent":
            return
        connection = sqlite3.connect(store.sidecar_path(vault))
        try:
            connection.execute(
                "INSERT INTO governance_authorization_sessions "
                "(session_id, locator_digest, verifier, verifier_key_id, "
                "credential_generation, principal_id, issuer_family, cell_id, "
                "logical_vault_id, keyring_id, status, created_at, rotated_at, "
                "expires_at, closed_at) VALUES "
                "('racing-session', ?, ?, 'racing-key', 1, 'racing-principal', "
                "'racing-issuer', 'racing-cell', ?, 'racing-keyring', 'active', "
                "?, NULL, ?, NULL)",
                (
                    b"l" * 32,
                    b"v" * 32,
                    plan.target.logical_vault_id,
                    now + 3,
                    now + 60,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        receipts.begin_event(
            vault,
            operation="test_racing_v4_state",
            prior="a" * 64,
            target="b" * 64,
            event_id=later_id,
        )
        receipts.commit_event(vault, later_id, outcome="recorded")

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", race)
    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )

    assert _schema_version(vault) == 4
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        assert connection.execute(
            "SELECT status FROM governance_authorization_sessions "
            "WHERE session_id='racing-session'"
        ).fetchone() == ("active",)
    finally:
        connection.close()
    assert any(item.get("event_id") == later_id for item in receipts.event_records(vault))


def test_pre_migration_backup_restore_replays_with_later_sorted_inactive_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    connection = store.open_connection(vault)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO receipt_instance(singleton, instance_id) VALUES (1, ?)",
            ("f" * 32,),
        )
        connection.commit()
    finally:
        connection.close()
    baseline_id = receipts.critical_event_id({"baseline": "inactive-chain"})
    receipts.begin_event(
        vault,
        operation="test_inactive_baseline",
        prior="a" * 64,
        target="b" * 64,
        event_id=baseline_id,
    )
    receipts.commit_event(vault, baseline_id, outcome="recorded")
    receipts._close_receipt_connections()  # noqa: SLF001
    connection = store.open_connection(vault)
    try:
        connection.execute(
            "UPDATE receipt_instance SET instance_id=? WHERE singleton=1",
            ("0" * 32,),
        )
        connection.commit()
    finally:
        connection.close()

    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)

    def crash(point: str) -> None:
        if point == "after_restore_receipt_intent":
            raise schema_migration._ForwardMigrationCrash(point)

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )
    assert _schema_version(vault) == 4

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", lambda _point: None)
    replay = schema_migration.restore_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        expected_backup_reference=committed.backup_reference,
        now=now + 4,
    )

    assert replay.replayed is True
    assert _schema_version(vault) == 3
    restore_records = [
        item
        for item in receipts.event_records(vault)
        if item.get("operation") == "governance_schema_v3_backup_restore"
        or item.get("causation_id") == replay.recovery_event_id
    ]
    assert [item["phase"] for item in restore_records] == ["intent", "committed"]


def test_pre_migration_backup_restore_serializes_receipts_through_store_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)
    attempted = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []
    later_id = receipts.critical_event_id({"later": "serialized"})

    def append_later() -> None:
        attempted.set()
        try:
            receipts.begin_event(
                vault,
                operation="test_serialized_later_receipt",
                prior="a" * 64,
                target="b" * 64,
                event_id=later_id,
            )
            receipts.commit_event(vault, later_id, outcome="recorded")
        except (
            OSError,
            RuntimeError,
            sqlite3.Error,
            receipts.ReceiptError,
        ) as error:  # pragma: no cover - asserted below
            failures.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=append_later)

    def start_writer(point: str) -> None:
        if point != "after_restore_receipt_intent":
            return
        worker.start()
        assert attempted.wait(timeout=2)
        assert not finished.wait(timeout=0.05)

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", start_writer)
    restored = schema_migration.restore_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        expected_backup_reference=committed.backup_reference,
        now=now + 3,
    )
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert not failures
    assert finished.is_set()
    records = receipts.event_records(vault)
    restore_terminal = next(
        item for item in records if item.get("causation_id") == restored.recovery_event_id
    )
    later_intent = next(item for item in records if item.get("event_id") == later_id)
    assert later_intent["instance_id"] == restore_terminal["instance_id"]
    assert later_intent["seq"] > restore_terminal["seq"]
    assert _schema_version(vault) == 3


def test_pre_migration_backup_restore_rolls_back_an_in_transaction_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)
    replace_schema = schema_migration._replace_database_schema

    def replace_then_fail(
        destination: sqlite3.Connection,
        source: sqlite3.Connection,
    ) -> None:
        replace_schema(destination, source)
        raise RuntimeError("injected transactional restore failure")

    monkeypatch.setattr(
        schema_migration,
        "_replace_database_schema",
        replace_then_fail,
    )
    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )

    assert _schema_version(vault) == 4
    connection = store.open_active_governance_read_connection(vault)
    try:
        assert schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=plan.target.logical_vault_id,
            expected_activation_store_id=plan.target.activation_store_id,
            expected_activation_epoch=plan.target.activation_epoch,
            expected_activation_state_digest=plan.target.activation_state_digest,
        ).active == plan.target
    finally:
        connection.close()


def test_pre_migration_backup_restore_replays_store_commit_before_schema_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)
    state = [writer_lease.SchemaFenceState(True, 4, 17)]
    transitions: list[tuple[int, int, int]] = []

    class Client:
        def schema_fence(self) -> writer_lease.SchemaFenceState:
            return state[0]

        def transition_schema_fence(
            self,
            *,
            expected_generation: int,
            schema_version: int,
        ) -> writer_lease.SchemaFenceState:
            transitions.append((expected_generation, schema_version, _schema_version(vault)))
            state[0] = writer_lease.SchemaFenceState(
                True,
                schema_version,
                expected_generation + 1,
            )
            return state[0]

    monkeypatch.setattr(
        writer_lease,
        "configured_schema_fence_operator_client",
        lambda: Client(),
    )

    def crash(point: str) -> None:
        if point == "after_store_restore":
            raise schema_migration._ForwardMigrationCrash("restore store crash")

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash, match="restore store"):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )

    assert _schema_version(vault) == 3
    legacy = legacy_v3_placement.legacy_v3_path(vault)
    assert legacy.is_file()
    with sqlite3.connect(legacy) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
    assert state == [writer_lease.SchemaFenceState(True, 4, 17)]
    assert transitions == []

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", lambda _point: None)
    replay = schema_migration.restore_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        expected_backup_reference=committed.backup_reference,
        now=now + 4,
    )

    assert replay.replayed is True
    assert state == [writer_lease.SchemaFenceState(True, 3, 18)]
    assert transitions == [(17, 3, 3)]


@pytest.mark.parametrize("backup_mutation", ("missing", "corrupt"))
def test_pre_migration_backup_restore_seals_after_fence_without_reading_legacy_successors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_mutation: str,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)
    state = [writer_lease.SchemaFenceState(True, 4, 17)]

    class Client:
        def schema_fence(self) -> writer_lease.SchemaFenceState:
            return state[0]

        def transition_schema_fence(
            self,
            *,
            expected_generation: int,
            schema_version: int,
        ) -> writer_lease.SchemaFenceState:
            state[0] = writer_lease.SchemaFenceState(
                True,
                schema_version,
                expected_generation + 1,
            )
            return state[0]

    monkeypatch.setattr(
        writer_lease,
        "configured_schema_fence_operator_client",
        lambda: Client(),
    )

    def crash(point: str) -> None:
        if point == "after_restore_schema_fence":
            raise schema_migration._ForwardMigrationCrash(point)

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )

    legacy = legacy_v3_placement.legacy_v3_path(vault)
    legacy_connection = sqlite3.connect(legacy)
    try:
        instance = legacy_connection.execute(
            "SELECT instance_id FROM receipt_instance WHERE singleton=1"
        ).fetchone()
        assert instance is not None
        current = legacy_connection.execute(
            "SELECT durable_seq, observed_seq, byte_offset FROM receipts_head "
            "WHERE instance_id=?",
            instance,
        ).fetchone()
        assert current is not None
        legacy_connection.execute(
            "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, "
            "observed_hash=?, path=?, byte_offset=? WHERE instance_id=?",
            (
                int(current[0]) + 1,
                "f" * 64,
                int(current[1]) + 1,
                "f" * 64,
                "_Governance/events/legacy-successor.jsonl",
                int(current[2]) + 1,
                instance[0],
            ),
        )
        legacy_connection.commit()
    finally:
        legacy_connection.close()

    backup_path = schema_migration.forward_migration_backup_path(
        vault,
        plan_digest=plan.plan_digest,
    )
    if backup_mutation == "missing":
        backup_path.unlink()
    else:
        raw = bytearray(backup_path.read_bytes())
        raw[-1] ^= 1
        backup_path.write_bytes(raw)

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", lambda _point: None)
    replay = schema_migration.restore_forward_migration_backup(
        vault,
        expected_plan_digest=plan.plan_digest,
        expected_backup_reference=committed.backup_reference,
        now=now + 4,
    )

    assert replay.replayed is True
    assert state == [writer_lease.SchemaFenceState(True, 3, 18)]


@pytest.mark.parametrize("drift", ("external-d1", "fence-generation"))
def test_pre_migration_backup_restore_refuses_postfence_proof_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    plan = _stage_reviewed_migration(vault, now=now)
    committed = schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )
    _drain_verified_membership(monkeypatch)
    state = [writer_lease.SchemaFenceState(True, 4, 17)]

    class Client:
        def schema_fence(self) -> writer_lease.SchemaFenceState:
            return state[0]

        def transition_schema_fence(
            self,
            *,
            expected_generation: int,
            schema_version: int,
        ) -> writer_lease.SchemaFenceState:
            state[0] = writer_lease.SchemaFenceState(
                True,
                schema_version,
                expected_generation + 1,
            )
            return state[0]

    monkeypatch.setattr(
        writer_lease,
        "configured_schema_fence_operator_client",
        lambda: Client(),
    )

    def crash(point: str) -> None:
        if point == "after_restore_schema_fence":
            raise schema_migration._ForwardMigrationCrash(point)

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", crash)
    with pytest.raises(schema_migration._ForwardMigrationCrash):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )

    if drift == "external-d1":
        connection = sqlite3.connect(store.sidecar_path(vault))
        try:
            connection.execute(
                "UPDATE receipt_instance SET instance_id=? WHERE singleton=1",
                ("f" * 32,),
            )
            connection.commit()
        finally:
            connection.close()
    else:
        state[0] = writer_lease.SchemaFenceState(True, 3, 19)

    monkeypatch.setattr(schema_migration, "_forward_migration_barrier", lambda _point: None)
    with pytest.raises(schema_migration.ForwardMigrationRestoreUnavailable):
        schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 4,
        )
