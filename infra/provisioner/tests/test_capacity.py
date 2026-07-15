from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from exomem_provisioner.capacity import (
    CapacityBlocked,
    CapacityConflict,
    CapacityObservation,
    CapacityReceiptError,
    CapacityReceiptVerifier,
    CapacityReservationAuthority,
    KubernetesCapacityObserver,
    VerifiedCapacityReceipt,
    canonical_contract_digest,
)
from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.lifecycle import OpaqueProviderMetadata
from exomem_provisioner.models import (
    CapacityLedger,
    CapacityReservation,
    CapacityReservationClass,
    OperationAction,
)
from exomem_provisioner.repository import OperationRepository, StaleFence

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def _settings(path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
        claim_seconds=60,
    )


def _request(
    *,
    operation: str,
    tenant: str,
    cell: str,
    fence: int,
    mode: str = "serve",
) -> dict[str, object]:
    return {
        "operationId": operation,
        "checkpoint": "requested",
        "fenceGeneration": fence,
        "tenantId": tenant,
        "cellId": cell,
        "provisionMode": mode,
        "protocolVersion": "exomem-hosted.v1",
        "releaseVersion": "0.22.0",
        "serviceCredential": "service-credential-sentinel-000000000",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
    }


def _resource(cell: str) -> str:
    return OpaqueProviderMetadata("tenant", cell, "operation", 1).resource_name


def _observation(
    *,
    users: set[str] | None = None,
    recovery: set[str] | None = None,
    orphans: set[str] | None = None,
    attached: int = 0,
    observed_at: datetime = NOW,
    cluster_uid: str = "11111111-1111-4111-8111-111111111111",
    server_id: int = 101,
    location: str = "fsn1",
) -> CapacityObservation:
    users = users or set()
    recovery = recovery or set()
    return CapacityObservation(
        observed_at=observed_at,
        cluster_uid=cluster_uid,
        hcloud_server_id=server_id,
        hcloud_location=location,
        user_resource_names=frozenset(users),
        recovery_resource_names=frozenset(recovery),
        orphan_attachment_ids=frozenset(orphans or set()),
        attached_hcloud_volumes=attached,
    )


def _receipt(
    observation: CapacityObservation,
    *,
    users: int | None = None,
    recovery: int | None = None,
    attached: int | None = None,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> VerifiedCapacityReceipt:
    return VerifiedCapacityReceipt(
        receipt_id="22222222-2222-4222-8222-222222222222",
        sequence=1,
        observed_at=NOW,
        expires_at=expires_at,
        cluster_uid=observation.cluster_uid,
        hcloud_server_id=observation.hcloud_server_id,
        hcloud_location=observation.hcloud_location,
        active_user_cells=(
            len(observation.user_resource_names) if users is None else users
        ),
        active_recovery_cells=(
            len(observation.recovery_resource_names) if recovery is None else recovery
        ),
        attached_volumes=(
            observation.attached_hcloud_volumes if attached is None else attached
        ),
    )


@pytest.fixture
async def capacity_context(
    tmp_path: Path,
) -> tuple[ProvisionerDatabase, OperationRepository, CapacityReservationAuthority]:
    settings = _settings(tmp_path / "capacity.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
    )
    authority = CapacityReservationAuthority(database.session_factory)
    try:
        yield database, repository, authority
    finally:
        await database.dispose()


async def _claimed(
    repository: OperationRepository,
    *,
    operation: str,
    tenant: str,
    cell: str,
    fence: int,
    mode: str = "serve",
    worker: str | None = None,
):
    submitted = await repository.submit(
        "provision",
        f"key-{operation}",
        _request(operation=operation, tenant=tenant, cell=cell, fence=fence, mode=mode),
    )
    owner = worker or f"worker-{operation}"
    claimed = await repository.claim_next(owner, now=NOW)
    assert claimed is not None and claimed.id == submitted.id and claimed.claim_token
    return claimed, owner


async def _reserve(
    repository: OperationRepository,
    authority: CapacityReservationAuthority,
    *,
    operation: str,
    tenant: str,
    cell: str,
    fence: int,
    mode: str = "serve",
    observation: CapacityObservation | None = None,
):
    claimed, worker = await _claimed(
        repository,
        operation=operation,
        tenant=tenant,
        cell=cell,
        fence=fence,
        mode=mode,
    )
    observed = observation or _observation()
    reservation = await authority.reserve(
        claimed,
        _request(operation=operation, tenant=tenant, cell=cell, fence=fence, mode=mode),
        receipt=_receipt(observed),
        observation=observed,
        worker_id=worker,
        claim_token=claimed.claim_token or "",
        claim_generation=claimed.claim_generation,
        now=NOW,
    )
    return claimed, worker, reservation


@pytest.mark.asyncio
async def test_test_database_seeds_singleton_exactly_once(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, _, _ = capacity_context
    await database.create_for_tests()
    async with database.session_factory() as session:
        rows = list(await session.scalars(select(CapacityLedger)))
    assert [(row.id, row.revision) for row in rows] == [(1, 0)]


@pytest.mark.asyncio
async def test_reservation_is_idempotent_and_changed_identity_conflicts(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, authority = capacity_context
    claimed, worker, first = await _reserve(
        repository,
        authority,
        operation="operation-one",
        tenant="tenant-one",
        cell="cell-one",
        fence=1,
    )
    observed = _observation()
    replay = await authority.reserve(
        claimed,
        _request(
            operation="operation-one", tenant="tenant-one", cell="cell-one", fence=1
        ),
        receipt=_receipt(observed),
        observation=observed,
        worker_id=worker,
        claim_token=claimed.claim_token or "",
        claim_generation=claimed.claim_generation,
        now=NOW,
    )
    assert replay.id == first.id
    with pytest.raises(CapacityConflict):
        await authority.reserve(
            claimed,
            _request(
                operation="operation-one",
                tenant="tenant-one",
                cell="cell-one",
                fence=1,
                mode="restore-candidate",
            ),
            receipt=_receipt(observed),
            observation=observed,
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )

    async with database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
        assert (await session.get(CapacityLedger, 1)).revision == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_user_recovery_and_attachment_ceilings_fail_closed(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    _, repository, authority = capacity_context
    for index in range(6):
        claimed, worker = await _claimed(
            repository,
            operation=f"user-{index}",
            tenant=f"tenant-user-{index}",
            cell=f"cell-user-{index}",
            fence=1,
        )
        observed = _observation()
        await authority.reserve(
            claimed,
            _request(
                operation=f"user-{index}",
                tenant=f"tenant-user-{index}",
                cell=f"cell-user-{index}",
                fence=1,
            ),
            receipt=_receipt(observed),
            observation=observed,
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    seventh, worker = await _claimed(
        repository,
        operation="user-seven",
        tenant="tenant-user-seven",
        cell="cell-user-seven",
        fence=1,
    )
    observed = _observation()
    with pytest.raises(CapacityBlocked) as user_error:
        await authority.reserve(
            seventh,
            _request(
                operation="user-seven",
                tenant="tenant-user-seven",
                cell="cell-user-seven",
                fence=1,
            ),
            receipt=_receipt(observed),
            observation=observed,
            worker_id=worker,
            claim_token=seventh.claim_token or "",
            claim_generation=seventh.claim_generation,
            now=NOW,
        )
    assert user_error.value.reason == "capacity-user-exhausted"



@pytest.mark.asyncio
async def test_recovery_and_orphan_limits_are_separate(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    _, repository, authority = capacity_context
    for index in range(2):
        await _reserve(
            repository,
            authority,
            operation=f"recovery-{index}",
            tenant=f"tenant-recovery-{index}",
            cell=f"cell-recovery-{index}",
            fence=1,
            mode="restore-candidate",
        )
    claimed, worker = await _claimed(
        repository,
        operation="recovery-three",
        tenant="tenant-recovery-three",
        cell="cell-recovery-three",
        fence=1,
        mode="restore-candidate",
    )
    observed = _observation()
    with pytest.raises(CapacityBlocked) as recovery_error:
        await authority.reserve(
            claimed,
            _request(
                operation="recovery-three",
                tenant="tenant-recovery-three",
                cell="cell-recovery-three",
                fence=1,
                mode="restore-candidate",
            ),
            receipt=_receipt(observed),
            observation=observed,
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    assert recovery_error.value.reason == "capacity-recovery-exhausted"

    # Two active recovery reservations plus six unattributable attachments leave
    # no potential attachment slot for another USER reservation.
    claimed, worker = await _claimed(
        repository,
        operation="orphan-nine",
        tenant="tenant-orphan-nine",
        cell="cell-orphan-nine",
        fence=1,
    )
    observed = _observation(orphans={f"orphan-{index}" for index in range(6)}, attached=6)
    with pytest.raises(CapacityBlocked) as orphan_error:
        await authority.reserve(
            claimed,
            _request(
                operation="orphan-nine",
                tenant="tenant-orphan-nine",
                cell="cell-orphan-nine",
                fence=1,
            ),
            receipt=_receipt(observed),
            observation=observed,
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    assert orphan_error.value.reason == "capacity-attachment-headroom-exhausted"


@pytest.mark.asyncio
async def test_concurrent_sixth_slot_attempts_cannot_over_admit(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, authority = capacity_context
    for index in range(5):
        await _reserve(
            repository,
            authority,
            operation=f"existing-{index}",
            tenant=f"tenant-existing-{index}",
            cell=f"cell-existing-{index}",
            fence=1,
        )
    attempts = [
        await _claimed(
            repository,
            operation=f"contender-{index}",
            tenant=f"tenant-contender-{index}",
            cell=f"cell-contender-{index}",
            fence=1,
        )
        for index in range(2)
    ]
    observed = _observation()

    async def reserve_one(index: int):
        claimed, worker = attempts[index]
        try:
            return await authority.reserve(
                claimed,
                _request(
                    operation=f"contender-{index}",
                    tenant=f"tenant-contender-{index}",
                    cell=f"cell-contender-{index}",
                    fence=1,
                ),
                receipt=_receipt(observed),
                observation=observed,
                worker_id=worker,
                claim_token=claimed.claim_token or "",
                claim_generation=claimed.claim_generation,
                now=NOW,
            )
        except CapacityBlocked:
            return None

    results = await asyncio.gather(reserve_one(0), reserve_one(1))
    assert sum(result is not None for result in results) == 1
    async with database.session_factory() as session:
        active = await session.scalar(
            select(func.count())
            .select_from(CapacityReservation)
            .where(CapacityReservation.released_at.is_(None))
        )
    assert active == 6


@pytest.mark.asyncio
async def test_receipt_expiry_and_stale_observation_commit_nothing(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, authority = capacity_context
    claimed, worker = await _claimed(
        repository,
        operation="stale-capacity",
        tenant="tenant-stale",
        cell="cell-stale",
        fence=1,
    )
    observed = _observation(observed_at=NOW - timedelta(seconds=31))
    with pytest.raises(CapacityBlocked) as stale:
        await authority.reserve(
            claimed,
            _request(
                operation="stale-capacity", tenant="tenant-stale", cell="cell-stale", fence=1
            ),
            receipt=_receipt(observed),
            observation=observed,
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    assert stale.value.reason == "capacity-live-observation-mismatch"
    with pytest.raises(CapacityBlocked) as expired:
        await authority.reserve(
            claimed,
            _request(
                operation="stale-capacity", tenant="tenant-stale", cell="cell-stale", fence=1
            ),
            receipt=_receipt(_observation(), expires_at=NOW - timedelta(seconds=1)),
            observation=_observation(),
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    assert expired.value.reason == "capacity-live-receipt-unavailable"
    async with database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
        assert (await session.get(CapacityLedger, 1)).revision == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_discard_release_is_atomic_proof_bound_and_history_is_immutable(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, authority = capacity_context
    provision, provision_worker, original = await _reserve(
        repository,
        authority,
        operation="provision-release",
        tenant="tenant-release",
        cell="cell-release",
        fence=1,
    )
    await repository.mark_pending(
        provision.id,
        provision_worker,
        claim_token=provision.claim_token or "",
        claim_generation=provision.claim_generation,
        checkpoint="namespace-ready",
        retry_after_seconds=300,
        now=NOW,
    )
    discard_request = _request(
        operation="discard-release",
        tenant="tenant-release",
        cell="cell-release",
        fence=2,
    )
    discard = await repository.submit("discard", "discard-release", discard_request)
    claimed = await repository.claim_next("discard-worker", now=NOW + timedelta(seconds=1))
    assert claimed is not None and claimed.id == discard.id and claimed.claim_token
    await repository.complete(
        claimed.id,
        {"computeDestroyed": True, "storageDestroyed": True, "keysDestroyed": True},
        worker_id="discard-worker",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        now=NOW + timedelta(seconds=1),
    )
    async with database.session_factory() as session:
        released = await session.get(CapacityReservation, original.id)
        ledger = await session.get(CapacityLedger, 1)
        assert released is not None and released.released_at is not None
        assert released.releasing_operation_id == discard.id
        assert ledger is not None and ledger.revision == 2

    # The old reserving operation is permanently consumed after release.
    with pytest.raises(CapacityConflict):
        await authority.reserve(
            provision,
            _request(
                operation="provision-release",
                tenant="tenant-release",
                cell="cell-release",
                fence=1,
            ),
            receipt=_receipt(_observation()),
            observation=_observation(),
            worker_id=provision_worker,
            claim_token=provision.claim_token or "",
            claim_generation=provision.claim_generation,
            now=NOW,
        )

    later, _later_worker, replacement = await _reserve(
        repository,
        authority,
        operation="provision-release-later",
        tenant="tenant-release",
        cell="cell-release",
        fence=3,
    )
    assert later.claim_token is not None
    assert replacement.id != original.id
    async with database.session_factory() as session:
        rows = list(
            await session.scalars(
                select(CapacityReservation)
                .where(CapacityReservation.tenant_id == "tenant-release")
                .order_by(CapacityReservation.reserved_at)
            )
        )
    assert [row.id for row in rows] == [original.id, replacement.id]
    assert rows[0].released_at is not None and rows[1].released_at is None


@pytest.mark.asyncio
async def test_malformed_proof_retains_and_equal_fence_release_rolls_back_completion(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, authority = capacity_context
    provision, worker, reservation = await _reserve(
        repository,
        authority,
        operation="provision-proof",
        tenant="tenant-proof",
        cell="cell-proof",
        fence=5,
    )
    await repository.mark_pending(
        provision.id,
        worker,
        claim_token=provision.claim_token or "",
        claim_generation=provision.claim_generation,
        checkpoint="release-applied",
        retry_after_seconds=300,
        now=NOW,
    )
    malformed = await repository.submit(
        "discard",
        "discard-malformed",
        _request(
            operation="discard-malformed",
            tenant="tenant-proof",
            cell="cell-proof",
            fence=6,
        ),
    )
    malformed_claim = await repository.claim_next(
        "discard-malformed-worker", now=NOW + timedelta(seconds=1)
    )
    assert malformed_claim is not None and malformed_claim.id == malformed.id
    await repository.complete(
        malformed.id,
        {
            "computeDestroyed": True,
            "storageDestroyed": True,
            "keysDestroyed": True,
            "callerProof": True,
        },
        worker_id="discard-malformed-worker",
        claim_token=malformed_claim.claim_token or "",
        claim_generation=malformed_claim.claim_generation,
        now=NOW + timedelta(seconds=1),
    )
    async with database.session_factory() as session:
        assert (await session.get(CapacityReservation, reservation.id)).released_at is None  # type: ignore[union-attr]

    # Create a new active reservation at the same fence as a destroy operation.
    later, later_worker = await _claimed(
        repository,
        operation="provision-equal",
        tenant="tenant-equal",
        cell="cell-equal",
        fence=7,
    )
    observed = _observation()
    equal_reservation = await authority.reserve(
        later,
        _request(
            operation="provision-equal", tenant="tenant-equal", cell="cell-equal", fence=7
        ),
        receipt=_receipt(observed),
        observation=observed,
        worker_id=later_worker,
        claim_token=later.claim_token or "",
        claim_generation=later.claim_generation,
        now=NOW,
    )
    await repository.mark_pending(
        later.id,
        later_worker,
        claim_token=later.claim_token or "",
        claim_generation=later.claim_generation,
        checkpoint="namespace-ready",
        retry_after_seconds=300,
        now=NOW,
    )
    destroy = await repository.submit(
        "destroy",
        "destroy-equal",
        _request(
            operation="destroy-equal", tenant="tenant-equal", cell="cell-equal", fence=7
        ),
    )
    destroy_claim = await repository.claim_next("destroy-equal-worker", now=NOW)
    assert destroy_claim is not None and destroy_claim.id == destroy.id
    with pytest.raises(StaleFence):
        await repository.complete(
            destroy.id,
            {
                "computeDestroyed": True,
                "storageDestroyed": True,
                "keysDestroyed": True,
                "tenantResourcesDestroyed": True,
            },
            worker_id="destroy-equal-worker",
            claim_token=destroy_claim.claim_token or "",
            claim_generation=destroy_claim.claim_generation,
            now=NOW,
        )
    async with database.session_factory() as session:
        row = await session.get(CapacityReservation, equal_reservation.id)
        assert row is not None and row.released_at is None
    assert (await repository.get_by_id(destroy.id)).state.name == "CLAIMED"  # type: ignore[union-attr]


def _contract(private_key: Ed25519PrivateKey) -> dict[str, object]:
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "schema_version": 1,
        "receipt_authentication": {
            "algorithm": "ed25519",
            "capacity_domain": "exomem.capacity-live-receipt.v1",
            "capacity_ttl_seconds": 300,
            "capacity_public_key_id": hashlib.sha256(raw).hexdigest(),
        },
        "limits": {
            "active_user_cells": 6,
            "active_recovery_cells": 2,
            "maximum_potential_attachments": 8,
            "provider_volume_attachment_limit": 16,
            "minimum_unused_provider_headroom": 8,
        },
    }


def _signed_receipt(
    private_key: Ed25519PrivateKey,
    contract: dict[str, object],
    *,
    users: int = 1,
    recovery: int = 1,
    attached: int = 2,
    cluster_uid: str = "11111111-1111-4111-8111-111111111111",
    server_id: int = 101,
    location: str = "fsn1",
    sequence: int = 1,
    observed_at: str = "2030-01-01T12:00:00Z",
    expires_at: str = "2030-01-01T12:05:00Z",
) -> str:
    unsigned = {
        "schema_version": 1,
        "issuer": "exomem-live-kubernetes-hcloud-v1",
        "contract_sha256": canonical_contract_digest(contract),
        "receipt_id": "22222222-2222-4222-8222-222222222222",
        "sequence": sequence,
        "cluster_uid": cluster_uid,
        "hcloud_server_id": server_id,
        "hcloud_location": location,
        "observed_at": observed_at,
        "expires_at": expires_at,
        "active_user_cells": users,
        "active_recovery_cells": recovery,
        "attached_volumes": attached,
    }
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    raw_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    receipt = {
        **unsigned,
        "authentication": {
            "algorithm": "ed25519",
            "key_id": hashlib.sha256(raw_key).hexdigest(),
            "signature": private_key.sign(
                b"exomem.capacity-live-receipt.v1\0" + canonical
            ).hex(),
        },
    }
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"))


def test_signed_receipt_binds_contract_cluster_server_location_counts_and_ttl() -> None:
    private_key = Ed25519PrivateKey.generate()
    contract = _contract(private_key)
    public_key = base64.urlsafe_b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode().rstrip("=")
    verifier = CapacityReceiptVerifier(
        contract=contract,
        public_key=public_key,
        expected_server_id=101,
        expected_location="fsn1",
    )
    observation = _observation(
        users={"user"}, recovery={"recovery"}, attached=2
    )
    verified = verifier.verify(
        _signed_receipt(private_key, contract), observation=observation, now=NOW
    )
    assert verified.sequence == 1

    mutations = (
        {"cluster_uid": "33333333-3333-4333-8333-333333333333"},
        {"server_id": 102},
        {"location": "hel1"},
        {"users": 2},
        {"recovery": 0},
        {"attached": 1},
        {"observed_at": "2030-01-01T12:00:00.000000Z"},
        {"expires_at": "2030-01-01T12:05:01Z"},
    )
    for values in mutations:
        with pytest.raises(CapacityReceiptError):
            verifier.verify(
                _signed_receipt(private_key, contract, **values),
                observation=observation,
                now=NOW,
            )

    forged = Ed25519PrivateKey.generate()
    with pytest.raises(CapacityReceiptError):
        verifier.verify(
            _signed_receipt(forged, contract), observation=observation, now=NOW
        )
    # Collector sequence restart is valid while the receipt remains independently fresh.
    assert verifier.verify(
        _signed_receipt(private_key, contract, sequence=1),
        observation=observation,
        now=NOW,
    ).sequence == 1


def _namespace(metadata: OpaqueProviderMetadata, mode: str):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=metadata.resource_name,
            uid=f"uid-{metadata.subject_id}",
            labels={"exomem.io/tenant-cell": "true"},
            annotations={
                **metadata.kubernetes_annotations,
                "exomem.io/resource-name": metadata.resource_name,
                "exomem.io/provision-mode": mode,
            },
        )
    )


@pytest.mark.asyncio
async def test_kubernetes_observer_classifies_modes_and_binds_exact_hcloud_node() -> None:
    serve = OpaqueProviderMetadata("tenant-one", "cell-one", "operation-one", 1)
    recovery = OpaqueProviderMetadata("tenant-two", "cell-two", "operation-two", 1)
    namespaces = [_namespace(serve, "serve"), _namespace(recovery, "restore-candidate")]
    pv = SimpleNamespace(
        metadata=SimpleNamespace(name="pv-one", uid="pv-uid-one"),
        spec=SimpleNamespace(
            csi=SimpleNamespace(driver="csi.hetzner.cloud", volume_handle="501"),
            claim_ref=SimpleNamespace(
                namespace=serve.resource_name,
                name=serve.resource_name + "-data",
                uid="pvc-uid-one",
            ),
        ),
    )
    pvc = SimpleNamespace(
        metadata=SimpleNamespace(
            name=serve.resource_name + "-data",
            namespace=serve.resource_name,
            uid="pvc-uid-one",
        ),
        spec=SimpleNamespace(volume_name="pv-one"),
    )
    attachment = SimpleNamespace(
        metadata=SimpleNamespace(name="va-one", uid="va-uid-one"),
        spec=SimpleNamespace(
            attacher="csi.hetzner.cloud",
            node_name="node-one",
            source=SimpleNamespace(persistent_volume_name="pv-one"),
        ),
        status=SimpleNamespace(attached=True),
    )

    class Core:
        def list_namespace(self, *, label_selector):
            assert label_selector == "exomem.io/tenant-cell=true"
            return SimpleNamespace(items=namespaces)

        def read_namespace(self, name):
            assert name == "kube-system"
            return SimpleNamespace(metadata=SimpleNamespace(uid="cluster-uid-0001"))

        def list_persistent_volume(self):
            return SimpleNamespace(items=[pv])

        def list_persistent_volume_claim_for_all_namespaces(self):
            return SimpleNamespace(items=[pvc])

        def list_node(self):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(name="node-one"),
                        spec=SimpleNamespace(provider_id="hcloud://101"),
                    )
                ]
            )

    class Storage:
        def list_volume_attachment(self):
            return SimpleNamespace(items=[attachment])

    observation = await KubernetesCapacityObserver(
        core_v1=Core(),
        storage_v1=Storage(),
        expected_server_id=101,
        expected_location="fsn1",
        now=lambda: NOW,
    ).observe()
    assert observation.user_resource_names == {serve.resource_name}
    assert observation.recovery_resource_names == {recovery.resource_name}
    assert observation.attached_hcloud_volumes == 1
    assert not observation.orphan_attachment_ids

    attachment.spec.node_name = "wrong-node"
    with pytest.raises(CapacityReceiptError):
        await KubernetesCapacityObserver(
            core_v1=Core(),
            storage_v1=Storage(),
            expected_server_id=101,
            expected_location="fsn1",
            now=lambda: NOW,
        ).observe()


@pytest.mark.asyncio
async def test_kubernetes_observer_rejects_unknown_duplicate_or_malformed_identity() -> None:
    metadata = OpaqueProviderMetadata("tenant-one", "cell-one", "operation-one", 1)
    namespace = _namespace(metadata, "unknown")

    class Core:
        def list_namespace(self, *, label_selector):
            return SimpleNamespace(items=[namespace, namespace])

        def read_namespace(self, name):
            return SimpleNamespace(metadata=SimpleNamespace(uid="cluster-uid-0001"))

        def list_persistent_volume(self):
            return SimpleNamespace(items=[])

        def list_persistent_volume_claim_for_all_namespaces(self):
            return SimpleNamespace(items=[])

        def list_node(self):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(name="node-one"),
                        spec=SimpleNamespace(provider_id="hcloud://101"),
                    )
                ]
            )

    class Storage:
        def list_volume_attachment(self):
            return SimpleNamespace(items=[])

    observer = KubernetesCapacityObserver(
        core_v1=Core(),
        storage_v1=Storage(),
        expected_server_id=101,
        expected_location="fsn1",
        now=lambda: NOW,
    )
    with pytest.raises(CapacityReceiptError):
        await observer.observe()
    namespace.metadata.annotations["exomem.io/provision-mode"] = "serve"
    with pytest.raises(CapacityReceiptError):
        await observer.observe()
    namespace.metadata.annotations["exomem.io/resource-name"] = "forged"
    with pytest.raises(CapacityReceiptError):
        await observer.observe()


def test_capacity_model_enums_are_exact() -> None:
    assert CapacityReservationClass.USER.value == "USER"
    assert CapacityReservationClass.RECOVERY.value == "RECOVERY"
    assert OperationAction.PROVISION.value == "provision"
