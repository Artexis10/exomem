"""Bounds on the server-side capture wait: it never parks the shared sync
workers, never outlives the caller's request budget, never inflates the busy
metric with retries no client saw, and never spends the budget before replay.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from exomem import mutation_lock, request_budget, runtime_resources, writer_lease
from exomem.cli_ops import OpError


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(writer_lease, "_CAPTURE_RETRY_FLOOR_SECONDS", 0.005)
    yield
    writer_lease.reset_managers_for_tests()


def _manager(tmp_path) -> writer_lease.LeaseManager:
    return writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path))


def _run(manager, run, *, budget: float | None = None):
    command = SimpleNamespace(name="remember")
    token = None
    if budget is not None:
        token = request_budget.set_current(request_budget.RequestBudget(seconds=budget))
    try:
        return manager._run_absorbing_capture_contention(
            command, {}, run, key="k", commit_state={"observed": False}, request_id="r"
        )
    finally:
        if token is not None:
            request_budget.reset_current(token)


def _busy() -> OpError:
    return mutation_lock._mutation_busy(None)


def test_waiters_are_capped_below_the_sync_workers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "20")
    cap = max(1, runtime_resources.resolve_policy().sync_workers // 4)
    assert cap < runtime_resources.resolve_policy().sync_workers
    manager = _manager(tmp_path)
    release = threading.Event()
    parked = threading.Semaphore(0)

    def busy_until_released():
        if release.is_set():
            return "ok"
        raise _busy()

    def waiter():
        parked.release()
        return _run(manager, busy_until_released)

    threads = [threading.Thread(target=waiter) for _ in range(cap)]
    for thread in threads:
        thread.start()
    for _ in threads:
        assert parked.acquire(timeout=5)
    time.sleep(0.3)
    started = time.monotonic()
    with pytest.raises(OpError) as refused:
        _run(manager, busy_until_released)
    assert time.monotonic() - started < 2
    assert refused.value.details["cause"] == "capture_waiters_full"
    assert refused.value.details["committed"] is False
    release.set()
    for thread in threads:
        thread.join(timeout=5)


def test_a_wait_never_outlives_the_request_budget(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "40")
    guard = 0.8
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path), mutation_timeout_seconds=guard
    )
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 3.0)

    def slow_busy():
        time.sleep(guard)  # a guard acquire that spends its whole timeout
        raise _busy()

    budget = 5.0
    started = time.monotonic()
    with pytest.raises(OpError):
        _run(manager, slow_busy, budget=budget)
    elapsed = time.monotonic() - started
    assert elapsed <= budget - 3.0 + 0.2, elapsed


def test_absorbed_retries_do_not_count_as_client_visible_busy(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "20")
    manager = _manager(tmp_path)
    bumps: list[str] = []
    monkeypatch.setattr(
        mutation_lock,
        "_bump_boundary_metric",
        lambda name, labels=None: bumps.append(name),
    )
    attempts = {"n": 0}

    def busy_then_ok():
        attempts["n"] += 1
        if attempts["n"] < 6:
            raise _busy()
        return "ok"

    assert _run(manager, busy_then_ok) == "ok"
    assert bumps == []

    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "0.05")
    with pytest.raises(OpError):
        _run(manager, lambda: (_ for _ in ()).throw(_busy()))
    assert bumps.count("exomem_mutation_busy_total") == 1


def test_warm_runs_after_the_replay_check() -> None:
    import inspect

    source = inspect.getsource(writer_lease.LeaseManager)
    assert source.index("completed_terminal(key, digest)") < source.index(
        "warm_identity_catalogue_before_boundary(receipt_vault_root)"
    )


def test_follower_wait_reserves_delivery_and_guard_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import reserved_paths

    budget = request_budget.RequestBudget(seconds=30)
    token = request_budget.set_current(budget)
    try:
        wait = reserved_paths._flight_wait_seconds()
    finally:
        request_budget.reset_current(token)
    assert wait <= 30 - request_budget.DELIVERY_RESERVE_SECONDS - 5.0


def test_a_read_completes_while_captures_wait(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Waiting captures share anyio's worker limiter with reads; the cap leaves room."""
    import anyio

    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "20")
    monkeypatch.setenv("EXOMEM_SYNC_WORKERS", "8")
    manager = _manager(tmp_path)
    release = threading.Event()

    def busy_until_released():
        if release.is_set():
            return "ok"
        raise _busy()

    async def scenario() -> float:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 8
        outcomes: list[object] = []

        async def capture():
            try:
                outcomes.append(await anyio.to_thread.run_sync(_run, manager, busy_until_released))
            except OpError as error:
                outcomes.append(error)

        async with anyio.create_task_group() as group:
            for _ in range(8):
                group.start_soon(capture)
            await anyio.sleep(0.5)
            started = time.monotonic()
            with anyio.fail_after(2):
                await anyio.to_thread.run_sync(lambda: "read")
            elapsed = time.monotonic() - started
            release.set()
        assert sum(1 for o in outcomes if isinstance(o, OpError)) >= 6
        return elapsed

    assert anyio.run(scenario) < 2
