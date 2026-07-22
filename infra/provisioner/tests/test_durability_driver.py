from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.driver import (
    DriverFinal,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
    FakeDriver,
)
from exomem_provisioner.durability import ExportBackupResult
from exomem_provisioner.durability_crypto import ArchiveAuthenticationError
from exomem_provisioner.durability_driver import DurabilityActionDriver
from exomem_provisioner.durability_repository import DurabilityRepository
from exomem_provisioner.models import DurabilityRunState


class ExportWorkflow:
    def __init__(self, repository: DurabilityRepository) -> None:
        self.repository = repository
        self.calls = 0
        self.delay = 0.0
        self.release: asyncio.Event | None = None
        self.error: Exception | None = None
        self.last_run_id = ""

    async def run(self, run, *, worker_id, expires_at=None):
        assert expires_at is not None
        self.last_run_id = run.id
        self.calls += 1
        if self.release is None:
            await asyncio.sleep(self.delay)
        else:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        result = ExportBackupResult(
            opaque_reference="export_opaque_alpha",
            release_reference=f"release_{run.id.replace('-', '')}",
            archive_sha256="a" * 64,
            manifest_sha256="b" * 64,
            archive_size=1234,
            encryption_scheme="envelope-aes-256-gcm",
            integrity_verified=True,
            quiescence_seconds=1.5,
        )
        await self.repository.complete(
            run.id,
            worker_id,
            claim_token=run.claim_token,
            claim_generation=run.claim_generation,
            result={
                "opaque_reference": result.opaque_reference,
                "release_reference": result.release_reference,
                "archive_sha256": result.archive_sha256,
                "manifest_sha256": result.manifest_sha256,
                "archive_size": result.archive_size,
                "encryption_scheme": result.encryption_scheme,
                "integrity_verified": result.integrity_verified,
            },
        )
        return result


class RestoreWorkflow:
    def __init__(self, repository: DurabilityRepository) -> None:
        self.repository = repository
        self.arguments = None

    async def run(self, run, **arguments):
        self.arguments = arguments
        await self.repository.complete(
            run.id,
            arguments["worker_id"],
            claim_token=run.claim_token,
            claim_generation=run.claim_generation,
            result={"restored": True},
        )
        return {"restored": True}


class ObjectService:
    async def release(self, reference, *, tenant_id):
        assert reference == "release_opaque_alpha" and tenant_id == "tenant-alpha"
        return {"released": True}

    async def download(self, reference, *, tenant_id, ttl_seconds):
        assert reference == "export_opaque_alpha" and tenant_id == "tenant-alpha"
        assert ttl_seconds == 900
        return {"url": "https://downloads.invalid/opaque", "expiresAt": "2030-01-01T00:15:00Z"}

    async def delete(self, reference, *, tenant_id):
        assert reference == "export_opaque_alpha" and tenant_id == "tenant-alpha"
        return {"objectDestroyed": True}


def _request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "operationId": "external-operation-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 9,
        "tenantId": "tenant-alpha",
        "cellId": "cell-alpha",
        "protocolVersion": "exomem-hosted.v1",
        "releaseVersion": "0.22.0",
        "serviceCredential": "service-credential-sentinel",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
        "providerRef": "provider-cell-alpha",
    }
    value.update(overrides)
    return value


def _export_request(**overrides: object) -> dict[str, object]:
    return _request(
        expiresAt=(datetime.now(UTC) + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        **overrides,
    )


def _context() -> EffectContext:
    return EffectContext(
        operation_id="database-operation-uuid",
        provider_operation_id="external-operation-alpha",
        tenant_id="tenant-alpha",
        cell_id="cell-alpha",
        fence_generation=9,
    )


@pytest.fixture
async def driver_context(tmp_path: Path):
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'driver.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = DurabilityRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        lease_seconds=3,
    )
    export = ExportWorkflow(repository)
    restore = RestoreWorkflow(repository)
    driver = DurabilityActionDriver(
        delegate=FakeDriver(),
        repository=repository,
        export_workflow=export,
        restore_workflow=restore,
        object_service=ObjectService(),
    )
    try:
        yield driver, export, restore
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_export_action_returns_exact_final_contract_from_real_durability_run(
    driver_context,
) -> None:
    driver, export, _ = driver_context
    result = await driver.execute("export", _export_request(), _context())
    assert isinstance(result, DriverFinal)
    assert result.result == {
        "exportRef": "export_opaque_alpha",
        "releaseRef": result.result["releaseRef"],
        "archiveSha256": "a" * 64,
        "manifestSha256": "b" * 64,
        "archiveSize": 1234,
        "encryptionScheme": "envelope-aes-256-gcm",
        "integrityVerified": True,
    }
    assert result.result["releaseRef"].startswith("release_")
    assert export.calls == 1


@pytest.mark.asyncio
async def test_completed_export_replays_encrypted_result_without_a_second_effect(
    driver_context,
) -> None:
    driver, export, _ = driver_context
    first = await driver.execute("export", _export_request(), _context())
    replay = await driver.execute("export", _export_request(), _context())

    assert replay == first
    assert export.calls == 1


@pytest.mark.asyncio
async def test_driver_renews_durability_claim_during_long_workflow(
    driver_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, export, _ = driver_context
    renewed_repeatedly = asyncio.Event()
    release = asyncio.Event()
    real_renew_claim = export.repository.renew_claim
    renewal_count = 0

    async def observed_renew_claim(*args, **kwargs):
        nonlocal renewal_count
        snapshot = await real_renew_claim(*args, **kwargs)
        renewal_count += 1
        if renewal_count >= 4:
            renewed_repeatedly.set()
        return snapshot

    monkeypatch.setattr(export.repository, "renew_claim", observed_renew_claim)
    export.release = release

    running = asyncio.create_task(driver.execute("export", _export_request(), _context()))
    try:
        # Four one-second heartbeat intervals carry the workflow beyond its
        # original three-second lease and prove renewal remains continuous.
        await asyncio.wait_for(renewed_repeatedly.wait(), timeout=15)
        release.set()
        result = await asyncio.wait_for(running, timeout=10)
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    assert isinstance(result, DriverFinal)
    assert export.calls == 1


@pytest.mark.asyncio
async def test_authenticated_archive_failure_maps_to_content_free_terminal_code(
    driver_context,
) -> None:
    driver, export, _ = driver_context
    export.error = ArchiveAuthenticationError("secret provider detail")

    with pytest.raises(DriverTerminal) as raised:
        await driver.execute("export", _export_request(), _context())

    assert raised.value.code == "PROVISIONER_DURABILITY_VERIFICATION_FAILED"
    assert "secret provider detail" not in str(raised.value)
    failed = await export.repository.get(export.last_run_id)
    assert failed is not None and failed.status is DurabilityRunState.ERROR


@pytest.mark.asyncio
async def test_transient_durability_io_maps_to_retryable_worker_outcome(driver_context) -> None:
    driver, export, _ = driver_context
    export.error = TimeoutError("secret provider timeout")

    with pytest.raises(DriverRetryable):
        await driver.execute("export", _export_request(), _context())

    released = await export.repository.get(export.last_run_id)
    assert released is not None and released.status is DurabilityRunState.PENDING


@pytest.mark.asyncio
async def test_restore_action_binds_every_caller_proof_to_provider_object(driver_context) -> None:
    driver, _, restore = driver_context
    result = await driver.execute(
        "restore",
        _request(
            restoreRef="recovery_opaque_source",
            sourceCellId="cell-source-alpha",
            archiveSha256="a" * 64,
            manifestSha256="b" * 64,
            archiveSize=1234,
        ),
        _context(),
    )
    assert isinstance(result, DriverFinal) and result.result == {}
    assert restore.arguments == {
        "worker_id": "durability-database-operation-uuid",
        "source_reference": "recovery_opaque_source",
        "expected_source_cell_id": "cell-source-alpha",
        "expected_archive_sha256": "a" * 64,
        "expected_manifest_sha256": "b" * 64,
        "expected_archive_size": 1234,
    }


@pytest.mark.asyncio
async def test_export_reference_actions_use_opaque_service_and_never_delegate(
    driver_context,
) -> None:
    driver, _, _ = driver_context
    released = await driver.execute(
        "export-release", _request(releaseRef="release_opaque_alpha"), _context()
    )
    downloaded = await driver.execute(
        "export-download",
        {
            "operationId": "external-operation-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 9,
            "tenantId": "tenant-alpha",
            "exportRef": "export_opaque_alpha",
        },
        _context(),
    )
    deleted = await driver.execute(
        "export-delete",
        {
            "operationId": "external-operation-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 9,
            "tenantId": "tenant-alpha",
            "exportRef": "export_opaque_alpha",
        },
        _context(),
    )
    assert isinstance(released, DriverFinal) and released.result == {}
    assert isinstance(downloaded, DriverFinal) and downloaded.result["url"].startswith("https://")
    assert isinstance(deleted, DriverFinal) and deleted.result == {"objectDestroyed": True}
