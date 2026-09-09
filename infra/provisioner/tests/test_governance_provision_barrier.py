from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_governance_migration_barrier import postgresql17 as postgresql17
from test_governance_migration_barrier import postgresql_repository as postgresql_repository
from test_governance_migration_barrier import repository as repository
from test_governance_migration_barrier import sqlite_repository as sqlite_repository
from test_worker import _v2_request

from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
from exomem_provisioner.models import Operation, OperationAction, OperationState
from exomem_provisioner.repository import ClaimConflict
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V1, WIRE_PROTOCOL_V2

NOW = datetime(2030, 1, 1, tzinfo=UTC)
GPI1 = "gpi1:initializing:" + "A" * 43
GM1 = MigrationCheckpoint("inspect", "a" * 64, "A" * 43).encode()


@pytest.mark.parametrize("checkpoint", [GPI1, GM1])
async def test_generic_pending_cannot_erase_governance_barrier(repository, checkpoint):
    operation = await _submit(repository)
    await _mark(repository, operation, state=OperationState.PENDING, checkpoint=checkpoint)
    claim = await repository.claim_next("worker", now=NOW)
    pending = await repository.mark_pending(
        operation.id, "worker", claim_token=claim.claim_token,
        claim_generation=claim.claim_generation, checkpoint="capacity-pending",
        retry_after_seconds=300, now=NOW,
    )
    assert pending.checkpoint == checkpoint
    assert pending.retry_after_seconds == 300
    assert await repository.claim_next("worker", now=NOW + timedelta(seconds=299)) is None
    assert await repository.claim_next("worker", now=NOW + timedelta(seconds=300)) is not None


async def test_migration_cannot_return_to_storage_initialization(repository):
    operation = await _submit(repository)
    await _mark(repository, operation, state=OperationState.PENDING, checkpoint=GM1)
    claim = await repository.claim_next("worker", now=NOW)
    with pytest.raises(ClaimConflict):
        await repository.mark_pending(
            operation.id, "worker", claim_token=claim.claim_token,
            claim_generation=claim.claim_generation, checkpoint=GPI1,
            retry_after_seconds=1, now=NOW,
        )
    assert (await repository.get_by_id(operation.id)).checkpoint == GM1


async def _submit(repository, action="provision", *, wire_protocol=WIRE_PROTOCOL_V2, **changes):
    request = _v2_request(**changes)
    return await repository.submit(
        action,
        action + str(request["operationId"]),
        request,
        wire_protocol=wire_protocol,
    )


async def _mark(repository, operation, *, state, checkpoint=GPI1):
    async with repository.session_factory.begin() as session:
        stored = await session.get(Operation, operation.id)
        stored.checkpoint = checkpoint
        stored.state = state
        if state is OperationState.CLAIMED:
            stored.claim_owner = "lost-worker"
            stored.claim_token = "lost-claim"
            stored.claim_expires_at = NOW


@pytest.mark.parametrize(
    "state", [OperationState.PENDING, OperationState.CLAIMED, OperationState.ERROR]
)
async def test_durable_provision_checkpoint_blocks_same_cell_successors(repository, state):
    provision = await _submit(repository, operationId="provision")
    await _mark(repository, provision, state=state)
    await _submit(repository, "resume", operationId="successor")

    assert (
        await repository.claim_next(
            "successor-worker",
            now=NOW + timedelta(hours=1),
            allowed_actions=frozenset({OperationAction.RESUME}),
        )
        is None
    )


async def test_provision_checkpoint_allows_unrelated_cell_and_its_own_retry(repository):
    provision = await _submit(repository, operationId="provision")
    await _mark(repository, provision, state=OperationState.PENDING)
    await _submit(repository, "resume", operationId="blocked")
    unrelated = await _submit(repository, "resume", operationId="unrelated", cellId="another-cell")

    acquired = await repository.claim_next(
        "other-worker",
        now=NOW,
        allowed_actions=frozenset({OperationAction.RESUME}),
    )
    assert acquired is not None and acquired.id == unrelated.id
    retry = await repository.claim_next(
        "provision-worker",
        now=NOW,
        allowed_actions=frozenset({OperationAction.PROVISION}),
    )
    assert retry is not None and retry.id == provision.id


@pytest.mark.parametrize("action", ["resume", "destroy"])
async def test_first_provision_checkpoint_refuses_overlapping_operation(repository, action):
    provision = await _submit(repository, operationId="provision")
    claim = await repository.claim_next(
        "provision-worker", now=NOW, allowed_actions=frozenset({OperationAction.PROVISION})
    )
    assert claim is not None and claim.id == provision.id
    overlap = await _submit(
        repository,
        action,
        operationId="overlap",
        **({"cellId": None} if action == "destroy" else {}),
    )
    if action == "resume":
        await _mark(repository, overlap, state=OperationState.CLAIMED, checkpoint="effect-prepared")

    with pytest.raises(ClaimConflict):
        await repository.mark_pending(
            provision.id,
            "provision-worker",
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            checkpoint=GPI1,
            retry_after_seconds=1,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("action", "wire_protocol"),
    [
        ("provision", WIRE_PROTOCOL_V1),
        ("rollforward", WIRE_PROTOCOL_V2),
        ("resume", WIRE_PROTOCOL_V2),
    ],
)
async def test_gpi1_only_fences_v2_provision(repository, action, wire_protocol):
    marker = await _submit(
        repository,
        action,
        operationId="non-barrier",
        wire_protocol=wire_protocol,
    )
    await _mark(repository, marker, state=OperationState.ERROR)
    successor = await _submit(repository, "resume", operationId="successor")

    claim = await repository.claim_next(
        "successor-worker",
        now=NOW,
        allowed_actions=frozenset({OperationAction.RESUME}),
    )
    assert claim is not None and claim.id == successor.id


async def test_gpi1_to_gm1_transition_and_terminal_failure_retain_barrier(repository):
    provision = await _submit(repository, operationId="provision")
    initial = await repository.claim_next(
        "provision-worker", now=NOW, allowed_actions=frozenset({OperationAction.PROVISION})
    )
    await repository.mark_pending(
        provision.id,
        "provision-worker",
        claim_token=initial.claim_token,
        claim_generation=initial.claim_generation,
        checkpoint=GPI1,
        retry_after_seconds=1,
        now=NOW,
    )
    retry = await repository.claim_next(
        "provision-worker",
        now=NOW + timedelta(seconds=2),
        allowed_actions=frozenset({OperationAction.PROVISION}),
    )
    await repository.mark_pending(
        provision.id,
        "provision-worker",
        claim_token=retry.claim_token,
        claim_generation=retry.claim_generation,
        checkpoint=GM1,
        retry_after_seconds=1,
        now=NOW + timedelta(seconds=2),
    )
    successor = await _submit(repository, "resume", operationId="successor")
    assert (
        await repository.claim_next(
            "successor-worker",
            now=NOW + timedelta(seconds=3),
            allowed_actions=frozenset({OperationAction.RESUME}),
        )
        is None
    )

    terminal = await repository.claim_next(
        "provision-worker",
        now=NOW + timedelta(seconds=4),
        allowed_actions=frozenset({OperationAction.PROVISION}),
    )
    failed = await repository.fail(
        provision.id,
        "provision-worker",
        claim_token=terminal.claim_token,
        claim_generation=terminal.claim_generation,
        code="PROVISIONER_GOVERNANCE_PROVISION_UNAVAILABLE",
        now=NOW + timedelta(seconds=4),
    )
    assert failed.checkpoint == GM1
    assert (
        await repository.claim_next(
            "successor-worker",
            now=NOW + timedelta(days=1),
            allowed_actions=frozenset({OperationAction.RESUME}),
        )
        is None
    )
    assert (await repository.get_by_id(successor.id)).state is OperationState.PENDING


@pytest.mark.parametrize("failure", ["terminal", "exhausted"])
async def test_terminal_provision_failure_retains_gpi1_barrier(repository, failure):
    provision = await _submit(repository, operationId="provision")
    initial = await repository.claim_next(
        "provision-worker", now=NOW, allowed_actions=frozenset({OperationAction.PROVISION})
    )
    await repository.mark_pending(
        provision.id,
        "provision-worker",
        claim_token=initial.claim_token,
        claim_generation=initial.claim_generation,
        checkpoint=GPI1,
        retry_after_seconds=1,
        now=NOW,
    )
    retry = await repository.claim_next(
        "provision-worker",
        now=NOW + timedelta(seconds=2),
        allowed_actions=frozenset({OperationAction.PROVISION}),
    )
    if failure == "terminal":
        failed = await repository.fail(
            provision.id,
            "provision-worker",
            claim_token=retry.claim_token,
            claim_generation=retry.claim_generation,
            code="PROVISIONER_GOVERNANCE_PROVISION_UNAVAILABLE",
            now=NOW + timedelta(seconds=2),
        )
    else:
        repository.max_failure_attempts = 1
        failed = await repository.record_retryable_failure(
            provision.id,
            "provision-worker",
            claim_token=retry.claim_token,
            claim_generation=retry.claim_generation,
            retry_after_seconds=1,
            now=NOW + timedelta(seconds=2),
        )
    await _submit(repository, "resume", operationId="successor")

    assert failed.state is OperationState.ERROR and failed.checkpoint == GPI1
    assert (
        await repository.claim_next(
            "successor-worker",
            now=NOW + timedelta(days=1),
            allowed_actions=frozenset({OperationAction.RESUME}),
        )
        is None
    )


async def test_final_provision_clears_gpi1_barrier_atomically(repository):
    provision = await _submit(repository, operationId="provision")
    initial = await repository.claim_next(
        "provision-worker", now=NOW, allowed_actions=frozenset({OperationAction.PROVISION})
    )
    await repository.mark_pending(
        provision.id,
        "provision-worker",
        claim_token=initial.claim_token,
        claim_generation=initial.claim_generation,
        checkpoint=GPI1,
        retry_after_seconds=1,
        now=NOW,
    )
    successor = await _submit(repository, "resume", operationId="successor")
    retry = await repository.claim_next(
        "provision-worker",
        now=NOW + timedelta(seconds=2),
        allowed_actions=frozenset({OperationAction.PROVISION}),
    )
    await repository.checkpoint_effect_applied(
        provision.id,
        worker_id="provision-worker",
        claim_token=retry.claim_token,
        claim_generation=retry.claim_generation,
        now=NOW + timedelta(seconds=2),
    )
    assert (await repository.get_by_id(provision.id)).checkpoint == GPI1
    final = await repository.complete(
        provision.id,
        {},
        worker_id="provision-worker",
        claim_token=retry.claim_token,
        claim_generation=retry.claim_generation,
        now=NOW + timedelta(seconds=2),
    )

    assert final.state is OperationState.FINAL and final.checkpoint == "complete"
    claim = await repository.claim_next("successor-worker", now=NOW + timedelta(seconds=2))
    assert claim is not None and claim.id == successor.id
