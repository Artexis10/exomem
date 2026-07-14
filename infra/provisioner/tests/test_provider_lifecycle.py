from __future__ import annotations

import base64
import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.driver import (
    DriverFinal,
    DriverPending,
    DriverTerminal,
    EffectContext,
    LostAcknowledgement,
)
from exomem_provisioner.lifecycle import (
    CellLifecycleDriver,
    HealthObservation,
    HighFidelityProviderPlane,
    LifecycleConfig,
    MetadataConflict,
    OpaqueProviderMetadata,
    RecordedVolume,
    VolumeLifecycleWorker,
)
from exomem_provisioner.models import OperationState
from exomem_provisioner.repository import OperationRepository
from exomem_provisioner.worker import ProvisionerWorker


def _request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "operationId": "operation-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 7,
        "tenantId": "tenant-alpha",
        "cellId": "cell-alpha",
        "protocolVersion": "1",
        "releaseVersion": "0.22.0",
        "serviceCredential": _credential(),
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
        "providerRef": "cell-irrelevant-caller-ref",
    }
    value.update(overrides)
    return value


def _credential(offset: int = 0) -> str:
    raw = bytes((index + offset) % 256 for index in range(32))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _context(**overrides: object) -> EffectContext:
    value: dict[str, object] = {
        "operation_id": "database-operation-alpha",
        "provider_operation_id": "operation-alpha",
        "tenant_id": "tenant-alpha",
        "cell_id": "cell-alpha",
        "fence_generation": 7,
        "checkpoint": "effect-prepared",
        "operation_created_at": "2030-01-01T00:00:00Z",
    }
    value.update(overrides)
    return EffectContext(**value)  # type: ignore[arg-type]


def _config() -> LifecycleConfig:
    return LifecycleConfig(
        image="registry.invalid/exomem@sha256:" + "a" * 64,
        chart_path="/opt/exomem/charts/cell",
        chart_version="0.1.0",
        helm_version="3.19.4",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        browser_origin="https://substratesystems.io",
        release_version="0.22.0",
        protocol_version="1",
        operator_contract_digest="c" * 64,
        contract_digest="b" * 64,
        location="fsn1",
    )


@pytest.mark.asyncio
async def test_release_unit_mismatch_is_terminal_before_any_provider_effect() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    driver = CellLifecycleDriver(
        plane=plane,
        volume_worker=VolumeLifecycleWorker(plane, plane),
        config=_config(),
    )

    for override in ({"releaseVersion": "0.22.1"}, {"protocolVersion": "2"}):
        with pytest.raises(DriverTerminal, match="PROVISIONER_RELEASE_UNIT_MISMATCH"):
            await driver.execute("provision", _request(**override), _context())
        assert plane._cells == {}
        assert plane._tenant_fences == {}


def _metadata(**overrides: object) -> OpaqueProviderMetadata:
    values: dict[str, object] = {
        "tenant_id": "tenant-alpha",
        "subject_id": "cell-alpha",
        "operation_id": "operation-alpha",
        "fence_generation": 7,
    }
    values.update(overrides)
    return OpaqueProviderMetadata(**values)  # type: ignore[arg-type]


async def _run_action(
    driver: CellLifecycleDriver,
    action: str,
    request: dict[str, object],
    context: EffectContext,
) -> tuple[DriverFinal, list[str]]:
    checkpoints: list[str] = []
    for _ in range(16):
        outcome = await driver.execute(action, request, context)
        if isinstance(outcome, DriverFinal):
            return outcome, checkpoints
        checkpoints.append(outcome.checkpoint)
        context = replace(context, checkpoint=outcome.checkpoint)
    raise AssertionError(f"{action} did not converge")


@pytest.mark.asyncio
async def test_provision_worker_restarts_across_checkpoints_and_encrypts_provider_refs(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lifecycle.sqlite"
    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{database_path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
        claim_seconds=10,
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=10,
    )
    plane = HighFidelityProviderPlane(location="fsn1")
    plane.lose_acknowledgement_after_csi_bind_once()
    driver = CellLifecycleDriver(
        plane=plane,
        volume_worker=VolumeLifecycleWorker(plane, plane),
        config=_config(),
    )
    operation = await repository.submit("provision", "durable-provision", _request())
    now = datetime(2030, 1, 1, tzinfo=UTC)

    for attempt in range(10):
        worker = ProvisionerWorker(repository, driver, worker_id=f"worker-{attempt}")
        await worker.run_once(now=now + timedelta(seconds=attempt * 3))
        current = await repository.get_by_id(operation.id)
        assert current is not None
        if current.state is OperationState.FINAL:
            break

    final = await repository.get_by_id(operation.id)
    assert final is not None and final.state is OperationState.FINAL
    assert await repository.load_result(operation.id) == {
        "providerRef": plane.provider_reference(_metadata()),
        "privateEndpoint": "https://control.example.invalid/cells/cell-alpha",
    }
    resources = await repository.list_resources(tenant_id="tenant-alpha", cell_id="cell-alpha")
    assert {resource.kind.value for resource in resources} == {
        "kubernetes-namespace",
        "helm-release",
        "pvc",
        "volume",
        "route",
    }
    await database.dispose()

    with sqlite3.connect(database_path) as connection:
        ciphertext = " ".join(
            row[0] for row in connection.execute("SELECT reference_ciphertext FROM resources")
        )
    assert "volume-" not in ciphertext
    assert "cell-alpha" not in ciphertext


def test_metadata_and_resource_names_are_deterministic_opaque_and_immutable() -> None:
    first = _metadata()
    replay = _metadata()

    assert first.resource_name == replay.resource_name
    assert first.resource_name.startswith("exo-")
    assert first.hcloud_labels == replay.hcloud_labels
    rendered = repr(first.hcloud_labels)
    assert "tenant-alpha" not in rendered
    assert "cell-alpha" not in rendered
    assert "operation-alpha" not in rendered
    assert first.hcloud_labels["exomem_fence"] == "7"
    assert OpaqueProviderMetadata.from_hcloud_labels(first.hcloud_labels) == first
    assert OpaqueProviderMetadata.from_kubernetes_annotations(first.kubernetes_annotations) == first

    with pytest.raises(MetadataConflict):
        first.require_same(_metadata(operation_id="operation-other"))


@pytest.mark.asyncio
async def test_volume_registration_rebind_and_absence_proof_use_original_handle() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    metadata = _metadata()
    plane.seed_bound_volume(metadata, handle="volume-42", pv_name="pv-42")
    worker = VolumeLifecycleWorker(plane, plane)

    recorded = await worker.register_bound_volume(metadata)

    assert recorded == RecordedVolume(
        volume_handle="volume-42",
        pv_name="pv-42",
        location="fsn1",
        metadata=metadata,
    )
    assert plane.volume_labels("volume-42") == metadata.hcloud_labels

    plane.lose_kubernetes_state()
    await worker.rebind_static(recorded, metadata, location="fsn1")
    assert plane.bound_handle(metadata) == "volume-42"

    with pytest.raises(MetadataConflict):
        await worker.rebind_static(recorded, metadata, location="nbg1")
    with pytest.raises(MetadataConflict):
        await worker.rebind_static(recorded, _metadata(subject_id="cell-other"), location="fsn1")

    proof = await worker.destroy_retained(recorded)
    assert proof.kubernetes_pv_absent is True
    assert proof.hcloud_volume_absent is True


@pytest.mark.asyncio
async def test_retained_destroy_waits_for_both_asynchronous_provider_absence_proofs() -> None:
    class DelayedAbsencePlane(HighFidelityProviderPlane):
        def __init__(self) -> None:
            super().__init__(location="fsn1")
            self.pv_checks = 0
            self.volume_checks = 0

        async def pv_absent(self, pv_name: str) -> bool:
            self.pv_checks += 1
            return self.pv_checks >= 3 and await super().pv_absent(pv_name)

        async def volume_absent(self, handle: str) -> bool:
            self.volume_checks += 1
            return self.volume_checks >= 2 and await super().volume_absent(handle)

    async def no_sleep(_seconds: float) -> None:
        return None

    plane = DelayedAbsencePlane()
    metadata = _metadata()
    plane.seed_bound_volume(metadata, handle="volume-delayed", pv_name="pv-delayed")
    recorded = await VolumeLifecycleWorker(plane, plane).register_bound_volume(metadata)
    worker = VolumeLifecycleWorker(
        plane,
        plane,
        absence_attempts=3,
        absence_interval_seconds=0,
        sleep=no_sleep,
    )

    proof = await worker.destroy_retained(recorded)

    assert proof.kubernetes_pv_absent is True
    assert proof.hcloud_volume_absent is True
    assert plane.pv_checks == 3
    assert plane.volume_checks == 2


@pytest.mark.asyncio
async def test_orphan_discovery_quarantines_unknown_provider_volume() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    registered = _metadata()
    orphan = _metadata(subject_id="candidate-orphan", operation_id="restore-orphan")
    plane.seed_provider_volume(registered, handle="volume-known")
    plane.seed_provider_volume(orphan, handle="volume-orphan")
    worker = VolumeLifecycleWorker(plane, plane)

    quarantined = await worker.quarantine_orphans(
        tenant_id="tenant-alpha",
        registered_handles={"volume-known"},
    )

    assert quarantined == ("volume-orphan",)
    assert plane.is_quarantined("volume-orphan") is True
    assert plane.volume_labels("volume-orphan") | orphan.hcloud_labels == plane.volume_labels(
        "volume-orphan"
    )


@pytest.mark.asyncio
async def test_provision_adopts_partial_attempt_and_waits_for_volume_health_and_route() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    plane.lose_acknowledgement_after_csi_bind_once()
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=VolumeLifecycleWorker(plane, plane), config=_config()
    )
    request = _request()
    context = _context()

    checkpoints: list[str] = []
    final: DriverFinal | None = None
    for _ in range(12):
        try:
            outcome = await driver.execute("provision", request, context)
        except LostAcknowledgement:
            continue
        if isinstance(outcome, DriverPending):
            checkpoints.append(outcome.checkpoint)
            context = _context(checkpoint=outcome.checkpoint)
            continue
        final = outcome
        break

    assert final is not None
    assert checkpoints == [
        "namespace-ready",
        "release-applied",
        "volume-owned",
        "initialized",
        "runtime-admitted",
        "routes-open",
    ]
    assert final.result == {
        "providerRef": plane.provider_reference(_metadata()),
        "privateEndpoint": "https://control.example.invalid/cells/cell-alpha",
    }
    assert plane.count_volumes(_metadata()) == 1
    assert plane.helm_values(_metadata()) == {
        "activeCredentialVersion": "1",
        "browserOrigin": "https://substratesystems.io",
        "cellId": "cell-alpha",
        "credentialsSecretName": "exomem-cell-credentials",
        "credentialsManagedExternally": True,
        "expectedProtocol": "1",
        "expectedRelease": "0.22.0",
        "featureGrants": "",
        "image": _config().image,
        "initOperationId": "operation-alpha",
        "initRequestId": "5ec7a442-663f-476b-80f8-dd803afe5590",
        "pvcSize": "10Gi",
        "providerIdentity": {
            "tenantId": "tenant-alpha",
            "cellId": "cell-alpha",
            "operationId": "operation-alpha",
            "fence": "7",
            "operationDigest": hashlib.sha256(b"operation-alpha").hexdigest(),
            "subjectDigest": hashlib.sha256(b"cell-alpha").hexdigest(),
            "tenantDigest": hashlib.sha256(b"tenant-alpha").hexdigest(),
        },
        "resourceName": _metadata().resource_name,
        "routes": {"controlHostname": "control.example.invalid", "enabled": False},
        "runtimeGid": 10001,
        "runtimeUid": 10001,
        "storageClassName": "exomem-hcloud-encrypted-retain",
        "storageLimitBytes": 5 * 1024**3,
        "transferHostname": "transfer.example.invalid",
        "uploadLimitBytes": 90 * 1024**2,
        "vaultId": "cell-alpha",
        "workerLimit": 0,
        "workerPolicyDigest": hashlib.sha256(
            b'{"media":false,"semantic":false,"workerCount":0}'
        ).hexdigest(),
        "workloadMode": "serve",
    }
    assert _credential() not in repr(plane)


def test_provider_metadata_long_opaque_ids_round_trip_without_recovery_registry() -> None:
    metadata = OpaqueProviderMetadata(
        tenant_id="t/" + "a" * 254,
        subject_id="c:" + "b" * 254,
        operation_id="o_" + "c" * 254,
        fence_generation=9_007_199_254_740_991,
    )
    labels = metadata.hcloud_labels

    assert labels["exomem_tenant_id_n"] == "8"
    assert all(
        len(value) <= 52
        for key, value in labels.items()
        if key.startswith("exomem_") and "_id_" in key and not key.endswith("_n")
    )
    assert OpaqueProviderMetadata.from_hcloud_labels(labels) == metadata
    assert len(labels) <= 31

    malformed = dict(labels)
    malformed.pop("exomem_tenant_id_7")
    with pytest.raises(MetadataConflict):
        OpaqueProviderMetadata.from_hcloud_labels(malformed)

    mismatched = dict(labels)
    mismatched["exomem_tenant"] = "0" * 24
    with pytest.raises(MetadataConflict):
        OpaqueProviderMetadata.from_hcloud_labels(mismatched)


@pytest.mark.asyncio
async def test_provision_capacity_block_stays_pending_without_allocating_namespace() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    plane.block_capacity("active-user-cell-capacity-exhausted")
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=VolumeLifecycleWorker(plane, plane), config=_config()
    )

    blocked = await driver.execute("provision", _request(), _context())

    assert blocked == DriverPending("capacity-active-user-cell-capacity-exhausted", 300)
    assert plane.has_namespace(_metadata()) is False


@pytest.mark.asyncio
async def test_health_flattens_exact_runtime_contract_and_fails_closed_on_drift() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=VolumeLifecycleWorker(plane, plane), config=_config()
    )
    await plane.seed_ready_cell(_metadata(), _request(), _config())

    healthy = await driver.execute("health", _request(), _context())
    assert isinstance(healthy, DriverFinal)
    assert healthy.result == {
        "live": True,
        "ready": True,
        "cellId": "cell-alpha",
        "protocolVersion": "1",
        "releaseVersion": "0.22.0",
        "serviceAuthenticated": True,
        "mutationAuthority": True,
        "readAdmission": True,
        "writeAdmission": True,
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
        "code": "CELL_READY",
    }

    plane.set_health(
        _metadata(),
        HealthObservation.ready_for(_metadata(), _request(), _config()).replace(
            admission_admitted=False
        ),
    )
    with pytest.raises(Exception) as rejected:
        await driver.execute("health", _request(), _context())
    assert getattr(rejected.value, "code", None) == "PROVISIONER_RUNTIME_CONTRACT_MISMATCH"


@pytest.mark.asyncio
async def test_maintenance_gate_closes_both_routes_drains_and_serializes_actions() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    await plane.seed_ready_cell(_metadata(), _request(), _config())
    plane.seed_unused_transfer_ticket(_metadata(), "ticket-unused")
    plane.set_in_flight(_metadata(), 2)
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=VolumeLifecycleWorker(plane, plane), config=_config()
    )

    assert await plane.acquire_maintenance(_metadata(), "backup-in-flight") is True
    waiting = await driver.execute(
        "stop",
        _request(operationId="operation-other", fenceGeneration=8),
        _context(
            operation_id="database-operation-other",
            provider_operation_id="operation-other",
            fence_generation=8,
        ),
    )
    assert isinstance(waiting, DriverPending)
    assert waiting.checkpoint == "maintenance-wait"
    await plane.release_maintenance(_metadata(), "backup-in-flight")

    quiesced, checkpoints = await _run_action(
        driver,
        "quiesce",
        _request(operationId="quiesce-after-wait", fenceGeneration=9),
        _context(
            operation_id="database-quiesce-after-wait",
            provider_operation_id="quiesce-after-wait",
            fence_generation=9,
        ),
    )
    assert isinstance(quiesced, DriverFinal)
    assert checkpoints == ["maintenance-acquired", "routes-closed", "runtime-drained"]
    assert plane.routes_enabled(_metadata()) == (False, False)
    assert plane.external_rejection_proved(_metadata()) == (True, True)
    assert plane.ticket_reached_cell("ticket-unused") is False
    assert plane.in_flight(_metadata()) == 0


@pytest.mark.asyncio
async def test_maintenance_ticket_proof_fails_if_the_transfer_route_is_still_open() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    await plane.seed_ready_cell(_metadata(), _request(), _config())
    plane.seed_unused_transfer_ticket(_metadata(), "open-route-ticket")

    assert await plane.prove_external_rejection(_metadata(), _request()) is False
    assert plane.ticket_reached_cell("open-route-ticket") is True


@pytest.mark.asyncio
async def test_stop_resume_rotation_and_seal_preserve_ordering() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    await plane.seed_ready_cell(_metadata(), _request(), _config())
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=VolumeLifecycleWorker(plane, plane), config=_config()
    )

    stopped, stop_checkpoints = await _run_action(
        driver,
        "stop",
        _request(operationId="stop-alpha", fenceGeneration=8),
        _context(
            operation_id="database-stop-alpha",
            provider_operation_id="stop-alpha",
            fence_generation=8,
        ),
    )
    assert isinstance(stopped, DriverFinal)
    assert stop_checkpoints == [
        "maintenance-acquired",
        "routes-closed",
        "runtime-drained",
        "compute-stopped",
    ]
    assert plane.replicas(_metadata()) == 0
    assert plane.bound_handle(_metadata()) is not None

    resumed, resume_checkpoints = await _run_action(
        driver,
        "resume",
        _request(operationId="resume-alpha", fenceGeneration=9),
        _context(
            operation_id="database-resume-alpha",
            provider_operation_id="resume-alpha",
            fence_generation=9,
        ),
    )
    assert isinstance(resumed, DriverFinal)
    assert resume_checkpoints == [
        "maintenance-acquired",
        "compute-started",
        "runtime-resumed",
        "runtime-admitted",
        "routes-open",
    ]
    assert plane.replicas(_metadata()) == 1
    assert plane.routes_enabled(_metadata()) == (True, True)

    staged, stage_checkpoints = await _run_action(
        driver,
        "rotate-credential",
        _request(
            operationId="rotate-stage-alpha",
            fenceGeneration=10,
            phase="stage",
            credentialVersion=2,
            nextCredential=_credential(32),
        ),
        _context(
            operation_id="database-rotate-stage-alpha",
            provider_operation_id="rotate-stage-alpha",
            fence_generation=10,
        ),
    )
    assert isinstance(staged, DriverFinal)
    assert stage_checkpoints == [
        "maintenance-acquired",
        "credential-staged",
        "credential-proved",
    ]
    assert staged.result == {"previousCredentialRejected": False}
    assert plane.accepted_credential_versions(_metadata()) == (1, 2)

    finalized, finalize_checkpoints = await _run_action(
        driver,
        "rotate-credential",
        _request(
            operationId="rotate-finalize-alpha",
            fenceGeneration=11,
            phase="finalize",
            credentialVersion=2,
            nextCredential=_credential(32),
        ),
        _context(
            operation_id="database-rotate-finalize-alpha",
            provider_operation_id="rotate-finalize-alpha",
            fence_generation=11,
        ),
    )
    assert isinstance(finalized, DriverFinal)
    assert finalize_checkpoints == [
        "maintenance-acquired",
        "credential-staged",
        "credential-proved",
        "credential-promoted",
    ]
    assert finalized.result == {"previousCredentialRejected": True}
    assert plane.accepted_credential_versions(_metadata()) == (2,)

    sealed, seal_checkpoints = await _run_action(
        driver,
        "seal",
        _request(operationId="seal-alpha", fenceGeneration=12),
        _context(
            operation_id="database-seal-alpha",
            provider_operation_id="seal-alpha",
            fence_generation=12,
        ),
    )
    assert isinstance(sealed, DriverFinal)
    assert seal_checkpoints == [
        "maintenance-acquired",
        "routes-closed",
        "runtime-drained",
        "runtime-sealed",
    ]
    assert plane.is_sealed(_metadata()) is True
    assert plane.seal_created_at(_metadata()) == "2030-01-01T00:00:00Z"
    assert plane.routes_enabled(_metadata()) == (False, False)


@pytest.mark.asyncio
async def test_discard_removes_only_candidate_and_preserves_active_cell_and_exports() -> None:
    plane = HighFidelityProviderPlane(location="fsn1")
    active = _metadata()
    candidate = _metadata(subject_id="candidate-failed", operation_id="restore-failed")
    await plane.seed_ready_cell(active, _request(), _config())
    await plane.seed_ready_cell(
        candidate,
        _request(cellId="candidate-failed", operationId="restore-failed"),
        _config(),
        candidate=True,
        failed=True,
    )
    plane.seed_export(active, "export-active")
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=VolumeLifecycleWorker(plane, plane), config=_config()
    )

    discarded, checkpoints = await _run_action(
        driver,
        "discard",
        _request(cellId="candidate-failed", operationId="restore-failed"),
        _context(
            cell_id="candidate-failed",
            provider_operation_id="restore-failed",
        ),
    )

    assert isinstance(discarded, DriverFinal)
    assert checkpoints == ["candidate-destroyed"]
    assert discarded.result == {
        "computeDestroyed": True,
        "storageDestroyed": True,
        "keysDestroyed": True,
    }
    assert plane.cell_exists(active) is True
    assert plane.cell_exists(candidate) is False
    assert plane.export_exists("export-active") is True

    with pytest.raises(Exception) as active_rejected:
        await driver.execute("discard", _request(), _context())
    assert getattr(active_rejected.value, "code", None) == (
        "PROVISIONER_PROVIDER_METADATA_CONFLICT"
    )


@pytest.mark.asyncio
async def test_destroy_revokes_online_resources_waits_without_attempts_then_proves_absence() -> (
    None
):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    plane = HighFidelityProviderPlane(location="fsn1", now=now)
    active = _metadata()
    orphan = _metadata(subject_id="candidate-orphan", operation_id="restore-orphan")
    await plane.seed_ready_cell(active, _request(), _config())
    await plane.seed_ready_cell(
        orphan,
        _request(cellId="candidate-orphan", operationId="restore-orphan"),
        _config(),
        candidate=True,
        failed=True,
    )
    plane.seed_provider_volume(orphan, handle="volume-orphan")
    plane.seed_orphan_route(orphan, "route-orphan")
    plane.seed_orphan_credential(orphan, "credential-orphan")
    plane.seed_export(active, "export-final")
    plane.seed_backup(active, "backup-locked", locked_until=now + timedelta(days=7))
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=VolumeLifecycleWorker(plane, plane), config=_config()
    )

    pending = await driver.execute(
        "destroy",
        {
            "operationId": "destroy-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 8,
            "tenantId": "tenant-alpha",
        },
        _context(
            operation_id="database-destroy-alpha",
            provider_operation_id="destroy-alpha",
            cell_id=None,
            fence_generation=8,
        ),
    )
    assert isinstance(pending, DriverPending)
    assert pending.checkpoint == "online-destroyed"
    assert plane.online_resources_absent("tenant-alpha") is True
    assert plane.online_access_revoked("tenant-alpha") is True
    assert plane.backup_exists("backup-locked") is True

    retained = await driver.execute(
        "destroy",
        {
            "operationId": "destroy-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 8,
            "tenantId": "tenant-alpha",
        },
        _context(
            operation_id="database-destroy-alpha",
            provider_operation_id="destroy-alpha",
            cell_id=None,
            fence_generation=8,
            checkpoint="online-destroyed",
        ),
    )
    assert isinstance(retained, DriverPending)
    assert retained.checkpoint == "retained-wait"

    plane.advance(timedelta(days=7, seconds=1))
    retention_destroyed = await driver.execute(
        "destroy",
        {
            "operationId": "destroy-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 8,
            "tenantId": "tenant-alpha",
        },
        _context(
            operation_id="database-destroy-alpha",
            provider_operation_id="destroy-alpha",
            cell_id=None,
            fence_generation=8,
            checkpoint="retained-wait",
        ),
    )
    assert isinstance(retention_destroyed, DriverPending)
    assert retention_destroyed.checkpoint == "retention-destroyed"
    assert plane.backup_exists("backup-locked") is False
    assert plane.export_exists("export-final") is False

    final = await driver.execute(
        "destroy",
        {
            "operationId": "destroy-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 8,
            "tenantId": "tenant-alpha",
        },
        _context(
            operation_id="database-destroy-alpha",
            provider_operation_id="destroy-alpha",
            cell_id=None,
            fence_generation=8,
            checkpoint="retention-destroyed",
        ),
    )
    assert isinstance(final, DriverFinal)
    assert final.result == {
        "computeDestroyed": True,
        "storageDestroyed": True,
        "keysDestroyed": True,
        "tenantResourcesDestroyed": True,
    }
    assert plane.tenant_absent("tenant-alpha") is True
