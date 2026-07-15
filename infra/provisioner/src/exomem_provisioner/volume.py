"""Privileged finite PV/HCloud worker and clean-cluster rebind entrypoint."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any

from .adapters import HCloudVolumeAdapter, KubernetesVolumeAdapter
from .config import ProvisionerSettings, VolumeWorkerSettings
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .entrypoint import help_requested
from .lifecycle import (
    OpaqueProviderMetadata,
    RecordedVolume,
    VolumeLifecycleWorker,
    VolumeRegistrationDriver,
)
from .logging import configure_content_free_logging
from .main import _require_production_database
from .models import ResourceKind
from .production import build_live_capacity_admission
from .provider_identity import ProviderRecoveryIdentityCodec
from .repository import OperationRepository
from .worker import CapacityAdmission, ProvisionerWorker


@dataclass(frozen=True, slots=True)
class VolumeProviderComponents:
    worker: VolumeLifecycleWorker
    driver: VolumeRegistrationDriver


def build_volume_provider_components(
    *,
    settings: VolumeWorkerSettings,
    core_v1: Any,
    hcloud_client: Any,
) -> VolumeProviderComponents:
    """Construct the only production seam that can mutate PVs and HCloud volumes."""

    identity_codec = ProviderRecoveryIdentityCodec.from_encoded_seed(
        settings.provider_recovery_signing_key.get_secret_value()
    )
    verifier = identity_codec.verifier()
    kubernetes = KubernetesVolumeAdapter(
        core_v1=core_v1,
        storage_class_name="exomem-hcloud-encrypted-retain",
        encryption_secret_name=settings.volume_encryption_secret_name,
        encryption_secret_namespace=settings.volume_encryption_secret_namespace,
        identity_verifier=verifier,
    )
    hcloud = HCloudVolumeAdapter(client=hcloud_client, identity_verifier=verifier)
    volume_worker = VolumeLifecycleWorker(
        kubernetes,
        hcloud,
        identity_codec=identity_codec,
    )
    return VolumeProviderComponents(
        worker=volume_worker,
        driver=VolumeRegistrationDriver(volume_worker, identity_verifier=verifier),
    )


def _repository(
    settings: ProvisionerSettings,
    database: ProvisionerDatabase,
) -> OperationRepository:
    return OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
        max_failure_attempts=settings.max_failure_attempts,
    )


def build_volume_registration_worker(
    *,
    repository: OperationRepository,
    driver: Any,
    worker_id: str,
    capacity_admission: CapacityAdmission | None,
) -> ProvisionerWorker:
    """Build the PROVISION checkpoint owner with the shared admission authority."""

    return ProvisionerWorker(
        repository,
        driver,
        worker_id=worker_id,
        include_checkpoints=frozenset({"volume-registration-required"}),
        capacity_admission=capacity_admission,
    )


async def _run_volume_worker() -> None:
    from hcloud import Client as HCloudClient
    from kubernetes import client, config

    common = ProvisionerSettings()  # type: ignore[call-arg]
    settings = VolumeWorkerSettings()  # type: ignore[call-arg]
    _require_production_database(common)
    database = ProvisionerDatabase(common)
    repository = _repository(common, database)
    config.load_incluster_config()
    api_client = client.ApiClient()
    core_v1 = client.CoreV1Api(api_client)
    capacity = build_live_capacity_admission(
        repository=repository,
        settings=settings,
        core_v1=core_v1,
        storage_v1=client.StorageV1Api(api_client),
    )
    components = build_volume_provider_components(
        settings=settings,
        core_v1=core_v1,
        hcloud_client=HCloudClient(token=settings.hcloud_token.get_secret_value()),
    )
    worker = build_volume_registration_worker(
        repository=repository,
        driver=components.driver,
        worker_id=settings.worker_id,
        capacity_admission=capacity,
    )
    try:
        while True:
            if not await worker.run_once():
                await asyncio.sleep(settings.poll_seconds)
    finally:
        await database.dispose()
        await asyncio.to_thread(api_client.close)


def run_volume_worker() -> None:
    if help_requested("exomem-volume-worker", "privileged retained-volume worker"):
        return
    configure_content_free_logging()
    asyncio.run(_run_volume_worker())


def _rebind_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exomem-provisioner-volume-rebind",
        description="Reconstruct one retained static PV/PVC from its encrypted registry record.",
    )
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--provider-operation-id", required=True)
    parser.add_argument("--fence-generation", type=int, required=True)
    return parser


async def _run_volume_rebind(arguments: list[str] | None = None) -> None:
    from hcloud import Client as HCloudClient
    from kubernetes import client, config

    parsed = _rebind_parser().parse_args(arguments)
    common = ProvisionerSettings()  # type: ignore[call-arg]
    settings = VolumeWorkerSettings()  # type: ignore[call-arg]
    _require_production_database(common)
    database = ProvisionerDatabase(common)
    repository = _repository(common, database)
    config.load_incluster_config()
    api_client = client.ApiClient()
    try:
        resources = await repository.list_resources(
            tenant_id=parsed.tenant_id,
            cell_id=parsed.cell_id,
        )
        resource = next(
            (
                item
                for item in resources
                if item.id == parsed.resource_id
                and item.kind is ResourceKind.VOLUME
                and item.provider_operation_id == parsed.provider_operation_id
                and item.provider_fence_generation == parsed.fence_generation
            ),
            None,
        )
        if resource is None:
            raise RuntimeError("volume resource identity does not match the registry")
        metadata = OpaqueProviderMetadata(
            tenant_id=parsed.tenant_id,
            subject_id=parsed.cell_id,
            operation_id=parsed.provider_operation_id,
            fence_generation=parsed.fence_generation,
        )
        reference = await repository.load_resource_reference(resource.id)
        recorded = RecordedVolume.from_recoverable_reference(reference, metadata)
        components = build_volume_provider_components(
            settings=settings,
            core_v1=client.CoreV1Api(api_client),
            hcloud_client=HCloudClient(token=settings.hcloud_token.get_secret_value()),
        )
        await components.worker.rebind_static(
            recorded,
            metadata,
            location=settings.location,
        )
    finally:
        await database.dispose()
        await asyncio.to_thread(api_client.close)


def run_volume_rebind() -> None:
    if help_requested(
        "exomem-provisioner-volume-rebind",
        "reconstruct one authenticated retained static PV/PVC",
    ):
        return
    configure_content_free_logging()
    asyncio.run(_run_volume_rebind())
