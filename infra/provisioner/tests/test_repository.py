from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.models import OperationState, ResourceKind
from exomem_provisioner.repository import (
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
        _claim_statement(datetime.now(UTC)).compile(dialect=postgresql.dialect())
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
    resource = await repository.record_resource(
        operation_id=operation.id,
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        kind=ResourceKind.KUBERNETES_NAMESPACE,
        recoverable_reference="namespace-provider-sentinel",
        provider_operation_id="operation-alpha",
        provider_fence_generation=7,
    )
    same = await repository.record_resource(
        operation_id=operation.id,
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
        cell_id="cell-alpha",
        version=2,
        credential_digest=digest,
        active=True,
    )
    await repository.record_export(
        operation_id=operation.id,
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
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        backup_reference="backup-provider-sentinel",
        object_sha256="c" * 64,
        provider_operation_id="operation-alpha",
        provider_fence_generation=7,
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
    await first_repository.record_resource(
        operation_id=operation.id,
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
