"""Bounded server-owned scheduling for exact derived component custody.

Component business logic is injected through ``dispatch``. This module owns
only claim limits, retry rotation, prompt wake-up, and clean lifecycle.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from . import (
    call_ledger,
    call_spans,
    deferred_index,
    derived_receipts,
    foreground_priority,
    mode,
    process_memory,
    state_paths,
)
from .derived_receipts import DerivedComponentStatus, RecoveryOutcome

if TYPE_CHECKING:
    from .mutation_lock import VaultMutationCoordinator

log = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 30.0
RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 120.0
CLAIM_LEASE_SECONDS = 60.0
# The hosted lifecycle calls a registered stopper with no arguments and never
# shares the deadline the quiesce caller asked for, so a stop that blocks for
# seconds silently overruns it.  Spend one bounded slice proving the thread is
# dead and fail closed otherwise; a caller whose pass was still running quiesces
# again rather than being told a live dispatch had stopped.
HOSTED_STOP_DEADLINE_SECONDS = 0.05
NORMAL_PASS_LIMIT = 16
QUIET_PASS_LIMIT = 1
PERFORMANCE_PASS_LIMIT = 32

#: `"not_required"` settles an optional component whose work can no longer apply.
ComponentDispatchResult = bool | Literal["pending", "not_required"]
ComponentDispatcher = Callable[[Path, DerivedComponentStatus], ComponentDispatchResult]
CanonicalGenerationObserver = Callable[[Path], str | None]
PendingVisibilityPublisher = Callable[
    [Path, derived_receipts.DerivedBatchReceipt], bool
]
#: Returns the batches it retired, or None when it cannot tell.
PendingVisibilityRetirer = Callable[[Path, tuple[str, ...]], Collection[str] | None]

_LOCK = threading.Lock()
_ACTIVE: dict[str, DerivedDrain] = {}

#: What the most recent pass observed, per cell. Content-free by construction:
#: counts, an attempt count, and ages in seconds. This exists because component
#: depth and age are the two numbers the rollout's pause triggers are written
#: against, and the receipt protocol exposes depth but not age -- a worker that
#: has already claimed a status can report the age it observed without any
#: caller reaching into the store to ask.
_PASS_OBSERVATION_LIMIT = 64
_PASS_LOCK = threading.Lock()
_PASS_OBSERVATIONS: dict[str, dict[str, float | int]] = {}
_CUSTODY_CURSORS: dict[str, tuple[str, str]] = {}


def _note_pass_observation(
    vault_root: Path,
    *,
    claimed: int,
    completed: int,
    max_attempt_count: int,
    oldest_due_at: float | None,
    at: float,
) -> None:
    key = _key(vault_root)
    with _PASS_LOCK:
        if key not in _PASS_OBSERVATIONS and len(_PASS_OBSERVATIONS) >= _PASS_OBSERVATION_LIMIT:
            _PASS_OBSERVATIONS.pop(next(iter(_PASS_OBSERVATIONS)), None)
        _PASS_OBSERVATIONS[key] = {
            "at": at,
            "claimed": claimed,
            "completed": completed,
            "max_attempt_count": max_attempt_count,
            "oldest_due_age_seconds": (
                0.0 if oldest_due_at is None else round(max(0.0, at - oldest_due_at), 3)
            ),
        }


def last_pass_observation(vault_root: Path) -> dict[str, float | int | None]:
    """The most recent pass's content-free observation for this cell."""
    with _PASS_LOCK:
        entry = _PASS_OBSERVATIONS.get(_key(vault_root))
    if entry is None:
        return {
            "at_age_seconds": None,
            "claimed": 0,
            "completed": 0,
            "max_attempt_count": 0,
            "oldest_due_age_seconds": None,
        }
    return {
        "at_age_seconds": round(max(0.0, time.time() - float(entry["at"])), 3),
        "claimed": int(entry["claimed"]),
        "completed": int(entry["completed"]),
        "max_attempt_count": int(entry["max_attempt_count"]),
        "oldest_due_age_seconds": float(entry["oldest_due_age_seconds"]),
    }


def reset_pass_observations() -> None:
    """Drop every cell's last-pass observation. Durable custody is untouched."""
    with _PASS_LOCK:
        _PASS_OBSERVATIONS.clear()
        _CUSTODY_CURSORS.clear()


def _key(vault_root: Path) -> str:
    return str(Path(vault_root).resolve(strict=False))


def progress_limit(*, mode_name: str | None = None, resource_limit: int | None = None) -> int:
    """Return a bounded pass allowance with one correctness slot minimum."""
    selected = "normal" if mode.service_profile_enabled() else (
        mode.normalize(mode_name) if mode_name is not None else mode.resolve_mode()
    )
    if selected == "quiet":
        policy_limit = QUIET_PASS_LIMIT
    elif selected == "performance":
        policy_limit = PERFORMANCE_PASS_LIMIT
    else:
        policy_limit = NORMAL_PASS_LIMIT
    if resource_limit is None:
        return policy_limit
    return max(1, min(policy_limit, max(0, int(resource_limit))))


def canonical_generation_observer() -> CanonicalGenerationObserver:
    """The one observation every proof in this cell is bound to.

    Both the acknowledgement and the drain must ask the same question of the
    vault, or a batch proved ready by one would be unprovable to the other.
    Lazily bound because ``writer_lease`` imports this module.
    """

    def observe(vault_root: Path) -> str | None:
        from .writer_lease import current_canonical_generation

        return current_canonical_generation(vault_root)

    return observe


def pending_visibility_publisher() -> PendingVisibilityPublisher:
    """Lane 2's publisher, bound lazily so the import graph is unchanged."""

    def publish(
        vault_root: Path, receipt: derived_receipts.DerivedBatchReceipt
    ) -> bool:
        from . import pending_recall

        return pending_recall.publish(vault_root, receipt)

    return publish


def pending_visibility_retirer() -> PendingVisibilityRetirer:
    """Lane 2's retirement re-check after completions, bound lazily."""

    def retire(vault_root: Path, batch_ids: tuple[str, ...]) -> tuple[str, ...] | None:
        from . import pending_recall

        return pending_recall.note_components_completed(vault_root, batch_ids)

    return retire


def component_dispatcher() -> ComponentDispatcher:
    """Route one already-claimed component to the lane that owns it.

    Routing is BY COMPONENT and never by trial. Handing a claim to a lane that
    does not own it is not free: that lane rotates it back with
    ``component_unhandled``, which spends an attempt belonging to the lane that
    does own it and pushes its next attempt out under backoff.

    ``write_advisory`` goes to Lane 4's executor. Every other closed component
    goes to the existing writer fan-out, now owned by the receipt instead of by
    the request thread (design decision 5: reuse the current owner, add no
    competing scheduler). Nothing is dispatched by trial, and no component is
    executed by a lane that does not own it.
    """

    def dispatch(vault_root: Path, status: DerivedComponentStatus) -> ComponentDispatchResult:
        if status.component is derived_receipts.DerivedComponent.WRITE_ADVISORY:
            from . import deferred_write_advisory

            # The advisory proves itself against its target's content
            # fingerprint, which `execute_write_advisory` re-observes before it
            # encodes or publishes anything (ruling R3). Publication still
            # requires the exact claimed component revision, the lease owner
            # and an unexpired lease.
            with call_spans.span("derived.advisory_execute"):
                execution = deferred_write_advisory.execute_write_advisory(
                    vault_root, status
                )
            call_ledger.note_derived_event(
                "advisory_vectors_reused"
                if execution.reused_vectors
                else "advisory_vectors_encoded"
            )
            if execution.outcome == "published":
                call_ledger.note_derived_event(
                    "advisory_failed"
                    if execution.state == "failed"
                    else "advisory_published"
                )
            elif execution.outcome == "already_published":
                call_ledger.note_derived_event("advisory_replayed")
            elif execution.outcome == "superseded":
                call_ledger.note_derived_event("receipt_superseded")
            if execution.outcome == "expired":
                log.info(
                    "derived write_advisory settled as not required: result expired"
                )
                return "not_required"
            accepted = execution.outcome in {
                "published",
                "already_published",
                "superseded",
            }
            if not accepted:
                log.info(
                    "derived write_advisory not published outcome=%s reason=%s",
                    execution.outcome,
                    execution.failure_code,
                )
            return accepted
        from . import index_sync

        receipt = derived_receipts._load_receipt(vault_root, status.batch_id)
        converged = index_sync.converge_derived_component(
            vault_root, receipt, status.component
        )
        if not converged:
            log.info(
                "derived component not converged component=%s",
                status.component.value,
            )
        return converged

    return dispatch


class DrainCadence:
    """What one drain thread carries from a pass to the next.

    ``busy`` reports that the pass met a mutation boundary held for a whole
    acquisition timeout. The settled sweep re-checks completed batches whose
    pending rows outlived a lost completion callback
    (`completed_visibility_batches`); it runs on the first pass after start, on
    every pass while it retires something, and otherwise once per
    MAX_RETRY_SECONDS. Each sweep rewrites the rotation of up to a pass limit
    of batches and re-checks their recall lanes; when the gate holds one back,
    a lost callback's rows wait at most MAX_RETRY_SECONDS longer to retire.
    """

    __slots__ = ("busy", "sweep_at")

    def __init__(self) -> None:
        self.busy = False
        self.sweep_at = float("-inf")

    def sweep_due(self, now: float) -> bool:
        return now >= self.sweep_at

    def after_sweep(self, now: float, *, retiring: bool) -> None:
        self.sweep_at = float("-inf") if retiring else now + MAX_RETRY_SECONDS


class _BoundaryBusy(Exception):
    """A mutation boundary stayed held for a whole acquisition timeout."""


def _raise_if_busy(error: Exception) -> None:
    from .cli_ops import OpError

    if isinstance(error, OpError) and error.code == "MUTATION_BUSY":
        raise _BoundaryBusy() from error


class _Turn:
    """One hold of the mutation boundary, shared by every unit of work it covers."""

    __slots__ = ("_coordinator", "_started")

    def __init__(self, coordinator: VaultMutationCoordinator) -> None:
        self._coordinator = coordinator
        self._started = time.monotonic()

    def spent(self) -> bool:
        """Whether to end the turn after this unit: a writer waits and the turn had its slice.

        A unit under the boundary cannot yield, so the turn ends between units
        after the same slice a bulk pass works between yields
        (`foreground_priority.MAX_BULK_WORK_SECONDS`). Without it a writer
        waits out every proof of the turn; when it ends a turn early, the
        rest of the pass waits for the next one and spends no attempt.
        """
        return (
            time.monotonic() - self._started >= foreground_priority.MAX_BULK_WORK_SECONDS
            and self._coordinator.waiter_count() > 0
        )


@contextmanager
def _boundary_turn(vault_root: Path, operation: str) -> Iterator[_Turn]:
    """Hold the boundary once for a phase; every inner guard re-enters it."""
    from .writer_lease import active_manager

    with ExitStack() as held:
        try:
            coordinator = held.enter_context(
                active_manager().consistency_guard(
                    vault_root, operation=operation, holder_kind="derived-worker"
                )
            )
        except Exception as error:
            _raise_if_busy(error)
            raise
        yield _Turn(coordinator)


def _claim_key(status: DerivedComponentStatus) -> tuple[str, str]:
    return status.batch_id, status.component.value


class _Pass:
    """One drain pass: its claims and tallies, carried across its boundary turns.

    The pass takes the boundary once per phase, never once per unit: one proof
    turn (imported handoff, recovery proofs, claims, and one proof per claimed
    batch), dispatch outside any turn, then completion turns that also claim
    and prove the successors a completed level promoted. An early cut ends a
    turn, never the pass: the next turn resumes the recovery, claim or proof
    it left. Completions are never cut.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        limit: int,
        started_at: float,
        owner: str,
        dispatch: ComponentDispatcher | None,
        observe: CanonicalGenerationObserver | None,
        publisher: PendingVisibilityPublisher | None,
    ) -> None:
        from . import index_sync

        self.vault_root = vault_root
        self.limit = limit
        self.started_at = started_at
        self.owner = owner
        self.dispatch = dispatch
        self.observe = observe
        self.publisher = publisher
        self.converge = index_sync.converge_paths_from_current_bytes
        # Every claim of the pass is leased at `started_at`. Stop starting
        # dispatches at half the lease, which leaves the rest for the unit
        # already running and the completion turn's acquisition timeout.
        self.dispatch_until = time.monotonic() + CLAIM_LEASE_SECONDS / 2
        self.outstanding: dict[tuple[str, str], DerivedComponentStatus] = {}
        self.recovery: list[tuple[str, RecoveryOutcome]] = []
        self.completed: list[str] = []
        self.handed_on = 0
        self.dispatched = 0
        self.claimed = 0
        self.max_attempt_count = 0
        self.oldest_due_at: float | None = None

    def run(self) -> None:
        if not deferred_index.store_path(self.vault_root).exists():
            return  # no custody yet, so no reason to take the boundary
        unhealthy = self._rotate_healthy()
        proven: list[DerivedComponentStatus] = []
        # Claimed but not yet proven: an early cut leaves them for the next turn.
        waiting: list[DerivedComponentStatus] = []
        handled: list[DerivedComponentStatus] = []
        settled: list[str] = []
        # The pass's first claim, then each dependency level a completion
        # promotes. A completion promotes its successors in the same store
        # transaction, so they are ready now: claim them inside this pass's
        # allowance instead of leaving each level for the next scheduler tick.
        # A failed claim backs off past this pass's clock, so nothing is
        # retried within it.
        claim_due = True
        first = True
        # Turns last while the pass has work and the dispatch window is open.
        # Every turn completes, recovers or proves at least one unit before a
        # cut can end it, so a writer that queues after every release still
        # lets the pass advance, and the recovery rotation above is paid once
        # per pass, not per cut.
        while True:
            foreground_priority.check_cancelled()
            operation = "derived_receipt_proof" if first else "derived_component_completion"
            with _boundary_turn(self.vault_root, operation) as turn:
                if first:
                    self._hand_on_imported()
                worked = bool(handled or unhealthy)
                completed_now = [*settled, *self._complete(handled)]
                claim_due = (claim_due or bool(completed_now)) and self.claimed < self.limit
                unhealthy = self._recover(turn, unhealthy)
                proven = []
                if not unhealthy and not (worked and turn.spent()):
                    if claim_due and not waiting:
                        waiting = list(self._claim(self.limit - self.claimed))
                        claim_due = False
                    proven, waiting = self._prove(turn, waiting)
            first = False
            if time.monotonic() >= self.dispatch_until:
                break
            handled, settled = self._dispatch(proven)
            proven = []
            if not (handled or settled or waiting or unhealthy or claim_due):
                break  # nothing to complete, recover, claim or prove: skip the acquisition
        self._release([*proven, *waiting])
        self._finish_recovery()

    def release_outstanding(self) -> None:
        self._release(list(self.outstanding.values()))

    def _rotate_healthy(self) -> list[str]:
        """Rotate healthy recovery candidates outside the boundary; return the rest."""
        if self.observe is None:
            return []
        unhealthy: list[str] = []
        candidates = derived_receipts.recovery_candidates(
            self.vault_root, limit=self.limit, now=self.started_at
        )
        for batch_id in foreground_priority.yielding_in_bulk(candidates):
            try:
                if not derived_receipts.rotate_if_healthy(
                    self.vault_root, batch_id, now=self.started_at
                ):
                    unhealthy.append(batch_id)
            except Exception as error:  # noqa: BLE001 - one batch cannot stop the rest
                derived_receipts.record_recovery_error(
                    self.vault_root, batch_id, error, now=self.started_at
                )
        return unhealthy

    def _hand_on_imported(self) -> None:
        key = _key(self.vault_root)
        with _PASS_LOCK:
            cursor = _CUSTODY_CURSORS.get(key, ("", ""))
        cursor, self.handed_on = derived_receipts.retire_imported_visibility(
            self.vault_root, limit=self.limit, after=cursor, now=self.started_at,
        )
        with _PASS_LOCK:
            if len(_CUSTODY_CURSORS) >= _PASS_OBSERVATION_LIMIT and key not in _CUSTODY_CURSORS:
                _CUSTODY_CURSORS.pop(next(iter(_CUSTODY_CURSORS)))
            _CUSTODY_CURSORS[key] = cursor

    def _recover(self, turn: _Turn, unhealthy: Sequence[str]) -> list[str]:
        """Prove unhealthy batches until the turn is spent; return the rest."""
        for index, batch_id in enumerate(unhealthy):
            try:
                outcome = derived_receipts.prove_recovery_batch(
                    self.vault_root,
                    batch_id,
                    observe_current_generation=self.observe,
                    visibility_publisher=self.publisher,
                    converge_current=self.converge,
                    now=self.started_at,
                )
            except Exception as error:  # noqa: BLE001 - callback failure stays durable and bounded
                _raise_if_busy(error)
                derived_receipts.record_recovery_error(
                    self.vault_root, batch_id, error, now=self.started_at
                )
            else:
                self.recovery.append((batch_id, outcome))
            if turn.spent():
                return list(unhealthy[index + 1:])
        return []

    def _finish_recovery(self) -> None:
        """Repair or rotate stranded recovery outcomes; repair takes its own guard."""
        for batch_id, outcome in self.recovery:
            try:
                derived_receipts.finish_recovery(
                    self.vault_root, batch_id, outcome,
                    converge_current=self.converge, now=self.started_at,
                )
            except Exception as error:  # noqa: BLE001 - callback failure stays durable and bounded
                _raise_if_busy(error)
                derived_receipts.record_recovery_error(
                    self.vault_root, batch_id, error, now=self.started_at
                )

    def _claim(self, limit: int) -> tuple[DerivedComponentStatus, ...]:
        claims = derived_receipts.claim_ready_components(
            self.vault_root,
            owner=self.owner,
            limit=limit,
            lease_seconds=CLAIM_LEASE_SECONDS,
            now=self.started_at,
        )
        if claims:
            self.claimed += len(claims)
            self.max_attempt_count = max(
                [self.max_attempt_count, *(status.attempt_count for status in claims)]
            )
            due = [status.next_attempt_at for status in claims]
            self.oldest_due_at = min(
                due if self.oldest_due_at is None else [self.oldest_due_at, *due]
            )
            self.outstanding.update((_claim_key(status), status) for status in claims)
        return claims

    def _prove(
        self, turn: _Turn, claims: Sequence[DerivedComponentStatus]
    ) -> tuple[list[DerivedComponentStatus], list[DerivedComponentStatus]]:
        """Prove each claimed batch once; return the proven and the unproven claims."""
        if self.observe is None:
            return list(claims), []
        by_batch: dict[str, list[DerivedComponentStatus]] = {}
        for status in claims:
            by_batch.setdefault(status.batch_id, []).append(status)
        proven: list[DerivedComponentStatus] = []
        unproven: list[DerivedComponentStatus] = []
        cut = False
        for batch_id, batch_claims in by_batch.items():
            if cut:
                unproven.extend(batch_claims)
                continue
            proven.extend(self._prove_batch(batch_id, batch_claims))
            cut = turn.spent()
        return proven, unproven

    def _prove_batch(
        self, batch_id: str, batch_claims: Sequence[DerivedComponentStatus]
    ) -> Sequence[DerivedComponentStatus]:
        """Prove one claimed batch; return its claims when it is ready to dispatch."""
        try:
            current_generation = self.observe(self.vault_root)
            if current_generation is None:
                raise RuntimeError("current generation observer is unavailable")
            receipt = derived_receipts._load_receipt(self.vault_root, batch_id)
            proof = derived_receipts.prove_committed(
                self.vault_root,
                receipt,
                current_generation=current_generation,
                now=self.started_at,
            )
        except Exception as error:  # noqa: BLE001 - exact claim remains retryable
            _raise_if_busy(error)
            log.warning(
                "derived component current-generation proof failed components=%s",
                ",".join(status.component.value for status in batch_claims),
                exc_info=True,
            )
            for status in batch_claims:
                self._retry(status, "handler_unavailable")
            return ()
        if proof.outcome == "ready":
            return batch_claims
        # Proof owns retirement/reconciliation. The stale claims no longer have
        # custody to dispatch or rotate.
        for status in batch_claims:
            self.outstanding.pop(_claim_key(status), None)
        return ()

    def _dispatch(
        self, proven: Sequence[DerivedComponentStatus]
    ) -> tuple[list[DerivedComponentStatus], list[str]]:
        """Dispatch proven claims outside any turn.

        Returns the handled claims awaiting completion, and the batches that a
        `not_required` settlement completed.
        """
        handled: list[DerivedComponentStatus] = []
        settled: list[str] = []
        for index, status in enumerate(foreground_priority.yielding_in_bulk(proven)):
            if time.monotonic() >= self.dispatch_until:
                self._release(proven[index:])
                break
            pending = False
            result: ComponentDispatchResult = False
            failure_code = "component_unhandled"
            try:
                with call_spans.span("derived.component_dispatch"):
                    if self.dispatch is not None:
                        self.dispatched += 1
                    result = False if self.dispatch is None else self.dispatch(self.vault_root, status)
                    pending = result == "pending"
                    if pending:
                        # Awaiting its executor, which signals the drain itself.
                        self.dispatched -= 1
                    accepted = not pending and bool(result)
                if self.dispatch is not None:
                    failure_code = "dispatch_failed"
            except Exception as error:  # noqa: BLE001 - exact custody remains retryable
                _raise_if_busy(error)
                accepted = False
                failure_code = "dispatch_failed"
                log.warning(
                    "derived component dispatch failed component=%s",
                    status.component.value,
                    exc_info=True,
                )
            if result == "not_required":
                self.outstanding.pop(_claim_key(status), None)
                try:
                    outcome = derived_receipts.settle_component_not_required(
                        self.vault_root, status, now=self.started_at
                    )
                except Exception:  # noqa: BLE001 - exact claim remains retryable
                    log.warning(
                        "derived component settle failed component=%s",
                        status.component.value,
                        exc_info=True,
                    )
                    outcome = "stale"
                if outcome == "batch_completed":
                    settled.append(status.batch_id)
                    self.completed.append(status.batch_id)
                    call_ledger.note_derived_event("component_completed")
                continue
            if pending:
                self._release((status,))
            elif accepted:
                handled.append(status)
            else:
                self._retry(status, failure_code)
        return handled, settled

    def _complete(self, handled: Sequence[DerivedComponentStatus]) -> list[str]:
        """Complete handled claims under the caller's turn; return their batches.

        Never cut short. Cutting here makes a writer wait less, but each extra
        turn is another chance of a refusal that ends the pass and redoes its
        dispatch; with holds longer than the acquisition timeout that cost the
        drain more than the turn saved the writer.
        """
        completed: list[str] = []
        for status in handled:
            failure_code = "generation_changed"
            try:
                with call_spans.span("derived.component_completion"):
                    completed_current = derived_receipts.complete_component(
                        self.vault_root,
                        status,
                        observe_current_generation=self.observe,
                        now=self.started_at,
                    )
            except Exception as error:  # noqa: BLE001 - callback/pass failure stays retryable
                _raise_if_busy(error)
                completed_current = False
                failure_code = "handler_unavailable"
                log.warning(
                    "derived component completion proof failed component=%s",
                    status.component.value,
                    exc_info=True,
                )
            if completed_current:
                self.outstanding.pop(_claim_key(status), None)
                completed.append(status.batch_id)
                self.completed.append(status.batch_id)
                call_ledger.note_derived_event("component_completed")
            else:
                self._retry(status, failure_code)
        return completed

    def _retry(self, status: DerivedComponentStatus, failure_code: str) -> None:
        self.outstanding.pop(_claim_key(status), None)
        try:
            derived_receipts.retry_component(
                self.vault_root,
                status,
                failure_code=failure_code,
                now=self.started_at,
                base_backoff_seconds=RETRY_SECONDS,
                max_backoff_seconds=MAX_RETRY_SECONDS,
            )
            call_ledger.note_derived_event("component_retried")
        except RuntimeError:
            # A newer claim/revision or proof transition already owns the row.
            log.debug(
                "derived component retry lost current custody component=%s",
                status.component.value,
            )

    def _release(self, claims: Sequence[DerivedComponentStatus]) -> None:
        """Hand claims back in one transaction without spending an attempt."""
        for status in claims:
            self.outstanding.pop(_claim_key(status), None)
        try:
            derived_receipts.defer_components(self.vault_root, claims, now=self.started_at)
        except Exception:  # noqa: BLE001 - an unreleased claim expires with its lease
            log.debug("derived component release failed claims=%d", len(claims), exc_info=True)


def drain_once(
    vault_root: Path,
    *,
    dispatch: ComponentDispatcher | None,
    observe_current_generation: CanonicalGenerationObserver | None = None,
    visibility_publisher: PendingVisibilityPublisher | None = None,
    retire_visibility: PendingVisibilityRetirer | None = None,
    limit: int,
    now: float | None = None,
    owner: str | None = None,
    cadence: DrainCadence | None = None,
) -> int:
    """Bound component dispatch and imported-custody handoff; return progress.

    Progress counts completed batches, handed-on custody and every component
    that was claimed and dispatched, so the scheduler keeps its pace while work
    is due even if no batch completed this pass.

    A mutation boundary held for a whole acquisition timeout ends the pass: its
    unsettled claims go back without spending an attempt, and ``cadence.busy``
    tells the drain thread. Without a ``cadence`` the settled sweep runs every pass.
    """
    if limit <= 0:
        return 0
    if cadence is not None:
        cadence.busy = False
    started_at = time.time() if now is None else float(now)
    work = _Pass(
        vault_root,
        limit=int(limit),
        started_at=started_at,
        owner=owner or f"derived-{secrets.token_hex(12)}",
        dispatch=dispatch,
        observe=observe_current_generation,
        publisher=visibility_publisher,
    )
    try:
        work.run()
    except _BoundaryBusy:
        if cadence is not None:
            cadence.busy = True
    finally:
        # Busy, cancelled or failed: nothing this pass still holds waits out its lease.
        work.release_outstanding()
    sweep = cadence is None or cadence.sweep_due(started_at)
    swept = (
        derived_receipts.completed_visibility_batches(vault_root, limit=int(limit), now=started_at)
        if sweep
        else ()
    )
    retirement_batches = tuple(dict.fromkeys((*work.completed, *swept)))
    retired: Collection[str] | None = None
    if retirement_batches and retire_visibility is not None:
        try:
            retired = retire_visibility(vault_root, retirement_batches)
        except Exception:  # noqa: BLE001 - read-side re-derivation; custody is unchanged
            log.warning("pending visibility retirement re-check failed", exc_info=True)
    if cadence is not None and sweep:
        # An unknown result keeps the sweep on every pass, as before the gate.
        only_swept = set(swept).difference(work.completed)
        cadence.after_sweep(
            started_at,
            retiring=bool(only_swept)
            and (retired is None or not only_swept.isdisjoint(retired)),
        )
    if work.completed:
        process_memory.trim_allocator()
    _note_pass_observation(
        vault_root,
        claimed=work.claimed,
        completed=len(work.completed),
        max_attempt_count=work.max_attempt_count,
        oldest_due_at=work.oldest_due_at,
        at=started_at,
    )
    return work.handed_on + max(len(work.completed), work.dispatched)


class DerivedDrain:
    """One prompt, bounded scheduler instance for a vault cell."""

    def __init__(
        self,
        vault_root: Path,
        *,
        dispatch: ComponentDispatcher | None = None,
        observe_current_generation: CanonicalGenerationObserver | None = None,
        visibility_publisher: PendingVisibilityPublisher | None = None,
        resource_limit: int | None = None,
        retire_visibility: PendingVisibilityRetirer | None = None,
    ) -> None:
        self.vault_root = Path(vault_root)
        # An unsupplied callback means "use production", never "run headless".
        # A drain with no router claims components it cannot dispatch, and one
        # with no observer returns zero from restart recovery: custody stays
        # durable and never converges, which is indistinguishable from a
        # working cell until the backlog is noticed.
        self.dispatch = component_dispatcher() if dispatch is None else dispatch
        self.observe_current_generation = (
            canonical_generation_observer()
            if observe_current_generation is None
            else observe_current_generation
        )
        self.visibility_publisher = (
            pending_visibility_publisher()
            if visibility_publisher is None
            else visibility_publisher
        )
        self.retire_visibility = (
            pending_visibility_retirer()
            if retire_visibility is None
            else retire_visibility
        )
        self.resource_limit = resource_limit
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner = f"derived-{secrets.token_hex(12)}"
        self._cadence = DrainCadence()

    def start(self) -> DerivedDrain:
        key = _key(self.vault_root)
        with _LOCK:
            existing = _ACTIVE.get(key)
            if existing is not None and existing is not self:
                thread = existing._thread
                if thread is not None and thread.is_alive():
                    raise RuntimeError("another derived drain owns this vault")
                _ACTIVE.pop(key, None)
            thread = self._thread
            if thread is not None and thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("derived drain is still stopping")
                _ACTIVE[key] = self
                return self
            self._stop.clear()
            self._wake.set()
            self._cadence = DrainCadence()
            self._thread = threading.Thread(
                target=self._run,
                name="exomem-derived-drain",
                daemon=True,
            )
            _ACTIVE[key] = self
            self._thread.start()
        return self

    def signal(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        interval = IDLE_POLL_SECONDS
        try:
            while not self._stop.is_set():
                self._wake.wait(timeout=interval)
                self._wake.clear()
                if self._stop.is_set():
                    break
                current = time.time()
                busy = False
                try:
                    limit = progress_limit(resource_limit=self.resource_limit)
                    # One tick reuses placement and pins the connector ceiling it first reads; admission
                    # carries that same boundary and egress re-reads it live. Executor threads get no memo.
                    # The pass is bulk work: it yields to in-flight requests outside its boundary turns.
                    with state_paths.resolution_scope(), foreground_priority.bulk(stop=self._stop):
                        processed = drain_once(
                            self.vault_root,
                            dispatch=self.dispatch,
                            observe_current_generation=self.observe_current_generation,
                            visibility_publisher=self.visibility_publisher,
                            retire_visibility=self.retire_visibility,
                            limit=limit,
                            now=current,
                            owner=self._owner,
                            cadence=self._cadence,
                        )
                        busy = self._cadence.busy
                        # Stranded batches keep the cadence too: recovery re-proves
                        # them each pass, and one heals once the recall lanes hold
                        # what moved under it.
                        pending = 0 if busy else (
                            derived_receipts.due_component_count(self.vault_root, now=current)
                            + derived_receipts.recoverable_batch_count(self.vault_root)
                            + derived_receipts.stranded_batch_count(self.vault_root)
                        )
                except foreground_priority.BulkCancelled:
                    log.info("derived drain pass cancelled; its claims went back unspent")
                    break
                except Exception:  # noqa: BLE001 - one bad pass cannot kill ownership
                    processed = 0
                    pending = 1
                    log.warning("derived component scheduler pass failed", exc_info=True)
                if busy:
                    # A writer held the boundary through a whole acquisition
                    # timeout. Wait on stop alone, so a write signal cannot start
                    # the next refusal at once; then run the next pass.
                    if self._stop.wait(RETRY_SECONDS):
                        break
                    interval = 0.0
                    continue
                if pending and processed:
                    interval = RETRY_SECONDS
                elif pending:
                    interval = min(
                        MAX_RETRY_SECONDS,
                        RETRY_SECONDS
                        if interval >= IDLE_POLL_SECONDS
                        else interval * 2,
                    )
                else:
                    interval = IDLE_POLL_SECONDS
        finally:
            with _LOCK:
                if _ACTIVE.get(_key(self.vault_root)) is self:
                    _ACTIVE.pop(_key(self.vault_root), None)

    def stop(self, timeout: float = HOSTED_STOP_DEADLINE_SECONDS) -> None:
        """Prove the pass thread is dead within ``timeout`` or fail closed."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is threading.current_thread():
            raise RuntimeError("derived drain cannot join itself")
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
            if thread.is_alive():
                raise TimeoutError("derived drain did not stop before its deadline")
        with _LOCK:
            if _ACTIVE.get(_key(self.vault_root)) is self:
                _ACTIVE.pop(_key(self.vault_root), None)


def start(
    vault_root: Path,
    *,
    dispatch: ComponentDispatcher | None = None,
    observe_current_generation: CanonicalGenerationObserver | None = None,
    visibility_publisher: PendingVisibilityPublisher | None = None,
    resource_limit: int | None = None,
) -> DerivedDrain:
    """Start once per vault and wake immediately for restart recovery."""
    key = _key(vault_root)
    with _LOCK:
        existing = _ACTIVE.get(key)
        if existing is not None and existing._thread is not None and existing._thread.is_alive():
            if existing._stop.is_set():
                raise RuntimeError("derived drain is still stopping")
            if dispatch is not None:
                existing.dispatch = dispatch
            if observe_current_generation is not None:
                existing.observe_current_generation = observe_current_generation
            if visibility_publisher is not None:
                existing.visibility_publisher = visibility_publisher
            existing.signal()
            return existing
        if existing is not None:
            _ACTIVE.pop(key, None)
            worker = existing
            if dispatch is not None:
                worker.dispatch = dispatch
            if observe_current_generation is not None:
                worker.observe_current_generation = observe_current_generation
            if visibility_publisher is not None:
                worker.visibility_publisher = visibility_publisher
            if resource_limit is not None:
                worker.resource_limit = resource_limit
        else:
            worker = DerivedDrain(
                vault_root,
                dispatch=dispatch,
                observe_current_generation=observe_current_generation,
                visibility_publisher=visibility_publisher,
                resource_limit=resource_limit,
            )
    return worker.start()


def signal(vault_root: Path) -> None:
    """Wake this process's owner; durable rows cover a missed signal."""
    with _LOCK:
        worker = _ACTIVE.get(_key(vault_root))
    if worker is not None:
        worker.signal()
