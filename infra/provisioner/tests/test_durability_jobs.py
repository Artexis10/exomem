from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from exomem_provisioner.durability_jobs import (
    DatabaseBackupSettings,
    ExportGcSettings,
    run_bounded_operation_batch,
    run_verified_backup_sweep,
)


def _settings(**overrides: object) -> ExportGcSettings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://exomem_provisioner_runtime:secret@db.invalid/app",
        "database_schema": "exomem_provisioner",
        "database_role": "exomem_provisioner_runtime",
        "envelope_key": "wrapping-key-material-which-is-long-enough",
        "b2_endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "b2_region": "us-west-004",
        "user_export_bucket": "exomem-private-alpha-export-deadbeef",
        "user_export_delete_key_id": "delete-key-id",
        "user_export_delete_key": "delete-key-secret",
    }
    values.update(overrides)
    return ExportGcSettings(**values)


def test_export_gc_settings_are_postgres_https_and_dedicated_role_only() -> None:
    assert _settings().delivery_limit == 1000
    with pytest.raises(ValidationError):
        _settings(database_url="sqlite+aiosqlite:///tmp/test.sqlite")
    with pytest.raises(ValidationError):
        _settings(b2_endpoint_url="http://s3.invalid")
    with pytest.raises(ValidationError):
        _settings(database_role="another_role")


def test_export_gc_console_entrypoint_is_packaged() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'exomem-export-gc = "exomem_provisioner.durability_jobs:run_export_gc"' in pyproject


def _database_settings(tmp_path: Path, **overrides: object) -> DatabaseBackupSettings:
    service_file = tmp_path / "pg_service.conf"
    password_file = tmp_path / ".pgpass"
    service_file.write_text("[production]\nhost=db.invalid\n", encoding="utf-8")
    password_file.write_text("*:*:*:*:secret\n", encoding="utf-8")
    service_file.chmod(0o600)
    password_file.chmod(0o600)
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://exomem_provisioner_runtime:secret@db.invalid/app",
        "database_schema": "exomem_provisioner",
        "database_role": "exomem_provisioner_runtime",
        "envelope_key": "wrapping-key-material-which-is-long-enough",
        "provider_recovery_signing_key": "4DM4rPdp5D6KbRM4eRBZ4tNCvMSi_5Jj7w7OZMAUdWc",
        "b2_endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "b2_region": "us-west-004",
        "database_backup_bucket": "exomem-private-alpha-database-deadbeef",
        "database_backup_upload_key_id": "upload-key-id",
        "database_backup_upload_key": "upload-key-secret",
        "scratch_root": tmp_path / "scratch",
        "pg_dump": "/usr/bin/pg_dump",
        "pg_restore": "/usr/bin/pg_restore",
        "psql": "/usr/bin/psql",
        "dropdb": "/usr/bin/dropdb",
        "createdb": "/usr/bin/createdb",
        "pg_service_file": service_file,
        "pgpass_file": password_file,
        "source_service": "production",
        "maintenance_service": "maintenance",
        "scratch_service": "scratch-empty",
        "scratch_database": "exomem_restore_scratch",
        "expected_restore_owner": "substrate_restore_owner",
        "verification_sql": "SELECT current_user, 'tenant-proof', 'cell-proof'",
        "proof_tenant_id": "tenant-proof",
        "proof_cell_id": "cell-proof",
    }
    values.update(overrides)
    return DatabaseBackupSettings(**values)


def test_database_backup_settings_bind_exact_secret_and_postgres_contract(tmp_path: Path) -> None:
    settings = _database_settings(tmp_path)
    assert settings.system_cell_id == "control-plane-databases"
    assert settings.pgpass_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValidationError):
        _database_settings(tmp_path, database_role="another_role")
    with pytest.raises(ValidationError):
        _database_settings(tmp_path, scratch_root=Path("relative-scratch"))


def test_database_backup_console_entrypoint_is_packaged() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        'exomem-database-backup-worker = "exomem_provisioner.durability_jobs:run_database_backup"'
        in pyproject
    )


def test_vault_backup_and_deletion_console_entrypoints_are_packaged() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

    assert (
        'exomem-durability-backup-worker = "exomem_provisioner.durability_jobs:run_durability_backup"'
        in pyproject
    )
    assert (
        'exomem-deletion-worker = "exomem_provisioner.durability_jobs:run_deletion_worker"'
        in pyproject
    )

    scripts = {
        entry.name: entry.load()
        for entry in entry_points(group="console_scripts")
        if entry.name in {"exomem-durability-backup-worker", "exomem-deletion-worker"}
    }
    assert set(scripts) == {"exomem-durability-backup-worker", "exomem-deletion-worker"}
    assert all(callable(script) for script in scripts.values())
    for dependency in ('"hcloud>=2.22,<3"', '"httpx>=0.28,<1"', '"kubernetes>=35.0,<36"'):
        assert dependency in pyproject


@pytest.mark.asyncio
async def test_operation_batch_is_bounded_and_stops_when_queue_is_empty() -> None:
    class Worker:
        def __init__(self, outcomes: list[bool]) -> None:
            self.outcomes = iter(outcomes)
            self.calls = 0

        async def run_once(self) -> bool:
            self.calls += 1
            return next(self.outcomes)

    bounded = Worker([True, True, True, True])
    assert await run_bounded_operation_batch(bounded, max_operations=3) == 3
    assert bounded.calls == 3

    drained = Worker([True, False, True])
    assert await run_bounded_operation_batch(drained, max_operations=10) == 1
    assert drained.calls == 2


@pytest.mark.asyncio
async def test_backup_sweep_fails_closed_on_any_failed_target_or_rpo_miss() -> None:
    class Scheduler:
        def __init__(self, report: object) -> None:
            self.report = report

        async def run_once(self) -> object:
            return self.report

    healthy = SimpleNamespace(failed=0, capacity_rpo_met=True)
    assert await run_verified_backup_sweep(Scheduler(healthy)) is healthy

    with pytest.raises(RuntimeError, match="verified durability success"):
        await run_verified_backup_sweep(Scheduler(SimpleNamespace(failed=1, capacity_rpo_met=True)))
    with pytest.raises(RuntimeError, match="verified durability success"):
        await run_verified_backup_sweep(
            Scheduler(SimpleNamespace(failed=0, capacity_rpo_met=False))
        )
