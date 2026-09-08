from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_repository import repository as repository
from test_worker import _v2_request

from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
from exomem_provisioner.models import Operation, OperationState
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2


@pytest.mark.parametrize("action", ["rollforward", "provision", "resume"])
@pytest.mark.parametrize("failure", ["terminal", "exhausted"])
async def test_terminal_migration_keeps_recovery_identity_without_automatic_retry(
    repository, action, failure
):
    operation = await repository.submit(
        action, "migration-terminal", _v2_request(), wire_protocol=WIRE_PROTOCOL_V2
    )
    now = datetime(2030, 1, 1, tzinfo=UTC)
    claimed = await repository.claim_next("worker", now=now)
    checkpoint = MigrationCheckpoint("enroll", "a" * 64, "A" * 43, "b" * 64, "c" * 64).encode()
    await repository.mark_pending(
        operation.id,
        "worker",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        checkpoint=checkpoint,
        retry_after_seconds=1,
        now=now,
    )
    now += timedelta(seconds=2)
    claimed = await repository.claim_next("worker", now=now)
    arguments = {
        "claim_token": claimed.claim_token,
        "claim_generation": claimed.claim_generation,
        "now": now,
    }
    if failure == "terminal":
        result = await repository.fail(
            operation.id, "worker", code="PROVISIONER_PROVIDER_METADATA_CONFLICT", **arguments
        )
    else:
        repository.max_failure_attempts = 1
        result = await repository.record_retryable_failure(
            operation.id, "worker", retry_after_seconds=30, **arguments
        )
    assert result.state is OperationState.ERROR
    assert result.checkpoint == ("failed" if action == "resume" else checkpoint)
    assert result.error_code == (
        "PROVISIONER_PROVIDER_METADATA_CONFLICT"
        if failure == "terminal"
        else "PROVISIONER_RETRY_EXHAUSTED"
    )
    assert result.claim_token is None and result.claim_expires_at is None
    async with repository._sessions() as session:
        stored = await session.get(Operation, operation.id)
        assert stored.claim_owner is None and stored.finalized_at is not None
    assert await repository.claim_next("worker", now=now + timedelta(days=1)) is None
