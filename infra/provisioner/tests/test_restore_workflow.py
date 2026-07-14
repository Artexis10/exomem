from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.durability import (
    PortableArchiveInspection,
    RestoreVerificationError,
    RestoreWorkflow,
)
from exomem_provisioner.durability_crypto import (
    AesGcmKeyWrapper,
    ChunkedArchiveCipher,
    RecoveryIdentity,
)
from exomem_provisioner.durability_repository import (
    DurabilityRepository,
    RecoveryObjectInput,
    RunIdentity,
    RunKind,
)
from exomem_provisioner.durability_store import ProviderObjectHead
from exomem_provisioner.models import RecoveryObject
from exomem_provisioner.provider_recovery import (
    ProviderRecoveryIdentityCodec,
    ProviderReference,
)

PROVIDER_BUCKET = "recovery-test-bucket"


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


class RestoreStore:
    def __init__(self, key: str, source: Path, head: ProviderObjectHead) -> None:
        self.key = key
        self.source = source
        self.object_head = head
        self.head_calls = 0
        self.download_calls = 0

    async def head(self, key: str) -> ProviderObjectHead | None:
        self.head_calls += 1
        return self.object_head if key == self.key else None

    async def download_file(self, key: str, destination: Path) -> None:
        self.download_calls += 1
        assert key == self.key
        destination.write_bytes(self.source.read_bytes())


class RestoreRuntime:
    def __init__(self, *, source_binding: bool = False, readiness: bool = True) -> None:
        self.source_binding = source_binding
        self.readiness = readiness
        self.events: list[str] = []

    async def inspect_portable_archive(self, path: Path) -> PortableArchiveInspection:
        assert path.is_file()
        return PortableArchiveInspection(
            manifest_sha256="b" * 64,
            source_cell_id="cell-source-alpha",
            hosted_state_included=self.source_binding,
            path_safe=True,
            schema_compatible=True,
            release_compatible=True,
        )

    async def stop_candidate(self, candidate_cell_id: str) -> None:
        self.events.append(f"stopped:{candidate_cell_id}")

    async def offline_restore(
        self,
        candidate_cell_id: str,
        archive_path: Path,
        *,
        helper_version: str,
        release_version: str,
        operation_id: str,
        fence_generation: int,
        source_cell_id: str,
        archive_sha256: str,
        artifact_reference: str,
    ) -> None:
        assert archive_path.is_file()
        assert helper_version == "1"
        assert release_version == "0.22.0"
        assert operation_id in {
            "restore-operation-alpha-baseline-1",
            "restore-operation-alpha-cleanup-1",
        }
        assert fence_generation == 10
        assert source_cell_id == "cell-source-alpha"
        assert archive_sha256 == hashlib.sha256(archive_path.read_bytes()).hexdigest()
        assert artifact_reference == "recovery_opaque_source"
        self.events.append(f"published:{candidate_cell_id}")

    async def authenticated_readiness(self, candidate_cell_id: str) -> bool:
        self.events.append(f"ready:{candidate_cell_id}")
        return self.readiness

    async def product_checks(self, candidate_cell_id: str) -> dict[str, bool]:
        return {
            "capture": True,
            "recall": True,
            "review": True,
            "export": True,
        }

    async def finalize_candidate(self, candidate_cell_id: str) -> dict[str, bool]:
        self.events.append(f"finalized:{candidate_cell_id}")
        return {
            "restart": True,
            "candidateIdentity": candidate_cell_id == "cell-candidate-alpha",
        }


class FailingProductCheckRuntime(RestoreRuntime):
    async def product_checks(self, candidate_cell_id: str) -> dict[str, bool]:
        raise RuntimeError("recall probe failed")


@pytest.fixture
async def restore_context(tmp_path: Path):
    settings = _settings(tmp_path / "restore.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=300,
    )
    source = tmp_path / "portable.tar"
    source.write_bytes(b"canonical portable data" * 1000)
    identity = RecoveryIdentity(
        tenant_id="tenant-durable-alpha",
        cell_id="cell-source-alpha",
        operation_id="backup-operation-alpha",
        fence_generation=9,
        archive_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        manifest_sha256="b" * 64,
        archive_size=source.stat().st_size,
    )
    key_wrapper = AesGcmKeyWrapper.from_secret("archive-wrapping-key" * 2)
    encrypted = tmp_path / "portable.encrypted"
    encryption = ChunkedArchiveCipher(chunk_size=16 * 1024).encrypt(
        source, encrypted, identity=identity, key_wrapper=key_wrapper
    )
    key = "vault-backup/opaque/object.recovery"
    provider_metadata = {
        **identity.provider_metadata(),
        "run-kind": RunKind.VAULT_BACKUP.value,
        "ciphertext-sha256": encryption.ciphertext_sha256,
        "ciphertext-size": str(encryption.ciphertext_size),
        "identity-envelope": _identity_signer().seal(
            provider="b2",
            provider_reference=ProviderReference.b2(bucket=PROVIDER_BUCKET, key=key),
            tenant_id=identity.tenant_id,
            cell_id=identity.cell_id,
            operation_id=identity.operation_id,
            fence_generation=identity.fence_generation,
        ),
    }
    metadata_sha256 = hashlib.sha256(
        __import__("json").dumps(provider_metadata, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    backup = await repository.begin(
        RunIdentity(
            kind=RunKind.VAULT_BACKUP,
            operation_id=identity.operation_id,
            tenant_id=identity.tenant_id,
            cell_id=identity.cell_id,
            fence_generation=identity.fence_generation,
            scheduled_for=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )
    )
    backup_claim = await repository.claim(backup.id, "backup-worker")
    saved = await repository.record_verified_object(
        backup.id,
        "backup-worker",
        claim_token=backup_claim.claim_token,
        claim_generation=backup_claim.claim_generation,
        value=RecoveryObjectInput(
            opaque_reference="recovery_opaque_source",
            provider_reference=f"b2://{key}#version-opaque",
            wrapped_data_key=encryption.wrapped_data_key,
            archive_sha256=identity.archive_sha256,
            manifest_sha256=identity.manifest_sha256,
            archive_size=identity.archive_size,
            ciphertext_sha256=encryption.ciphertext_sha256,
            ciphertext_size=encryption.ciphertext_size,
            metadata_sha256=metadata_sha256,
            object_lock_until=datetime.now(UTC) + timedelta(days=7),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        ),
    )
    await repository.complete(
        backup.id,
        "backup-worker",
        claim_token=backup_claim.claim_token,
        claim_generation=backup_claim.claim_generation,
        result={"opaque_reference": saved.opaque_reference},
    )
    head = ProviderObjectHead(
        key=key,
        size=encryption.ciphertext_size,
        metadata=provider_metadata,
        version_id="version-opaque",
        retain_until=saved.object_lock_until,
    )
    try:
        yield database, repository, key_wrapper, RestoreStore(key, encrypted, head)
    finally:
        await database.dispose()


async def _claimed_restore(repository: DurabilityRepository):
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.RESTORE,
            operation_id="restore-operation-alpha",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-candidate-alpha",
            fence_generation=10,
            scheduled_for=datetime(2030, 1, 1, 13, 0, tzinfo=UTC),
        )
    )
    return await repository.claim(run.id, "restore-worker")


@pytest.mark.asyncio
async def test_restore_decrypts_provider_object_and_publishes_only_after_product_checks(
    tmp_path: Path,
    restore_context,
) -> None:
    _, repository, key_wrapper, store = restore_context
    claimed = await _claimed_restore(repository)
    runtime = RestoreRuntime()
    workflow = RestoreWorkflow(
        repository=repository,
        restore_store=store,
        runtime=runtime,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=key_wrapper,
        provider_identity_verifier=_identity_signer().verifier(),
        provider_bucket=PROVIDER_BUCKET,
        scratch_root=tmp_path,
        release_version="0.22.0",
    )

    result = await workflow.run(
        claimed,
        worker_id="restore-worker",
        source_reference="recovery_opaque_source",
    )

    assert result == {"restored": True, "candidateCellId": "cell-candidate-alpha"}
    assert runtime.events == [
        "stopped:cell-candidate-alpha",
        "published:cell-candidate-alpha",
        "stopped:cell-candidate-alpha",
        "published:cell-candidate-alpha",
        "finalized:cell-candidate-alpha",
        "ready:cell-candidate-alpha",
    ]
    final = await repository.get(claimed.id)
    assert final is not None and final.checkpoint == "complete"


@pytest.mark.asyncio
async def test_restore_resets_candidate_when_product_checks_fail(
    tmp_path: Path,
    restore_context,
) -> None:
    _, repository, key_wrapper, store = restore_context
    claimed = await _claimed_restore(repository)
    runtime = FailingProductCheckRuntime()
    workflow = RestoreWorkflow(
        repository=repository,
        restore_store=store,
        runtime=runtime,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=key_wrapper,
        provider_identity_verifier=_identity_signer().verifier(),
        provider_bucket=PROVIDER_BUCKET,
        scratch_root=tmp_path,
        release_version="0.22.0",
    )

    with pytest.raises(RuntimeError, match="recall probe failed"):
        await workflow.run(
            claimed,
            worker_id="restore-worker",
            source_reference="recovery_opaque_source",
        )

    assert runtime.events == [
        "stopped:cell-candidate-alpha",
        "published:cell-candidate-alpha",
        "stopped:cell-candidate-alpha",
        "published:cell-candidate-alpha",
    ]


@pytest.mark.asyncio
async def test_restore_replay_after_candidate_publication_never_reads_or_rewrites_storage(
    tmp_path: Path,
    restore_context,
) -> None:
    _, repository, key_wrapper, store = restore_context
    claimed = await _claimed_restore(repository)
    published = await repository.checkpoint(
        claimed.id,
        "restore-worker",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        checkpoint="candidate-published",
        state={"archive_sha256": "a" * 64},
    )
    runtime = RestoreRuntime()
    workflow = RestoreWorkflow(
        repository=repository,
        restore_store=store,
        runtime=runtime,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=key_wrapper,
        provider_identity_verifier=_identity_signer().verifier(),
        provider_bucket=PROVIDER_BUCKET,
        scratch_root=tmp_path,
        release_version="0.22.0",
    )

    result = await workflow.run(
        published,
        worker_id="restore-worker",
        source_reference="recovery_opaque_source",
    )

    assert result["restored"] is True
    assert store.head_calls == 0
    assert store.download_calls == 0
    assert runtime.events == ["ready:cell-candidate-alpha"]


@pytest.mark.asyncio
async def test_restore_rejects_source_binding_before_candidate_publication(
    tmp_path: Path,
    restore_context,
) -> None:
    _, repository, key_wrapper, store = restore_context
    claimed = await _claimed_restore(repository)
    runtime = RestoreRuntime(source_binding=True)
    workflow = RestoreWorkflow(
        repository=repository,
        restore_store=store,
        runtime=runtime,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=key_wrapper,
        provider_identity_verifier=_identity_signer().verifier(),
        provider_bucket=PROVIDER_BUCKET,
        scratch_root=tmp_path,
        release_version="0.22.0",
    )

    with pytest.raises(RestoreVerificationError, match="hosted state"):
        await workflow.run(
            claimed,
            worker_id="restore-worker",
            source_reference="recovery_opaque_source",
        )
    assert not any(event.startswith("published:") for event in runtime.events)


@pytest.mark.asyncio
async def test_restore_rejects_expired_user_export_before_provider_read(
    tmp_path: Path,
    restore_context,
) -> None:
    database, repository, key_wrapper, store = restore_context
    async with database.session_factory.begin() as session:
        source = await session.scalar(select(RecoveryObject))
        assert source is not None
        source.kind = RunKind.USER_EXPORT
        source.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    claimed = await _claimed_restore(repository)
    workflow = RestoreWorkflow(
        repository=repository,
        restore_store=store,
        runtime=RestoreRuntime(),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=key_wrapper,
        provider_identity_verifier=_identity_signer().verifier(),
        provider_bucket=PROVIDER_BUCKET,
        scratch_root=tmp_path,
        release_version="0.22.0",
    )

    with pytest.raises(RestoreVerificationError, match="expired"):
        await workflow.run(
            claimed,
            worker_id="restore-worker",
            source_reference="recovery_opaque_source",
        )
    assert store.head_calls == 0


@pytest.mark.asyncio
async def test_restore_rejects_provider_version_substitution(
    tmp_path: Path,
    restore_context,
) -> None:
    _, repository, key_wrapper, store = restore_context
    claimed = await _claimed_restore(repository)
    head = store.object_head
    store.object_head = ProviderObjectHead(
        key=head.key,
        size=head.size,
        metadata=head.metadata,
        version_id="substituted-version",
        retain_until=head.retain_until,
    )
    workflow = RestoreWorkflow(
        repository=repository,
        restore_store=store,
        runtime=RestoreRuntime(),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=key_wrapper,
        provider_identity_verifier=_identity_signer().verifier(),
        provider_bucket=PROVIDER_BUCKET,
        scratch_root=tmp_path,
        release_version="0.22.0",
    )

    with pytest.raises(RestoreVerificationError, match="provider object proof"):
        await workflow.run(
            claimed,
            worker_id="restore-worker",
            source_reference="recovery_opaque_source",
        )


@pytest.mark.asyncio
async def test_restore_rejects_same_source_and_candidate_identity(
    tmp_path: Path,
    restore_context,
) -> None:
    _, repository, key_wrapper, store = restore_context
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.RESTORE,
            operation_id="restore-operation-alpha",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-source-alpha",
            fence_generation=10,
            scheduled_for=datetime(2030, 1, 1, 13, 0, tzinfo=UTC),
        )
    )
    claimed = await repository.claim(run.id, "restore-worker")
    workflow = RestoreWorkflow(
        repository=repository,
        restore_store=store,
        runtime=RestoreRuntime(),
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=key_wrapper,
        provider_identity_verifier=_identity_signer().verifier(),
        provider_bucket=PROVIDER_BUCKET,
        scratch_root=tmp_path,
        release_version="0.22.0",
    )

    with pytest.raises(RestoreVerificationError, match="new candidate"):
        await workflow.run(
            claimed,
            worker_id="restore-worker",
            source_reference="recovery_opaque_source",
        )
