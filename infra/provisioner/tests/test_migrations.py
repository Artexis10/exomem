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
    } <= tables
    assert revision == ("0005_capacity_reservations",)
    assert ledger == [(1, 0)]
    assert {
        "caller_checkpoint",
        "checkpoint",
        "claim_owner",
        "claim_token",
        "claim_generation",
        "claim_expires_at",
    } <= operation_columns


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
            assert revision == (target,)
        else:
            assert {"capacity_ledger", "capacity_reservations"} <= tables
            assert revision == ("0005_capacity_reservations",)


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
