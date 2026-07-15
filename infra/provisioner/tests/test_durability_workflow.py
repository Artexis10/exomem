from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.durability import (
    BackupScheduler,
    BackupTarget,
    CentralBackupScheduler,
    ExportBackupWorkflow,
    PortableArchive,
    QuiescenceTargetExceeded,
)
from exomem_provisioner.durability_crypto import AesGcmKeyWrapper, ChunkedArchiveCipher
from exomem_provisioner.durability_repository import (
    DurabilityRepository,
    RunIdentity,
    RunKind,
)
from exomem_provisioner.durability_store import ProviderObjectHead
from exomem_provisioner.provider_recovery import (
    ProviderRecoveryIdentityCodec,
    ProviderRecoveryIdentityDecoder,
    ProviderReference,
)


def _identity_signer() -> ProviderRecoveryIdentityCodec:
    return ProviderRecoveryIdentityCodec.from_secret("provider-recovery-signing-test-seed")


def _settings(path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )


class RecordingRoutes:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close_and_verify(self, cell_id: str, operation_id: str) -> None:
        self.events.append(f"routes-closed:{cell_id}:{operation_id}")

    async def open(self, cell_id: str, operation_id: str) -> None:
        self.events.append(f"routes-open:{cell_id}:{operation_id}")


class RecordingRuntime:
    def __init__(self, scratch: Path, events: list[str]) -> None:
        self.scratch = scratch
        self.events = events

    async def quiesce(self, cell_id: str, operation_id: str, *, routing_stopped: bool) -> None:
        assert routing_stopped is True
        self.events.append(f"quiesced:{cell_id}:{operation_id}")

    async def portable_export(self, cell_id: str, operation_id: str) -> PortableArchive:
        self.events.append(f"archive:{cell_id}:{operation_id}")
        archive = self.scratch / "archive.tar"
        manifest = self.scratch / "manifest.json"
        archive.write_bytes(b"canonical portable data\n" * 1000)
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "sourceCellId": cell_id,
                    "releaseVersion": "0.22.0",
                    "hostedStateIncluded": False,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return PortableArchive(
            archive_path=archive,
            manifest_path=manifest,
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            archive_size=archive.stat().st_size,
            source_cell_id=cell_id,
            release_version="0.22.0",
            hosted_state_included=False,
        )

    async def release(self, cell_id: str, operation_id: str) -> None:
        self.events.append(f"released:{cell_id}:{operation_id}")


class RecordingUploadStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.objects: dict[str, ProviderObjectHead] = {}
        self.retain_until_values: list[datetime | None] = []

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        metadata: dict[str, str],
        retain_until: datetime | None,
    ) -> ProviderObjectHead:
        assert any(event.startswith("routes-open:") for event in self.events)
        self.events.append(f"uploaded:{key}")
        self.retain_until_values.append(retain_until)
        head = ProviderObjectHead(
            key=key,
            size=source.stat().st_size,
            metadata=metadata,
            version_id="version-opaque",
            retain_until=retain_until,
        )
        self.objects[key] = head
        return head

    async def head(self, key: str) -> ProviderObjectHead | None:
        return self.objects.get(key)


class LostAcknowledgementStore(RecordingUploadStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.calls = 0
        self.first_retain_until: datetime | None = None

    async def put_file(self, key, source, *, metadata, retain_until):
        self.calls += 1
        if self.first_retain_until is None:
            self.first_retain_until = retain_until
        else:
            assert retain_until == self.first_retain_until
        head = await super().put_file(key, source, metadata=metadata, retain_until=retain_until)
        if self.calls == 1:
            raise RuntimeError("provider committed before acknowledgement")
        return head


class FailedBeforeCommitStore(RecordingUploadStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.calls = 0

    async def put_file(self, key, source, *, metadata, retain_until):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("provider rejected upload before commit")
        return await super().put_file(
            key,
            source,
            metadata=metadata,
            retain_until=retain_until,
        )


@pytest.fixture
async def workflow_context(tmp_path: Path):
    settings = _settings(tmp_path / "workflow.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=300,
    )
    try:
        yield database, repository
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_backup_reopens_routes_before_encryption_upload_and_records_remote_proof(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    routes = RecordingRoutes(events)
    runtime = RecordingRuntime(tmp_path, events)
    store = RecordingUploadStore(events)
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-20300101t1200",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    claimed = await repository.claim(run.id, "backup-worker")
    workflow = ExportBackupWorkflow(
        repository=repository,
        routes=routes,
        runtime=runtime,
        upload_store=store,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
        provider_identity_signer=_identity_signer(),
        provider_bucket="recovery-test-bucket",
        scratch_root=tmp_path,
        min_archive_bytes=1024,
    )

    result = await workflow.run(claimed, worker_id="backup-worker")

    assert result.encryption_scheme == "envelope-aes-256-gcm"
    assert result.integrity_verified is True
    assert result.opaque_reference.startswith("recovery_")
    uploaded = next(iter(store.objects.values()))
    assert uploaded.metadata["wrapped-key-reference"] == result.opaque_reference
    observation = ProviderRecoveryIdentityDecoder.b2(
        provider_reference=ProviderReference.b2(bucket="recovery-test-bucket", key=uploaded.key),
        metadata=uploaded.metadata,
        observed_at=datetime.now(UTC),
        identity_codec=_identity_signer().verifier(),
    )
    assert observation.operation_id == "backup-20300101t1200"
    assert events.index("released:cell-durable-alpha:backup-20300101t1200") < next(
        index for index, event in enumerate(events) if event.startswith("uploaded:")
    )
    final = await repository.get(run.id)
    assert final is not None and final.checkpoint == "complete"
    assert not (tmp_path / f"{run.id}.encrypted").exists()


@pytest.mark.asyncio
async def test_backup_reopens_routes_when_quiesce_fails(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-quiesce-failure",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime.now(UTC),
        )
    )
    claimed = await repository.claim(run.id, "backup-worker")

    class FailingRuntime(RecordingRuntime):
        async def quiesce(self, *args, **kwargs) -> None:
            events.append("quiesce-failed")
            raise RuntimeError("quiesce unavailable")

        async def release(self, cell_id: str, operation_id: str) -> None:
            events.append(f"release-attempted:{cell_id}:{operation_id}")

    workflow = ExportBackupWorkflow(
        repository=repository,
        routes=RecordingRoutes(events),
        runtime=FailingRuntime(tmp_path, events),
        upload_store=RecordingUploadStore(events),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
        provider_identity_signer=_identity_signer(),
        provider_bucket="recovery-test-bucket",
        scratch_root=tmp_path,
        min_archive_bytes=1024,
    )

    with pytest.raises(RuntimeError, match="quiesce unavailable"):
        await workflow.run(claimed, worker_id="backup-worker")

    assert "release-attempted:cell-durable-alpha:backup-quiesce-failure" in events
    assert "routes-open:cell-durable-alpha:backup-quiesce-failure" in events


@pytest.mark.asyncio
async def test_archive_hash_copy_and_encryption_never_block_claim_heartbeat_loop(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-nonblocking-filesystem",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime.now(UTC),
        )
    )
    claimed = await repository.claim(run.id, "backup-worker")
    workflow = ExportBackupWorkflow(
        repository=repository,
        routes=RecordingRoutes(events),
        runtime=RecordingRuntime(tmp_path, events),
        upload_store=RecordingUploadStore(events),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
        provider_identity_signer=_identity_signer(),
        provider_bucket="recovery-test-bucket",
        scratch_root=tmp_path,
        min_archive_bytes=1024,
    )
    original = workflow._stage_and_verify
    blocking = threading.Event()

    def slow_stage(*args):
        blocking.set()
        time.sleep(0.15)
        try:
            return original(*args)
        finally:
            blocking.clear()

    monkeypatch.setattr(workflow, "_stage_and_verify", slow_stage)
    observed_during_block: list[bool] = []

    async def heartbeat_probe() -> None:
        while not any(event.startswith("uploaded:") for event in events):
            if blocking.is_set():
                observed_during_block.append(True)
            await asyncio.sleep(0.01)

    probe = asyncio.create_task(heartbeat_probe())
    await workflow.run(claimed, worker_id="backup-worker")
    await probe

    assert observed_during_block


@pytest.mark.asyncio
async def test_snapshot_staging_never_follows_preexisting_scratch_symlink(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-scratch-symlink",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime.now(UTC),
        )
    )
    claimed = await repository.claim(run.id, "backup-worker")
    scratch_root = tmp_path / "bounded-scratch"
    scratch_root.mkdir(mode=0o700)
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"must remain unchanged")
    (scratch_root / f"{run.id}.archive").symlink_to(outside)
    workflow = ExportBackupWorkflow(
        repository=repository,
        routes=RecordingRoutes(events),
        runtime=RecordingRuntime(tmp_path, events),
        upload_store=RecordingUploadStore(events),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
        provider_identity_signer=_identity_signer(),
        provider_bucket="recovery-test-bucket",
        scratch_root=scratch_root,
        min_archive_bytes=1024,
    )

    result = await workflow.run(claimed, worker_id="backup-worker")

    assert result.integrity_verified is True
    assert outside.read_bytes() == b"must remain unchanged"


@pytest.mark.asyncio
async def test_user_export_binds_exact_product_expiry_and_uses_no_object_lock(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    store = RecordingUploadStore(events)
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=1)
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.USER_EXPORT,
            operation_id="export-exact-expiry",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=now,
        )
    )
    claimed = await repository.claim(run.id, "export-worker")
    workflow = ExportBackupWorkflow(
        repository=repository,
        routes=RecordingRoutes(events),
        runtime=RecordingRuntime(tmp_path, events),
        upload_store=store,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
        provider_identity_signer=_identity_signer(),
        provider_bucket="recovery-test-bucket",
        scratch_root=tmp_path,
        min_archive_bytes=1024,
        clock=lambda: now,
    )

    result = await workflow.run(
        claimed,
        worker_id="export-worker",
        expires_at=expires_at,
    )

    saved = await repository.get_recovery_object(result.opaque_reference)
    assert result.opaque_reference.startswith("export_")
    assert store.retain_until_values == [None]
    assert saved is not None and saved.expires_at == expires_at
    assert saved.object_lock_until <= saved.verified_at


@pytest.mark.asyncio
async def test_quiescence_over_two_minutes_reopens_routes_but_refuses_success(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    moments = iter((0.0, 121.0))
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-slow",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    claimed = await repository.claim(run.id, "backup-worker")
    workflow = ExportBackupWorkflow(
        repository=repository,
        routes=RecordingRoutes(events),
        runtime=RecordingRuntime(tmp_path, events),
        upload_store=RecordingUploadStore(events),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
        provider_identity_signer=_identity_signer(),
        provider_bucket="recovery-test-bucket",
        scratch_root=tmp_path,
        min_archive_bytes=1024,
        monotonic=lambda: next(moments),
    )

    with pytest.raises(QuiescenceTargetExceeded):
        await workflow.run(claimed, worker_id="backup-worker")
    assert "routes-open:cell-durable-alpha:backup-slow" in events
    assert not any(event.startswith("uploaded:") for event in events)


def test_scheduler_uses_exact_thirty_minute_slots_and_threshold_metrics() -> None:
    scheduler = BackupScheduler()
    assert scheduler.slot(datetime(2030, 1, 1, 12, 29, 59, tzinfo=UTC)) == datetime(
        2030, 1, 1, 12, 0, tzinfo=UTC
    )
    assert scheduler.slot(datetime(2030, 1, 1, 12, 30, tzinfo=UTC)) == datetime(
        2030, 1, 1, 12, 30, tzinfo=UTC
    )
    metrics = scheduler.freshness_metrics(age_seconds=46 * 60)
    assert metrics["exomem_recovery_backup_age_seconds"] == 46 * 60
    assert metrics["exomem_recovery_backup_warning"] == 1
    assert metrics["exomem_recovery_alpha_blocked"] == 0
    assert scheduler.freshness_metrics(age_seconds=60 * 60)["exomem_recovery_alpha_blocked"] == 1


@pytest.mark.asyncio
async def test_central_scheduler_starts_each_cell_once_per_slot_and_reports_freshness(
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    now = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)

    class Targets:
        async def list_backup_targets(self):
            return [
                BackupTarget("tenant-a", "cell-a", 7),
                BackupTarget("tenant-b", "cell-b", 4),
            ]

    class Workflow:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, run, *, worker_id):
            self.calls.append(run.identity.cell_id)
            await repository.complete(
                run.id,
                worker_id,
                claim_token=run.claim_token,
                claim_generation=run.claim_generation,
                result={"integrity_verified": True},
            )

    workflow = Workflow()
    scheduler = CentralBackupScheduler(
        repository=repository,
        target_source=Targets(),
        workflow=workflow,
        worker_id="central-backup",
    )

    first = await scheduler.run_once(now=now)
    replay = await scheduler.run_once(now=now + timedelta(minutes=1))

    assert sorted(workflow.calls) == ["cell-a", "cell-b"]
    assert first.started == 2 and first.completed == 2 and first.failed == 0
    assert replay.started == 0 and replay.completed == 0
    assert replay.alpha_blocked_cells == ("cell-a", "cell-b")
    assert replay.metrics["cell-a"]["exomem_recovery_backup_age_seconds"] == -1


@pytest.mark.asyncio
async def test_central_scheduler_uses_bounded_parallelism_and_reports_measured_rpo_capacity(
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    active = 0
    peak = 0
    both_started = asyncio.Event()

    class Targets:
        async def list_backup_targets(self):
            return [BackupTarget("tenant-a", "cell-a", 1), BackupTarget("tenant-b", "cell-b", 1)]

    class Workflow:
        async def run(self, run, *, worker_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            await repository.complete(
                run.id,
                worker_id,
                claim_token=run.claim_token,
                claim_generation=run.claim_generation,
                result={"integrity_verified": True},
            )
            active -= 1

    moments = iter((0.0, 12.5))
    report = await CentralBackupScheduler(
        repository=repository,
        target_source=Targets(),
        workflow=Workflow(),
        worker_id="parallel-backup",
        max_concurrency=2,
        monotonic=lambda: next(moments),
    ).run_once(now=datetime.now(UTC))

    assert peak == report.peak_concurrency == 2
    assert report.sweep_seconds == 12.5
    assert report.capacity_rpo_met is True


@pytest.mark.asyncio
async def test_central_scheduler_defers_cell_with_active_export_without_overlap(
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    await repository.begin(
        RunIdentity(
            kind=RunKind.USER_EXPORT,
            operation_id="active-export-before-schedule",
            tenant_id="tenant-a",
            cell_id="cell-a",
            fence_generation=7,
            scheduled_for=now,
        )
    )

    class Targets:
        async def list_backup_targets(self):
            return [BackupTarget("tenant-a", "cell-a", 7)]

    class Workflow:
        async def run(self, run, *, worker_id):
            raise AssertionError("busy cell must not overlap")

    report = await CentralBackupScheduler(
        repository=repository,
        target_source=Targets(),
        workflow=Workflow(),
        worker_id="central-backup",
    ).run_once(now=now)

    assert report.deferred_busy == ("cell-a",)
    assert report.started == 0


@pytest.mark.asyncio
async def test_central_scheduler_can_drive_complete_database_backup_on_same_cadence(
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    seen: list[RunKind] = []

    class Targets:
        async def list_backup_targets(self):
            return [BackupTarget("control-plane", "control-plane-databases", 1)]

    class Workflow:
        async def run(self, run, *, worker_id):
            seen.append(run.identity.kind)
            await repository.complete(
                run.id,
                worker_id,
                claim_token=run.claim_token,
                claim_generation=run.claim_generation,
                result={"scratchRestoreVerified": True},
            )

    report = await CentralBackupScheduler(
        repository=repository,
        target_source=Targets(),
        workflow=Workflow(),
        worker_id="central-database-backup",
        run_kind=RunKind.DATABASE_BACKUP,
    ).run_once(now=datetime.now(UTC))

    assert report.completed == 1
    assert seen == [RunKind.DATABASE_BACKUP]


@pytest.mark.asyncio
async def test_upload_lost_ack_resumes_from_encrypted_checkpoint_without_resnapshot(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    store = LostAcknowledgementStore(events)
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-lost-ack",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    first = await repository.claim(
        run.id, "worker-before", now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    )

    def workflow(clock_time: datetime) -> ExportBackupWorkflow:
        return ExportBackupWorkflow(
            repository=repository,
            routes=RecordingRoutes(events),
            runtime=RecordingRuntime(tmp_path, events),
            upload_store=store,
            cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
            key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
            provider_identity_signer=_identity_signer(),
            provider_bucket="recovery-test-bucket",
            scratch_root=tmp_path,
            min_archive_bytes=1024,
            clock=lambda: clock_time,
        )

    with pytest.raises(RuntimeError, match="acknowledgement"):
        await workflow(datetime(2030, 1, 1, 12, 0, tzinfo=UTC)).run(
            first, worker_id="worker-before"
        )
    interrupted = await repository.get(run.id)
    assert interrupted is not None and interrupted.checkpoint == "encrypted"

    resumed = await repository.claim(
        run.id, "worker-after", now=datetime(2030, 1, 1, 12, 6, tzinfo=UTC)
    )
    result = await workflow(datetime(2030, 1, 1, 12, 6, tzinfo=UTC)).run(
        resumed, worker_id="worker-after"
    )

    assert result.integrity_verified is True
    assert store.calls == 1
    assert sum(event.startswith("archive:") for event in events) == 1


@pytest.mark.asyncio
async def test_missing_remote_object_after_restart_resnapshots_scheduled_backup(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    store = FailedBeforeCommitStore(events)
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-rejected-before-commit",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )

    def workflow(clock_time: datetime) -> ExportBackupWorkflow:
        return ExportBackupWorkflow(
            repository=repository,
            routes=RecordingRoutes(events),
            runtime=RecordingRuntime(tmp_path, events),
            upload_store=store,
            cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
            key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
            provider_identity_signer=_identity_signer(),
            provider_bucket="recovery-test-bucket",
            scratch_root=tmp_path,
            min_archive_bytes=1024,
            clock=lambda: clock_time,
        )

    first = await repository.claim(
        run.id,
        "worker-before",
        now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(RuntimeError, match="before commit"):
        await workflow(datetime(2030, 1, 1, 12, 0, tzinfo=UTC)).run(
            first,
            worker_id="worker-before",
        )

    resumed = await repository.claim(
        run.id,
        "worker-after",
        now=datetime(2030, 1, 1, 12, 6, tzinfo=UTC),
    )
    result = await workflow(datetime(2030, 1, 1, 12, 6, tzinfo=UTC)).run(
        resumed,
        worker_id="worker-after",
    )

    assert result.integrity_verified is True
    assert store.calls == 2
    assert sum(event.startswith("archive:") for event in events) == 2


@pytest.mark.asyncio
async def test_uploaded_checkpoint_finishes_after_restart_without_local_scratch(
    tmp_path: Path,
    workflow_context: tuple[ProvisionerDatabase, DurabilityRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository = workflow_context
    events: list[str] = []
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id="backup-uploaded-restart",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    claimed = await repository.claim(
        run.id, "worker-before", now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    )
    workflow = ExportBackupWorkflow(
        repository=repository,
        routes=RecordingRoutes(events),
        runtime=RecordingRuntime(tmp_path, events),
        upload_store=RecordingUploadStore(events),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2),
        provider_identity_signer=_identity_signer(),
        provider_bucket="recovery-test-bucket",
        scratch_root=tmp_path,
        min_archive_bytes=1024,
        clock=lambda: datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )
    record_verified = repository.record_verified_object
    failed_once = False

    async def lose_ack(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("worker stopped after uploaded checkpoint")
        return await record_verified(*args, **kwargs)

    monkeypatch.setattr(repository, "record_verified_object", lose_ack)
    with pytest.raises(RuntimeError, match="uploaded checkpoint"):
        await workflow.run(claimed, worker_id="worker-before")
    interrupted = await repository.get(run.id)
    assert interrupted is not None and interrupted.checkpoint == "uploaded"
    for suffix in ("archive", "manifest.json", "encrypted"):
        (tmp_path / f"{run.id}.{suffix}").unlink(missing_ok=True)

    resumed = await repository.claim(
        run.id, "worker-after", now=datetime(2030, 1, 1, 12, 6, tzinfo=UTC)
    )
    result = await workflow.run(resumed, worker_id="worker-after")

    assert result.integrity_verified is True
    assert sum(event.startswith("archive:") for event in events) == 1
