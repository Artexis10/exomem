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
  just after a burst, not the middle of one. For whole-vault work that moment is
  a quiet window as long as one whole-vault pass: a shorter gap cannot publish.
  The window is bounded: no attempt waits for it longer than the retry ceiling.
* **Bounded.** Debt that cannot be drained -- an unsettled epoch, a vault not yet
  ready -- must not spin. The retry interval backs off to a ceiling, so a queue
  that is stuck costs one attempt every couple of minutes rather than one per
  second. A write's debt signal cannot cut a whole-vault backoff short; only a
  publication by another owner can, because that is what ends the condition.

The periodic reconcile stays exactly as it was. It remains the cross-process
backstop for debt this process never observed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
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

#: Quiet time a whole-vault repair waits for after the last graph debt signal,
#: until this process has timed a whole-vault pass. A whole-vault pass publishes
#: only if no write lands while it runs, so starting one mid-burst buys a pass
#: the next write throws away. Once a pass has been timed, the window is that
#: pass's duration instead (up to the retry ceiling), with no floor: a gap
#: shorter than one pass cannot publish one, and a gap as long as one can.
#: Either way an attempt is held at most `MAX_RETRY_SECONDS` past the first
#: debt signal it was held for, because the window is a preference: a stream
#: that never pauses for one pass must still get attempts.
WHOLE_VAULT_SETTLE_SECONDS = 5.0

_LOCK = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()

#: Set whenever graph debt is enqueued in this process. Module-level rather than
#: per-vault: a server serves one vault, and a cross-vault signal costs at worst
#: one extra drain that finds an empty queue and goes back to sleep.
_DEBT = threading.Event()

#: Set when another owner published the graph, which ends any backoff the drain
#: is serving: the condition that backoff described is gone.
_PROGRESS = threading.Event()

#: Monotonic time of the last debt signal: the start of the current quiet window.
_last_debt = 0.0


def note_graph_debt() -> None:
    """Wake the drain: this process just queued epistemic-graph repair."""
    global _last_debt
    _last_debt = time.monotonic()
    _DEBT.set()


def note_graph_progress() -> None:
    """Wake the drain without waiting out a backoff: the graph was just published."""
    _PROGRESS.set()
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


def _marker_pending(vault_root: Path) -> bool:
    """True when a whole-vault rebuild marker stands. Never raises."""
    from . import deferred_index

    try:
        return deferred_index.graph_full_rebuild_pending(vault_root) is not None
    except Exception:  # noqa: BLE001 - an unreadable marker is not a standing one
        return False


def _whole_vault_pending(vault_root: Path) -> bool:
    """True when this pass would rebuild the whole vault rather than repair paths.

    A standing marker is converged by a whole-vault rebuild, and a barrier is
    recovered by one. Those are the passes a concurrent write invalidates, so
    they are the ones that wait for a quiet window and hold their backoff. An
    unavailable graph on its own is not: the ordinary per-path fence withdraws
    availability and the drain restores it by repairing the queued paths.
    """
    return _marker_pending(vault_root) or _barrier_pending(vault_root)


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
    from . import deferred_index, epistemic_graph, index_sync

    try:
        # A full marker is the one queue item whose work is a whole rebuild.
        # Keep the publication-refusal memo authoritative after the marker has
        # been recorded as well as before it: otherwise every scheduler retry
        # re-enters convergence and defeats the established bounded backoff.
        if (
            deferred_index.graph_full_rebuild_pending(vault_root) is not None
            and epistemic_graph.publication_refusal_active(vault_root)
        ):
            return 0
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


def _republish_once(vault_root: Path) -> bool:
    """Restore a stranded availability marker. Never raises; True when restored."""
    from . import epistemic_graph

    try:
        return bool(
            epistemic_graph.EpistemicGraphIndex(vault_root).republish_availability_if_current()
        )
    except Exception:  # noqa: BLE001 - the graph stays unavailable, so the signal stays
        log.warning("graph drain: availability republication failed", exc_info=True)
        return False


def _work_once(vault_root: Path) -> int:
    """Drain what is queued, then repair a barrier if one is still standing.

    Both in one pass, in that order: draining is proportional and may itself
    clear the condition the rebuild would have been re-run for.

    A whole-vault marker that is still standing after the drain means this pass
    has already spent its whole-vault attempt on it, and that attempt's
    publication would have cleared the barrier as well. Recovering the barrier
    too would be a second rebuild of the same graph in the same pass.
    """
    processed = _drain_once(vault_root) if _queue_pending(vault_root) else 0
    if _marker_pending(vault_root):
        return processed
    if _barrier_pending(vault_root):
        if _recover_once(vault_root):
            processed += 1
        else:
            from . import epistemic_graph, freshness

            # A barrier with an unpublished external epoch has no bounded
            # receipt coverage to recover from.  The ordinary recovery refuses
            # it correctly; route that unknown scope through the existing
            # guarded full-marker convergence instead of leaving the barrier
            # to retry a refusal forever.  A recent publication refusal keeps
            # its established backoff rather than spending another rebuild.
            if (
                freshness.external_pending(vault_root)
                and not epistemic_graph.publication_refusal_active(vault_root)
                and _request_full_rebuild(vault_root)
            ):
                processed += 1
    elif _availability_pending(vault_root):
        # Only where there is no barrier: with one standing, repair is the
        # cheaper and more specific answer, and it is the one that knows how to
        # decline. Reaching here means the graph is unreadable and nothing in
        # the system is holding a signal that says so.
        #
        # The stranded state lands here rather than in the drain above: a
        # withdrawn marker with an empty queue is not queued work, so
        # `_drain_once` -- and the republication inside it -- never runs, and a
        # whole-vault rebuild would be spent on a sidecar whose rows may already
        # match disk. Try that proof first; it declines on its own when repair
        # is genuinely owed, and the rebuild is still there when it does.
        if _republish_once(vault_root) or _request_full_rebuild(vault_root):
            processed += 1
    return processed


def _whole_vault_settle(vault_root: Path) -> float:
    """The quiet window a whole-vault pass needs: one timed pass, else the floor."""
    from . import epistemic_graph

    try:
        last_pass = epistemic_graph.last_whole_vault_pass_seconds(vault_root)
    except Exception:  # noqa: BLE001 - an unknown duration falls back to the floor
        last_pass = None
    if last_pass is None:
        return WHOLE_VAULT_SETTLE_SECONDS
    return min(MAX_RETRY_SECONDS, max(0.0, last_pass))


def _whole_vault_hold(vault_root: Path, not_before: float, held_since: float | None) -> float:
    """Seconds a whole-vault pass must still wait, or 0.0 when it may run now.

    Two waits, whichever ends later: the backoff after an attempt that made no
    progress, and a quiet window since the last debt signal. The quiet window
    never extends past `MAX_RETRY_SECONDS` after `held_since`, the first debt
    signal this attempt was held for. Per-path repair is never held -- it is
    proportional, and a write landing mid-drain only appends work rather than
    invalidating it.
    """
    now = time.monotonic()
    quiet = _last_debt + _whole_vault_settle(vault_root) - now
    if held_since is not None:
        quiet = min(quiet, held_since + MAX_RETRY_SECONDS - now)
    return max(0.0, not_before - now, quiet)


def _run(vault_root: Path) -> None:
    interval = IDLE_POLL_SECONDS
    # The no-progress backoff (0.0 after progress), the earliest moment the next
    # whole-vault attempt may start, and the first debt signal the next
    # whole-vault attempt is being held for (None while nothing is held).
    backoff = 0.0
    not_before = 0.0
    held_since: float | None = None
    while not _stop.is_set():
        signalled = _DEBT.wait(timeout=interval)
        if _stop.is_set():
            break
        _DEBT.clear()
        if _PROGRESS.is_set():
            # Another owner published the graph; the backoff described a graph
            # that no longer exists.
            _PROGRESS.clear()
            backoff = 0.0
            not_before = 0.0
            held_since = None
        if signalled:
            # Settle. `wait` returning True here means a stop was requested.
            if _stop.wait(DEBOUNCE_SECONDS):
                break
        if not _pending(vault_root):
            backoff = 0.0
            held_since = None
            interval = IDLE_POLL_SECONDS
            continue
        whole_vault = _whole_vault_pending(vault_root)
        hold = _whole_vault_hold(vault_root, not_before, held_since) if whole_vault else 0.0
        if hold > 0.0:
            # A write signal during the hold only wakes this loop to re-read the
            # quiet window; it can never start the attempt early. Every write
            # of a burst therefore shares the one attempt after it.
            if held_since is None:
                held_since = _last_debt or time.monotonic()
            interval = hold
            continue
        # This attempt is no longer held; the next one gets its own ceiling.
        held_since = None
        processed = _work_once(vault_root)
        if not _pending(vault_root):
            log.info("graph drain: graph settled (%d unit(s) of work cleared)", processed)
            backoff = 0.0
            not_before = 0.0
            interval = IDLE_POLL_SECONDS
        elif processed:
            # Progress with work left: come straight back for the remainder.
            backoff = 0.0
            not_before = 0.0
            interval = RETRY_SECONDS
        else:
            # No progress. The ordinary cause is an epoch that is not settled
            # yet, which the next pass clears -- so retry, but back off, because
            # the other cause is a queue that cannot drain at all and must not
            # become a busy loop.
            backoff = min(MAX_RETRY_SECONDS, RETRY_SECONDS if backoff <= 0.0 else backoff * 2)
            interval = backoff
            if whole_vault:
                # A whole-vault attempt that lost -- to a busy boundary, a
                # rebuild already in flight, or a write landing mid-pass -- is
                # retried on the backoff alone, never on a write's signal.
                not_before = time.monotonic() + backoff


def start(vault_root: Path) -> threading.Thread | None:
    """Start the drain daemon. Idempotent -- a second call returns the live one."""
    global _thread, _last_debt
    if disabled():
        log.info("graph drain disabled by EXOMEM_DISABLE_GRAPH_DRAIN")
        return None
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _PROGRESS.clear()
        # A new daemon knows of no burst in progress; its first whole-vault pass
        # waits only for signals it hears itself.
        _last_debt = 0.0
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
