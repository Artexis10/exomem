"""The derived drain takes the vault mutation boundary once per phase, not once per unit.

A writer that holds the boundary past the claims' lease must cost the drain no
component attempts, one release window must prove every claimed batch, an early
cut must end a turn but never the pass, a refused completion must not throw away
its dispatch, a long mutation's loans must carry one unit each, and the drain
pauses for requests only outside its boundary turns.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import traceback
from pathlib import Path

import pytest
from test_derived_batch_receipts import _commit_and_prove, _prepare_one

from exomem import (
    deferred_index,
    derived_drain,
    derived_receipts,
    foreground_priority,
    mutation_lock,
    writer_lease,
)


def _ready_batches(vault: Path, count: int) -> list[derived_receipts.DerivedBatchReceipt]:
    """`count` committed, published batches; each owes one lexical component."""
    receipts = []
    for index in range(count):
        receipt, target, _before, after = _prepare_one(
            vault, batch_id=f"turn-{index}", now=float(index)
        )
        _commit_and_prove(vault, receipt, target, after)
        receipts.append(receipt)
    return receipts


def _short_timeout_manager(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> writer_lease.LeaseManager:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault.parent / "leases"),
        mutation_timeout_seconds=0.1,
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    return manager


class _Writer:
    """A writer on another thread that takes the boundary and keeps it until released."""

    def __init__(
        self, manager: writer_lease.LeaseManager, vault: Path, *, hold_seconds: float = 30.0
    ) -> None:
        self._manager = manager
        self._vault = vault
        self._hold_seconds = hold_seconds
        self._held = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def take(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._hold, daemon=True)
            self._thread.start()
        assert self._held.wait(timeout=5), "the writer never took the boundary"

    def _hold(self) -> None:
        with self._manager.consistency_guard(self._vault, operation="test_writer"):
            self._held.set()
            self._done.wait(timeout=self._hold_seconds)

    def release(self) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def test_a_boundary_held_past_the_claim_lease_gets_the_claims_back_without_an_attempt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts = _ready_batches(vault, 3)
    manager = _short_timeout_manager(vault, monkeypatch)
    monkeypatch.setattr(derived_drain, "CLAIM_LEASE_SECONDS", 3.0)
    writer = _Writer(manager, vault)
    coordinator = manager._mutation_coordinator_for(vault)
    refusals_before = coordinator.snapshot()["contention"]["busy_refusals"]

    def dispatch(_root: Path, _status: derived_receipts.DerivedComponentStatus) -> bool:
        writer.take()  # a write arrives while the drain is dispatching
        return True

    try:
        derived_drain.drain_once(
            vault,
            dispatch=dispatch,
            observe_current_generation=lambda _root: "generation-1",
            limit=16,
            now=10.0,
        )
        refusals = coordinator.snapshot()["contention"]["busy_refusals"] - refusals_before
    finally:
        writer.release()
        manager.close()

    assert refusals >= 1
    statuses = [
        derived_receipts.component_status(
            vault, receipt, derived_receipts.DerivedComponent.LEXSTORE
        )
        for receipt in receipts
    ]
    assert [status.attempt_count for status in statuses] == [0, 0, 0]
    assert all(status.state != "claimed" for status in statuses)


def test_one_release_window_proves_every_claimed_batch(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts = _ready_batches(vault, 3)
    manager = _short_timeout_manager(vault, monkeypatch)
    writer = _Writer(manager, vault, hold_seconds=0.3)
    dispatched: list[str] = []

    def dispatch(_root: Path, status: derived_receipts.DerivedComponentStatus) -> bool:
        writer.take()  # the window closes as soon as the first dispatch starts
        dispatched.append(status.batch_id)
        return True

    try:
        derived_drain.drain_once(
            vault,
            dispatch=dispatch,
            observe_current_generation=lambda _root: "generation-1",
            limit=16,
            now=10.0,
        )
    finally:
        writer.release()
        manager.close()

    assert sorted(dispatched) == sorted(receipt.batch_id for receipt in receipts)


def test_one_pass_finishes_a_dependency_chain_when_every_turn_is_cut(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = derived_receipts.DerivedComponent
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="chain",
        required={components.FRESHNESS, components.MEMORY_REFS, components.LEXSTORE},
    )
    _commit_and_prove(vault, receipt, target, after)
    manager = _short_timeout_manager(vault, monkeypatch)
    # A writer waits at every turn and every turn has used its slice, so the
    # drain cuts each turn after its first unit.
    monkeypatch.setattr(foreground_priority, "MAX_BULK_WORK_SECONDS", 0.0)
    monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "waiter_count", lambda _self: 1)
    try:
        derived_drain.drain_once(
            vault,
            dispatch=lambda _root, _status: True,
            observe_current_generation=lambda _root: "generation-1",
            limit=16,
            now=10.0,
        )
    finally:
        manager.close()

    assert derived_receipts._load_receipt(vault, receipt.batch_id).state == "completed"


def test_a_refused_completion_turn_asks_again_within_the_lease_and_never_redoes_dispatch(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts = _ready_batches(vault, 3)
    manager = _short_timeout_manager(vault, monkeypatch)
    # The write outlasts several acquisition timeouts but not the claims' lease.
    writer = _Writer(manager, vault, hold_seconds=0.5)
    coordinator = manager._mutation_coordinator_for(vault)
    refusals_before = coordinator.snapshot()["contention"]["busy_refusals"]
    dispatched: list[str] = []

    def dispatch(_root: Path, status: derived_receipts.DerivedComponentStatus) -> bool:
        writer.take()  # a write arrives while the drain is dispatching
        dispatched.append(status.batch_id)
        return True

    try:
        derived_drain.drain_once(
            vault,
            dispatch=dispatch,
            observe_current_generation=lambda _root: "generation-1",
            limit=16,
            now=10.0,
        )
        refusals = coordinator.snapshot()["contention"]["busy_refusals"] - refusals_before
    finally:
        writer.release()
        manager.close()

    assert refusals >= 1
    assert sorted(dispatched) == sorted(receipt.batch_id for receipt in receipts)
    assert all(
        derived_receipts._load_receipt(vault, receipt.batch_id).state == "completed"
        for receipt in receipts
    )


def test_the_drain_works_through_a_long_mutation_one_lent_unit_at_a_time(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts = _ready_batches(vault, 3)
    manager = _short_timeout_manager(vault, monkeypatch)
    coordinator = manager._mutation_coordinator_for(vault)
    held, done = threading.Event(), threading.Event()
    loans: list[int] = []

    def long_mutation() -> None:
        # Holds the boundary throughout and lends it between its units, as an import does.
        with manager.consistency_guard(vault, operation="test_import"):
            held.set()
            while not done.is_set():
                if coordinator.next_borrower_deadline() is not None:
                    loans.append(coordinator.lend(until=time.monotonic() + 1.0))
                time.sleep(0.002)

    units_per_loan: dict[int, int] = {}
    undeclared_on_loan: list[str] = []

    def on_loan(name, real, *, declared: bool):
        def run(*args, **kwargs):
            if coordinator.current_thread_borrows_boundary():
                if declared:
                    loan = len(loans)  # the loan in progress has not returned yet
                    units_per_loan[loan] = units_per_loan.get(loan, 0) + 1
                else:
                    undeclared_on_loan.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(derived_receipts, name, run)

    for name in ("prove_committed", "complete_component"):
        on_loan(name, getattr(derived_receipts, name), declared=True)
    for name in ("retire_imported_visibility", "prove_recovery_batch"):
        on_loan(name, getattr(derived_receipts, name), declared=False)
    mutation = threading.Thread(target=long_mutation, daemon=True)
    mutation.start()
    assert held.wait(timeout=5)
    try:
        derived_drain.drain_once(
            vault,
            dispatch=lambda _root, _status: True,
            observe_current_generation=lambda _root: "generation-1",
            limit=16,
            now=10.0,
        )
    finally:
        done.set()
        mutation.join(timeout=5)
        manager.close()

    assert all(
        derived_receipts._load_receipt(vault, receipt.batch_id).state == "completed"
        for receipt in receipts
    )
    assert set(units_per_loan.values()) == {1}
    assert undeclared_on_loan == []


def test_a_signal_cannot_cut_the_busy_gap_short(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deferred_index._connect(vault, create=True).close()
    manager = _short_timeout_manager(vault, monkeypatch)
    writer = _Writer(manager, vault)
    writer.take()
    monkeypatch.setattr(derived_drain, "RETRY_SECONDS", 0.4)
    starts: list[float] = []
    ends: list[float] = []
    real_drain_once = derived_drain.drain_once

    def timed_drain_once(*args, **kwargs) -> int:
        starts.append(time.monotonic())
        try:
            return real_drain_once(*args, **kwargs)
        finally:
            ends.append(time.monotonic())

    monkeypatch.setattr(derived_drain, "drain_once", timed_drain_once)
    drain = derived_drain.DerivedDrain(
        vault,
        dispatch=lambda _root, _status: True,
        observe_current_generation=lambda _root: "generation-1",
        visibility_publisher=lambda _root, _receipt: True,
        retire_visibility=lambda _root, _batches: (),
    ).start()
    try:
        deadline = time.monotonic() + 5
        while len(starts) < 2 and time.monotonic() < deadline:
            drain.signal()  # every write signals the drain
            time.sleep(0.01)
    finally:
        drain.stop(timeout=5)
        writer.release()
        manager.close()

    assert len(starts) >= 2, "the drain never ran a second pass"
    assert starts[1] - ends[0] >= 0.4


def test_the_drain_yields_to_requests_and_never_under_a_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [receipt] = _ready_batches(vault, 1)
    callers: set[str] = set()
    held_while_yielding: list[str] = []
    real_holds = mutation_lock.current_thread_holds_boundary

    def spy(*, max_wait: float = foreground_priority.MAX_YIELD_SECONDS) -> float:
        frames = [frame.name for frame in traceback.extract_stack(limit=12)]
        callers.update(frames)
        if real_holds():
            held_while_yielding.append(frames[-3])
        return 0.0

    monkeypatch.setattr(foreground_priority, "yield_to_foreground", spy)
    drain = derived_drain.DerivedDrain(
        vault,
        dispatch=lambda _root, _status: True,
        observe_current_generation=lambda _root: "generation-1",
    ).start()
    try:
        deadline = time.monotonic() + 10
        while "_settled_batches" not in callers and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        drain.stop(timeout=5)

    assert derived_receipts._load_receipt(vault, receipt.batch_id).state == "completed"
    assert {"_rotate_healthy", "_dispatch", "_settled_batches"} <= callers
    assert held_while_yielding == []


def test_a_replaced_or_altered_receipt_store_is_probed_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_batches(vault, 1)
    store = deferred_index.store_path(vault)
    statements: list[str] = []
    real_connect = deferred_index._connect

    def traced_connect(*args, **kwargs) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(deferred_index, "_connect", traced_connect)

    def schema_probes() -> int:
        statements.clear()
        derived_receipts.recoverable_batch_count(vault)
        return sum("PRAGMA table_info" in statement for statement in statements)

    def drop_active_index(path: Path) -> None:
        with sqlite3.connect(path) as raw:
            raw.execute("DROP INDEX pending_recall_active")
        raw.close()

    def has_active_index() -> bool:
        with sqlite3.connect(store) as raw:
            found = raw.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'pending_recall_active'"
            ).fetchone()
        raw.close()
        return found is not None

    schema_probes()
    assert schema_probes() == 0, "an unchanged store re-ran its schema probe"

    drop_active_index(store)
    assert schema_probes() > 0
    assert has_active_index(), "the altered store was not repaired"

    stale = store.with_name("stale-copy.sqlite")
    with sqlite3.connect(store) as raw:
        raw.execute("VACUUM INTO ?", (str(stale),))
    raw.close()
    drop_active_index(stale)
    stale.replace(store)
    assert schema_probes() > 0
    assert has_active_index(), "the replaced store was not repaired"
