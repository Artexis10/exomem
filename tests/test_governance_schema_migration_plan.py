from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
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
