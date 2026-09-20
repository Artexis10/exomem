"""Strict production construction and worker entrypoint."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import uvicorn

from .activation_ack_api import ActivationAckService, create_activation_ack_app
from .adapters import (
    HelmCliAdapter,
    KubernetesCellAdapter,
    KubernetesMaintenanceLeaseAdapter,
    KubernetesVaultFingerprintAdapter,
    PrivateCellApiAdapter,
    TraefikRoutingAdapter,
)
from .capacity import LiveCapacityAdmission, load_capacity_contract
from .config import (
    DeploymentLock,
    ProviderWorkerSettings,
    ProvisionerSettings,
)
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .durability_driver import DurabilityActionDriver
from .entrypoint import help_requested
from .governance_migration_coordinator import HostedGovernanceMigrationCoordinator
from .governance_migration_job import KubernetesGovernanceMigrationAdapter
from .governance_storage_binding import KubernetesGovernanceStorageBindingAdapter
from .governance_storage_init import KubernetesGovernanceStorageInitAdapter
from .lifecycle import CellLifecycleDriver, LifecycleConfig
from .live import (
    KubernetesProviderRegistry,
    LiveLifecyclePlane,
)
from .logging import configure_content_free_logging
from .main import _require_production_database
from .provider_identity import ProviderRecoveryIdentityVerifier
from .repository import OperationRepository
from .worker import ProvisionerWorker
from .worker_loop import run_polling_loop
from .worker_ownership import ROUTINE_OPERATION_ACTIONS


@dataclass(frozen=True, slots=True)
class LiveProviderComponents:
    lock: DeploymentLock
    plane: LiveLifecyclePlane
    driver: CellLifecycleDriver
    capacity: LiveCapacityAdmission
    cell: KubernetesCellAdapter
    runtime: PrivateCellApiAdapter


class CapacityVerifierSettings(Protocol):
    capacity_receipt_public_key: str
    capacity_contract_path: str
    capacity_receipt_namespace: str
    capacity_receipt_config_map: str
    hcloud_server_id: int
    location: str


def build_live_capacity_admission(
    *,
    repository: OperationRepository,
    settings: CapacityVerifierSettings,
    core_v1: Any,
    storage_v1: Any,
) -> LiveCapacityAdmission:
    """Build the shared public-only capacity authority for PROVISION workers."""

    return LiveCapacityAdmission(
        core_v1=core_v1,
        storage_v1=storage_v1,
        sessions=repository.session_factory,
        contract=load_capacity_contract(settings.capacity_contract_path),
        public_key=settings.capacity_receipt_public_key,
        receipt_namespace=settings.capacity_receipt_namespace,
        receipt_config_map=settings.capacity_receipt_config_map,
        expected_server_id=settings.hcloud_server_id,
        expected_location=settings.location,
    )


def build_routine_operation_worker(
    *,
    repository: OperationRepository,
    driver: Any,
    worker_id: str,
    capacity_admission: Any,
) -> ProvisionerWorker:
    """Build the routine owner for every non-destructive operation action."""

    return ProvisionerWorker(
        repository,
        driver,
        worker_id=worker_id,
        exclude_checkpoints=frozenset({"volume-registration-required"}),
        exclude_checkpoint_prefixes=frozenset({"gpi1:registering:"}),
        allowed_actions=ROUTINE_OPERATION_ACTIONS,
        capacity_admission=capacity_admission,
    )


def build_live_routine_action_driver(
    *,
    lifecycle_driver: Any,
    durability_repository: Any,
    export_workflow: Any,
    restore_workflow: Any,
    object_service: Any,
) -> DurabilityActionDriver:
    """Compose the live lifecycle and durability action owners without deletion authority."""

    return DurabilityActionDriver(
        delegate=lifecycle_driver,
        repository=durability_repository,
        export_workflow=export_workflow,
        restore_workflow=restore_workflow,
        object_service=object_service,
        deletion_workflow=None,
        runtime_target_validator=getattr(lifecycle_driver, "runtime_target_matches", None),
    )


def build_live_provider_components(
    *,
    repository: OperationRepository,
    settings: ProviderWorkerSettings,
    core_v1: Any,
    apps_v1: Any,
    batch_v1: Any,
    coordination_v1: Any,
    storage_v1: Any,
    custom_objects: Any,
    requester: Any,
    external_probe: Any,
    stream_request: Any | None = None,
) -> LiveProviderComponents:
    """Build only real adapters; production has no emulator selection flag."""

    lock = settings.deployment_lock
    selected = lock.selected_runtime(settings.runtime_selection)
    target = selected.runtimeTarget
    identity_verifier = ProviderRecoveryIdentityVerifier.from_public_key(
        settings.provider_recovery_public_key
    )
    lifecycle_config = LifecycleConfig(
        image=selected.image,
        chart_path=settings.cell_chart_path,
        chart_version=settings.cell_chart_version,
        helm_version=settings.helm_version,
        control_hostname=settings.control_hostname,
        transfer_hostname=settings.transfer_hostname,
        browser_origin=settings.browser_origin,
        release_version=target.releaseVersion,
        protocol_version=target.protocolVersion,
        contract_digest=target.gatewayContractDigest,
        location=settings.location,
        runtime_target=target.model_dump(mode="json"),
        legacy_runtime_units={
            (unit.releaseVersion, unit.protocolVersion): unit.contract.model_dump(mode="json")
            for unit in lock.composition.legacyCatalog
        },
        records_reader_version=selected.recordsReaderVersion,
        lifecycle_actions_enabled=selected.lifecycleActionsEnabled,
        compatibility_digest=selected.compatibilityDigest,
        migration_mode=selected.migrationMode,
    )
    cell = KubernetesCellAdapter(
        core_v1=core_v1,
        apps_v1=apps_v1,
        identity_verifier=identity_verifier,
    )
    capacity = build_live_capacity_admission(
        repository=repository,
        settings=settings,
        core_v1=core_v1,
        storage_v1=storage_v1,
    )
    runtime = PrivateCellApiAdapter(
        request=requester,
        internal_origin=settings.internal_origin,
        stream_request=stream_request,
    )
    plane = LiveLifecyclePlane(
        repository=repository,
        registry=KubernetesProviderRegistry(
            core_v1=core_v1,
            apps_v1=apps_v1,
            batch_v1=batch_v1,
            custom_objects=custom_objects,
            identity_verifier=identity_verifier,
        ),
        cell=cell,
        helm=HelmCliAdapter(
            binary=settings.helm_binary,
            expected_version=settings.helm_version,
            chart_path=settings.cell_chart_path,
            chart_version=settings.cell_chart_version,
            core_v1=core_v1,
        ),
        runtime=runtime,
        routes=TraefikRoutingAdapter(
            custom_objects=custom_objects,
            control_hostname=settings.control_hostname,
            transfer_hostname=settings.transfer_hostname,
            probe=external_probe,
        ),
        maintenance=KubernetesMaintenanceLeaseAdapter(
            coordination_v1=coordination_v1,
        ),
        capacity=capacity,
        identity_verifier=identity_verifier,
        config=lifecycle_config,
        storage_init=KubernetesGovernanceStorageInitAdapter(
            core_v1=core_v1,
            batch_v1=batch_v1,
            apps_v1=apps_v1,
            identity_verifier=identity_verifier,
            runtime_image=lifecycle_config.image,
        ),
        storage_binding=KubernetesGovernanceStorageBindingAdapter(
            core_v1=core_v1,
            batch_v1=batch_v1,
            apps_v1=apps_v1,
            identity_verifier=identity_verifier,
            runtime_image=lifecycle_config.image,
            cell=cell,
        ),
        governance_migration=HostedGovernanceMigrationCoordinator(
            cell=cell,
            jobs=KubernetesGovernanceMigrationAdapter(
                core_v1=core_v1,
                apps_v1=apps_v1,
                batch_v1=batch_v1,
            ),
        ),
        fingerprint=KubernetesVaultFingerprintAdapter(
            core_v1=core_v1,
            batch_v1=batch_v1,
            apps_v1=apps_v1,
            image=lock.components.provisioner.image,
        ),
    )
    return LiveProviderComponents(
        lock=lock,
        plane=plane,
        driver=CellLifecycleDriver(
            plane=plane,
            volume_worker=None,
            config=lifecycle_config,
        ),
        capacity=capacity,
        cell=cell,
        runtime=runtime,
    )


def build_activation_ack_server(
    app: Any,
    *,
    certificate_path: str,
    key_path: str,
) -> uvicorn.Server:
    """Build the fixed worker-only TLS listener without access logging."""

    return uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=8443,
            ssl_certfile=certificate_path,
            ssl_keyfile=key_path,
            access_log=False,
            server_header=False,
            date_header=False,
            log_config=None,
            lifespan="off",
            limit_concurrency=9,
            backlog=8,
            timeout_keep_alive=1,
            h11_max_incomplete_event_size=8 * 1024,
        )
    )


async def run_worker_with_activation_ack(
    worker: Awaitable[None],
    listener: Awaitable[None],
) -> None:
    """Stop and join the paired worker/listener when either one exits."""

    tasks = (asyncio.create_task(worker), asyncio.create_task(listener))
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_worker() -> None:
    from kubernetes import client, config

    settings = ProvisionerSettings()  # type: ignore[call-arg]
    provider = ProviderWorkerSettings()  # type: ignore[call-arg]
    _require_production_database(settings)
    database = ProvisionerDatabase(settings)
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
        max_failure_attempts=settings.max_failure_attempts,
    )
    config.load_incluster_config()
    api_client = client.ApiClient()
    core_v1 = client.CoreV1Api(api_client)
    apps_v1 = client.AppsV1Api(api_client)
    batch_v1 = client.BatchV1Api(api_client)
    coordination_v1 = client.CoordinationV1Api(api_client)
    storage_v1 = client.StorageV1Api(api_client)
    custom_objects = client.CustomObjectsApi(api_client)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(10.0, connect=5.0),
    ) as http:

        async def requester(method: str, url: str, **kwargs: Any) -> httpx.Response:
            return await http.request(method, url, **kwargs)

        async def external_probe(method: str, url: str, headers: dict[str, str]) -> int:
            response = await http.request(method, url, headers=headers)
            return response.status_code

        components = build_live_provider_components(
            repository=repository,
            settings=provider,
            core_v1=core_v1,
            apps_v1=apps_v1,
            batch_v1=batch_v1,
            coordination_v1=coordination_v1,
            storage_v1=storage_v1,
            custom_objects=custom_objects,
            requester=requester,
            external_probe=external_probe,
            stream_request=http.stream,
        )
        worker = build_routine_operation_worker(
            repository=repository,
            driver=components.driver,
            worker_id=provider.worker_id,
            capacity_admission=components.capacity,
        )
        try:
            polling = run_polling_loop(
                worker,
                poll_seconds=provider.poll_seconds,
                idle_poll_seconds=provider.idle_poll_seconds,
            )
            if provider.activation_ack_protocol is None:
                await polling
            else:
                assert provider.activation_ack_tls_cert_path is not None
                assert provider.activation_ack_tls_key_path is not None
                target = components.lock.selected_runtime(provider.runtime_selection).runtimeTarget
                ack_service = ActivationAckService(
                    cell_lookup=repository,
                    cell=components.cell,
                    runtime=components.runtime,
                    runtime_release=target.releaseVersion,
                    runtime_protocol_version=target.protocolVersion,
                )
                ack_app = create_activation_ack_app(ack_service)
                ack_server = build_activation_ack_server(
                    ack_app,
                    certificate_path=provider.activation_ack_tls_cert_path,
                    key_path=provider.activation_ack_tls_key_path,
                )

                async def serve_activation_ack() -> None:
                    try:
                        await ack_server.serve()
                    finally:
                        await ack_app.state.activation_ack_drain()

                await run_worker_with_activation_ack(polling, serve_activation_ack())
        finally:
            await database.dispose()
            await asyncio.to_thread(api_client.close)


def run_worker() -> None:
    if help_requested("exomem-provisioner-worker", "routine hosted lifecycle worker"):
        return
    configure_content_free_logging()
    asyncio.run(_run_worker())
