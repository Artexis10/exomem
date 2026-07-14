"""Strict production construction and worker entrypoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from .adapters import (
    HelmCliAdapter,
    KubernetesCellAdapter,
    KubernetesMaintenanceLeaseAdapter,
    PrivateCellApiAdapter,
    TraefikRoutingAdapter,
)
from .config import (
    HostedReleaseManifest,
    ProviderWorkerSettings,
    ProvisionerSettings,
    load_hosted_release_manifest,
)
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .entrypoint import help_requested
from .lifecycle import CellLifecycleDriver, LifecycleConfig
from .live import (
    KubernetesCapacityGate,
    KubernetesProviderRegistry,
    LiveLifecyclePlane,
)
from .logging import configure_content_free_logging
from .main import _require_production_database
from .provider_identity import ProviderRecoveryIdentityVerifier
from .repository import OperationRepository
from .worker import ProvisionerWorker


@dataclass(frozen=True, slots=True)
class LiveProviderComponents:
    release: HostedReleaseManifest
    plane: LiveLifecyclePlane
    driver: CellLifecycleDriver


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
) -> LiveProviderComponents:
    """Build only real adapters; production has no emulator selection flag."""

    release = load_hosted_release_manifest(settings.release_manifest_path)
    identity_verifier = ProviderRecoveryIdentityVerifier.from_public_key(
        settings.provider_recovery_public_key
    )
    lifecycle_config = LifecycleConfig(
        image=release.runtimeImage,
        chart_path=settings.cell_chart_path,
        chart_version=settings.cell_chart_version,
        helm_version=settings.helm_version,
        control_hostname=settings.control_hostname,
        transfer_hostname=settings.transfer_hostname,
        browser_origin=settings.browser_origin,
        release_version=release.release,
        protocol_version=release.hostedProtocol,
        operator_contract_digest=release.operatorContractSha256,
        contract_digest=release.gatewayContractSha256,
        location=settings.location,
    )
    cell = KubernetesCellAdapter(
        core_v1=core_v1,
        apps_v1=apps_v1,
        identity_verifier=identity_verifier,
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
        ),
        runtime=PrivateCellApiAdapter(
            request=requester,
            internal_origin=settings.internal_origin,
        ),
        routes=TraefikRoutingAdapter(
            custom_objects=custom_objects,
            control_hostname=settings.control_hostname,
            transfer_hostname=settings.transfer_hostname,
            probe=external_probe,
        ),
        maintenance=KubernetesMaintenanceLeaseAdapter(
            coordination_v1=coordination_v1,
        ),
        capacity=KubernetesCapacityGate(
            core_v1=core_v1,
            storage_v1=storage_v1,
        ),
        identity_verifier=identity_verifier,
        config=lifecycle_config,
    )
    return LiveProviderComponents(
        release=release,
        plane=plane,
        driver=CellLifecycleDriver(
            plane=plane,
            volume_worker=None,
            config=lifecycle_config,
        ),
    )


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
        )
        worker = ProvisionerWorker(
            repository,
            components.driver,
            worker_id=provider.worker_id,
            exclude_checkpoints=frozenset({"volume-registration-required"}),
        )
        try:
            while True:
                if not await worker.run_once():
                    await asyncio.sleep(provider.poll_seconds)
        finally:
            await database.dispose()
            await asyncio.to_thread(api_client.close)


def run_worker() -> None:
    if help_requested("exomem-provisioner-worker", "routine hosted lifecycle worker"):
        return
    configure_content_free_logging()
    asyncio.run(_run_worker())
