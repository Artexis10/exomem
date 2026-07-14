from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.database_backup import (
    CommandResult,
    DatabaseBackupWorkflow,
    DatabaseRecoveryVerificationError,
    DatabaseRestoreWorkflow,
    PostgresLogicalBackup,
    PostgresRecoveryConfig,
    ScheduledDatabaseBackupWorkflow,
)
from exomem_provisioner.durability_crypto import (
    AesGcmKeyWrapper,
    AesGcmRecoveryEnvelopeCodec,
    ChunkedArchiveCipher,
)
from exomem_provisioner.durability_repository import DurabilityRepository, RunIdentity, RunKind
from exomem_provisioner.durability_store import ProviderObjectHead
from exomem_provisioner.provider_recovery import (
    ProviderRecoveryIdentityCodec,
    ProviderRecoveryIdentityDecoder,
    ProviderReference,
)


def _identity_signer() -> ProviderRecoveryIdentityCodec:
    return ProviderRecoveryIdentityCodec.from_secret("provider-recovery-signing-test-seed")


class RecordingExecutor:
    def __init__(
        self, *, proof: str = "substrate_restore_owner\ttenant-alpha\tcell-alpha\n"
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.proof = proof

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
    ) -> CommandResult:
        self.calls.append((argv, env))
        if argv[0] == "/usr/bin/pg_dump":
            destination = Path(argv[argv.index("--file") + 1])
            destination.write_bytes(b"postgres custom dump" * 100)
        return CommandResult(returncode=0, stdout=self.proof if argv[0] == "/usr/bin/psql" else "")


def _config(tmp_path: Path) -> PostgresRecoveryConfig:
    service_file = tmp_path / "pg_service.conf"
    password_file = tmp_path / ".pgpass"
    service_file.write_text("[production]\nhost=db.invalid\n", encoding="utf-8")
    password_file.write_text("*:*:*:*:secret-sentinel\n", encoding="utf-8")
    service_file.chmod(0o600)
    password_file.chmod(0o600)
    return PostgresRecoveryConfig(
        pg_dump="/usr/bin/pg_dump",
        pg_restore="/usr/bin/pg_restore",
        psql="/usr/bin/psql",
        dropdb="/usr/bin/dropdb",
        createdb="/usr/bin/createdb",
        service_file=service_file,
        password_file=password_file,
        source_service="production",
        maintenance_service="maintenance",
        scratch_service="scratch-empty",
        scratch_database="exomem_restore_scratch",
        expected_restore_owner="substrate_restore_owner",
        verification_sql=(
            "SELECT current_user, h.tenant_id, h.cell_id "
            "FROM app.hosted_cells h "
            "WHERE h.tenant_id = current_setting('app.restore_tenant_id') "
            "AND h.cell_id = current_setting('app.restore_cell_id')"
        ),
    )


@pytest.mark.asyncio
async def test_complete_database_dump_is_transactionally_consistent_and_not_schema_filtered(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    backup = PostgresLogicalBackup(_config(tmp_path), executor=executor)
    destination = tmp_path / "complete.dump"

    proof = await backup.dump_complete_database(destination)

    argv, env = executor.calls[0]
    assert argv == (
        "/usr/bin/pg_dump",
        "--format=custom",
        "--compress=0",
        "--no-owner",
        "--no-privileges",
        "--serializable-deferrable",
        "--file",
        str(tmp_path / ".complete.dump.partial"),
    )
    assert not any(argument.startswith("--schema") for argument in argv)
    assert env == {
        "PGSERVICE": "production",
        "PGSERVICEFILE": str(tmp_path / "pg_service.conf"),
        "PGPASSFILE": str(tmp_path / ".pgpass"),
    }
    assert "secret-sentinel" not in repr(executor.calls)
    assert proof.size == destination.stat().st_size
    assert proof.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_empty_scratch_restore_is_atomic_and_proves_owner_tenant_cell_resolution(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    backup = PostgresLogicalBackup(_config(tmp_path), executor=executor)
    source = tmp_path / "complete.dump"
    source.write_bytes(b"postgres custom dump")

    proof = await backup.restore_and_verify_empty_scratch(
        source,
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
    )

    drop_argv, drop_env = executor.calls[0]
    create_argv, create_env = executor.calls[1]
    assert drop_argv == (
        "/usr/bin/dropdb",
        "--if-exists",
        "--force",
        "--maintenance-db=service=maintenance",
        "exomem_restore_scratch",
    )
    assert create_argv == (
        "/usr/bin/createdb",
        "--maintenance-db=service=maintenance",
        "--owner=substrate_restore_owner",
        "exomem_restore_scratch",
    )
    assert drop_env["PGSERVICE"] == create_env["PGSERVICE"] == "maintenance"
    restore_argv, restore_env = executor.calls[2]
    assert restore_argv == (
        "/usr/bin/pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-privileges",
        "--dbname=service=scratch-empty",
        str(source),
    )
    assert "--clean" not in restore_argv
    verify_argv, verify_env = executor.calls[3]
    assert verify_argv[:4] == (
        "/usr/bin/psql",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
    )
    assert "tenant-alpha" not in " ".join(verify_argv)
    assert (
        verify_env["PGOPTIONS"]
        == "-c app.restore_tenant_id=tenant-alpha -c app.restore_cell_id=cell-alpha"
    )
    assert restore_env["PGSERVICE"] == verify_env["PGSERVICE"] == "scratch-empty"
    assert proof.owner_authenticated is True
    assert proof.tenant_resolved is True
    assert proof.cell_resolved is True


@pytest.mark.asyncio
async def test_scratch_restore_rejects_wrong_owner_or_resolution(tmp_path: Path) -> None:
    executor = RecordingExecutor(proof="wrong_owner\ttenant-alpha\tcell-other\n")
    backup = PostgresLogicalBackup(_config(tmp_path), executor=executor)
    source = tmp_path / "complete.dump"
    source.write_bytes(b"postgres custom dump")

    with pytest.raises(DatabaseRecoveryVerificationError):
        await backup.restore_and_verify_empty_scratch(
            source,
            tenant_id="tenant-alpha",
            cell_id="cell-alpha",
        )


@pytest.mark.asyncio
async def test_scratch_restore_rejects_identifier_that_could_inject_pgoptions(
    tmp_path: Path,
) -> None:
    backup = PostgresLogicalBackup(_config(tmp_path), executor=RecordingExecutor())
    source = tmp_path / "complete.dump"
    source.write_bytes(b"postgres custom dump")

    with pytest.raises(DatabaseRecoveryVerificationError, match="identity"):
        await backup.restore_and_verify_empty_scratch(
            source,
            tenant_id="tenant-alpha -c session_preload_libraries=evil",
            cell_id="cell-alpha",
        )


def test_recovery_config_rejects_world_readable_password_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.password_file.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        PostgresLogicalBackup(config, executor=RecordingExecutor())


@pytest.mark.asyncio
async def test_scheduled_database_adapter_binds_representative_identity_proof() -> None:
    calls: list[dict[str, object]] = []

    class Workflow:
        async def run(self, run, **arguments):
            calls.append(arguments)
            return {"scratchRestoreVerified": True}

    result = await ScheduledDatabaseBackupWorkflow(
        Workflow(),
        proof_tenant_id="tenant-proof",
        proof_cell_id="cell-proof",
    ).run(object(), worker_id="database-worker")

    assert result == {"scratchRestoreVerified": True}
    assert calls == [
        {
            "worker_id": "database-worker",
            "proof_tenant_id": "tenant-proof",
            "proof_cell_id": "cell-proof",
        }
    ]


class RecordingDatabaseStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.heads: dict[str, ProviderObjectHead] = {}

    async def put_file(self, key, source, *, metadata, retain_until):
        assert "scratch-verified" in self.events
        self.events.append("uploaded-envelope" if key.endswith(".envelope") else "uploaded")
        head = ProviderObjectHead(
            key=key,
            size=source.stat().st_size,
            metadata=metadata,
            version_id="database-version-opaque",
            retain_until=retain_until,
        )
        self.heads[key] = head
        return head

    async def head(self, key):
        return self.heads.get(key)


class RemoteArtifactStore(RecordingDatabaseStore):
    """Fake B2 whose contents survive disposal of the provisioner database."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.objects: dict[str, bytes] = {}

    async def put_file(self, key, source, *, metadata, retain_until):
        head = ProviderObjectHead(
            key=key,
            size=source.stat().st_size,
            metadata=metadata,
            version_id=f"version-{len(self.objects) + 1}",
            retain_until=retain_until,
        )
        self.heads[key] = head
        self.objects[key] = source.read_bytes()
        return head

    async def list_page(self, *, prefix, continuation_token=None):
        assert continuation_token is None
        return ([key for key in sorted(self.objects) if key.startswith(prefix)], None)

    async def download_file(self, key, destination):
        destination.write_bytes(self.objects[key])
        destination.chmod(0o600)


class LostAcknowledgementDatabaseStore(RemoteArtifactStore):
    def __init__(self, events: list[str], *, fail_artifact: str) -> None:
        super().__init__(events)
        self.fail_artifact = fail_artifact
        self.failed = False

    async def put_file(self, key, source, *, metadata, retain_until):
        artifact = "envelope" if key.endswith(".envelope") else "database"
        contents = source.read_bytes()
        if key in self.objects:
            assert self.objects[key] == contents
            head = self.heads[key]
            assert head.metadata == metadata
            assert head.retain_until == retain_until
        else:
            head = await super().put_file(
                key,
                source,
                metadata=metadata,
                retain_until=retain_until,
            )
        if artifact == self.fail_artifact and not self.failed:
            self.failed = True
            raise RuntimeError(f"lost {artifact} acknowledgement")
        return head


@pytest.mark.asyncio
async def test_database_workflow_encrypts_only_after_empty_restore_proof_and_records_remote_object(
    tmp_path: Path,
) -> None:
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'database-workflow.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=300,
    )
    events: list[str] = []

    class ProvingLogicalBackup(PostgresLogicalBackup):
        async def restore_and_verify_empty_scratch(self, *args, **kwargs):
            result = await super().restore_and_verify_empty_scratch(*args, **kwargs)
            events.append("scratch-verified")
            return result

    logical = ProvingLogicalBackup(_config(tmp_path), executor=RecordingExecutor())
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.DATABASE_BACKUP,
            operation_id="database-backup-20300101t1200",
            tenant_id="control-plane-alpha",
            cell_id="control-plane-databases",
            fence_generation=1,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    claimed = await repository.claim(run.id, "database-backup-worker")
    store = RecordingDatabaseStore(events)
    workflow = DatabaseBackupWorkflow(
        repository=repository,
        logical_backup=logical,
        upload_store=store,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("database-recovery-key" * 2),
        recovery_envelope_codec=AesGcmRecoveryEnvelopeCodec.from_secret(
            "database-recovery-key" * 2
        ),
        provider_identity_signer=_identity_signer(),
        provider_bucket="database-test-bucket",
        scratch_root=tmp_path,
        minimum_dump_bytes=100,
    )
    try:
        result = await workflow.run(
            claimed,
            worker_id="database-backup-worker",
            proof_tenant_id="tenant-alpha",
            proof_cell_id="cell-alpha",
        )
        assert result["integrityVerified"] is True
        assert result["encryptionScheme"] == "envelope-aes-256-gcm"
        assert events == ["scratch-verified", "uploaded", "uploaded-envelope"]
        primary = next(head for key, head in store.heads.items() if not key.endswith(".envelope"))
        assert primary.metadata["wrapped-key-reference"] == result["databaseBackupRef"]
        for key, head in store.heads.items():
            observation = ProviderRecoveryIdentityDecoder.b2(
                provider_reference=ProviderReference.b2(bucket="database-test-bucket", key=key),
                metadata=head.metadata,
                observed_at=datetime.now(UTC),
                identity_codec=_identity_signer().verifier(),
            )
            assert observation.operation_id == "database-backup-20300101t1200"
        final = await repository.get(run.id)
        assert final is not None and final.checkpoint == "complete"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_uploaded_database_checkpoint_finishes_after_restart_without_local_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'database-restart.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=300,
    )
    events: list[str] = []
    store = RecordingDatabaseStore(events)
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.DATABASE_BACKUP,
            operation_id="database-backup-uploaded-restart",
            tenant_id="control-plane-alpha",
            cell_id="control-plane-databases",
            fence_generation=1,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    first = await repository.claim(
        run.id, "database-worker-before", now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    )

    class ProvingLogicalBackup(PostgresLogicalBackup):
        async def restore_and_verify_empty_scratch(self, *args, **kwargs):
            result = await super().restore_and_verify_empty_scratch(*args, **kwargs)
            events.append("scratch-verified")
            return result

    logical = ProvingLogicalBackup(_config(tmp_path), executor=RecordingExecutor())
    workflow = DatabaseBackupWorkflow(
        repository=repository,
        logical_backup=logical,
        upload_store=store,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("database-recovery-key" * 2),
        recovery_envelope_codec=AesGcmRecoveryEnvelopeCodec.from_secret(
            "database-recovery-key" * 2
        ),
        provider_identity_signer=_identity_signer(),
        provider_bucket="database-test-bucket",
        scratch_root=tmp_path,
        minimum_dump_bytes=100,
        clock=lambda: datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )
    record_verified = repository.record_verified_object
    failed_once = False

    async def lose_ack(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("worker stopped after database upload")
        return await record_verified(*args, **kwargs)

    monkeypatch.setattr(repository, "record_verified_object", lose_ack)
    try:
        with pytest.raises(RuntimeError, match="database upload"):
            await workflow.run(
                first,
                worker_id="database-worker-before",
                proof_tenant_id="tenant-alpha",
                proof_cell_id="cell-alpha",
            )
        interrupted = await repository.get(run.id)
        assert interrupted is not None and interrupted.checkpoint == "uploaded"
        (tmp_path / f"{run.id}.database.dump").unlink(missing_ok=True)
        (tmp_path / f"{run.id}.database.encrypted").unlink(missing_ok=True)

        resumed = await repository.claim(
            run.id,
            "database-worker-after",
            now=datetime(2030, 1, 1, 12, 6, tzinfo=UTC),
        )
        result = await workflow.run(
            resumed,
            worker_id="database-worker-after",
            proof_tenant_id="tenant-alpha",
            proof_cell_id="cell-alpha",
        )

        assert result["scratchRestoreVerified"] is True
        assert events.count("uploaded") == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_database_restores_after_total_ledger_loss_from_remote_artifacts_and_root_escrow(
    tmp_path: Path,
) -> None:
    """The restore input is deliberately limited to B2 artifacts and the escrow root."""
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'disposable-ledger.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=300,
    )
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.DATABASE_BACKUP,
            operation_id="database-root-escrow-proof",
            tenant_id="control-plane-alpha",
            cell_id="control-plane-databases",
            fence_generation=7,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    claimed = await repository.claim(
        run.id,
        "database-backup-worker",
        now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )
    remote = RemoteArtifactStore([])
    root_secret = "offline-root-escrow-secret"
    wrapper = AesGcmKeyWrapper.from_secret(root_secret)
    envelope_codec = AesGcmRecoveryEnvelopeCodec.from_secret(root_secret)
    backup = DatabaseBackupWorkflow(
        repository=repository,
        logical_backup=PostgresLogicalBackup(_config(tmp_path), executor=RecordingExecutor()),
        upload_store=remote,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=wrapper,
        recovery_envelope_codec=envelope_codec,
        provider_identity_signer=_identity_signer(),
        provider_bucket="database-test-bucket",
        scratch_root=tmp_path / "backup-scratch",
        minimum_dump_bytes=100,
        clock=lambda: datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )
    await backup.run(
        claimed,
        worker_id="database-backup-worker",
        proof_tenant_id="tenant-alpha",
        proof_cell_id="cell-alpha",
    )
    assert any(key.endswith(".envelope") for key in remote.objects)

    await database.dispose()
    (tmp_path / "disposable-ledger.sqlite").unlink()

    restore_executor = RecordingExecutor()
    restore = DatabaseRestoreWorkflow(
        restore_store=remote,
        logical_backup=PostgresLogicalBackup(_config(tmp_path), executor=restore_executor),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=wrapper,
        recovery_envelope_codec=envelope_codec,
        scratch_root=tmp_path / "restore-scratch",
    )
    proof = await restore.restore_latest(
        tenant_id="control-plane-alpha",
        cell_id="control-plane-databases",
        proof_tenant_id="tenant-alpha",
        proof_cell_id="cell-alpha",
    )

    assert proof["databaseRestored"] is True
    assert proof["operationId"] == "database-root-escrow-proof"
    assert any(call[0][0] == "/usr/bin/pg_restore" for call in restore_executor.calls)

    object_key = next(key for key in remote.objects if key.endswith(".recovery"))
    head = remote.heads[object_key]
    remote.heads[object_key] = ProviderObjectHead(
        key=head.key,
        size=head.size,
        metadata=head.metadata,
        version_id="substituted-database-version",
        retain_until=head.retain_until,
    )
    with pytest.raises(DatabaseRecoveryVerificationError, match="object is unavailable"):
        await restore.restore_latest(
            tenant_id="control-plane-alpha",
            cell_id="control-plane-databases",
            proof_tenant_id="tenant-alpha",
            proof_cell_id="cell-alpha",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_artifact", ["database", "envelope"])
async def test_database_backup_recovers_lost_provider_ack_without_redump(
    tmp_path: Path,
    fail_artifact: str,
) -> None:
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'lost-{fail_artifact}.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=300,
    )
    events: list[str] = []

    class ProvingLogicalBackup(PostgresLogicalBackup):
        async def restore_and_verify_empty_scratch(self, *args, **kwargs):
            result = await super().restore_and_verify_empty_scratch(*args, **kwargs)
            events.append("scratch-verified")
            return result

    run = await repository.begin(
        RunIdentity(
            kind=RunKind.DATABASE_BACKUP,
            operation_id=f"database-lost-{fail_artifact}-ack",
            tenant_id="control-plane-alpha",
            cell_id="control-plane-databases",
            fence_generation=1,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    first = await repository.claim(
        run.id,
        "database-worker-before",
        now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )
    store = LostAcknowledgementDatabaseStore(events, fail_artifact=fail_artifact)
    logical = ProvingLogicalBackup(_config(tmp_path), executor=RecordingExecutor())
    workflow = DatabaseBackupWorkflow(
        repository=repository,
        logical_backup=logical,
        upload_store=store,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("database-recovery-key" * 2),
        recovery_envelope_codec=AesGcmRecoveryEnvelopeCodec.from_secret(
            "database-recovery-key" * 2
        ),
        provider_identity_signer=_identity_signer(),
        provider_bucket="database-test-bucket",
        scratch_root=tmp_path / f"scratch-{fail_artifact}",
        minimum_dump_bytes=100,
        clock=lambda: datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )
    try:
        with pytest.raises(RuntimeError, match=f"lost {fail_artifact}"):
            await workflow.run(
                first,
                worker_id="database-worker-before",
                proof_tenant_id="tenant-alpha",
                proof_cell_id="cell-alpha",
            )
        resumed = await repository.claim(
            run.id,
            "database-worker-after",
            now=datetime(2030, 1, 1, 12, 6, tzinfo=UTC),
        )
        result = await workflow.run(
            resumed,
            worker_id="database-worker-after",
            proof_tenant_id="tenant-alpha",
            proof_cell_id="cell-alpha",
        )

        assert result["integrityVerified"] is True
        assert events.count("scratch-verified") == 1
        assert len(store.objects) == 2
    finally:
        await database.dispose()
