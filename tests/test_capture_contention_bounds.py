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
            command, run, key="k", commit_state={"observed": False}, request_id="r"
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
    cap = max(1, runtime_resources.resolve_policy().sync_workers // 2)
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


def test_a_slow_attempt_is_not_started_again_past_the_request_budget(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline leaves room for one guard timeout, not for slow preparation."""
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "40")
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path), mutation_timeout_seconds=0.2
    )
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 1.0)
    attempts: list[float] = []

    def slow_preparation_then_busy():
        attempts.append(time.monotonic())
        time.sleep(1.2)  # preparation before the leaf's own guard
        raise _busy()

    budget = 3.0
    started = time.monotonic()
    with pytest.raises(OpError) as refused:
        _run(manager, slow_preparation_then_busy, budget=budget)
    elapsed = time.monotonic() - started

    assert refused.value.details["cause"] == "capture_wait_exhausted"
    assert len(attempts) == 1
    assert elapsed <= budget - 1.0, elapsed


def test_a_wait_in_line_is_counted_before_a_slow_attempt_starts_again(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writers ahead in line can spend the budget a slow attempt needs."""
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "40")
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path), mutation_timeout_seconds=0.2
    )
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 1.0)

    def behind_other_writers(_ticket, _deadline):
        time.sleep(1.5)
        return True

    monkeypatch.setattr(manager._capture_queue, "wait_turn", behind_other_writers)
    attempts: list[float] = []

    def slow_preparation_then_busy():
        attempts.append(time.monotonic())
        time.sleep(1.0)
        raise _busy()

    budget = 4.0
    started = time.monotonic()
    with pytest.raises(OpError) as refused:
        _run(manager, slow_preparation_then_busy, budget=budget)
    elapsed = time.monotonic() - started

    assert refused.value.details["cause"] == "capture_wait_exhausted"
    assert len(attempts) == 1
    assert elapsed <= budget - 1.0, elapsed


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


def test_preparation_admission_retains_its_allowance_and_slot_across_physical_contention(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_SYNC_WORKERS", "2")
    manager = _manager(tmp_path)
    deadlines = []

    def run():
        retry = writer_lease._active_capture_preparation_retry()
        deadlines.append(retry.deadline)
        assert retry.admit()
        if len(deadlines) == 1:
            raise _busy()
        return "ok"

    assert _run(manager, run) == "ok"
    assert deadlines[0] == deadlines[1]
    assert _run(manager, lambda: writer_lease._active_capture_preparation_retry().admit())


def test_preparation_admission_rechecks_the_deadline_after_an_immediate_head_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    wait_turn = manager._capture_queue.wait_turn
    bumps = []
    monkeypatch.setattr(mutation_lock, "_bump_boundary_metric", lambda name, labels=None: bumps.append(name))

    def expires_at_turn(ticket, deadline):
        assert wait_turn(ticket, deadline)
        monkeypatch.setattr(writer_lease.time, "monotonic", lambda: deadline)
        return True

    monkeypatch.setattr(manager._capture_queue, "wait_turn", expires_at_turn)
    assert _run(manager, lambda: writer_lease._active_capture_preparation_retry().admit()) is False
    assert "exomem_mutation_busy_total" not in bumps


@pytest.mark.parametrize("ineligible", ["unkeyed", "disabled"])
def test_nested_ineligible_capture_cannot_borrow_preparation_admission(
    tmp_path, monkeypatch: pytest.MonkeyPatch, ineligible: str
) -> None:
    manager = _manager(tmp_path)

    def outer():
        assert writer_lease._active_capture_preparation_retry() is not None
        if ineligible == "disabled":
            monkeypatch.setenv("EXOMEM_CAPTURE_CONTENTION_ABSORB", "0")
        return manager._run_absorbing_capture_contention(
            SimpleNamespace(name="episode_memory"),
            lambda: writer_lease._active_capture_preparation_retry(),
            key=None if ineligible == "unkeyed" else "nested",
            commit_state={"observed": False}, request_id="nested",
        )

    assert _run(manager, outer) is None


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e400", "1e18"])
def test_a_non_finite_or_huge_wait_setting_never_crashes_a_queued_waiter(
    tmp_path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EXOMEM_SYNC_WORKERS", "16")
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", value)
    assert writer_lease._capture_wait_seconds() <= 600.0
    manager = _manager(tmp_path)
    head = manager._capture_queue.enter_line()
    releaser = threading.Timer(0.3, manager._capture_queue.leave, args=(head,))
    releaser.start()
    try:
        assert _run(manager, lambda: "ok") == "ok"
    finally:
        releaser.join()


def test_the_pre_boundary_warm_swallows_errors_but_not_assertion_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import reserved_paths

    monkeypatch.setattr(reserved_paths, "identity_catalogue_ready", lambda root: False)
    monkeypatch.setattr(reserved_paths, "_identity_coordination_active", lambda root: False)

    def failing(error: BaseException):
        def build(root):
            raise error

        return build

    monkeypatch.setattr(reserved_paths, "_baseline_identity_catalogue", failing(RuntimeError("walk")))
    reserved_paths.warm_identity_catalogue_before_boundary(tmp_path)  # never refuses a write

    monkeypatch.setattr(reserved_paths, "_baseline_identity_catalogue", failing(AssertionError("bug")))
    with pytest.raises(AssertionError):
        reserved_paths.warm_identity_catalogue_before_boundary(tmp_path)


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
        assert sum(1 for o in outcomes if isinstance(o, OpError)) >= 4
        return elapsed

    assert anyio.run(scenario) < 2
