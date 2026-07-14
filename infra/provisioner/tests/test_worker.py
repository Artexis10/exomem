from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.driver import DriverFinal, DriverRetryable, EffectContext, FakeDriver
from exomem_provisioner.models import OperationState
from exomem_provisioner.repository import OperationRepository
from exomem_provisioner.worker import ProvisionerWorker


def _settings(path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
        claim_seconds=10,
    )


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "operationId": "operation-worker-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 4,
        "tenantId": "tenant-worker-alpha",
        "cellId": "cell-worker-alpha",
        "protocolVersion": "exomem-hosted.v1",
        "releaseVersion": "0.22.0",
        "serviceCredential": "service-credential-sentinel-000000000",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
    }
    request.update(overrides)
    return request


@pytest.fixture
async def worker_context(
    tmp_path: Path,
) -> tuple[ProvisionerDatabase, OperationRepository, FakeDriver]:
    settings = _settings(tmp_path / "worker.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
    )
    driver = FakeDriver()
    try:
        yield database, repository, driver
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_worker_checkpoints_before_and_after_effect(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, _ = worker_context
    operation = await repository.submit("provision", "checkpoint-key", _request())

    class ObservingDriver:
        calls = 0

        async def observed_fence(self, _tenant_id: str) -> int:
            return 0

        async def execute(
            self,
            action: str,
            request: dict[str, object],
            context: EffectContext,
        ) -> DriverFinal:
            self.calls += 1
            during = await repository.get_by_id(context.operation_id)
            assert during is not None
            assert during.state is OperationState.CLAIMED
            assert during.checkpoint == "effect-prepared"
            assert action == "provision"
            assert request == _request()
            return DriverFinal(
                result={
                    "providerRef": "provider-cell-worker-alpha",
                    "privateEndpoint": "https://cell-worker-alpha.cells.internal",
                }
            )

    driver = ObservingDriver()
    worker = ProvisionerWorker(repository, driver, worker_id="worker-alpha")

    assert await worker.run_once() is True
    final = await repository.get_by_id(operation.id)
    assert driver.calls == 1
    assert final is not None
    assert final.state is OperationState.FINAL
    assert final.checkpoint == "complete"


@pytest.mark.asyncio
async def test_expired_claim_is_resumed_after_worker_restart(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, _ = worker_context
    operation = await repository.submit("provision", "restart-claim", _request())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    first = await repository.claim_next("dead-worker", now=now)
    blocked = await repository.claim_next("new-worker", now=now + timedelta(seconds=9))
    resumed = await repository.claim_next("new-worker", now=now + timedelta(seconds=11))

    assert first is not None and first.id == operation.id
    assert blocked is None
    assert resumed is not None and resumed.id == operation.id
    assert resumed.state is OperationState.CLAIMED
    assert resumed.checkpoint == "effect-prepared"


@pytest.mark.asyncio
async def test_worker_renews_claim_while_long_provider_effect_is_running(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, _ = worker_context
    repository.claim_seconds = 0.15
    operation = await repository.submit("provision", "long-effect", _request())
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowDriver:
        async def observed_fence(self, _tenant_id: str) -> int:
            return 0

        async def execute(
            self,
            _action: str,
            _request_data: dict[str, object],
            _context: EffectContext,
        ) -> DriverFinal:
            entered.set()
            await release.wait()
            return DriverFinal(
                result={
                    "providerRef": "provider-cell-worker-alpha",
                    "privateEndpoint": "https://cell-worker-alpha.cells.internal",
                }
            )

    worker = ProvisionerWorker(repository, SlowDriver(), worker_id="slow-worker")
    running = asyncio.create_task(worker.run_once())
    await entered.wait()
    await asyncio.sleep(0.25)
    stolen = await repository.claim_next("thief-worker")
    release.set()
    with suppress(Exception):
        await running

    assert stolen is None
    final = await repository.get_by_id(operation.id)
    assert final is not None and final.state is OperationState.FINAL


@pytest.mark.asyncio
async def test_lost_ack_replays_one_fake_provider_effect(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, driver = worker_context
    operation = await repository.submit("provision", "lost-ack", _request())
    driver.lose_next_acknowledgement("provision")
    first_worker = ProvisionerWorker(repository, driver, worker_id="worker-before-restart")
    second_worker = ProvisionerWorker(repository, driver, worker_id="worker-after-restart")
    now = datetime(2030, 1, 1, tzinfo=UTC)

    assert await first_worker.run_once(now=now) is True
    pending = await repository.get_by_id(operation.id)
    assert pending is not None and pending.state is OperationState.PENDING
    assert driver.effect_count("provision", operation.id) == 1

    assert await second_worker.run_once(now=now + timedelta(seconds=3)) is True
    final = await repository.get_by_id(operation.id)
    assert final is not None and final.state is OperationState.FINAL
    assert driver.effect_count("provision", operation.id) == 1


@pytest.mark.asyncio
async def test_provider_observed_higher_fence_rejects_before_driver_effect(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, driver = worker_context
    operation = await repository.submit("resume", "stale-provider", _request())
    driver.set_observed_fence("tenant-worker-alpha", 5)
    worker = ProvisionerWorker(repository, driver, worker_id="worker-alpha")

    assert await worker.run_once() is True
    failed = await repository.get_by_id(operation.id)
    assert failed is not None
    assert failed.state is OperationState.ERROR
    assert failed.error_code == "PROVISIONER_STALE_FENCE"
    assert driver.effect_count("resume", operation.id) == 0


@pytest.mark.asyncio
async def test_long_operation_remains_pending_beyond_six_polls_then_finishes(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, driver = worker_context
    operation = await repository.submit("restore", "long-restore", _request())
    driver.remain_pending("restore", polls=7)
    worker = ProvisionerWorker(repository, driver, worker_id="worker-alpha")
    now = datetime(2030, 1, 1, tzinfo=UTC)

    for poll in range(7):
        assert await worker.run_once(now=now + timedelta(seconds=poll * 3)) is True
        pending = await repository.get_by_id(operation.id)
        assert pending is not None
        assert pending.state is OperationState.PENDING
        assert pending.checkpoint == "provider-wait"
        assert pending.progress["pending_count"] == poll + 1
        assert pending.progress.get("failure_attempts", 0) == 0
    assert driver.effect_count("restore", operation.id) == 1

    assert await worker.run_once(now=now + timedelta(seconds=21)) is True
    final = await repository.get_by_id(operation.id)
    assert final is not None and final.state is OperationState.FINAL
    assert driver.effect_count("restore", operation.id) == 1


@pytest.mark.asyncio
async def test_retryable_driver_failures_consume_bounded_failure_attempts(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, _ = worker_context
    repository.max_failure_attempts = 3
    operation = await repository.submit("resume", "bounded-retry", _request())

    class FailingDriver:
        calls = 0

        async def observed_fence(self, _tenant_id: str) -> int:
            return 0

        async def execute(
            self,
            _action: str,
            _request_data: dict[str, object],
            _context: EffectContext,
        ) -> DriverFinal:
            self.calls += 1
            raise DriverRetryable("provider temporarily unavailable")

    driver = FailingDriver()
    worker = ProvisionerWorker(repository, driver, worker_id="retry-worker")
    now = datetime(2030, 1, 1, tzinfo=UTC)

    for attempt in (1, 2):
        assert await worker.run_once(now=now + timedelta(seconds=attempt * 3)) is True
        pending = await repository.get_by_id(operation.id)
        assert pending is not None and pending.state is OperationState.PENDING
        assert pending.progress["failure_attempts"] == attempt
        assert pending.progress.get("pending_count", 0) == 0

    assert await worker.run_once(now=now + timedelta(seconds=9)) is True
    failed = await repository.get_by_id(operation.id)
    assert failed is not None and failed.state is OperationState.ERROR
    assert failed.progress["failure_attempts"] == 3
    assert failed.error_code == "PROVISIONER_RETRY_EXHAUSTED"
    assert driver.calls == 3


@pytest.mark.asyncio
async def test_fake_driver_never_retains_secret_request_material(
    worker_context: tuple[ProvisionerDatabase, OperationRepository, FakeDriver],
) -> None:
    _, repository, driver = worker_context
    operation = await repository.submit("provision", "secret-redaction", _request())
    worker = ProvisionerWorker(repository, driver, worker_id="worker-alpha")

    await worker.run_once()

    rendered = repr(driver)
    assert "service-credential-sentinel" not in rendered
    assert driver.effect_count("provision", operation.id) == 1
