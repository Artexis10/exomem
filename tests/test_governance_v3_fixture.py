from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from governance_v3_fixtures import (
    DIRECT_SOURCE_POLICY_BYTES,
    DIRECT_SOURCE_POLICY_PATH,
    FROZEN_V3_DUMP_SHA256,
    frozen_v3_dump,
    install_frozen_v3_fixture,
)

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


def _dump_digest(dump: tuple[str, ...]) -> str:
    return hashlib.sha256(("\n".join(dump) + "\n").encode()).hexdigest()


def _migration_seed(
    vault: Path,
    staged: authorization_custody.StandaloneV3StagingResult,
    *,
    now: int,
) -> schema_v4.MigrationSeed:
    snapshot = policy.observe_authoring_snapshot(vault)
    assert snapshot is not None
    compiled = policy.compile_documents(dict(snapshot.documents))
    assert not compiled.empty and not compiled.blocked
    descriptor = json.dumps(
        {
            "artifacts": [
                {
                    "content_hash": "a" * 64,
                    "path": "Notes/fixture.md",
                }
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return schema_v4.MigrationSeed(
        activation_store_id="activation-store-fixture",
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
            authoring_event_id="event-v3-fixture-migration",
            receipt_event_id="receipt-v3-fixture-migration",
            created_at=now,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=descriptor,
            artifact_count=1,
            created_at=now,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="projection-namespace-v3-fixture",
            evidence=b'{"catalog_complete":true,"ready":true}',
            ready_at=now,
        ),
        migrated_at=now,
    )


def test_frozen_v3_fixture_has_exact_schema_and_rich_legacy_authority(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    install_frozen_v3_fixture(vault)

    dump = frozen_v3_dump(vault)
    assert _dump_digest(dump) == FROZEN_V3_DUMP_SHA256
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        schema_v4.require_exact_v3_connection(connection)
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute(
            "SELECT authorization_session FROM governance_session_grants"
        ).fetchone() == ("legacy-session",)
        assert connection.execute(
            "SELECT authorization_session FROM governance_session_purpose"
        ).fetchone() == ("legacy-session",)
        assert connection.execute(
            "SELECT authorization_session FROM withhold_tokens"
        ).fetchone() == ("legacy-session",)
        assert connection.execute(
            "SELECT authorization_session, phase FROM governance_operation_journals"
        ).fetchone() == ("legacy-session", "pending")
        assert connection.execute(
            "SELECT durable_seq, observed_seq FROM receipts_head"
        ).fetchone() == (7, 7)
    finally:
        connection.close()
    assert (
        vault / "Knowledge Base" / "_Governance" / DIRECT_SOURCE_POLICY_PATH
    ).read_bytes() == DIRECT_SOURCE_POLICY_BYTES


def test_every_ordinary_store_opener_leaves_frozen_v3_logically_unchanged(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    install_frozen_v3_fixture(vault)
    before = frozen_v3_dump(vault)

    store.open_connection(vault).close()
    readonly = store.open_readonly_connection(vault)
    assert readonly is not None
    readonly.close()
    assert store.authorization_session_schema_version(vault) == 3
    with pytest.raises(store.UnsupportedGovernanceSchema):
        store.open_authorization_session_connection(vault)
    with pytest.raises(store.UnsupportedGovernanceSchema):
        store.open_active_governance_read_connection(vault)

    after = frozen_v3_dump(vault)
    assert after == before
    assert not any("governance_authorization_sessions" in line for line in after)
    assert not any("active_governance_tuple" in line for line in after)


@pytest.mark.parametrize(
    "crash_point",
    ["after-legacy-archive", "after-schema", "after-seed", "before-commit"],
)
def test_rich_v3_fixture_rolls_back_every_precommit_migration_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    vault = tmp_path / "vault"
    install_frozen_v3_fixture(vault)
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=100)
    seed = _migration_seed(vault, staged, now=101)
    before = frozen_v3_dump(vault)

    def crash(point: str) -> None:
        if point == crash_point:
            raise RuntimeError(f"injected {point}")

    monkeypatch.setattr(schema_v4, "_crash_point", crash)
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        with pytest.raises(RuntimeError, match=crash_point):
            schema_v4.migrate_v3_connection(connection, seed)
    finally:
        connection.close()

    assert frozen_v3_dump(vault) == before


def test_enrolled_rich_v3_fixture_migrates_authority_conservatively(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    install_frozen_v3_fixture(vault)
    now = int(time.time())
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    seed = _migration_seed(vault, staged, now=now + 1)
    target = schema_v4.migration_target(seed)
    authorization_custody.enroll_standalone_v3_migration(
        vault,
        target=target,
        now=now,
    )

    assert (
        store.migrate_enrolled_v3_store(
            vault,
            seed=seed,
            now=now + 2,
        )
        == target
    )

    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        archived = connection.execute(
            "SELECT source_table, source_key, reason, expired_at "
            "FROM governance_legacy_authority ORDER BY source_table, source_key"
        ).fetchall()
        assert archived == [
            ("governance_session_grants", "legacy-grant", "v3-unbound-authorization", now + 1),
            ("governance_session_purpose", "legacy-session", "v3-unbound-authorization", now + 1),
            (
                "governance_session_purpose_staging",
                "legacy-purpose-stage",
                "v3-unbound-authorization",
                now + 1,
            ),
            ("withhold_tokens", "legacy-token", "v3-unbound-authorization", now + 1),
        ]
        assert connection.execute(
            "SELECT phase, blocked_reason FROM governance_operation_journals "
            "WHERE event_id='legacy-recovery-event'"
        ).fetchone() == ("pending", "v3-unbound-authorization")
        assert connection.execute(
            "SELECT durable_seq, observed_seq FROM receipts_head"
        ).fetchone() == (7, 7)
        assert connection.execute("SELECT COUNT(*) FROM governance_proposals").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_operation_components"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM governance_session_grants").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM governance_session_purpose").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM withhold_tokens").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM compiled_policy_generations"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT catalog_generation FROM active_governance_tuple"
        ).fetchone() == (1,)
    finally:
        connection.close()
