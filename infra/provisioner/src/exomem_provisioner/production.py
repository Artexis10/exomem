"""Strict production construction and worker entrypoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from .adapters import (
    HCloudVolumeAdapter,
    HelmCliAdapter,
    KubernetesCellAdapter,
    KubernetesHostedOperatorAdapter,
    KubernetesMaintenanceLeaseAdapter,
    KubernetesVolumeAdapter,
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
from .lifecycle import CellLifecycleDriver, LifecycleConfig, VolumeLifecycleWorker
from .live import (
    KubernetesHCloudCapacityGate,
    KubernetesProviderRegistry,
    LiveLifecyclePlane,
)
from .logging import configure_content_free_logging
from .main import _require_production_database
from .repository import OperationRepository
from .worker import ProvisionerWorker


@dataclass(frozen=True, slots=True)
class LiveProviderComponents:
    release: HostedReleaseManifest
    plane: LiveLifecyclePlane
    volume_worker: VolumeLifecycleWorker
    driver: CellLifecycleDriver


def build_live_provider_components(
    *,
    repository: OperationRepository,
    settings: ProviderWorkerSettings,
    core_v1: Any,
    apps_v1: Any,
    batch_v1: Any,
    coordination_v1: Any,
    custom_objects: Any,
    hcloud_client: Any,
    requester: Any,
    external_probe: Any,
) -> LiveProviderComponents:
    """Build only real adapters; production has no emulator selection flag."""

    release = load_hosted_release_manifest(settings.release_manifest_path)
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
    kubernetes_volume = KubernetesVolumeAdapter(
        core_v1=core_v1,
        storage_class_name="exomem-hcloud-encrypted-retain",
        encryption_secret_name=settings.volume_encryption_secret_name,
        encryption_secret_namespace=settings.volume_encryption_secret_namespace,
    )
    hcloud_volume = HCloudVolumeAdapter(client=hcloud_client)
    volume_worker = VolumeLifecycleWorker(kubernetes_volume, hcloud_volume)
    plane = LiveLifecyclePlane(
        repository=repository,
        registry=KubernetesProviderRegistry(
            core_v1=core_v1,
            apps_v1=apps_v1,
            batch_v1=batch_v1,
            custom_objects=custom_objects,
        ),
        cell=KubernetesCellAdapter(core_v1=core_v1, apps_v1=apps_v1),
        kubernetes_volume=kubernetes_volume,
        hcloud_volume=hcloud_volume,
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
        operator=KubernetesHostedOperatorAdapter(core_v1=core_v1),
        capacity=KubernetesHCloudCapacityGate(
            core_v1=core_v1,
            hcloud_client=hcloud_client,
        ),
        config=lifecycle_config,
    )
    return LiveProviderComponents(
        release=release,
        plane=plane,
        volume_worker=volume_worker,
        driver=CellLifecycleDriver(
            plane=plane,
            volume_worker=volume_worker,
            config=lifecycle_config,
        ),
    )


async def _run_worker() -> None:
    from hcloud import Client as HCloudClient
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
            custom_objects=custom_objects,
            hcloud_client=HCloudClient(token=provider.hcloud_token.get_secret_value()),
            requester=requester,
            external_probe=external_probe,
        )
        worker = ProvisionerWorker(
            repository,
            components.driver,
            worker_id=provider.worker_id,
        )
        try:
            while True:
                if not await worker.run_once():
                    await asyncio.sleep(provider.poll_seconds)
        finally:
            await database.dispose()
            await asyncio.to_thread(api_client.close)


def run_worker() -> None:
    configure_content_free_logging()
    asyncio.run(_run_worker())
