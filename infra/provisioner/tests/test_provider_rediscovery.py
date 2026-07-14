from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.durability_repository import DurabilityRepository
from exomem_provisioner.provider_recovery import (
    PaginatedProviderScanner,
    ProviderMetadataConflict,
    ProviderMetadataObservation,
    ProviderRecoveryIdentityCodec,
    ProviderRecoveryIdentityDecoder,
    ProviderRediscoveryGate,
    ProviderReference,
)
from exomem_provisioner.repository import OperationRepository, StaleFence


def test_generated_base64url_seed_round_trips_to_matching_public_verifier() -> None:
    raw_seed = bytes(range(32))
    encoded = base64.urlsafe_b64encode(raw_seed).decode("ascii").rstrip("=")
    expected_public = (
        Ed25519PrivateKey.from_private_bytes(raw_seed)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )

    signer = ProviderRecoveryIdentityCodec.from_encoded_seed(encoded)

    assert base64.urlsafe_b64decode(signer.public_key() + "==") == expected_public


def _settings(path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )


def _request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "operationId": "operation-known-five",
        "checkpoint": "requested",
        "fenceGeneration": 5,
        "tenantId": "tenant-recovered-alpha",
        "cellId": "cell-recovered-alpha",
        "protocolVersion": "exomem-hosted.v1",
        "releaseVersion": "0.22.0",
        "serviceCredential": "service-credential-sentinel-000000000",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
    }
    value.update(overrides)
    return value


class Scanner:
    def __init__(self, provider: str, values: list[ProviderMetadataObservation], events: list[str]):
        self.provider = provider
        self.values = values
        self.events = events

    async def scan(self) -> list[ProviderMetadataObservation]:
        self.events.append(f"scanned:{self.provider}")
        return self.values


def _observation(
    provider: str,
    reference: str,
    operation_id: str,
    fence: int,
) -> ProviderMetadataObservation:
    return ProviderMetadataObservation(
        provider=provider,
        provider_reference=reference,
        tenant_id="tenant-recovered-alpha",
        cell_id="cell-recovered-alpha",
        operation_id=operation_id,
        fence_generation=fence,
        observed_at=datetime(2030, 1, 1, tzinfo=UTC),
        metadata_authenticated=True,
    )


@pytest.fixture
async def rediscovery_context(tmp_path: Path):
    settings = _settings(tmp_path / "rediscovery.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    codec = AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value())
    operations = OperationRepository(database.session_factory, codec=codec)
    durability = DurabilityRepository(database.session_factory, codec=codec)
    await operations.submit("provision", "known-five", _request())
    try:
        yield database, operations, durability
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_all_providers_are_scanned_before_max_fence_blocks_mutation_and_classifies_resources(
    rediscovery_context,
) -> None:
    _, operations, durability = rediscovery_context
    events: list[str] = []
    scanners = [
        Scanner(
            "kubernetes",
            [_observation("kubernetes", "namespace/cell-alpha", "operation-known-five", 5)],
            events,
        ),
        Scanner(
            "hcloud",
            [_observation("hcloud", "volume/opaque-new", "operation-missing-seven", 7)],
            events,
        ),
        Scanner(
            "traefik",
            [_observation("traefik", "route/opaque-six", "operation-missing-six", 6)],
            events,
        ),
        Scanner(
            "b2",
            [_observation("b2", "backup/opaque-seven", "operation-missing-seven", 7)],
            events,
        ),
    ]
    gate = ProviderRediscoveryGate(repository=durability, scanners=scanners)

    result = await gate.reconcile()

    assert events == [
        "scanned:kubernetes",
        "scanned:hcloud",
        "scanned:traefik",
        "scanned:b2",
    ]
    assert result.maximum_fences == {"tenant-recovered-alpha": 7}
    disposition = {
        (item.provider, item.operation_id): item.disposition for item in result.observations
    }
    assert disposition[("kubernetes", "operation-known-five")] == "adopted"
    assert disposition[("hcloud", "operation-missing-seven")] == "quarantined"
    assert disposition[("traefik", "operation-missing-six")] == "quarantined"
    assert disposition[("b2", "operation-missing-seven")] == "quarantined"

    with pytest.raises(StaleFence):
        await operations.submit(
            "resume",
            "lower-after-recovery",
            _request(operationId="operation-lower-six", fenceGeneration=6),
        )


@pytest.mark.asyncio
async def test_conflicting_provider_identity_aborts_before_fence_or_adoption_mutation(
    rediscovery_context,
) -> None:
    _, operations, durability = rediscovery_context
    events: list[str] = []
    conflict = _observation("hcloud", "volume/same", "operation-new-seven", 7)
    changed = _observation("hcloud", "volume/same", "operation-other-eight", 8)
    gate = ProviderRediscoveryGate(
        repository=durability,
        scanners=[
            Scanner("kubernetes", [], events),
            Scanner("hcloud", [conflict, changed], events),
            Scanner("traefik", [], events),
            Scanner("b2", [], events),
        ],
    )

    with pytest.raises(ProviderMetadataConflict):
        await gate.reconcile()

    current = await durability.tenant_fence("tenant-recovered-alpha")
    assert current == 5
    accepted = await operations.submit(
        "resume",
        "still-current-six",
        _request(operationId="operation-current-six", fenceGeneration=6),
    )
    assert accepted.fence_generation == 6


def test_rediscovery_requires_kubernetes_hcloud_traefik_and_b2_scanners() -> None:
    with pytest.raises(ValueError, match="exactly"):
        ProviderRediscoveryGate(repository=object(), scanners=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["kubernetes", "hcloud", "traefik", "b2"])
async def test_provider_scanner_consumes_every_page(provider: str) -> None:
    calls: list[str | None] = []

    async def read(token: str | None):
        calls.append(token)
        if token is None:
            return [_observation(provider, f"{provider}/one", "operation-one", 1)], "next"
        return [_observation(provider, f"{provider}/two", "operation-two", 2)], None

    values = await PaginatedProviderScanner(provider=provider, page_reader=read).scan()

    assert calls == [None, "next"]
    assert [value.provider_reference for value in values] == [
        f"{provider}/one",
        f"{provider}/two",
    ]


def _chunked(prefix: str, value: str) -> dict[str, str]:
    encoded = base64.b32encode(value.encode("ascii")).decode("ascii").rstrip("=").lower()
    chunks = [encoded[index : index + 52] for index in range(0, len(encoded), 52)]
    return {
        f"{prefix}_n": str(len(chunks)),
        **{f"{prefix}_{index}": chunk for index, chunk in enumerate(chunks)},
    }


def test_recovery_identity_decoder_consumes_exact_kubernetes_and_hcloud_contract() -> None:
    tenant = "tenant-recovered-alpha"
    cell = "cell-recovered-alpha"
    operation = "operation-created-after-database-snapshot"
    observed_at = datetime(2030, 1, 1, tzinfo=UTC)
    codec = ProviderRecoveryIdentityCodec.from_secret("provider-recovery-root")
    kubernetes_annotations = {
        "exomem.io/tenant-id": tenant,
        "exomem.io/cell-id": cell,
        "exomem.io/operation-id": operation,
        "exomem.io/fence": "11",
        "exomem.io/tenant-digest": hashlib.sha256(tenant.encode()).hexdigest(),
        "exomem.io/subject-digest": hashlib.sha256(cell.encode()).hexdigest(),
        "exomem.io/operation-digest": hashlib.sha256(operation.encode()).hexdigest(),
        "exomem.io/recovery-envelope": codec.seal(
            provider="kubernetes",
            provider_reference="namespace/exo-cell",
            tenant_id=tenant,
            cell_id=cell,
            operation_id=operation,
            fence_generation=11,
        ),
    }
    hcloud_labels = {
        **_chunked("exomem_tenant_id", tenant),
        **_chunked("exomem_cell_id", cell),
        **_chunked("exomem_operation_id", operation),
        "exomem_fence": "11",
        "exomem_tenant": hashlib.sha256(tenant.encode()).hexdigest()[:24],
        "exomem_subject": hashlib.sha256(cell.encode()).hexdigest()[:24],
        "exomem_operation": hashlib.sha256(operation.encode()).hexdigest()[:24],
        **_chunked(
            "exomem_identity",
            codec.seal(
                provider="hcloud",
                provider_reference="volume/1234",
                tenant_id=tenant,
                cell_id=cell,
                operation_id=operation,
                fence_generation=11,
            ),
        ),
    }

    kubernetes = ProviderRecoveryIdentityDecoder.kubernetes(
        provider_reference="namespace/exo-cell",
        annotations=kubernetes_annotations,
        observed_at=observed_at,
        identity_codec=codec,
    )
    hcloud = ProviderRecoveryIdentityDecoder.hcloud(
        provider_reference="volume/1234",
        labels=hcloud_labels,
        observed_at=observed_at,
        identity_codec=codec,
    )

    assert kubernetes == ProviderMetadataObservation(
        provider="kubernetes",
        provider_reference="namespace/exo-cell",
        tenant_id=tenant,
        cell_id=cell,
        operation_id=operation,
        fence_generation=11,
        observed_at=observed_at,
        metadata_authenticated=True,
    )
    assert hcloud.tenant_id == tenant
    assert hcloud.cell_id == cell
    assert hcloud.operation_id == operation
    assert hcloud.fence_generation == 11
    assert hcloud.metadata_authenticated is True


def test_recovery_identity_decoder_rejects_digest_mismatch_and_chunk_ambiguity() -> None:
    labels = {
        **_chunked("exomem_tenant_id", "tenant-alpha"),
        **_chunked("exomem_cell_id", "cell-alpha"),
        **_chunked("exomem_operation_id", "operation-alpha"),
        "exomem_fence": "9",
        "exomem_tenant": "0" * 24,
        "exomem_subject": hashlib.sha256(b"cell-alpha").hexdigest()[:24],
        "exomem_operation": hashlib.sha256(b"operation-alpha").hexdigest()[:24],
        "exomem_tenant_id_7": "orphan",
    }

    with pytest.raises(ProviderMetadataConflict):
        ProviderRecoveryIdentityDecoder.hcloud(
            provider_reference="volume/1234",
            labels=labels,
            observed_at=datetime(2030, 1, 1, tzinfo=UTC),
            identity_codec=ProviderRecoveryIdentityCodec.from_secret("provider-recovery-root"),
        )


def test_recovery_identity_decoder_rejects_copied_or_tampered_envelope() -> None:
    codec = ProviderRecoveryIdentityCodec.from_secret("provider-recovery-root")
    tenant = "tenant-alpha"
    cell = "cell-alpha"
    operation = "operation-alpha"
    annotations = {
        "exomem.io/tenant-id": tenant,
        "exomem.io/cell-id": cell,
        "exomem.io/operation-id": operation,
        "exomem.io/fence": "9",
        "exomem.io/tenant-digest": hashlib.sha256(tenant.encode()).hexdigest(),
        "exomem.io/subject-digest": hashlib.sha256(cell.encode()).hexdigest(),
        "exomem.io/operation-digest": hashlib.sha256(operation.encode()).hexdigest(),
        "exomem.io/recovery-envelope": codec.seal(
            provider="kubernetes",
            provider_reference="namespace/original",
            tenant_id=tenant,
            cell_id=cell,
            operation_id=operation,
            fence_generation=9,
        ),
    }

    with pytest.raises(ProviderMetadataConflict, match="authenticate"):
        ProviderRecoveryIdentityDecoder.kubernetes(
            provider_reference="namespace/copied",
            annotations=annotations,
            observed_at=datetime(2030, 1, 1, tzinfo=UTC),
            identity_codec=codec,
        )


def test_provider_reference_is_canonical_and_collision_free_per_exact_object() -> None:
    namespace = ProviderReference.kubernetes(
        provider="kubernetes",
        api_version="v1",
        kind="Namespace",
        namespace="",
        name="exo-cell-alpha",
    )
    pvc = ProviderReference.kubernetes(
        provider="kubernetes",
        api_version="v1",
        kind="PersistentVolumeClaim",
        namespace="exo-cell-alpha",
        name="data",
    )
    route = ProviderReference.kubernetes(
        provider="traefik",
        api_version="traefik.io/v1alpha1",
        kind="IngressRoute",
        namespace="exo-cell-alpha",
        name="cell-control",
    )

    assert len({namespace, pvc, route}) == 3
    assert ProviderReference.parse(namespace) == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "name": "exo-cell-alpha",
        "namespace": "",
        "provider": "kubernetes",
        "version": 1,
    }
    assert (
        ProviderReference.parse(ProviderReference.hcloud(kind="volume", resource_id=123))["id"]
        == "123"
    )
    assert (
        ProviderReference.parse(ProviderReference.b2(bucket="recovery", key="a/b"))["key"] == "a/b"
    )
