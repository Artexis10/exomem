from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.durability_repository import (
    ActiveDurabilityRun,
    DurabilityRepository,
    ImmutableRecoveryConflict,
    RecoveryObjectInput,
    RunIdentity,
    RunKind,
)
from exomem_provisioner.provider_identity import ProviderReference
from exomem_provisioner.repository import OperationRepository


def _settings(path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )


def _identity(**overrides: object) -> RunIdentity:
    values: dict[str, object] = {
        "kind": RunKind.VAULT_BACKUP,
        "operation_id": "backup-20300101t1200",
        "tenant_id": "tenant-durable-alpha",
        "cell_id": "cell-durable-alpha",
        "fence_generation": 9,
        "scheduled_for": datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return RunIdentity(**values)  # type: ignore[arg-type]


@pytest.fixture
async def durability_repository(tmp_path: Path):
    settings = _settings(tmp_path / "durability.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=30,
    )
    try:
        yield database, repository
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_run_checkpoint_and_claim_survive_process_restart(
    durability_repository: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    database, first = durability_repository
    run = await first.begin(_identity())
    claimed = await first.claim(
        run.id, "worker-before", now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    )
    await first.checkpoint(
        run.id,
        "worker-before",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        checkpoint="scratch-verified",
        state={"scratch_path": "/system-scratch/opaque/run/archive.tar"},
        now=datetime(2030, 1, 1, 12, 0, 1, tzinfo=UTC),
    )

    restarted = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret("k" * 32),
        lease_seconds=30,
    )
    resumed = await restarted.claim(
        run.id,
        "worker-after",
        now=datetime(2030, 1, 1, 12, 0, 31, tzinfo=UTC),
    )

    assert resumed.checkpoint == "scratch-verified"
    assert resumed.state == {"scratch_path": "/system-scratch/opaque/run/archive.tar"}
    assert resumed.claim_generation == 2


@pytest.mark.asyncio
async def test_active_cell_durability_work_is_non_overlapping_and_slot_is_idempotent(
    durability_repository: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = durability_repository
    first = await repository.begin(_identity())
    replay = await repository.begin(_identity())

    assert replay.id == first.id
    with pytest.raises(ActiveDurabilityRun):
        await repository.begin(
            _identity(
                operation_id="backup-20300101t1230",
                scheduled_for=datetime(2030, 1, 1, 12, 30, tzinfo=UTC),
            )
        )


@pytest.mark.asyncio
async def test_recovery_object_is_bound_to_claimed_run_identity_and_immutable(
    durability_repository: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = durability_repository
    run = await repository.begin(_identity())
    claimed = await repository.claim(run.id, "backup-worker")
    record = RecoveryObjectInput(
        opaque_reference="recovery_01jopaqueonly",
        provider_reference="b2://opaque-prefix/object-01j",
        wrapped_data_key="wrapped-secret-key-material",
        archive_sha256="a" * 64,
        manifest_sha256="b" * 64,
        archive_size=123_456,
        ciphertext_sha256="c" * 64,
        ciphertext_size=123_999,
        metadata_sha256="d" * 64,
        object_lock_until=datetime.now(UTC) + timedelta(days=7),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    saved = await repository.record_verified_object(
        run.id,
        "backup-worker",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        value=record,
    )
    replay = await repository.record_verified_object(
        run.id,
        "backup-worker",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        value=record,
    )

    assert saved.id == replay.id
    assert saved.fence_generation == 9
    assert saved.opaque_reference == "recovery_01jopaqueonly"
    with pytest.raises(ImmutableRecoveryConflict):
        await repository.record_verified_object(
            run.id,
            "backup-worker",
            claim_token=claimed.claim_token,
            claim_generation=claimed.claim_generation,
            value=RecoveryObjectInput(**{**record.as_dict(), "ciphertext_sha256": "e" * 64}),
        )

    with pytest.raises(ImmutableRecoveryConflict, match="absence proof"):
        await repository.destroy_recovery_wrapped_key(
            record.opaque_reference, tenant_id="tenant-durable-alpha"
        )
    await repository.mark_recovery_object_deleted(
        record.opaque_reference, tenant_id="tenant-durable-alpha"
    )
    erased = await repository.destroy_recovery_wrapped_key(
        record.opaque_reference, tenant_id="tenant-durable-alpha"
    )
    assert erased.wrapped_data_key is None
    assert erased.key_destroyed_at is not None


@pytest.mark.asyncio
async def test_export_delivery_exact_version_survives_repository_restart(
    durability_repository: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    database, repository = durability_repository
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    run = await repository.begin(
        _identity(kind=RunKind.USER_EXPORT, operation_id="export-delivery-source")
    )
    claimed = await repository.claim(run.id, "export-worker", now=now)
    source = await repository.record_verified_object(
        run.id,
        "export-worker",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        value=RecoveryObjectInput(
            opaque_reference="export_source_opaque",
            provider_reference=ProviderReference.b2(
                bucket="user-export-bucket",
                key="user-export/source.enc",
                version_id="source-version",
            ),
            wrapped_data_key="wrapped-secret-key-material",
            archive_sha256="a" * 64,
            manifest_sha256="b" * 64,
            archive_size=123_456,
            ciphertext_sha256="c" * 64,
            ciphertext_size=123_999,
            metadata_sha256="d" * 64,
            object_lock_until=now,
            expires_at=now + timedelta(days=30),
        ),
        verified_at=now,
    )
    provider_reference = ProviderReference.b2(
        bucket="user-export-bucket",
        key="user-export-delivery/aa/delivery.portable",
        version_id="delivery-version-exact",
    )

    saved = await repository.record_export_delivery(
        source_object_id=source.id,
        tenant_id="tenant-durable-alpha",
        provider_reference=provider_reference,
        expires_at=now + timedelta(minutes=15),
        verified_at=now,
    )
    restarted = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret("k" * 32),
    )
    records = await restarted.tenant_export_deliveries("tenant-durable-alpha")

    assert records == [saved]
    assert ProviderReference.parse(records[0].provider_reference)["objectVersionId"] == (
        "delivery-version-exact"
    )
    assert records[0].source_object_id == source.id
    assert records[0].deleted_at is None


@pytest.mark.asyncio
async def test_freshness_uses_verified_remote_time_not_run_start(
    durability_repository: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    _, repository = durability_repository
    now = datetime(2030, 1, 1, 13, 0, tzinfo=UTC)
    run = await repository.begin(_identity())
    claimed = await repository.claim(run.id, "backup-worker", now=now - timedelta(minutes=46))
    record = RecoveryObjectInput(
        opaque_reference="recovery_01jverified",
        provider_reference="b2://opaque-prefix/object-verified",
        wrapped_data_key="wrapped-secret-key-material",
        archive_sha256="a" * 64,
        manifest_sha256="b" * 64,
        archive_size=123_456,
        ciphertext_sha256="c" * 64,
        ciphertext_size=123_999,
        metadata_sha256="d" * 64,
        object_lock_until=now + timedelta(days=7),
        expires_at=now + timedelta(days=30),
    )
    await repository.record_verified_object(
        run.id,
        "backup-worker",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        value=record,
        verified_at=now - timedelta(minutes=46),
    )

    freshness = await repository.backup_freshness("cell-durable-alpha", now=now)
    assert freshness.age_seconds == 46 * 60
    assert freshness.warning is True
    assert freshness.alpha_blocked is False

    blocked = await repository.backup_freshness(
        "cell-durable-alpha", now=now + timedelta(minutes=14)
    )
    assert blocked.alpha_blocked is True


@pytest.mark.asyncio
async def test_durability_and_lifecycle_claims_share_one_fenced_cell_lock(
    durability_repository: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    database, durability = durability_repository
    operations = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret("k" * 32),
        claim_seconds=30,
    )
    await operations.submit(
        "stop",
        "stop-cell-durable-alpha",
        {
            "operationId": "lifecycle-operation-alpha",
            "tenantId": "tenant-durable-alpha",
            "cellId": "cell-durable-alpha",
            "fenceGeneration": 9,
            "checkpoint": "requested",
        },
    )
    lifecycle_claim = await operations.claim_next("lifecycle-worker")
    assert lifecycle_claim is not None

    scheduled = await durability.begin(
        _identity(
            operation_id="scheduled-backup-concurrent",
            scheduled_for=datetime(2030, 1, 1, 12, 30, tzinfo=UTC),
        )
    )
    with pytest.raises(ActiveDurabilityRun, match="cell operation"):
        await durability.claim(scheduled.id, "backup-worker")


@pytest.mark.asyncio
async def test_outer_operation_can_reenter_shared_lock_for_its_durability_run(
    durability_repository: tuple[ProvisionerDatabase, DurabilityRepository],
) -> None:
    database, durability = durability_repository
    operations = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret("k" * 32),
    )
    await operations.submit(
        "export",
        "export-cell-durable-alpha",
        {
            "operationId": "durability-operation-alpha",
            "tenantId": "tenant-durable-alpha",
            "cellId": "cell-durable-alpha",
            "fenceGeneration": 9,
            "checkpoint": "requested",
        },
    )
    assert await operations.claim_next("operation-worker") is not None
    run = await durability.begin(
        _identity(
            kind=RunKind.USER_EXPORT,
            operation_id="durability-operation-alpha",
        )
    )

    claimed = await durability.claim(run.id, "durability-worker")

    assert claimed.status.value == "claimed"
