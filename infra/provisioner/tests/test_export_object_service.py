from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.durability import (
    ExportGarbageCollector,
    ExportObjectService,
    ExportObjectUnavailable,
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


class RestoreCapability:
    def __init__(self, head: ProviderObjectHead, ciphertext: bytes) -> None:
        self.object_head = head
        self.ciphertext = ciphertext
        self.ttls: list[int] = []

    async def head(self, key: str) -> ProviderObjectHead | None:
        return self.object_head if key == self.object_head.key else None

    async def download_file(self, key: str, destination: Path) -> None:
        assert key == self.object_head.key
        destination.write_bytes(self.ciphertext)
        destination.chmod(0o600)


class DeliveryCapability:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.ttls: list[int] = []

    async def put_file(self, key, source, *, metadata, retain_until):
        assert retain_until is None
        self.objects[key] = source.read_bytes()
        return ProviderObjectHead(key, source.stat().st_size, metadata, "delivery-v1", None)

    async def presigned_download(self, key: str, *, ttl_seconds: int) -> str:
        assert key in self.objects
        self.ttls.append(ttl_seconds)
        return f"https://downloads.invalid/{key}?ttl={ttl_seconds}"


class DeleteCapability:
    def __init__(self, key: str) -> None:
        self.key = key
        self.deleted = False
        self.delivery_objects: dict[str, ProviderObjectHead] = {}

    async def head(self, key: str) -> ProviderObjectHead | None:
        return self.delivery_objects.get(key)

    async def list_page(
        self, *, prefix: str, continuation_token: str | None = None
    ) -> tuple[list[str], str | None]:
        keys = sorted(key for key in self.delivery_objects if key.startswith(prefix))
        offset = int(continuation_token or "0")
        page = keys[offset : offset + 1]
        next_offset = offset + len(page)
        return page, str(next_offset) if next_offset < len(keys) else None

    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        if key == self.key:
            assert version_id == "version-opaque"
            self.deleted = True
        else:
            assert self.delivery_objects[key].version_id == version_id
            del self.delivery_objects[key]

    async def absent(self, key: str) -> bool:
        if key == self.key:
            return self.deleted
        return key not in self.delivery_objects


@pytest.fixture
async def export_service_context(tmp_path: Path):
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'export.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
    )
    run = await repository.begin(
        RunIdentity(
            kind=RunKind.USER_EXPORT,
            operation_id="export-operation-alpha",
            tenant_id="tenant-durable-alpha",
            cell_id="cell-durable-alpha",
            fence_generation=9,
            scheduled_for=datetime.now(UTC),
        )
    )
    claim = await repository.claim(run.id, "export-worker")
    key = "user-export/opaque/object.recovery"
    now = datetime.now(UTC)
    plaintext = b"portable-user-export\n" * 100
    archive = tmp_path / "portable.zip"
    encrypted = tmp_path / "portable.encrypted"
    archive.write_bytes(plaintext)
    identity = RecoveryIdentity(
        tenant_id="tenant-durable-alpha",
        cell_id="cell-durable-alpha",
        operation_id="export-operation-alpha",
        fence_generation=9,
        archive_sha256=hashlib.sha256(plaintext).hexdigest(),
        manifest_sha256="b" * 64,
        archive_size=len(plaintext),
    )
    wrapper = AesGcmKeyWrapper.from_secret("export-delivery-root")
    cipher = ChunkedArchiveCipher(chunk_size=16 * 1024)
    receipt = cipher.encrypt(archive, encrypted, identity=identity, key_wrapper=wrapper)
    saved = await repository.record_verified_object(
        run.id,
        "export-worker",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        value=RecoveryObjectInput(
            opaque_reference="export_opaque_reference",
            provider_reference=f"b2://{key}#version-opaque",
            wrapped_data_key=receipt.wrapped_data_key,
            archive_sha256=identity.archive_sha256,
            manifest_sha256="b" * 64,
            archive_size=identity.archive_size,
            ciphertext_sha256=receipt.ciphertext_sha256,
            ciphertext_size=receipt.ciphertext_size,
            metadata_sha256="d" * 64,
            object_lock_until=now - timedelta(seconds=1),
            expires_at=now + timedelta(hours=1),
        ),
    )
    await repository.complete(
        run.id,
        "export-worker",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        result={"available": True},
    )
    head = ProviderObjectHead(
        key=key,
        size=receipt.ciphertext_size,
        metadata={},
        version_id="version-opaque",
        retain_until=saved.object_lock_until,
    )
    restore = RestoreCapability(head, encrypted.read_bytes())
    delivery = DeliveryCapability()
    delete = DeleteCapability(key)
    try:
        yield (
            repository,
            ExportObjectService(
                repository=repository,
                restore_store=restore,
                delivery_store=delivery,
                deletion_store=delete,
                cipher=cipher,
                key_wrapper=wrapper,
                scratch_root=tmp_path / "delivery-scratch",
            ),
            restore,
            delivery,
            delete,
            plaintext,
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_release_and_download_return_only_opaque_short_lived_product_values(
    export_service_context,
) -> None:
    _, service, _, delivery, _, plaintext = export_service_context
    assert await service.release("export_opaque_reference", tenant_id="tenant-durable-alpha") == {
        "released": True
    }
    result = await service.download(
        "export_opaque_reference",
        tenant_id="tenant-durable-alpha",
        ttl_seconds=900,
    )
    assert result["url"].startswith("https://downloads.invalid/user-export-delivery/")
    assert result["expiresAt"].endswith("Z")
    assert delivery.ttls == [900]
    assert list(delivery.objects.values()) == [plaintext]
    assert "b2://" not in str(result)


@pytest.mark.asyncio
async def test_delete_uses_privileged_capability_and_marks_absence_only_after_proof(
    export_service_context,
) -> None:
    repository, service, _, _, deletion, _ = export_service_context
    assert await service.delete("export_opaque_reference", tenant_id="tenant-durable-alpha") == {
        "objectDestroyed": True
    }
    assert deletion.deleted is True
    saved = await repository.get_recovery_object("export_opaque_reference")
    assert saved is not None and saved.deleted_at is not None
    assert saved.wrapped_data_key is None and saved.key_destroyed_at is not None
    with pytest.raises(ExportObjectUnavailable):
        await service.download(
            "export_opaque_reference", tenant_id="tenant-durable-alpha", ttl_seconds=60
        )


@pytest.mark.asyncio
async def test_cross_tenant_export_reference_is_content_free_unavailable(
    export_service_context,
) -> None:
    _, service, _, _, _, _ = export_service_context
    with pytest.raises(ExportObjectUnavailable):
        await service.release("export_opaque_reference", tenant_id="tenant-other")


@pytest.mark.asyncio
async def test_privileged_expiry_sweep_deletes_due_export_and_proves_absence(
    export_service_context,
    tmp_path: Path,
) -> None:
    repository, _, restore, delivery, deletion, _ = export_service_context
    service = ExportObjectService(
        repository=repository,
        restore_store=restore,
        delivery_store=delivery,
        deletion_store=deletion,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("export-delivery-root"),
        scratch_root=tmp_path / "delivery-scratch",
        clock=lambda: datetime(2099, 1, 1, tzinfo=UTC),
    )

    assert await service.delete_expired() == 1
    assert deletion.deleted is True
    saved = await repository.get_recovery_object("export_opaque_reference")
    assert saved is not None and saved.deleted_at == datetime(2099, 1, 1, tzinfo=UTC)
    assert await service.delete_expired() == 0


@pytest.mark.asyncio
async def test_plaintext_delivery_sweep_is_paginated_and_preserves_unexpired_objects(
    export_service_context,
    tmp_path: Path,
) -> None:
    repository, _, restore, delivery, deletion, _ = export_service_context
    expired = "user-export-delivery/aa/expired.portable"
    live = "user-export-delivery/bb/live.portable"
    deletion.delivery_objects = {
        expired: ProviderObjectHead(
            expired,
            10,
            {"expires-at": "2030-01-01T00:00:00Z"},
            "expired-v1",
            None,
        ),
        live: ProviderObjectHead(
            live,
            10,
            {"expires-at": "2040-01-01T00:00:00Z"},
            "live-v1",
            None,
        ),
    }
    service = ExportObjectService(
        repository=repository,
        restore_store=restore,
        delivery_store=delivery,
        deletion_store=deletion,
        cipher=ChunkedArchiveCipher(chunk_size=16 * 1024),
        key_wrapper=AesGcmKeyWrapper.from_secret("export-delivery-root"),
        scratch_root=tmp_path / "delivery-scratch",
        clock=lambda: datetime(2035, 1, 1, tzinfo=UTC),
    )

    assert await service.delete_expired_deliveries() == 1
    assert set(deletion.delivery_objects) == {live}


@pytest.mark.asyncio
async def test_least_privilege_gc_needs_no_restore_delivery_or_wrapping_capability(
    export_service_context,
) -> None:
    repository, _, _, _, deletion, _ = export_service_context
    delivery_key = "user-export-delivery/aa/expired.portable"
    deletion.delivery_objects[delivery_key] = ProviderObjectHead(
        delivery_key,
        10,
        {"expires-at": "2030-01-01T00:00:00Z"},
        "delivery-v1",
        None,
    )
    collector = ExportGarbageCollector(
        repository=repository,
        deletion_store=deletion,
        clock=lambda: datetime(2099, 1, 1, tzinfo=UTC),
    )

    assert await collector.run_once() == {"exportsDeleted": 1, "deliveriesDeleted": 1}
    saved = await repository.get_recovery_object("export_opaque_reference")
    assert saved is not None and saved.wrapped_data_key is None
