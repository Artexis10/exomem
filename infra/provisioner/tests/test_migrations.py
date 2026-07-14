from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


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
    assert {
        "alembic_version",
        "operations",
        "tenant_fences",
        "resources",
        "credential_metadata",
        "exports",
        "backups",
    } <= tables
    assert revision == ("0001_initial",)
