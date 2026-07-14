from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from exomem_provisioner import durability_jobs
from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.durability_jobs import (
    DatabaseBackupSettings,
    DeletionJobSettings,
    ExportGcSettings,
    _run_live_deletion_worker,
    run_bounded_operation_batch,
    run_verified_backup_sweep,
)
from exomem_provisioner.repository import OperationRepository


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


def test_deletion_dispatcher_settings_and_entrypoint_are_credential_free(tmp_path: Path) -> None:
    template = tmp_path / "deletion-job.json"
    template.write_text("{}", encoding="utf-8")
    settings = durability_jobs.DeletionDispatcherSettings(
        database_url="postgresql+asyncpg://exomem_provisioner_runtime:secret@db.invalid/app",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        namespace="exomem-platform",
        job_template_path=template,
    )

    assert set(settings.model_dump()) == {
        "database_url",
        "database_schema",
        "database_role",
        "namespace",
        "job_template_path",
    }
    assert not {
        "envelope_key",
        "hcloud_token",
        "recovery_delete_key",
        "user_export_delete_key",
        "provider_recovery_public_key",
    } & set(type(settings).model_fields)
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert (
        'exomem-deletion-dispatcher = '
        '"exomem_provisioner.durability_jobs:run_deletion_dispatcher"'
    ) in pyproject


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
async def test_deletion_entrypoint_runs_one_bounded_batch_and_exits(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from exomem_provisioner import durability_runtime

    class Worker:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> bool:
            self.calls += 1
            return self.calls <= 2

    worker = Worker()

    @asynccontextmanager
    async def live_worker():
        yield worker

    monkeypatch.setattr(durability_runtime, "live_deletion_worker", live_worker)

    await _run_live_deletion_worker(DeletionJobSettings(batch_size=5))

    assert worker.calls == 3


@pytest.mark.asyncio
async def test_deletion_dispatcher_creates_only_one_claimed_job_and_no_work_creates_none() -> None:
    class Source:
        def __init__(self, operation_id: str | None) -> None:
            self.operation_id = operation_id
            self.calls = 0

        async def next_dispatchable_deletion(self) -> str | None:
            self.calls += 1
            return self.operation_id

    class Launcher:
        def __init__(self, *, active: bool = False) -> None:
            self.active = active
            self.created: list[str] = []

        async def has_active_job(self) -> bool:
            return self.active

        async def create_scoped_job(self, operation_id: str) -> None:
            self.created.append(operation_id)

    empty_source = Source(None)
    empty_launcher = Launcher()
    assert (
        await durability_jobs.dispatch_one_deletion_job(empty_source, empty_launcher) is False
    )
    assert empty_launcher.created == []

    active_source = Source("operation-ignored")
    active_launcher = Launcher(active=True)
    assert (
        await durability_jobs.dispatch_one_deletion_job(active_source, active_launcher) is False
    )
    assert active_source.calls == 0
    assert active_launcher.created == []

    ready_source = Source("operation-ready")
    ready_launcher = Launcher()
    assert await durability_jobs.dispatch_one_deletion_job(ready_source, ready_launcher) is True
    assert ready_launcher.created == ["operation-ready"]


@pytest.mark.asyncio
async def test_deletion_dispatch_source_reads_only_eligible_destroy_or_discard(tmp_path: Path) -> None:
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'dispatcher.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret("k" * 32),
    )
    now = datetime(2030, 1, 1, tzinfo=UTC)
    request = {
        "operationId": "operation-ready",
        "tenantId": "tenant-alpha",
        "cellId": "cell-alpha",
        "fenceGeneration": 8,
        "checkpoint": "requested",
    }
    try:
        await repository.submit("provision", "provision-first", request)
        destroy = await repository.submit("destroy", "destroy-ready", request)
        source = durability_jobs.RepositoryDeletionDispatchSource(database.session_factory)

        assert await source.next_dispatchable_deletion(now=now) == destroy.id
        unchanged = await repository.get_by_id(destroy.id)
        assert unchanged is not None and unchanged.state.value == "pending"
        assert unchanged.claim_token is None and unchanged.claim_generation == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_kubernetes_deletion_launcher_uses_scoped_template_and_content_free_receipt() -> None:
    class Batch:
        def __init__(self) -> None:
            self.items: list[object] = []
            self.created: list[tuple[str, dict[str, object]]] = []

        def list_namespaced_job(self, namespace: str, *, label_selector: str):
            assert namespace == "exomem-platform"
            assert label_selector == "exomem.io/deletion-job=true"
            return SimpleNamespace(items=self.items)

        def create_namespaced_job(self, namespace: str, body: dict[str, object]):
            self.created.append((namespace, body))

    template = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "generateName": "exomem-deletion-",
            "labels": {"exomem.io/deletion-job": "true"},
        },
        "spec": {
            "activeDeadlineSeconds": 240,
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "spec": {
                    "serviceAccountName": "exomem-deletion-worker",
                    "restartPolicy": "Never",
                    "containers": [
                        {"name": "deletion-worker", "command": ["exomem-deletion-worker"]}
                    ],
                }
            },
        },
    }
    batch = Batch()
    launcher = durability_jobs.KubernetesDeletionJobLauncher(
        batch_v1=batch,
        namespace="exomem-platform",
        job_template=template,
    )

    assert await launcher.has_active_job() is False
    await launcher.create_scoped_job("operation-sensitive-opaque")

    assert len(batch.created) == 1
    namespace, body = batch.created[0]
    assert namespace == "exomem-platform"
    annotations = body["metadata"]["annotations"]
    assert set(annotations) == {"exomem.io/deletion-operation-sha256"}
    assert annotations["exomem.io/deletion-operation-sha256"] != "operation-sensitive-opaque"
    assert "operation-sensitive-opaque" not in str(body)


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
