from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_repository import repository as repository
from test_worker import _v2_request

from exomem_provisioner.driver import DriverPending, FakeDriver
from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
from exomem_provisioner.lifecycle import CellLifecycleDriver
from exomem_provisioner.models import Operation, OperationAction, OperationState
from exomem_provisioner.repository import ClaimConflict, _acquire_cell_operation_lock
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2
from exomem_provisioner.worker import ProvisionerWorker

NOW = datetime(2030, 1, 1, tzinfo=UTC)
CHECKPOINT = MigrationCheckpoint("inspect", "a" * 64, "A" * 43).encode()


async def _submit(repository, action="rollforward", **changes):
    request = _v2_request(**changes)
    return await repository.submit(
        action, action + str(request["operationId"]), request, wire_protocol=WIRE_PROTOCOL_V2
    )


async def _pending(repository, claim, *, checkpoint=CHECKPOINT, now=NOW):
    return await repository.mark_pending(
        claim.id,
        "migration-worker",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        checkpoint=checkpoint,
        retry_after_seconds=1,
        now=now,
    )


@pytest.mark.parametrize(
    "state", [OperationState.PENDING, OperationState.CLAIMED, OperationState.ERROR]
)
@pytest.mark.parametrize("action", ["resume", "destroy", "restore"])
async def test_durable_migration_blocks_successors_after_worker_lease_expires(
    repository, state, action
):
    migration = await _submit(repository)
    claim = await repository.claim_next("migration-worker", now=NOW)
    await _pending(repository, claim)
    async with repository.session_factory.begin() as session:
        stored = await session.get(Operation, migration.id)
        stored.state = state
        if state is OperationState.CLAIMED:
            stored.claim_owner = "lost-worker"
            stored.claim_token = "lost-claim"
            stored.claim_expires_at = NOW
    successor = await _submit(
        repository,
        action,
        operationId="successor",
        fenceGeneration=8,
        **({"cellId": None} if action == "destroy" else {}),
    )
    assert await repository.claim_next("successor-worker", now=NOW + timedelta(hours=1)) is None
    assert (await repository.get_by_id(successor.id)).state is OperationState.PENDING


async def test_barrier_does_not_starve_an_unrelated_cell_or_its_own_retry(repository):
    migration = await _submit(repository)
    claim = await repository.claim_next("migration-worker", now=NOW)
    await _pending(repository, claim)
    await _submit(repository, "resume", operationId="blocked-successor")
    unrelated = await _submit(repository, "resume", operationId="unrelated", cellId="another-cell")
    acquired = await repository.claim_next(
        "other-worker",
        now=NOW + timedelta(seconds=2),
        allowed_actions=frozenset({OperationAction.RESUME}),
    )
    assert acquired is not None and acquired.id == unrelated.id
    retry = await repository.claim_next(
        "migration-worker",
        now=NOW + timedelta(seconds=2),
        allowed_actions=frozenset({OperationAction.ROLLFORWARD}),
    )
    assert retry is not None and retry.id == migration.id


async def test_barrier_uses_internal_identity_not_reused_provider_operation_id(repository):
    await _submit(repository)
    claim = await repository.claim_next("migration-worker", now=NOW)
    await _pending(repository, claim)
    await _submit(repository, "resume")
    assert (
        await repository.claim_next(
            "other-worker",
            now=NOW + timedelta(seconds=2),
            allowed_actions=frozenset({OperationAction.RESUME}),
        )
        is None
    )


@pytest.mark.parametrize(
    "state", [OperationState.PENDING, OperationState.CLAIMED, OperationState.ERROR]
)
async def test_first_migration_checkpoint_refuses_nonfinal_tenant_destruction(repository, state):
    migration = await _submit(repository)
    claim = await repository.claim_next("migration-worker", now=NOW)
    destruction = await _submit(repository, "destroy", operationId="destroy-tenant", cellId=None)
    async with repository.session_factory.begin() as session:
        stored = await session.get(Operation, destruction.id)
        stored.state = state
    before = await repository.get_by_id(migration.id)
    with pytest.raises(ClaimConflict):
        await _pending(repository, claim)
    assert await repository.get_by_id(migration.id) == before


async def test_active_claim_guard_rereads_durable_barrier(repository):
    active = await _submit(repository, "resume", operationId="already-claimed")
    claim = await repository.claim_next("migration-worker", now=NOW)
    migration = await _submit(repository)
    # Model restored durable progress arriving after an older worker acquired
    # its lease. A fresh effect guard must not trust the earlier claim snapshot.
    async with repository.session_factory.begin() as session:
        stored = await session.get(Operation, migration.id)
        stored.checkpoint = CHECKPOINT
        stored.state = OperationState.ERROR
    with pytest.raises(ClaimConflict):
        await repository.assert_active_claim(
            active.id,
            "migration-worker",
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            now=NOW,
        )


async def test_first_barrier_refuses_foreign_claim_with_reused_provider_identity(repository):
    await _submit(repository)
    migration_claim = await repository.claim_next("migration-worker", now=NOW)
    await _submit(repository, "resume")
    other = await repository.claim_next(
        "other-worker",
        now=NOW + timedelta(seconds=1),
        allowed_actions=frozenset({OperationAction.RESUME}),
    )
    assert other is not None
    with pytest.raises(ClaimConflict):
        await _pending(repository, migration_claim, now=NOW + timedelta(seconds=2))
    assert (await repository.get_by_id(migration_claim.id)).checkpoint == "effect-prepared"


@pytest.mark.parametrize("action", ["resume", "destroy"])
async def test_lock_acquisition_rechecks_barrier_after_candidate_selection(repository, action):
    candidate = await _submit(
        repository,
        action,
        operationId="candidate",
        **({"cellId": None} if action == "destroy" else {}),
    )
    migration = await _submit(repository)
    async with repository.session_factory.begin() as session:
        marker = await session.get(Operation, migration.id)
        marker.checkpoint = "gm1:unparsed-recovery-evidence"
        marker.state = OperationState.ERROR
        await session.flush()
        operation = await session.get(Operation, candidate.id)
        assert not await _acquire_cell_operation_lock(
            session, operation, checked_at=NOW, lease_expires_at=NOW + timedelta(seconds=30)
        )


async def test_final_migration_does_not_hold_successor_barrier(repository):
    migration = await _submit(repository)
    async with repository.session_factory.begin() as session:
        stored = await session.get(Operation, migration.id)
        stored.checkpoint = CHECKPOINT
        stored.state = OperationState.FINAL
    successor = await _submit(repository, "resume", operationId="successor")
    claim = await repository.claim_next("next-worker", now=NOW)
    assert claim is not None and claim.id == successor.id


async def test_higher_fence_and_delayed_old_create_never_dispatch_successor_effects(repository):
    await _submit(repository)
    first = await repository.claim_next("migration-worker", now=NOW)
    await _pending(repository, first)
    old = await repository.claim_next("migration-worker", now=NOW + timedelta(seconds=2))
    await repository.assert_active_claim(
        old.id,
        "migration-worker",
        claim_token=old.claim_token,
        claim_generation=old.claim_generation,
        now=NOW + timedelta(seconds=2),
    )
    jobs = []
    assert not jobs  # Provider observation can see absence before the late create.
    await _submit(repository, "resume", operationId="successor", fenceGeneration=8)
    jobs.append("delayed-old-migration-job")

    class NoEffects(FakeDriver):
        async def execute(self, *args, **kwargs):
            pytest.fail("successor dispatched an effect across the durable migration barrier")

    worker = ProvisionerWorker(
        repository,
        NoEffects(),
        worker_id="next-worker",
        allowed_actions=frozenset({OperationAction.RESUME}),
    )
    assert not await worker.run_once(now=NOW + timedelta(hours=1))
    assert jobs == ["delayed-old-migration-job"]


async def test_pending_provider_barrier_does_not_consume_worker_failure_budget(repository):
    operation = await _submit(repository, "resume", operationId="waiting-operation")

    async def fence(*args):
        return 0

    async def observe(context, request):
        return DriverPending(context.checkpoint, 30)

    driver = CellLifecycleDriver(
        plane=SimpleNamespace(observed_fence=fence, observe_operation=observe),
        volume_worker=None,
        config=SimpleNamespace(matches_runtime_request=lambda *args, **kwargs: True),
    )
    worker = ProvisionerWorker(
        repository,
        driver,
        worker_id="waiting-worker",
        allowed_actions=frozenset({OperationAction.RESUME}),
    )
    for attempt in range(repository.max_failure_attempts + 2):
        assert await worker.run_once(now=NOW + timedelta(seconds=31 * attempt))
    waiting = await repository.get_by_id(operation.id)
    assert waiting.state is OperationState.PENDING
    assert waiting.checkpoint == "effect-prepared"
    assert waiting.progress.get("failure_attempts", 0) == 0
    assert waiting.error_code is None


async def test_final_ack_crash_retains_barrier_until_result_is_atomically_final(repository):
    migration = await _submit(repository)
    initial = await repository.claim_next("migration-worker", now=NOW)
    checkpoint = MigrationCheckpoint("confirmed", "a" * 64, "A" * 43, "b" * 64, "c" * 64).encode()
    await _pending(repository, initial, checkpoint=checkpoint)
    claim = await repository.claim_next("migration-worker", now=NOW + timedelta(seconds=2))
    await repository.checkpoint_effect_applied(
        migration.id,
        worker_id="migration-worker",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        now=NOW + timedelta(seconds=2),
    )
    interrupted = await repository.get_by_id(migration.id)
    assert interrupted.state is OperationState.CLAIMED
    assert interrupted.checkpoint == checkpoint
    successor = await _submit(repository, "resume", operationId="next-operation")
    later = NOW + timedelta(hours=1)
    assert (
        await repository.claim_next(
            "next-worker", now=later, allowed_actions=frozenset({OperationAction.RESUME})
        )
        is None
    )
    resumed = await repository.claim_next(
        "migration-worker", now=later, allowed_actions=frozenset({OperationAction.ROLLFORWARD})
    )
    assert resumed is not None and resumed.checkpoint == checkpoint
    final = await repository.complete(
        migration.id,
        {},
        worker_id="migration-worker",
        claim_token=resumed.claim_token,
        claim_generation=resumed.claim_generation,
        now=later,
    )
    assert final.state is OperationState.FINAL and final.checkpoint == "complete"
    next_claim = await repository.claim_next("next-worker", now=later)
    assert next_claim is not None and next_claim.id == successor.id
