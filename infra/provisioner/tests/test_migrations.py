from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def test_alembic_upgrades_empty_sqlite_database_to_head(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite"
    root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "EXOMEM_PROVISIONER_DATABASE_URL": f"sqlite:///{database}",
            "EXOMEM_PROVISIONER_DATABASE_SCHEMA": "exomem_provisioner",
            "EXOMEM_PROVISIONER_DATABASE_ROLE": "exomem_provisioner_runtime",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        operation_columns = {row[1] for row in connection.execute("PRAGMA table_info(operations)")}
        receipt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(operation_recovery_receipts)")
        }
        ledger = connection.execute("SELECT id, revision FROM capacity_ledger").fetchall()
    assert {
        "alembic_version",
        "operations",
        "tenant_fences",
        "resources",
        "credential_metadata",
        "exports",
        "backups",
        "durability_runs",
        "recovery_objects",
        "export_deliveries",
        "provider_observations",
        "cell_operation_locks",
        "capacity_ledger",
        "capacity_reservations",
        "capacity_destructive_fences",
        "operation_recovery_receipts",
    } <= tables
    assert revision == ("0007_operation_recovery_receipt",)
    assert ledger == [(1, 0)]
    assert {
        "caller_checkpoint",
        "checkpoint",
        "claim_owner",
        "claim_token",
        "claim_generation",
        "claim_expires_at",
        "wire_protocol",
    } <= operation_columns
    assert {
        "operation_id",
        "helper_source_sha256",
        "request_ciphertext_sha256",
        "committed_operation_sha256",
        "committed_at",
    } <= receipt_columns


def test_capacity_migration_downgrade_upgrade_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "capacity-round-trip.sqlite"
    root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "EXOMEM_PROVISIONER_DATABASE_URL": f"sqlite:///{database}",
            "EXOMEM_PROVISIONER_DATABASE_SCHEMA": "exomem_provisioner",
            "EXOMEM_PROVISIONER_DATABASE_ROLE": "exomem_provisioner_runtime",
        }
    )

    for target in ("head", "0004_export_delivery_ledger", "head"):
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", target]
            if target == "head"
            else [sys.executable, "-m", "alembic", "-c", "alembic.ini", "downgrade", target],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            }
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if target == "0004_export_delivery_ledger":
            assert "capacity_ledger" not in tables
            assert "capacity_reservations" not in tables
            assert "capacity_destructive_fences" not in tables
            assert revision == (target,)
        else:
            assert {
                "capacity_ledger",
                "capacity_reservations",
                "capacity_destructive_fences",
                "operation_recovery_receipts",
            } <= tables
            assert revision == ("0007_operation_recovery_receipt",)


def test_wire_protocol_sqlite_backfill_default_constraint_and_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "wire-protocol-round-trip.sqlite"
    root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "EXOMEM_PROVISIONER_DATABASE_URL": f"sqlite:///{database}",
            "EXOMEM_PROVISIONER_DATABASE_SCHEMA": "exomem_provisioner",
            "EXOMEM_PROVISIONER_DATABASE_ROLE": "exomem_provisioner_runtime",
        }
    )

    def migrate(command: str, target: str) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, target],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    def insert_legacy(connection: sqlite3.Connection, operation_id: str) -> None:
        connection.execute(
            "INSERT INTO operations (id, action, idempotency_key, canonical_request_sha256, "
            "tenant_id, external_operation_id, fence_generation, provider_operation_id, "
            "provider_fence_generation, state, caller_checkpoint, checkpoint, progress, "
            "request_ciphertext, result_redacted, retry_after_seconds, available_at, "
            "claim_generation, created_at, updated_at) VALUES (?, 'PROVISION', ?, ?, ?, ?, "
            "1, ?, 1, 'PENDING', 'requested', 'queued', '{}', 'ciphertext', '{}', 2, "
            "'2026-01-01T00:00:00Z', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (operation_id, f"key-{operation_id}", "a" * 64, "tenant", operation_id, operation_id),
        )

    migrate("upgrade", "0005_capacity_reservations")
    with sqlite3.connect(database) as connection:
        insert_legacy(connection, "legacy-before-0006")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT wire_protocol FROM operations WHERE id = 'legacy-before-0006'"
        ).fetchone() == ("exomem-cell-provisioner.v1",)
        insert_legacy(connection, "default-after-0006")
        assert connection.execute(
            "SELECT wire_protocol FROM operations WHERE id = 'default-after-0006'"
        ).fetchone() == ("exomem-cell-provisioner.v1",)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE operations SET wire_protocol = 'exomem-cell-provisioner.v9' "
                "WHERE id = 'default-after-0006'"
            )
    migrate("downgrade", "0005_capacity_reservations")
    with sqlite3.connect(database) as connection:
        assert "wire_protocol" not in {
            row[1] for row in connection.execute("PRAGMA table_info(operations)")
        }
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT DISTINCT wire_protocol FROM operations ORDER BY wire_protocol"
        ).fetchall() == [("exomem-cell-provisioner.v1",)]


@pytest.mark.parametrize(
    ("database_url", "schema", "role"),
    [
        (
            "postgresql+asyncpg://postgres:secret@database.invalid/exomem",
            "public",
            "postgres",
        ),
        (
            "postgresql+asyncpg://wrong_role:secret@database.invalid/exomem",
            "exomem_provisioner",
            "exomem_provisioner_runtime",
        ),
    ],
)
def test_alembic_rejects_unsafe_production_identity_before_connecting(
    database_url: str,
    schema: str,
    role: str,
) -> None:
    root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "EXOMEM_PROVISIONER_DATABASE_URL": database_url,
            "EXOMEM_PROVISIONER_DATABASE_SCHEMA": schema,
            "EXOMEM_PROVISIONER_DATABASE_ROLE": role,
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "dedicated provisioner schema and matching runtime role are required" in (
        completed.stdout + completed.stderr
    )
