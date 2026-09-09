"""Recovery changes scheduling, never the migration's identity or effect authority."""

import asyncio
from datetime import timedelta

import pytest
from test_governance_migration_barrier import postgresql17 as postgresql17
from test_governance_migration_barrier import postgresql_repository as postgresql_repository
from test_governance_migration_barrier import repository as repository
from test_governance_migration_barrier import sqlite_repository as sqlite_repository
from test_governance_provision_barrier import GM1, GPI1, NOW, _submit

from exomem_provisioner.models import CellOperationLock, Operation, OperationState, TenantFence
from exomem_provisioner.repository import RepositoryConflict


async def failed(repository, *, action="provision", checkpoint=GPI1):
    operation = await _submit(repository, action, provisionMode="serve")
    claim = await repository.claim_next("worker", now=NOW)
    await repository.mark_pending(
        operation.id, "worker", claim_token=claim.claim_token,
        claim_generation=claim.claim_generation, checkpoint=checkpoint,
        retry_after_seconds=1, now=NOW,
    )
    claim = await repository.claim_next("worker", now=NOW + timedelta(seconds=1))
    await repository.fail(
        operation.id, "worker", claim_token=claim.claim_token,
        claim_generation=claim.claim_generation, code="PROVISIONER_RETRY_EXHAUSTED", now=NOW,
    )
    async with repository.session_factory.begin() as session:
        stored = await session.get(Operation, operation.id)
        stored.progress = {**stored.progress, "failure_attempts": 6, "other_evidence": "retained"}
    return operation.id


async def row(repository, operation_id):
    async with repository.session_factory() as session:
        operation = await session.get(Operation, operation_id)
        return {column.name: getattr(operation, column.name) for column in Operation.__table__.columns}


@pytest.mark.parametrize("action,checkpoint", [("provision", GPI1), ("provision", GM1), ("rollforward", GM1)])
async def test_recovery_preserves_exact_operation_and_denial_until_worker_completion(
    repository, action, checkpoint,
):
    operation_id = await failed(repository, action=action, checkpoint=checkpoint)
    before = await row(repository, operation_id)
    digest = await repository.preflight_governance_recovery(operation_id, now=NOW)
    assert len(digest) == 64
    assert await row(repository, operation_id) == before  # preflight writes nothing
    assert await repository.resume_governance_recovery(
        operation_id, expected_digest=digest, now=NOW,
    ) == "queued"
    after = await row(repository, operation_id)
    changed = {key for key in before if before[key] != after[key]}
    assert changed <= {"state", "progress", "error_code", "finalized_at", "available_at", "updated_at", "retry_after_seconds"}
    assert after["state"] is OperationState.PENDING
    assert after["checkpoint"] == checkpoint
    assert after["progress"]["failure_attempts"] == 0
    assert after["progress"]["other_evidence"] == "retained"
    assert after["progress"]["pending_count"] == before["progress"]["pending_count"]
    await _submit(repository, "resume", operationId="successor")
    from exomem_provisioner.models import OperationAction

    assert await repository.claim_next(
        "successor", now=NOW, allowed_actions=frozenset({OperationAction.RESUME}),
    ) is None
    claim = await repository.claim_next("worker", now=NOW)
    assert claim.id == operation_id and claim.checkpoint == checkpoint
    assert claim.claim_generation == before["claim_generation"] + 1
    await repository.assert_active_claim(
        operation_id, "worker", claim_token=claim.claim_token,
        claim_generation=claim.claim_generation, now=NOW,
    )


async def test_acknowledgement_replay_cannot_requeue_a_later_failure(repository):
    operation_id = await failed(repository)
    digest = await repository.preflight_governance_recovery(operation_id, now=NOW)
    assert await repository.resume_governance_recovery(operation_id, expected_digest=digest, now=NOW) == "queued"
    claim = await repository.claim_next("worker", now=NOW)
    await repository.fail(
        operation_id, "worker", claim_token=claim.claim_token,
        claim_generation=claim.claim_generation, code="PROVISIONER_CHECKPOINT_INVALID", now=NOW,
    )
    before = await row(repository, operation_id)
    assert await repository.resume_governance_recovery(operation_id, expected_digest=digest, now=NOW) == "already-queued"
    assert await row(repository, operation_id) == before
    fresh = await repository.preflight_governance_recovery(operation_id, now=NOW)
    assert fresh != digest
    assert await repository.resume_governance_recovery(operation_id, expected_digest=fresh, now=NOW) == "queued"
    with pytest.raises(RepositoryConflict):
        await repository.resume_governance_recovery(operation_id, expected_digest=digest, now=NOW)


@pytest.mark.parametrize("field,value", [
    ("checkpoint", "gm1:malformed"), ("checkpoint", "gpi1:initializing:" + "B" * 43),
    ("checkpoint", "volume-owned"), ("checkpoint", "gpi1:unknown:" + "A" * 43),
    ("state", OperationState.PENDING), ("claim_owner", "foreign"),
    ("claim_token", "foreign"), ("claim_expires_at", NOW),
    ("finalized_at", None), ("result_ciphertext", "opaque"),
    ("result_redacted", {"completed": True}), ("error_code", None),
    ("provider_operation_id", "foreign"), ("provider_fence_generation", 8),
    ("canonical_request_sha256", "f" * 64), ("request_ciphertext", "tampered"),
])
async def test_invalid_or_ineligible_terminal_row_remains_unchanged(sqlite_repository, field, value):
    repository = sqlite_repository
    operation_id = await failed(repository)
    async with repository.session_factory.begin() as session:
        setattr(await session.get(Operation, operation_id), field, value)
    before = await row(repository, operation_id)
    with pytest.raises(RepositoryConflict):
        await repository.preflight_governance_recovery(operation_id, now=NOW)
    assert await row(repository, operation_id) == before


@pytest.mark.parametrize("change", ["checkpoint", "request", "progress", "fence", "live-lease", "foreign-barrier", "destroy", "claimed"])
async def test_recovery_rechecks_snapshot_and_conflicts_in_commit_transaction(repository, change):
    operation_id = await failed(repository)
    digest = await repository.preflight_governance_recovery(operation_id, now=NOW)
    async with repository.session_factory.begin() as session:
        operation = await session.get(Operation, operation_id)
        if change == "checkpoint":
            operation.checkpoint = "gpi1:complete:" + "A" * 43
        elif change == "request":
            operation.request_ciphertext = "changed"
        elif change == "progress":
            operation.progress = {**operation.progress, "changed": True}
        elif change == "fence":
            (await session.get(TenantFence, operation.tenant_id)).fence_generation += 1
        elif change == "live-lease":
            session.add(CellOperationLock(
                cell_id=operation.cell_id, tenant_id=operation.tenant_id,
                operation_id=operation.external_operation_id,
                fence_generation=operation.fence_generation,
                lease_expires_at=NOW + timedelta(seconds=60),
            ))
    if change in {"foreign-barrier", "destroy", "claimed"}:
        other = await _submit(repository, "destroy" if change == "destroy" else "provision",
                              operationId="overlap", **({"cellId": None} if change == "destroy" else {}))
        async with repository.session_factory.begin() as session:
            operation = await session.get(Operation, other.id)
            if change == "foreign-barrier":
                operation.checkpoint = "gm1:malformed"
                operation.state = OperationState.ERROR
            if change == "claimed":
                operation.state = OperationState.CLAIMED
                operation.claim_expires_at = NOW  # expired is still unresolved
    before = await row(repository, operation_id)
    with pytest.raises(RepositoryConflict):
        await repository.resume_governance_recovery(operation_id, expected_digest=digest, now=NOW)
    assert await row(repository, operation_id) == before


async def test_postgresql_concurrent_resume_has_one_transition(postgresql_repository):
    repository = postgresql_repository
    operation_id = await failed(repository)
    digest = await repository.preflight_governance_recovery(operation_id, now=NOW)
    results = await asyncio.gather(*(
        repository.resume_governance_recovery(operation_id, expected_digest=digest, now=NOW)
        for _ in range(2)
    ))
    assert sorted(results) == ["already-queued", "queued"]
