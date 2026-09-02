"""Bounded server-owned scheduling for exact derived component custody.

Component business logic is injected through ``dispatch``. This module owns
only claim limits, retry rotation, prompt wake-up, and clean lifecycle.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import call_ledger, call_spans, derived_receipts, mode
from .derived_receipts import DerivedComponentStatus

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

ComponentDispatcher = Callable[[Path, DerivedComponentStatus], bool]
CanonicalGenerationObserver = Callable[[Path], str | None]
PendingVisibilityPublisher = Callable[
    [Path, derived_receipts.DerivedBatchReceipt], bool
]

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


def _key(vault_root: Path) -> str:
    return str(Path(vault_root).resolve(strict=False))


def progress_limit(*, mode_name: str | None = None, resource_limit: int | None = None) -> int:
    """Return a bounded pass allowance with one correctness slot minimum."""
    selected = mode.normalize(mode_name) if mode_name is not None else mode.resolve_mode()
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

    def dispatch(vault_root: Path, status: DerivedComponentStatus) -> bool:
        if status.component is derived_receipts.DerivedComponent.WRITE_ADVISORY:
            from . import deferred_write_advisory

            with call_spans.span("derived.advisory_execute"):
                execution = deferred_write_advisory.execute_write_advisory(
                    vault_root,
                    status,
                    observe_current_generation=canonical_generation_observer(),
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
            return execution.outcome in {
                "published",
                "already_published",
                "superseded",
            }
        from . import index_sync

        receipt = derived_receipts._load_receipt(vault_root, status.batch_id)
        return index_sync.converge_derived_component(
            vault_root, receipt, status.component
        )

    return dispatch


def drain_once(
    vault_root: Path,
    *,
    dispatch: ComponentDispatcher | None,
    observe_current_generation: CanonicalGenerationObserver | None = None,
    visibility_publisher: PendingVisibilityPublisher | None = None,
    limit: int,
    now: float | None = None,
    owner: str | None = None,
) -> int:
    """Claim and dispatch no more than ``limit`` exact component revisions."""
    if limit <= 0:
        return 0
    started_at = time.time() if now is None else float(now)
    claim_owner = owner or f"derived-{secrets.token_hex(12)}"
    derived_receipts.recover_prepared_batches(
        vault_root,
        observe_current_generation=observe_current_generation,
        visibility_publisher=visibility_publisher,
        limit=int(limit),
        now=started_at,
    )
    claims = derived_receipts.claim_ready_components(
        vault_root,
        owner=claim_owner,
        limit=int(limit),
        lease_seconds=CLAIM_LEASE_SECONDS,
        now=started_at,
    )
    completed = 0
    max_attempt_count = max((status.attempt_count for status in claims), default=0)
    oldest_due_at = min((status.next_attempt_at for status in claims), default=None)
    for status in claims:
        failure_code = "component_unhandled"
        if observe_current_generation is not None:
            try:
                current_generation = observe_current_generation(vault_root)
                if current_generation is None:
                    raise RuntimeError("current generation observer is unavailable")
                receipt = derived_receipts._load_receipt(vault_root, status.batch_id)
                proof = derived_receipts.prove_committed(
                    vault_root,
                    receipt,
                    current_generation=current_generation,
                    now=started_at,
                )
            except Exception:  # noqa: BLE001 - exact claim remains retryable
                failure_code = "handler_unavailable"
                log.warning(
                    "derived component current-generation proof failed component=%s",
                    status.component.value,
                    exc_info=True,
                )
            else:
                if proof.outcome != "ready":
                    # Proof owns retirement/reconciliation.  The stale claim no
                    # longer has custody to dispatch or rotate.
                    continue
                failure_code = ""
        if failure_code == "handler_unavailable":
            handled = False
        else:
            failure_code = "component_unhandled"
            try:
                with call_spans.span("derived.component_dispatch"):
                    handled = (
                        False if dispatch is None else bool(dispatch(vault_root, status))
                    )
                if dispatch is not None:
                    failure_code = "dispatch_failed"
            except Exception:  # noqa: BLE001 - exact custody remains retryable
                handled = False
                failure_code = "dispatch_failed"
                log.warning(
                    "derived component dispatch failed component=%s",
                    status.component.value,
                    exc_info=True,
                )
        completion_failed = False
        if handled:
            try:
                with call_spans.span("derived.component_completion"):
                    completed_current = derived_receipts.complete_component(
                        vault_root,
                        status,
                        observe_current_generation=observe_current_generation,
                        now=started_at,
                    )
            except Exception:  # noqa: BLE001 - callback/pass failure stays retryable
                completed_current = False
                completion_failed = True
                failure_code = "handler_unavailable"
                log.warning(
                    "derived component completion proof failed component=%s",
                    status.component.value,
                    exc_info=True,
                )
            if completed_current:
                completed += 1
                call_ledger.note_derived_event("component_completed")
                continue
            if not completion_failed:
                failure_code = "generation_changed"
        try:
            derived_receipts.retry_component(
                vault_root,
                status,
                failure_code=failure_code,
                now=started_at,
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
    _note_pass_observation(
        vault_root,
        claimed=len(claims),
        completed=completed,
        max_attempt_count=max_attempt_count,
        oldest_due_at=oldest_due_at,
        at=started_at,
    )
    return completed


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
        self.resource_limit = resource_limit
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner = f"derived-{secrets.token_hex(12)}"

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
                try:
                    limit = progress_limit(resource_limit=self.resource_limit)
                    processed = drain_once(
                        self.vault_root,
                        dispatch=self.dispatch,
                        observe_current_generation=self.observe_current_generation,
                        visibility_publisher=self.visibility_publisher,
                        limit=limit,
                        now=current,
                        owner=self._owner,
                    )
                    pending = derived_receipts.due_component_count(
                        self.vault_root, now=current
                    ) + derived_receipts.recoverable_batch_count(self.vault_root)
                except Exception:  # noqa: BLE001 - one bad pass cannot kill ownership
                    processed = 0
                    pending = 1
                    log.warning("derived component scheduler pass failed", exc_info=True)
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
