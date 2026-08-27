from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import mutation_lock, writer_lease
from exomem.governance import authorization_custody, policy, schema_v4, store

POLICY_PATH = "scopes/migration.yaml"
POLICY_BYTES = (
    b"governance_version: 1\n"
    b"id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
    b"paths:\n"
    b"  - Notes/**\n"
)


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
    governance = vault / "Knowledge Base" / "_Governance" / "scopes"
    governance.mkdir(parents=True)
    (governance / "migration.yaml").write_bytes(POLICY_BYTES)
    connection = store.open_connection(vault)
    connection.close()
    return vault


def _seed(
    vault: Path,
    staged: authorization_custody.StandaloneV3StagingResult,
    *,
    now: int,
    catalog_generation: int = 1,
) -> schema_v4.MigrationSeed:
    snapshot = policy.observe_authoring_snapshot(vault)
    assert snapshot is not None
    compiled = policy.compile_documents(dict(snapshot.documents))
    assert not compiled.empty and not compiled.blocked
    return schema_v4.MigrationSeed(
        activation_store_id="activation-store-initial",
        logical_vault_id=staged.logical_vault_id,
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source_documents=snapshot.documents,
            source_fingerprint=snapshot.source_fingerprint,
            conflict_digest=snapshot.conflict_set_digest,
            compiled_policy=policy.canonical_compiled_bytes(compiled),
            policy_fingerprint=compiled.fingerprint,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-migration-policy",
            receipt_event_id="receipt-migration-policy",
            created_at=now,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=catalog_generation,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=now,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id=f"projection-namespace-{catalog_generation}",
            evidence=b'{"ready":true}',
            ready_at=now,
        ),
        migrated_at=now,
    )


def _stage_and_enroll(
    vault: Path,
    *,
    now: int,
) -> tuple[schema_v4.MigrationSeed, schema_v4.VerifiedActiveGovernanceState]:
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    seed = _seed(vault, staged, now=now + 1)
    target = schema_v4.migration_target(seed)
    authorization_custody.enroll_standalone_v3_migration(
        vault,
        target=target,
        now=now,
    )
    return seed, target


def _schema_version(vault: Path) -> int:
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_enrolled_exact_v3_store_migrates_to_the_precommitted_target(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    seed, target = _stage_and_enroll(vault, now=now)
    assert policy.load(vault).blocked

    active = store.migrate_enrolled_v3_store(vault, seed=seed, now=now + 2)

    assert active == target
    assert _schema_version(vault) == 4
    custody = authorization_custody.load_authorization_custody(
        vault,
        now=now + 3,
    )
    assert custody.control.governance_enrolled is True
    assert custody.control.activation_store_id == target.activation_store_id
    assert custody.control.activation_epoch == target.activation_epoch
    assert custody.control.activation_state_digest == target.activation_state_digest
    connection = store.open_active_governance_read_connection(vault)
    try:
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=target.logical_vault_id,
            expected_activation_store_id=target.activation_store_id,
            expected_activation_epoch=target.activation_epoch,
            expected_activation_state_digest=target.activation_state_digest,
        )
    finally:
        connection.close()
    assert snapshot.active == target
    loaded = policy.load(vault)
    assert not loaded.empty
    assert not loaded.blocked
    assert loaded.fingerprint == target.policy_fingerprint


def test_store_migration_post_commit_crash_replays_exact_v4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    seed, target = _stage_and_enroll(vault, now=now)
    crashed = False

    def crash(point: str) -> None:
        nonlocal crashed
        if point == "after_store_commit" and not crashed:
            crashed = True
            raise RuntimeError("injected post-commit crash")

    monkeypatch.setattr(store, "_schema_migration_barrier", crash)
    with pytest.raises(RuntimeError, match="post-commit"):
        store.migrate_enrolled_v3_store(vault, seed=seed, now=now + 2)

    assert _schema_version(vault) == 4
    monkeypatch.setattr(store, "_schema_migration_barrier", lambda _point: None)
    assert store.migrate_enrolled_v3_store(
        vault,
        seed=seed,
        now=now + 3,
    ) == target


@pytest.mark.parametrize("fault", ["not-enrolled", "target", "workspace", "schema"])
def test_store_migration_refuses_unsafe_preconditions_without_changing_v3(
    tmp_path: Path,
    fault: str,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    seed = _seed(vault, staged, now=now + 1)
    target = schema_v4.migration_target(seed)
    if fault != "not-enrolled":
        authorization_custody.enroll_standalone_v3_migration(
            vault,
            target=target,
            now=now,
        )
    if fault == "target":
        seed = _seed(vault, staged, now=now + 1, catalog_generation=2)
    elif fault == "workspace":
        (vault / "Knowledge Base" / "_Governance" / POLICY_PATH).write_bytes(
            POLICY_BYTES + b"description: changed after review\n"
        )
    elif fault == "schema":
        connection = sqlite3.connect(store.sidecar_path(vault))
        try:
            connection.execute("CREATE TABLE unexpected_migration_state(value TEXT)")
            connection.commit()
        finally:
            connection.close()

    with pytest.raises(
        (
            authorization_custody.AuthorizationCustodyUnavailable,
            schema_v4.SchemaV4Error,
            store.UnsupportedGovernanceSchema,
        )
    ):
        store.migrate_enrolled_v3_store(vault, seed=seed, now=now + 2)

    assert _schema_version(vault) == 3


def test_store_migration_refuses_a_copied_attachment(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    copied = _vault(tmp_path, "copied-vault")
    now = int(time.time())
    seed, _target = _stage_and_enroll(vault, now=now)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        store.migrate_enrolled_v3_store(copied, seed=seed, now=now + 2)

    assert _schema_version(copied) == 3


def test_store_migration_refuses_future_schema_before_parsing_seed(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        connection.execute("PRAGMA user_version=5")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(store.UnsupportedGovernanceSchema):
        store.migrate_enrolled_v3_store(
            vault,
            seed=object(),  # type: ignore[arg-type]
            now=int(time.time()),
        )

    assert _schema_version(vault) == 5


def test_store_migration_replay_refuses_extended_v4_without_writes(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    seed, _target = _stage_and_enroll(vault, now=now)
    store.migrate_enrolled_v3_store(vault, seed=seed, now=now + 2)
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        connection.execute(
            "ALTER TABLE compiled_policy ADD COLUMN unexpected_v4_state TEXT"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(schema_v4.SchemaV4Error):
        store.migrate_enrolled_v3_store(vault, seed=seed, now=now + 3)

    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert "unexpected_v4_state" in {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(compiled_policy)")
        }
    finally:
        connection.close()
