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
    LiveCapacityAdmission,
    VerifiedCapacityReceipt,
    canonical_contract_digest,
)
from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.lifecycle import OpaqueProviderMetadata
from exomem_provisioner.models import (
    CapacityDestructiveFence,
    CapacityLedger,
    CapacityReservation,
    CapacityReservationClass,
    OperationAction,
)
from exomem_provisioner.repository import (
    ImmutableMetadataConflict,
    OperationRepository,
    StaleFence,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)

_NAMESPACE_OWNED_MARKERS = (
    *(("labels", marker) for marker in (
        "exomem.io/tenant-cell",
        "exomem.io/cell-resource",
    )),
    *(("annotations", marker) for marker in (
        "exomem.io/tenant-id",
        "exomem.io/cell-id",
        "exomem.io/operation-id",
        "exomem.io/tenant-digest",
        "exomem.io/subject-digest",
        "exomem.io/operation-digest",
        "exomem.io/fence",
        "exomem.io/recovery-envelope",
        "exomem.io/resource-name",
        "exomem.io/pvc-name",
        "exomem.io/credentials-secret-name",
        "exomem.io/init-request-configmap-name",
        "exomem.io/provision-mode",
        "exomem.io/vault-id",
        "exomem.io/expected-release",
        "exomem.io/worker-policy-digest",
        "exomem.io/browser-origin",
        "exomem.io/transfer-hostname",
        "exomem.io/runtime-admitted",
    )),
)


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
async def test_idempotent_reservation_rejects_observed_class_disagreement(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    _, repository, authority = capacity_context
    claimed, worker, reservation = await _reserve(
        repository,
        authority,
        operation="operation-class",
        tenant="tenant-class",
        cell="cell-class",
        fence=1,
    )
    opposite = _observation(recovery={reservation.resource_name})
    with pytest.raises(CapacityBlocked) as mismatch:
        await authority.reserve(
            claimed,
            _request(
                operation="operation-class",
                tenant="tenant-class",
                cell="cell-class",
                fence=1,
            ),
            receipt=_receipt(opposite),
            observation=opposite,
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    assert mismatch.value.reason == "capacity-live-observation-mismatch"

    dual = _observation(
        users={reservation.resource_name}, recovery={reservation.resource_name}
    )
    with pytest.raises(CapacityBlocked) as ambiguous:
        await authority.reserve(
            claimed,
            _request(
                operation="operation-class",
                tenant="tenant-class",
                cell="cell-class",
                fence=1,
            ),
            receipt=_receipt(dual),
            observation=dual,
            worker_id=worker,
            claim_token=claimed.claim_token or "",
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    assert ambiguous.value.reason == "capacity-live-observation-mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant", "cell"),
    [
        ("tenant-conflict", "cell-conflict"),
        ("tenant-other", "cell-conflict"),
    ],
)
async def test_active_capacity_identity_conflict_is_detected_before_flush(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
    tenant: str,
    cell: str,
) -> None:
    _, repository, authority = capacity_context
    existing, existing_worker, _ = await _reserve(
        repository,
        authority,
        operation="operation-existing-conflict",
        tenant="tenant-conflict",
        cell="cell-conflict",
        fence=1,
    )
    await repository.mark_pending(
        existing.id,
        existing_worker,
        claim_token=existing.claim_token or "",
        claim_generation=existing.claim_generation,
        checkpoint="namespace-ready",
        retry_after_seconds=300,
        now=NOW,
    )
    contender, worker = await _claimed(
        repository,
        operation=f"operation-contender-{tenant}",
        tenant=tenant,
        cell=cell,
        fence=2,
    )
    observation = _observation()
    with pytest.raises(CapacityConflict):
        await authority.reserve(
            contender,
            _request(
                operation=f"operation-contender-{tenant}",
                tenant=tenant,
                cell=cell,
                fence=2,
            ),
            receipt=_receipt(observation),
            observation=observation,
            worker_id=worker,
            claim_token=contender.claim_token or "",
            claim_generation=contender.claim_generation,
            now=NOW,
        )


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
async def test_discard_rejects_tampered_reservation_resource_before_finalize(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, authority = capacity_context
    provision, provision_worker, reservation = await _reserve(
        repository,
        authority,
        operation="provision-tampered-resource",
        tenant="tenant-tampered-resource",
        cell="cell-tampered-resource",
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
    async with database.session_factory.begin() as session:
        row = await session.get(CapacityReservation, reservation.id)
        assert row is not None
        row.resource_name = "exo-00000000000000000000"

    discard = await repository.submit(
        "discard",
        "discard-tampered-resource",
        _request(
            operation="discard-tampered-resource",
            tenant="tenant-tampered-resource",
            cell="cell-tampered-resource",
            fence=2,
        ),
    )
    claimed = await repository.claim_next(
        "discard-tampered-worker",
        now=NOW,
        allowed_actions=frozenset({OperationAction.DISCARD}),
    )
    assert claimed is not None and claimed.id == discard.id and claimed.claim_token
    with pytest.raises(ImmutableMetadataConflict, match="resource"):
        await repository.complete(
            discard.id,
            {"computeDestroyed": True, "storageDestroyed": True, "keysDestroyed": True},
            worker_id="discard-tampered-worker",
            claim_token=claimed.claim_token,
            claim_generation=claimed.claim_generation,
            now=NOW,
        )
    async with database.session_factory() as session:
        row = await session.get(CapacityReservation, reservation.id)
        ledger = await session.get(CapacityLedger, 1)
        assert row is not None and row.released_at is None
        assert ledger is not None and ledger.revision == 1
    assert (await repository.get_by_id(discard.id)).state.name == "CLAIMED"  # type: ignore[union-attr]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "proof"),
    [
        (
            "discard",
            {"computeDestroyed": True, "storageDestroyed": True, "keysDestroyed": True},
        ),
        (
            "destroy",
            {
                "computeDestroyed": True,
                "storageDestroyed": True,
                "keysDestroyed": True,
                "tenantResourcesDestroyed": True,
            },
        ),
    ],
)
async def test_destructive_completion_without_reservation_blocks_equal_fence_resurrection(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
    action: str,
    proof: dict[str, bool],
) -> None:
    database, repository, authority = capacity_context
    operation_id = f"operation-{action}-first"
    destructive = await repository.submit(
        action,
        f"key-{action}-first",
        _request(
            operation=operation_id,
            tenant="tenant-destroy-first",
            cell="cell-destroy-first",
            fence=4,
        ),
    )
    claimed = await repository.claim_next(
        f"worker-{action}-first",
        now=NOW,
        allowed_actions=frozenset({OperationAction(action)}),
    )
    assert claimed is not None and claimed.id == destructive.id and claimed.claim_token
    await repository.complete(
        claimed.id,
        proof,
        worker_id=f"worker-{action}-first",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        now=NOW,
    )

    equal, worker = await _claimed(
        repository,
        operation=f"operation-provision-after-{action}",
        tenant="tenant-destroy-first",
        cell=(
            "cell-destroy-first"
            if action == "discard"
            else "cell-after-tenant-destroy"
        ),
        fence=4,
    )
    observation = _observation()
    with pytest.raises(CapacityConflict):
        await authority.reserve(
            equal,
            _request(
                operation=f"operation-provision-after-{action}",
                tenant="tenant-destroy-first",
                cell=(
                    "cell-destroy-first"
                    if action == "discard"
                    else "cell-after-tenant-destroy"
                ),
                fence=4,
            ),
            receipt=_receipt(observation),
            observation=observation,
            worker_id=worker,
            claim_token=equal.claim_token or "",
            claim_generation=equal.claim_generation,
            now=NOW,
        )

    async with database.session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(CapacityDestructiveFence)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_malformed_destructive_proof_writes_no_fence_and_allows_same_fence_provision(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, authority = capacity_context
    destructive = await repository.submit(
        "discard",
        "key-malformed-first",
        _request(
            operation="operation-malformed-first",
            tenant="tenant-malformed-first",
            cell="cell-malformed-first",
            fence=4,
        ),
    )
    claimed = await repository.claim_next(
        "worker-malformed-first",
        now=NOW,
        allowed_actions=frozenset({OperationAction.DISCARD}),
    )
    assert claimed is not None and claimed.id == destructive.id and claimed.claim_token
    await repository.complete(
        claimed.id,
        {
            "computeDestroyed": True,
            "storageDestroyed": True,
            "keysDestroyed": True,
            "callerProof": True,
        },
        worker_id="worker-malformed-first",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        now=NOW,
    )
    provision, worker = await _claimed(
        repository,
        operation="operation-after-malformed",
        tenant="tenant-malformed-first",
        cell="cell-malformed-first",
        fence=4,
    )
    observation = _observation()
    reserved = await authority.reserve(
        provision,
        _request(
            operation="operation-after-malformed",
            tenant="tenant-malformed-first",
            cell="cell-malformed-first",
            fence=4,
        ),
        receipt=_receipt(observation),
        observation=observation,
        worker_id=worker,
        claim_token=provision.claim_token or "",
        claim_generation=provision.claim_generation,
        now=NOW,
    )
    assert reserved.fence_generation == 4
    async with database.session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(CapacityDestructiveFence)
            )
            == 0
        )


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
            labels={
                "exomem.io/tenant-cell": "true",
                "exomem.io/cell-resource": metadata.resource_name,
            },
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
        def list_namespace(self):
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
        def list_namespace(self):
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


@pytest.mark.asyncio
async def test_kubernetes_observer_lists_broadly_and_ignores_only_unrelated_namespaces() -> None:
    identity = OpaqueProviderMetadata("tenant-broad", "cell-broad", "operation-broad", 1)
    valid = _namespace(identity, "serve")
    unrelated = SimpleNamespace(
        metadata=SimpleNamespace(name="kube-public", labels={}, annotations={})
    )
    returned = [valid, unrelated]

    class Core:
        def list_namespace(self):
            return SimpleNamespace(items=returned)

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
    observation = await observer.observe()
    assert observation.user_resource_names == {identity.resource_name}

    valid.metadata.labels.pop("exomem.io/tenant-cell")
    with pytest.raises(CapacityReceiptError, match="namespace identity"):
        await observer.observe()

    returned[:] = [
        SimpleNamespace(
            metadata=SimpleNamespace(
                name="ordinary-namespace",
                labels={"exomem.io/tenant-cell": "false"},
                annotations={},
            )
        )
    ]
    with pytest.raises(CapacityReceiptError, match="namespace identity"):
        await observer.observe()


@pytest.mark.asyncio
@pytest.mark.parametrize(("section", "marker"), _NAMESPACE_OWNED_MARKERS)
async def test_kubernetes_observer_rejects_each_incomplete_owned_namespace_marker(
    section: str,
    marker: str,
) -> None:
    metadata = SimpleNamespace(
        name="ordinary-namespace",
        labels={},
        annotations={},
    )
    getattr(metadata, section)[marker] = "owned-marker-sentinel"

    class Core:
        def list_namespace(self):
            return SimpleNamespace(items=[SimpleNamespace(metadata=metadata)])

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

    with pytest.raises(CapacityReceiptError, match="namespace identity"):
        await KubernetesCapacityObserver(
            core_v1=Core(),
            storage_v1=Storage(),
            expected_server_id=101,
            expected_location="fsn1",
            now=lambda: NOW,
        ).observe()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_pv", "include_pvc", "include_namespace", "clean_orphan"),
    [
        (False, False, False, True),
        (False, True, False, False),
        (True, False, True, True),
        (True, True, False, True),
    ],
)
async def test_kubernetes_observer_counts_only_clean_missing_object_combinations_as_orphans(
    include_pv: bool,
    include_pvc: bool,
    include_namespace: bool,
    clean_orphan: bool,
) -> None:
    identity = OpaqueProviderMetadata("tenant-orphan", "cell-orphan", "operation-orphan", 1)
    namespace = _namespace(identity, "serve")
    pv = SimpleNamespace(
        metadata=SimpleNamespace(name="pv-orphan", uid="pv-uid-orphan"),
        spec=SimpleNamespace(
            csi=SimpleNamespace(driver="csi.hetzner.cloud", volume_handle="601"),
            claim_ref=SimpleNamespace(
                namespace=identity.resource_name,
                name=identity.resource_name + "-data",
                uid="pvc-uid-orphan",
            ),
        ),
    )
    pvc = SimpleNamespace(
        metadata=SimpleNamespace(
            name=identity.resource_name + "-data",
            namespace=identity.resource_name,
            uid="pvc-uid-orphan",
        ),
        spec=SimpleNamespace(volume_name="pv-orphan"),
    )
    attachment = SimpleNamespace(
        metadata=SimpleNamespace(name="va-orphan", uid="va-uid-orphan"),
        spec=SimpleNamespace(
            attacher="csi.hetzner.cloud",
            node_name="node-one",
            source=SimpleNamespace(persistent_volume_name="pv-orphan"),
        ),
        status=SimpleNamespace(attached=True),
    )

    class Core:
        def list_namespace(self):
            return SimpleNamespace(items=[namespace] if include_namespace else [])

        def read_namespace(self, name):
            return SimpleNamespace(metadata=SimpleNamespace(uid="cluster-uid-0001"))

        def list_persistent_volume(self):
            return SimpleNamespace(items=[pv] if include_pv else [])

        def list_persistent_volume_claim_for_all_namespaces(self):
            return SimpleNamespace(items=[pvc] if include_pvc else [])

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

    observer = KubernetesCapacityObserver(
        core_v1=Core(),
        storage_v1=Storage(),
        expected_server_id=101,
        expected_location="fsn1",
        now=lambda: NOW,
    )
    if not clean_orphan:
        with pytest.raises(CapacityReceiptError, match="PVC ownership"):
            await observer.observe()
        return
    observation = await observer.observe()
    assert observation.attached_hcloud_volumes == 1
    assert len(observation.orphan_attachment_ids) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("pvc_uid", "volume_name"), [("wrong-uid", "pv-one"), ("pvc-uid-one", "wrong-pv")])
async def test_kubernetes_observer_rejects_contradictory_present_pvc_without_namespace(
    pvc_uid: str,
    volume_name: str,
) -> None:
    identity = OpaqueProviderMetadata("tenant-pvc", "cell-pvc", "operation-pvc", 1)
    pv = SimpleNamespace(
        metadata=SimpleNamespace(name="pv-one", uid="pv-uid-one"),
        spec=SimpleNamespace(
            csi=SimpleNamespace(driver="csi.hetzner.cloud", volume_handle="701"),
            claim_ref=SimpleNamespace(
                namespace=identity.resource_name,
                name=identity.resource_name + "-data",
                uid="pvc-uid-one",
            ),
        ),
    )
    pvc = SimpleNamespace(
        metadata=SimpleNamespace(
            name=identity.resource_name + "-data",
            namespace=identity.resource_name,
            uid=pvc_uid,
        ),
        spec=SimpleNamespace(volume_name=volume_name),
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
        def list_namespace(self):
            return SimpleNamespace(items=[])

        def read_namespace(self, name):
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

    with pytest.raises(CapacityReceiptError, match="PVC ownership"):
        await KubernetesCapacityObserver(
            core_v1=Core(),
            storage_v1=Storage(),
            expected_server_id=101,
            expected_location="fsn1",
            now=lambda: NOW,
        ).observe()


@pytest.mark.asyncio
async def test_live_admission_converts_transient_kubernetes_read_error_to_pending(
    capacity_context: tuple[
        ProvisionerDatabase, OperationRepository, CapacityReservationAuthority
    ],
) -> None:
    database, repository, _ = capacity_context
    private_key = Ed25519PrivateKey.generate()
    contract = _contract(private_key)
    public_key = base64.urlsafe_b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode().rstrip("=")

    class Core:
        def list_namespace(self):
            raise RuntimeError("transient Kubernetes read failure")

        def read_namespace(self, name):
            return SimpleNamespace(metadata=SimpleNamespace(uid="cluster-uid-0001"))

        def list_persistent_volume(self):
            return SimpleNamespace(items=[])

        def list_persistent_volume_claim_for_all_namespaces(self):
            return SimpleNamespace(items=[])

        def list_node(self):
            return SimpleNamespace(items=[])

        def read_namespaced_config_map(self, name, namespace):
            raise AssertionError("receipt must not be read after observation failure")

    class Storage:
        def list_volume_attachment(self):
            return SimpleNamespace(items=[])

    claimed, worker = await _claimed(
        repository,
        operation="operation-transient-observer",
        tenant="tenant-transient-observer",
        cell="cell-transient-observer",
        fence=1,
    )
    admission = LiveCapacityAdmission(
        core_v1=Core(),
        storage_v1=Storage(),
        sessions=database.session_factory,
        contract=contract,
        public_key=public_key,
        receipt_namespace="exomem-platform",
        receipt_config_map="capacity-receipt",
        expected_server_id=101,
        expected_location="fsn1",
        now=lambda: NOW,
    )
    reason = await admission.admit(
        claimed,
        _request(
            operation="operation-transient-observer",
            tenant="tenant-transient-observer",
            cell="cell-transient-observer",
            fence=1,
        ),
        worker_id=worker,
        claim_token=claimed.claim_token or "",
        claim_generation=claimed.claim_generation,
        provider_operation_id=claimed.external_operation_id,
        provider_fence_generation=claimed.fence_generation,
        now=NOW,
    )
    assert reason == "capacity-live-observation-mismatch"


def test_capacity_model_enums_are_exact() -> None:
    assert CapacityReservationClass.USER.value == "USER"
    assert CapacityReservationClass.RECOVERY.value == "RECOVERY"
    assert OperationAction.PROVISION.value == "provision"
