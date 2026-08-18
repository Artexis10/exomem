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


def _pending(vault_root: Path) -> bool:
    """True when the durable queue owes work. Never raises."""
    from . import deferred_index

    try:
        if deferred_index.graph_full_rebuild_pending(vault_root) is not None:
            return True
        return int(deferred_index.graph_status(vault_root).get("count") or 0) > 0
    except Exception:  # noqa: BLE001 - an unreadable queue must not kill the worker
        log.debug("graph drain: queue depth unreadable", exc_info=True)
        return False


def _drain_once(vault_root: Path) -> int:
    """One bounded drain. Never raises; returns receipts cleared."""
    from . import index_sync

    try:
        return int(index_sync.drain_graph_work(vault_root, limit=DRAIN_LIMIT) or 0)
    except Exception:  # noqa: BLE001 - queued work stays durable and retryable
        log.warning("graph drain: pass failed; work remains queued", exc_info=True)
        return 0


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
        processed = _drain_once(vault_root)
        if not _pending(vault_root):
            log.info("graph drain: queue settled (%d receipt(s) cleared)", processed)
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
