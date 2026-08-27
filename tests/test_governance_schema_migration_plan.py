from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import mutation_lock, writer_lease
from exomem.governance import (
    authorization_custody,
    projection_store,
    projections,
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
