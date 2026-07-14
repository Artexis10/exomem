from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.models import CredentialMetadata, OperationState, ResourceKind
from exomem_provisioner.repository import (
    ClaimConflict,
    IdempotencyConflict,
    ImmutableMetadataConflict,
    OperationRepository,
    StaleFence,
    _claim_statement,
    canonical_request_sha256,
)


def _settings(database: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{database}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )


def _request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "operationId": "operation-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 7,
        "tenantId": "tenant-alpha",
        "cellId": "cell-alpha",
        "protocolVersion": "exomem-hosted.v1",
        "releaseVersion": "0.22.0",
        "serviceCredential": "service-credential-sentinel-000000000",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
    }
    value.update(overrides)
    return value


@pytest.fixture
async def repository(tmp_path: Path) -> OperationRepository:
    settings = _settings(tmp_path / "provisioner.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repo = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
    )
    try:
        yield repo
    finally:
        await database.dispose()


def test_canonical_request_hash_is_order_independent_and_secret_sensitive() -> None:
    request = _request()
    reordered = dict(reversed(list(request.items())))
    changed = _request(serviceCredential="different-credential-sentinel-000000")

    assert canonical_request_sha256(request) == canonical_request_sha256(reordered)
    assert canonical_request_sha256(request) != canonical_request_sha256(changed)
    assert len(canonical_request_sha256(request)) == 64


def test_postgres_claim_query_uses_skip_locked() -> None:
    compiled = str(
        _claim_statement("operation-id", datetime.now(UTC)).compile(dialect=postgresql.dialect())
    ).upper()

    assert "FOR UPDATE SKIP LOCKED" in compiled


@pytest.mark.asyncio
async def test_submit_replays_exact_request_and_conflicts_changed_body(
    repository: OperationRepository,
) -> None:
    first = await repository.submit("provision", "idempotency-alpha", _request())
    replay = await repository.submit("provision", "idempotency-alpha", _request())

    assert first.id == replay.id
    assert first.state is replay.state is OperationState.PENDING
    assert first.canonical_request_sha256 == canonical_request_sha256(_request())

    with pytest.raises(IdempotencyConflict):
        await repository.submit(
            "provision",
            "idempotency-alpha",
            _request(serviceCredential="changed-credential-sentinel-0000000"),
        )

    other_action = await repository.submit("health", "idempotency-alpha", _request())
    assert other_action.id != first.id


@pytest.mark.asyncio
async def test_tenant_fence_is_monotonic_before_operation_creation(
    repository: OperationRepository,
) -> None:
    newest = await repository.submit("destroy", "destroy-new", _request(fenceGeneration=12))

    with pytest.raises(StaleFence):
        await repository.submit("resume", "resume-stale", _request(fenceGeneration=11))

    assert await repository.get("resume", "resume-stale") is None
    same_fence = await repository.submit("health", "health-current", _request(fenceGeneration=12))
    assert newest.fence_generation == same_fence.fence_generation == 12


@pytest.mark.asyncio
async def test_exact_replay_is_rejected_after_tenant_fence_advances(
    repository: OperationRepository,
) -> None:
    stale_request = _request(fenceGeneration=7)
    stale = await repository.submit("provision", "stale-replay", stale_request)
    await repository.submit("destroy", "newer-destroy", _request(fenceGeneration=8))

    with pytest.raises(StaleFence):
        await repository.submit("provision", "stale-replay", stale_request)

    unchanged = await repository.get("provision", "stale-replay")
    assert unchanged is not None
    assert unchanged.id == stale.id
    assert unchanged.state is OperationState.PENDING


@pytest.mark.asyncio
async def test_claim_skips_queued_operation_behind_durable_tenant_fence(
    repository: OperationRepository,
) -> None:
    stale = await repository.submit(
        "provision",
        "queued-stale",
        _request(operationId="operation-stale", fenceGeneration=7),
    )
    current = await repository.submit(
        "destroy",
        "queued-current",
        _request(operationId="operation-current", fenceGeneration=8),
    )

    claimed = await repository.claim_next("fence-worker", now=datetime.now(UTC))

    assert claimed is not None
    assert claimed.id == current.id
    unchanged = await repository.get_by_id(stale.id)
    assert unchanged is not None and unchanged.state is OperationState.PENDING


@pytest.mark.asyncio
async def test_sqlite_claim_fallback_atomically_assigns_one_worker(
    repository: OperationRepository,
) -> None:
    await repository.submit("provision", "single-claim", _request())
    now = datetime.now(UTC) + timedelta(seconds=1)

    claims = await asyncio.gather(
        repository.claim_next("worker-one", now=now),
        repository.claim_next("worker-two", now=now),
    )

    assert sum(claim is not None for claim in claims) == 1


@pytest.mark.asyncio
async def test_reclaimed_operation_rotates_token_and_rejects_old_same_owner_writes(
    repository: OperationRepository,
) -> None:
    operation = await repository.submit("provision", "claim-token", _request())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    first = await repository.claim_next("reused-worker-id", now=now)
    assert first is not None
    assert first.claim_generation == 1
    assert first.claim_token

    reclaimed = await repository.claim_next(
        "reused-worker-id",
        now=now + timedelta(seconds=31),
    )
    assert reclaimed is not None and reclaimed.id == operation.id
    assert reclaimed.claim_generation == 2
    assert reclaimed.claim_token
    assert reclaimed.claim_token != first.claim_token

    with pytest.raises(ClaimConflict):
        await repository.record_resource(
            operation_id=operation.id,
            worker_id="reused-worker-id",
            claim_token=first.claim_token,
            claim_generation=first.claim_generation,
            now=now + timedelta(seconds=31),
            tenant_id="tenant-alpha",
            cell_id="cell-alpha",
            kind=ResourceKind.KUBERNETES_NAMESPACE,
            recoverable_reference="stale-worker-resource",
            provider_operation_id="operation-alpha",
            provider_fence_generation=7,
        )
    with pytest.raises(ClaimConflict):
        await repository.complete(
            operation.id,
            {},
            worker_id="reused-worker-id",
            claim_token=first.claim_token,
            claim_generation=first.claim_generation,
            now=now + timedelta(seconds=31),
        )


@pytest.mark.asyncio
async def test_live_claim_can_renew_but_expired_claim_cannot_be_revived(
    repository: OperationRepository,
) -> None:
    await repository.submit("provision", "renew-claim", _request())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    claim = await repository.claim_next("renewing-worker", now=now)
    assert claim is not None and claim.claim_token

    renewed = await repository.renew_claim(
        claim.id,
        "renewing-worker",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        now=now + timedelta(seconds=20),
    )
    assert renewed.claim_expires_at is not None
    assert renewed.claim_expires_at >= now + timedelta(seconds=50)

    with pytest.raises(ClaimConflict):
        await repository.renew_claim(
            claim.id,
            "renewing-worker",
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            now=now + timedelta(seconds=51),
        )


@pytest.mark.asyncio
async def test_concurrent_initial_fences_converge_without_false_idempotency_conflict(
    repository: OperationRepository,
) -> None:
    outcomes = await asyncio.gather(
        repository.submit("provision", "fence-low", _request(fenceGeneration=4)),
        repository.submit("destroy", "fence-high", _request(fenceGeneration=10)),
        return_exceptions=True,
    )

    assert not any(isinstance(outcome, IdempotencyConflict) for outcome in outcomes)
    high = await repository.get("destroy", "fence-high")
    assert high is not None and high.fence_generation == 10
    with pytest.raises(StaleFence):
        await repository.submit("resume", "after-high", _request(fenceGeneration=9))


@pytest.mark.asyncio
async def test_repository_persists_typed_records_and_immutable_provider_metadata(
    repository: OperationRepository,
) -> None:
    operation = await repository.submit("provision", "records", _request())
    claim = await repository.claim_next("records-worker")
    assert claim is not None and claim.id == operation.id and claim.claim_token
    claim_args = {
        "worker_id": "records-worker",
        "claim_token": claim.claim_token,
        "claim_generation": claim.claim_generation,
    }
    resource = await repository.record_resource(
        operation_id=operation.id,
        **claim_args,
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        kind=ResourceKind.KUBERNETES_NAMESPACE,
        recoverable_reference="namespace-provider-sentinel",
        provider_operation_id="operation-alpha",
        provider_fence_generation=7,
    )
    same = await repository.record_resource(
        operation_id=operation.id,
        **claim_args,
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        kind=ResourceKind.KUBERNETES_NAMESPACE,
        recoverable_reference="namespace-provider-sentinel",
        provider_operation_id="operation-alpha",
        provider_fence_generation=7,
    )
    assert resource.id == same.id

    with pytest.raises(ImmutableMetadataConflict):
        await repository.record_resource(
            operation_id=operation.id,
            **claim_args,
            tenant_id="tenant-alpha",
            cell_id="cell-alpha",
            kind=ResourceKind.KUBERNETES_NAMESPACE,
            recoverable_reference="namespace-provider-sentinel",
            provider_operation_id="different-operation",
            provider_fence_generation=8,
        )

    digest = hashlib.sha256(b"credential-sentinel").hexdigest()
    await repository.record_credential_metadata(
        operation_id=operation.id,
        **claim_args,
        cell_id="cell-alpha",
        version=2,
        credential_digest=digest,
        active=True,
    )
    await repository.record_export(
        operation_id=operation.id,
        **claim_args,
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        export_reference="export-provider-sentinel",
        archive_sha256="a" * 64,
        manifest_sha256="b" * 64,
        archive_size=1024,
        provider_operation_id="operation-alpha",
        provider_fence_generation=7,
    )
    await repository.record_backup(
        operation_id=operation.id,
        **claim_args,
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        backup_reference="backup-provider-sentinel",
        object_sha256="c" * 64,
        provider_operation_id="operation-alpha",
        provider_fence_generation=7,
    )


@pytest.mark.asyncio
async def test_export_replay_compares_every_immutable_field(
    repository: OperationRepository,
) -> None:
    operation = await repository.submit("export", "immutable-export", _request())
    claim = await repository.claim_next("export-worker")
    assert claim is not None and claim.claim_token
    original: dict[str, object] = {
        "operation_id": operation.id,
        "worker_id": "export-worker",
        "claim_token": claim.claim_token,
        "claim_generation": claim.claim_generation,
        "tenant_id": "tenant-alpha",
        "cell_id": "cell-alpha",
        "export_reference": "export-provider-sentinel",
        "archive_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "archive_size": 1024,
        "provider_operation_id": "operation-alpha",
        "provider_fence_generation": 7,
    }
    await repository.record_export(**original)  # type: ignore[arg-type]

    changes: dict[str, object] = {
        "tenant_id": "tenant-other",
        "cell_id": "cell-other",
        "export_reference": "export-other",
        "archive_sha256": "c" * 64,
        "manifest_sha256": "d" * 64,
        "archive_size": 2048,
        "provider_operation_id": "operation-other",
        "provider_fence_generation": 8,
    }
    for field, changed in changes.items():
        replay = {**original, field: changed}
        with pytest.raises(ImmutableMetadataConflict, match="export provider metadata"):
            await repository.record_export(**replay)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_backup_replay_compares_every_immutable_field(
    repository: OperationRepository,
) -> None:
    operation = await repository.submit("export", "immutable-backup", _request())
    claim = await repository.claim_next("backup-worker")
    assert claim is not None and claim.claim_token
    original: dict[str, object] = {
        "operation_id": operation.id,
        "worker_id": "backup-worker",
        "claim_token": claim.claim_token,
        "claim_generation": claim.claim_generation,
        "tenant_id": "tenant-alpha",
        "cell_id": "cell-alpha",
        "backup_reference": "backup-provider-sentinel",
        "object_sha256": "a" * 64,
        "provider_operation_id": "operation-alpha",
        "provider_fence_generation": 7,
    }
    await repository.record_backup(**original)  # type: ignore[arg-type]

    changes: dict[str, object] = {
        "tenant_id": "tenant-other",
        "cell_id": "cell-other",
        "backup_reference": "backup-other",
        "object_sha256": "b" * 64,
        "provider_operation_id": "operation-other",
        "provider_fence_generation": 8,
    }
    for field, changed in changes.items():
        replay = {**original, field: changed}
        with pytest.raises(ImmutableMetadataConflict, match="backup provider metadata"):
            await repository.record_backup(**replay)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_side_effect_records_require_claim_bound_operation_identity(
    repository: OperationRepository,
) -> None:
    operation = await repository.submit("provision", "bound-effects", _request())
    claim = await repository.claim_next("bound-worker")
    assert claim is not None and claim.claim_token
    claim_args = {
        "worker_id": "bound-worker",
        "claim_token": claim.claim_token,
        "claim_generation": claim.claim_generation,
    }

    with pytest.raises(ImmutableMetadataConflict, match="operation identity"):
        await repository.record_resource(
            operation_id=operation.id,
            **claim_args,
            tenant_id="tenant-other",
            cell_id="cell-alpha",
            kind=ResourceKind.VOLUME,
            recoverable_reference="volume-reference",
            provider_operation_id="operation-alpha",
            provider_fence_generation=7,
        )
    with pytest.raises(ImmutableMetadataConflict, match="operation identity"):
        await repository.record_credential_metadata(
            operation_id=operation.id,
            **claim_args,
            cell_id="cell-other",
            version=1,
            credential_digest="a" * 64,
            active=False,
        )
    with pytest.raises(ImmutableMetadataConflict, match="operation identity"):
        await repository.record_export(
            operation_id=operation.id,
            **claim_args,
            tenant_id="tenant-alpha",
            cell_id="cell-alpha",
            export_reference="export-reference",
            archive_sha256="a" * 64,
            manifest_sha256="b" * 64,
            archive_size=1024,
            provider_operation_id="operation-other",
            provider_fence_generation=7,
        )
    with pytest.raises(ImmutableMetadataConflict, match="operation identity"):
        await repository.record_backup(
            operation_id=operation.id,
            **claim_args,
            tenant_id="tenant-alpha",
            cell_id="cell-alpha",
            backup_reference="backup-reference",
            object_sha256="c" * 64,
            provider_operation_id="operation-alpha",
            provider_fence_generation=8,
        )


@pytest.mark.asyncio
async def test_credential_stage_promote_is_claim_bound_and_monotonic(
    repository: OperationRepository,
) -> None:
    operation = await repository.submit("rotate-credential", "credential-promotion", _request())
    claim = await repository.claim_next("credential-worker")
    assert claim is not None and claim.claim_token
    claim_args = {
        "worker_id": "credential-worker",
        "claim_token": claim.claim_token,
        "claim_generation": claim.claim_generation,
    }
    first_digest = hashlib.sha256(b"credential-one").hexdigest()
    next_digest = hashlib.sha256(b"credential-two").hexdigest()

    await repository.record_credential_metadata(
        operation_id=operation.id,
        **claim_args,
        cell_id="cell-alpha",
        version=1,
        credential_digest=first_digest,
        active=True,
    )
    await repository.record_credential_metadata(
        operation_id=operation.id,
        **claim_args,
        cell_id="cell-alpha",
        version=2,
        credential_digest=next_digest,
        active=False,
    )
    await repository.record_credential_metadata(
        operation_id=operation.id,
        **claim_args,
        cell_id="cell-alpha",
        version=2,
        credential_digest=next_digest,
        active=True,
    )

    async with repository._sessions() as session:  # noqa: SLF001 - persistence invariant
        active_count = await session.scalar(
            select(func.count())
            .select_from(CredentialMetadata)
            .where(CredentialMetadata.cell_id == "cell-alpha", CredentialMetadata.active.is_(True))
        )
    assert active_count == 1
    with pytest.raises(ImmutableMetadataConflict, match="cannot be reversed"):
        await repository.record_credential_metadata(
            operation_id=operation.id,
            **claim_args,
            cell_id="cell-alpha",
            version=2,
            credential_digest=next_digest,
            active=False,
        )
    with pytest.raises(ImmutableMetadataConflict, match="identity"):
        await repository.record_credential_metadata(
            operation_id=operation.id,
            **claim_args,
            cell_id="cell-alpha",
            version=2,
            credential_digest="f" * 64,
            active=True,
        )


@pytest.mark.asyncio
async def test_encrypted_request_and_refs_survive_restart_without_plaintext_in_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.sqlite"
    settings = _settings(path)
    codec = AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value())
    first_database = ProvisionerDatabase(settings)
    await first_database.create_for_tests()
    first_repository = OperationRepository(first_database.session_factory, codec=codec)
    operation = await first_repository.submit("provision", "restart-key", _request())
    claim = await first_repository.claim_next("restart-worker")
    assert claim is not None and claim.id == operation.id and claim.claim_token
    await first_repository.record_resource(
        operation_id=operation.id,
        worker_id="restart-worker",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        kind=ResourceKind.VOLUME,
        recoverable_reference="volume-reference-sentinel",
        provider_operation_id="operation-alpha",
        provider_fence_generation=7,
    )
    await first_database.dispose()

    database_bytes = path.read_bytes()
    for forbidden in (
        b"service-credential-sentinel",
        b"volume-reference-sentinel",
        b"private note sentinel",
    ):
        assert forbidden not in database_bytes

    second_database = ProvisionerDatabase(settings)
    second_repository = OperationRepository(second_database.session_factory, codec=codec)
    recovered = await second_repository.get("provision", "restart-key")
    request = await second_repository.load_request(operation.id)
    await second_database.dispose()

    assert recovered is not None
    assert recovered.id == operation.id
    assert request == _request()

    with sqlite3.connect(path) as connection:
        operation_columns = {row[1] for row in connection.execute("PRAGMA table_info(operations)")}
    assert "request_json" not in operation_columns
    assert "request_ciphertext" in operation_columns
