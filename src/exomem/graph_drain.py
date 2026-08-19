"""Background drain for the durable epistemic-graph repair queue.

The queue and the incremental drain that consumes it both work. What was never
wired is a *scheduler*: every call site of the graph drain lived inside
``file_watcher._reconcile_once``, which runs on ``reconcile_interval_seconds`` --
300s by default, 900s in quiet mode -- and the watcher is optional. It returns
``False`` and logs a no-op when ``watchdog`` is absent, and the server skips it
entirely under ``EXOMEM_DISABLE_FILE_WATCHER``.

So graph convergence depended on an optional component, and where that component
was missing nothing drained the queue at all. A write left ``graph_sync.status()``
reporting ``recovery_required`` and readers getting ``graph sidecar unavailable``,
with the repair already queued, already admissible, and nothing scheduled to run
it. The product E2E proved it: ``epoch_kind='coherent'``,
``external_pending=False``, ``graph_queue_depth=13``, unchanged across the whole
120s it waits.

This owns that schedule and nothing else. It does not repair anything itself --
``index_sync.drain_graph_work`` does the work, unchanged. It decides *when*.

Three properties the timing has to have:

* **Prompt.** A signal fires when debt is enqueued, so repair follows the write
  that caused it rather than the next tick of a five-minute clock.
* **Settled.** A short debounce after that signal lets a burst of writes finish.
  Draining into a live batch wastes a pass: the epoch will not admit incremental
  repair mid-flight, and a whole-vault rebuild loses its optimistic
  concurrency check to whichever write lands next. Repair wants the quiet moment
  just after a burst, not the middle of one.
* **Bounded.** Debt that cannot be drained -- an unsettled epoch, a vault not yet
  ready -- must not spin. The retry interval backs off to a ceiling, so a queue
  that is stuck costs one attempt every couple of minutes rather than one per
  second.

The periodic reconcile stays exactly as it was. It remains the cross-process
backstop for debt this process never observed.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

#: Let a burst of writes settle before draining. Repair dispatched into a live
#: canonical batch is a wasted pass, not a faster one.
DEBOUNCE_SECONDS = 1.0

#: Fallback poll with no local signal, so debt enqueued by another process (a
#: CLI `exomem index`, a second server) is still picked up here.
IDLE_POLL_SECONDS = 30.0

#: First retry after a drain that made no progress.
RETRY_SECONDS = 5.0

#: Ceiling for that retry. A queue that cannot drain costs one attempt every two
#: minutes, not one per second.
MAX_RETRY_SECONDS = 120.0

#: Paths per drain. Bounds one pass so a large queue cannot hold the worker in a
#: single call, matching the cap the periodic reconcile already applies.
DRAIN_LIMIT = 64

_LOCK = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()

#: Set whenever graph debt is enqueued in this process. Module-level rather than
#: per-vault: a server serves one vault, and a cross-vault signal costs at worst
#: one extra drain that finds an empty queue and goes back to sleep.
_DEBT = threading.Event()


def note_graph_debt() -> None:
    """Wake the drain: this process just queued epistemic-graph repair."""
    _DEBT.set()


def disabled() -> bool:
    """True when the operator has turned the drain off.

    A kill switch rather than a config knob. The failure this exists to prevent
    is silence, so the lever to turn it off should be as blunt and as visible as
    the one for the watcher beside it.
    """
    return bool(os.environ.get("EXOMEM_DISABLE_GRAPH_DRAIN"))


def _barrier_pending(vault_root: Path) -> bool:
    """True when a stopped rebuild left a barrier to repair. Never raises.

    Debt the queue cannot express. A rebuild that stops is terminal --
    `graph_sync` records the error, clears `_running` and returns -- and the
    persisted barrier it leaves behind is the retry signal.
    """
    from . import epistemic_graph

    try:
        if not epistemic_graph.graph_enabled():
            return False
        if not epistemic_graph.sidecar_path(vault_root).exists():
            return False
        return bool(epistemic_graph.EpistemicGraphIndex(vault_root).reads_suspended())
    except Exception:  # noqa: BLE001 - an unreadable barrier must not kill the worker
        log.debug("graph drain: barrier state unreadable", exc_info=True)
        return False


def _availability_pending(vault_root: Path) -> bool:
    """True when readers cannot use the graph and a rebuild could still fix it.

    The barrier is the *ordinary* signal that a rebuild stopped, and the one
    recovery acts on -- but it is not the only route to an unreadable graph. A
    rebuild that exhausts its publication attempts leaves no barrier at all,
    and the drain, seeing neither queued work nor a barrier, reported the graph
    settled and went idle against a graph no reader could open.

    That is not hypothetical. A product E2E run on `main` caught it exactly:
    generation 1 published, later generations died Class C ("the recall
    projection identity moved across the pass") and then
    `GRAPH_SYNC_STABILIZATION_EXHAUSTED`, and from there the queue logged
    "graph settled" three times in five seconds while every read answered
    `graph sidecar unavailable` -- for the remaining 120s, until the run failed.

    Two states are deliberately not debt, because no amount of draining changes
    them: a vault with no sidecar has nothing to repair yet, and a disabled
    graph is an operator's decision. Both would otherwise poll forever.
    """
    from . import epistemic_graph

    try:
        if not epistemic_graph.graph_enabled():
            return False
        if not epistemic_graph.sidecar_path(vault_root).exists():
            return False
        return not epistemic_graph.EpistemicGraphIndex(vault_root).available()
    except Exception:  # noqa: BLE001 - unreadable availability must not kill the worker
        log.debug("graph drain: availability unreadable", exc_info=True)
        return False


def _request_full_rebuild(vault_root: Path) -> bool:
    """Queue a whole-vault rebuild for an unreadable graph. True when queued.

    Routed through the durable marker rather than by calling the rebuild here,
    so the one path that runs rebuilds keeps running them and this stays a
    statement of debt. It is also why the retry rate is already bounded: the
    marker makes the queue non-empty, so the next pass is an ordinary drain
    under the backoff that is already proven for work that cannot clear.

    Never queues a second marker over a standing one -- that would be the same
    debt counted twice, and would keep `processed` non-zero, which is how the
    caller decides it is making progress.
    """
    from . import deferred_index, graph_sync

    try:
        if deferred_index.graph_full_rebuild_pending(vault_root) is not None:
            return False
        generation = int(graph_sync.status(vault_root).get("generation") or 0)
        deferred_index.mark_graph_full_rebuild(vault_root, generation=generation)
    except Exception:  # noqa: BLE001 - the graph stays unavailable, so the signal stays
        log.warning("graph drain: could not queue a rebuild for an unreadable graph")
        return False
    log.info(
        "graph drain: graph unreadable with no barrier; queued a whole-vault "
        "rebuild at generation %d",
        generation,
    )
    return True


def _queue_pending(vault_root: Path) -> bool:
    """True when the durable queue owes work. Never raises."""
    from . import deferred_index

    try:
        if deferred_index.graph_full_rebuild_pending(vault_root) is not None:
            return True
        return int(deferred_index.graph_status(vault_root).get("count") or 0) > 0
    except Exception:  # noqa: BLE001 - an unreadable queue must not kill the worker
        log.debug("graph drain: queue depth unreadable", exc_info=True)
        return False


def _pending(vault_root: Path) -> bool:
    """True when the graph owes work of either kind.

    Folding the barrier in here rather than giving recovery its own schedule is
    deliberate: it makes a stopped rebuild ordinary debt, so the backoff already
    proven for a queue that cannot drain covers a rebuild that cannot publish --
    one attempt every couple of minutes rather than a full rebuild every poll.
    """
    return (
        _queue_pending(vault_root)
        or _barrier_pending(vault_root)
        or _availability_pending(vault_root)
    )


def _recover_once(vault_root: Path) -> bool:
    """Re-arm a rebuild that stopped. Never raises; True when it recovered.

    Draining the queue is not the whole of convergence. When the incremental
    path falls back, the whole-vault rebuild that replaces it is terminal if it
    stops: `graph_sync` records the error, clears `_running` and returns. The
    persisted barrier it leaves is the retry signal, and until now the only
    thing that acted on it was the watcher's reconcile -- 300s, and skipped
    entirely under `EXOMEM_DISABLE_FILE_WATCHER`.

    A product E2E run showed the cost exactly: the queue settled four times in
    seven seconds, the rebuild stopped at +7.3s, and the server then answered
    readiness polls for the remaining 120s without ever attempting another one.
    `recover_suspended_graph` declines by itself when the barrier is absent or
    when a publication is already proven doomed for this checkpoint, so calling
    it on a settled queue costs a few cheap checks in the ordinary case.
    """
    from . import epistemic_graph

    try:
        return bool(epistemic_graph.recover_suspended_graph(vault_root))
    except Exception:  # noqa: BLE001 - the barrier stays, so the signal stays
        log.warning("graph drain: barrier recovery failed; barrier remains", exc_info=True)
        return False


def _drain_once(vault_root: Path) -> int:
    """One bounded drain. Never raises; returns receipts cleared."""
    from . import index_sync

    try:
        return int(index_sync.drain_graph_work(vault_root, limit=DRAIN_LIMIT) or 0)
    except Exception:  # noqa: BLE001 - queued work stays durable and retryable
        log.warning("graph drain: pass failed; work remains queued", exc_info=True)
        return 0


def _recover_once(vault_root: Path) -> bool:
    """Re-arm a rebuild that stopped. Never raises; True when it recovered.

    `recover_suspended_graph` declines on its own when the barrier is absent, a
    fresh external change is pending, or a publication is already proven doomed
    for this exact checkpoint (contract R2) -- so this is safe to reach on any
    pass that finds a barrier.
    """
    from . import epistemic_graph

    try:
        return bool(epistemic_graph.recover_suspended_graph(vault_root))
    except Exception:  # noqa: BLE001 - the barrier stays, so the signal stays
        log.warning("graph drain: barrier recovery failed; barrier remains", exc_info=True)
        return False


def _work_once(vault_root: Path) -> int:
    """Drain what is queued, then repair a barrier if one is still standing.

    Both in one pass, in that order: draining is proportional and may itself
    clear the condition the rebuild would have been re-run for.
    """
    processed = _drain_once(vault_root) if _queue_pending(vault_root) else 0
    if _barrier_pending(vault_root):
        if _recover_once(vault_root):
            processed += 1
    elif _availability_pending(vault_root) and _request_full_rebuild(vault_root):
        # Only where there is no barrier: with one standing, repair is the
        # cheaper and more specific answer, and it is the one that knows how to
        # decline. Reaching here means the graph is unreadable and nothing in
        # the system is holding a signal that says so.
        processed += 1
    return processed


def _run(vault_root: Path) -> None:
    interval = IDLE_POLL_SECONDS
    while not _stop.is_set():
        signalled = _DEBT.wait(timeout=interval)
        if _stop.is_set():
            break
        _DEBT.clear()
        if signalled:
            # Settle. `wait` returning True here means a stop was requested.
            if _stop.wait(DEBOUNCE_SECONDS):
                break
        if not _pending(vault_root):
            interval = IDLE_POLL_SECONDS
            continue
        processed = _work_once(vault_root)
        if not _pending(vault_root):
            log.info("graph drain: graph settled (%d unit(s) of work cleared)", processed)
            interval = IDLE_POLL_SECONDS
        elif processed:
            # Progress with work left: come straight back for the remainder.
            interval = RETRY_SECONDS
        else:
            # No progress. The ordinary cause is an epoch that is not settled
            # yet, which the next pass clears -- so retry, but back off, because
            # the other cause is a queue that cannot drain at all and must not
            # become a busy loop.
            interval = min(
                MAX_RETRY_SECONDS, RETRY_SECONDS if interval >= IDLE_POLL_SECONDS else interval * 2
            )


def start(vault_root: Path) -> threading.Thread | None:
    """Start the drain daemon. Idempotent -- a second call returns the live one."""
    global _thread
    if disabled():
        log.info("graph drain disabled by EXOMEM_DISABLE_GRAPH_DRAIN")
        return None
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _DEBT.set()  # Drain once at startup: debt can outlive the process that queued it.
        thread = threading.Thread(
            target=_run,
            args=(Path(vault_root),),
            name="exomem-graph-drain",
            daemon=True,
        )
        _thread = thread
        thread.start()
        log.info("graph drain started on %s", vault_root)
        return thread


def stop(timeout: float = 2.0) -> None:
    """Stop the drain daemon and wait briefly for it to finish."""
    global _thread
    with _LOCK:
        thread = _thread
        _thread = None
    _stop.set()
    _DEBT.set()
    if thread is not None:
        thread.join(timeout=timeout)
