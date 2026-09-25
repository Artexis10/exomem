"""The dreamer: one default-off background thread that proposes bounded upkeep.

What it is. A worker inside the managed local service that reads derived state
and pages and writes ONLY its own disposable sidecar (`dreamer_store`) in the
machine-local vault state directory. What it hands the agent is called upkeep:
a small number of structural proposals, each an existing governed action the
agent may take or dismiss. "Dreamer" stays the operator-facing name.

The seven nevers, each pinned by `tests/test_dreamer_no_side_effects.py`. It
never writes the vault, never takes the writer lease or the mutation guard,
never enqueues graph debt, never marks freshness pending, never builds or
repairs an index, never writes review state, and never loads or runs a model.

When it runs. Only when the service is idle and the graph owes nothing
(`dreamer_policy.decide`): graph work strictly outranks upkeep and there is no
max-wait, because nothing online waits on it. Inside a tick it yields per page
and stops before the next page on the first foreground request or the first
vault change. Starvation is the safe failure, and `status()` reports it.

What it reads. The freshness registry's delta, or an exact diff of the live
signature map against its persisted `seen` map (`dreamer_delta`). Never a
filesystem walk, and no vault-wide snapshot to lose to a concurrent write.

Controls. `EXOMEM_DREAMER` (a kill switch that overrides the file), else the
config key `dreamer` in the per-machine config file: `off` (default: no thread,
no sidecar), `on`, or `paused` (the thread lives, runs no tick and keeps its
checkpoint). The worker re-reads the setting once per poll, so pause and resume
take effect without a restart. There is deliberately no out-of-process run:
a second process driving the pass against a live service is exactly what the
live-cell rules forbid.
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dreamer_delta, dreamer_families, dreamer_store, foreground_activity, freshness
from . import dreamer_policy as policy

log = logging.getLogger(__name__)

ENV = "EXOMEM_DREAMER"
CONFIG_KEY = "dreamer"
THREAD_NAME = "exomem-dreamer"


@dataclass(frozen=True)
class Clock:
    """The three clocks a tick reads. The test seam replaces them."""

    time: Callable[[], float] = _time.time
    monotonic: Callable[[], float] = _time.monotonic
    thread_time: Callable[[], float] = _time.thread_time


@dataclass(frozen=True)
class Budget:
    pages: int = policy.PAGES_PER_TICK
    cpu: float = policy.TICK_CPU_SECONDS
    wall: float = policy.TICK_WALL_SECONDS


@dataclass(frozen=True)
class TickResult:
    ran: bool
    processed: tuple[str, ...]
    stop_reason: str
    wall: float
    cpu: float
    error_code: str | None = None
    #: Why pages were held this tick (a closed code), or None.
    waiting: str | None = None


class CpuLedger:
    """Thread CPU spent per rolling window, for the hourly budget."""

    def __init__(self, window: float = policy.CPU_WINDOW_SECONDS) -> None:
        self.window = window
        self._entries: deque[tuple[float, float]] = deque()

    def add(self, now: float, seconds: float) -> None:
        if seconds > 0:
            self._entries.append((now, float(seconds)))
        self._expire(now)

    def used(self, now: float) -> float:
        self._expire(now)
        return sum(seconds for _stamp, seconds in self._entries)

    def _expire(self, now: float) -> None:
        while self._entries and now - self._entries[0][0] >= self.window:
            self._entries.popleft()


@dataclass
class _State:
    """This process's in-memory worker state. Holds no path and no content."""

    setting: str = "off"
    phase: str = "off"
    waiting_reason: str | None = None
    waiting_since: float | None = None
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    failed_since: float | None = None
    last_tick_at: float | None = None
    last_stop_reason: str | None = None
    last_error_code: str | None = None
    ticks: int = 0
    pages_processed: int = 0
    loops: int = 0
    vault_generation: int | None = None
    vault_changed_at: float = field(default_factory=_time.monotonic)
    ledger: CpuLedger = field(default_factory=CpuLedger)


_LOCK = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_STATE = _State()

#: `(id, fingerprint)` pairs disposed of in this process since the worker's last
#: precompute: delivery stops at once rather than on the next pass. Bounded.
_DISPOSED: dict[tuple[str, str], None] = {}
_DISPOSED_LIMIT = 512


def setting() -> str:
    """The resolved operator setting: env, then config file, then `off`."""
    from . import mode

    return policy.resolve_setting(os.environ.get(ENV), mode.read_config())


def write_setting(value: str) -> Path:
    """Persist the operator setting in the per-machine config file (atomic).

    Every other key in that shared file is preserved. Raises on an unknown
    value and on an unwritable file.
    """
    import json

    from . import mode

    value = str(value or "").strip().lower()
    if value not in policy.SETTINGS:
        raise ValueError(f"unknown dreamer setting: {value!r} (expected one of {policy.SETTINGS})")
    path = mode.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = mode.read_config()
    data[CONFIG_KEY] = value
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def note_disposition(cid: str, fingerprint: str) -> None:
    """Record that an item was just triaged, so this process stops offering it."""
    with _LOCK:
        _DISPOSED.pop((cid, fingerprint), None)
        _DISPOSED[(cid, fingerprint)] = None
        while len(_DISPOSED) > _DISPOSED_LIMIT:
            _DISPOSED.pop(next(iter(_DISPOSED)))


def disposed(cid: str, fingerprint: str) -> bool:
    with _LOCK:
        return (cid, fingerprint) in _DISPOSED


def reset_for_tests() -> None:
    global _STATE
    stop()
    with _LOCK:
        _STATE = _State()
        _DISPOSED.clear()


# ----------------------------------------------------------------------
# lifecycle
# ----------------------------------------------------------------------


def start(vault_root: Path) -> threading.Thread | None:
    """Start the worker when enabled. Idempotent; off creates nothing at all."""
    global _thread
    current = setting()
    if current == "off":
        return None
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _STATE.setting = current
        _STATE.phase = current
        _STATE.vault_changed_at = _time.monotonic()
        thread = threading.Thread(
            target=_run, args=(Path(vault_root),), name=THREAD_NAME, daemon=True
        )
        _thread = thread
        thread.start()
    log.info("dreamer started (%s)", current)
    return thread


def stop(timeout: float = 2.0) -> None:
    """Stop the worker and wait briefly. Safe to call twice or when never started."""
    global _thread
    with _LOCK:
        thread = _thread
        _thread = None
    _stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=timeout)


def running() -> bool:
    with _LOCK:
        return _thread is not None and _thread.is_alive()


def delivering() -> bool:
    """True when this process serves upkeep items: its worker runs, on or paused.

    The activation carrier is live only here, where deliveries are remembered
    and then recorded by the worker. Reads no file.
    """
    with _LOCK:
        alive = _thread is not None and _thread.is_alive()
        return alive and _STATE.setting in {"on", "paused"}


def failure() -> dict[str, Any] | None:
    """`{"since": <UTC date-time>}` while the worker is failing, else None."""
    with _LOCK:
        if (
            _STATE.setting == "off"
            or _STATE.consecutive_failures < policy.FAILED_AFTER
            or _STATE.failed_since is None
        ):
            return None
        since = _STATE.failed_since
    return {"since": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(since))}


def _run(vault_root: Path) -> None:
    clock = Clock()
    while not _stop.is_set():
        try:
            sleep = _loop_once(vault_root, clock)
        except Exception:  # noqa: BLE001 - the worker must outlive any one loop
            log.warning("dreamer: loop failed", exc_info=True)
            sleep = policy.POLL_SECONDS
        if _stop.wait(sleep):
            break


def _loop_once(vault_root: Path, clock: Clock) -> float:
    """One gate evaluation and, when it opens, one tick. Returns the sleep."""
    signals = gather_signals(vault_root, clock)
    decision = policy.decide(signals)
    with _LOCK:
        _STATE.setting = signals.setting
    if not decision.run:
        _note_waiting(decision.reason, clock)
        sleep = decision.sleep_s
    else:
        with _LOCK:
            _STATE.waiting_reason = None
            _STATE.waiting_since = None
        result = run_once(vault_root, clock=clock, should_stop=_stop.is_set)
        # A tick that moved nothing forward (nothing to do, or only pages that
        # cannot run yet) waits a full poll, not a duty cycle.
        sleep = policy.sleep_after_tick(result.wall) if result.processed else policy.POLL_SECONDS
    with _LOCK:
        _STATE.loops += 1
    return sleep


def _note_waiting(reason: str, clock: Clock) -> None:
    with _LOCK:
        if _STATE.waiting_reason != reason:
            _STATE.waiting_since = clock.time()
        _STATE.waiting_reason = reason
        if reason in {"off", "paused"}:
            _STATE.phase = reason
        elif _STATE.consecutive_failures >= policy.FAILED_AFTER:
            _STATE.phase = "failed"
        else:
            _STATE.phase = "waiting"


def gather_signals(vault_root: Path, clock: Clock | None = None) -> policy.TickSignals:
    """Probe the gate once. The costly probes run only when the cheap ones pass."""
    from . import mode, service_standby

    clock = clock or Clock()
    now = clock.monotonic()
    current = setting()
    generation = freshness.generation(vault_root, dreamer_delta.SCOPE)
    with _LOCK:
        if generation != _STATE.vault_generation:
            _STATE.vault_generation = generation
            _STATE.vault_changed_at = now
        quiet = max(0.0, now - _STATE.vault_changed_at)
        failures = _STATE.consecutive_failures
        since_failure = (
            None if _STATE.last_failure_at is None else max(0.0, now - _STATE.last_failure_at)
        )
        hour_cpu = _STATE.ledger.used(now)
    try:
        from . import epistemic_graph

        last_pass = epistemic_graph.last_whole_vault_pass_seconds(vault_root)
    except Exception:  # noqa: BLE001 - an unknown pass length falls back to the floor
        last_pass = None
    signals = policy.TickSignals(
        setting=current,
        standby=service_standby.in_standby(),
        compute_mode=mode.resolve_mode(),
        pressure=None,
        idle_seconds=foreground_activity.idle_seconds(vault_root),
        vault_quiet_seconds=quiet,
        last_whole_vault_pass=last_pass,
        graph_debt=False,
        full_upsert_backlog=0,
        freshness_live=freshness.is_live(vault_root, dreamer_delta.SCOPE),
        hour_cpu_used=hour_cpu,
        consecutive_failures=failures,
        seconds_since_failure=since_failure,
    )
    if not policy.decide(signals).run:
        return signals
    from dataclasses import replace

    from . import auto_quiet, graph_drain, index_sync

    try:
        backlog = int(index_sync.deferred_work_status(vault_root)["full_upserts"]["count"])
    except Exception:  # noqa: BLE001 - an unreadable backlog holds optional work
        backlog = 1
    pressure: bool | None = None
    if auto_quiet.enabled():
        pressure = auto_quiet.pressure_active()
    return replace(
        signals,
        graph_debt=graph_drain.debt_pending(vault_root),
        full_upsert_backlog=backlog,
        pressure=pressure,
    )


# ----------------------------------------------------------------------
# the tick: the test seam
# ----------------------------------------------------------------------


def run_once(
    vault_root: Path,
    *,
    clock: Clock | None = None,
    budget: Budget | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> TickResult:
    """One bounded tick over the next pages, ignoring the idle gate.

    Progress commits per page: one transaction records the page's `seen`
    signature, its contribution and its removal from `pending`, so a tick cut
    off by shutdown, a foreground request, a budget or a crash loses at most
    the page in flight. Never raises.
    """
    clock = clock or Clock()
    budget = budget or Budget()
    vault_root = Path(vault_root)
    started_wall = clock.monotonic()
    started_cpu = clock.thread_time()
    started_real = _time.monotonic()
    processed: list[str] = []
    held: list[str] = []
    waiting: str | None = None
    stop_reason = "drained"
    error_code: str | None = None
    store = dreamer_store.DreamerStore(vault_root)
    conn = None
    from . import state_paths

    try:
        conn = store.connect()
        # One tick is one unit of placement: resolve the state directory once.
        with state_paths.resolution_scope(), foreground_activity.background_scope(vault_root):
            generation = freshness.generation(vault_root, dreamer_delta.SCOPE)
            has_work = dreamer_delta.has_work(store, conn, vault_root)
            work = dreamer_delta.Work()
            if has_work:
                with store.write(conn):
                    work = dreamer_delta.next_paths(store, conn, vault_root, limit=budget.pages)
            if work.waiting:
                stop_reason = f"waiting:{work.waiting}"
            for rel in work.paths:
                spent = policy.tick_exhausted(
                    pages=len(processed),
                    cpu=clock.thread_time() - started_cpu,
                    wall=clock.monotonic() - started_wall,
                    page_limit=budget.pages,
                    cpu_limit=budget.cpu,
                    wall_limit=budget.wall,
                )
                if spent is not None:
                    stop_reason = spent
                    break
                if should_stop is not None and should_stop():
                    stop_reason = "stop"
                    break
                if foreground_activity.foreground_since(vault_root, started_real):
                    stop_reason = "foreground"
                    break
                if freshness.generation(vault_root, dreamer_delta.SCOPE) != generation:
                    stop_reason = "generation"
                    break
                try:
                    _process(store, conn, vault_root, rel, now=clock.time())
                except dreamer_families.Deferred as deferred:
                    # Not now: the page stays pending, behind the pages that can run.
                    with store.write(conn):
                        store.pending_hold(conn, rel)
                    held.append(rel)
                    waiting = deferred.reason
                    continue
                processed.append(rel)
            else:
                if held and not processed:
                    stop_reason = "deferred"
                # More pending than this tick took, or subjects a changed page
                # just queued: the next tick continues rather than idling.
                elif work.remaining > len(processed) or (
                    processed and store.pending_count(conn) > 0
                ):
                    stop_reason = "pages"
            now = clock.time()
            delivered = _record_deliveries(store, conn)
            token = dreamer_families.review_state_token(vault_root)
            next_settle = store.get_meta(conn, "next_settle_at")
            refresh = (
                delivered
                or bool(processed)
                or token != store.get_meta(conn, "deliverable_token")
                or (isinstance(next_settle, (int, float)) and now >= float(next_settle))
            )
            if has_work or refresh:
                with store.write(conn):
                    dreamer_delta.advance_if_drained(store, conn)
                    if refresh:
                        ctx = dreamer_families.Context(
                            vault_root=vault_root, store=store, conn=conn, now=now
                        )
                        store.set_meta(
                            conn, "next_settle_at", dreamer_families.precompute_deliverable(ctx)
                        )
                        store.set_meta(conn, "deliverable_token", token)
            elif stop_reason == "drained":
                stop_reason = "idle"
    except Exception as exc:  # noqa: BLE001 - a failed tick is recorded, never raised
        log.warning("dreamer: tick failed", exc_info=True)
        error_code = type(exc).__name__
        stop_reason = "error"
    wall = clock.monotonic() - started_wall
    cpu = max(0.0, clock.thread_time() - started_cpu)
    _record_tick(
        store,
        conn,
        clock=clock,
        processed=processed,
        stop=stop_reason,
        error_code=error_code,
        cpu=cpu,
        waiting=waiting,
    )
    if conn is not None:
        conn.close()
    return TickResult(
        ran=True,
        processed=tuple(processed),
        stop_reason=stop_reason,
        wall=wall,
        cpu=cpu,
        error_code=error_code,
        waiting=waiting,
    )


def _process(
    store: dreamer_store.DreamerStore,
    conn: Any,
    vault_root: Path,
    rel: str,
    *,
    now: float,
) -> None:
    """One page, one transaction: contribution, `seen` and `pending` together."""
    signature = dreamer_delta.live_signature(vault_root, rel)
    ctx = dreamer_families.Context(vault_root=vault_root, store=store, conn=conn, now=now)
    try:
        with store.write(conn):
            changed = dreamer_store.encode_sig(signature) != store.seen_get(conn, rel)
            dreamer_families.process_page(ctx, rel, exists=signature is not None, changed=changed)
            dreamer_delta.mark_processed(store, conn, vault_root, rel, signature)
    finally:
        ctx.close()


def _record_deliveries(store: dreamer_store.DreamerStore, conn: Any) -> bool:
    """Write the carrier's in-process deliveries to the sidecar. True if any."""
    from . import upkeep

    pending = upkeep.pending_deliveries()
    if not pending:
        return False
    with store.write(conn):
        store.record_deliveries(conn, pending)
    upkeep.forget_deliveries(pending)
    return True


def _record_tick(
    store: dreamer_store.DreamerStore,
    conn: Any,
    *,
    clock: Clock,
    processed: list[str],
    stop: str,
    error_code: str | None,
    cpu: float,
    waiting: str | None = None,
) -> None:
    now_mono = clock.monotonic()
    with _LOCK:
        state = _STATE
        state.ledger.add(now_mono, cpu)
        state.ticks += 1
        state.pages_processed += len(processed)
        state.last_tick_at = clock.time()
        state.last_stop_reason = stop
        if error_code is not None:
            state.consecutive_failures += 1
            state.last_failure_at = now_mono
            state.last_error_code = error_code
            if state.consecutive_failures >= policy.FAILED_AFTER and state.failed_since is None:
                state.failed_since = clock.time()
        else:
            state.consecutive_failures = 0
            state.failed_since = None
        if waiting is None:
            state.waiting_reason = None
            state.waiting_since = None
        elif state.waiting_reason != waiting:
            state.waiting_reason = waiting
            state.waiting_since = clock.time()
        if state.consecutive_failures >= policy.FAILED_AFTER:
            state.phase = "failed"
        else:
            state.phase = "idle" if waiting is None else "waiting"
        health = _health_locked(now_mono)
    if conn is None or stop == "idle":
        return
    try:
        pending = store.pending_count(conn)
        reseeding = bool(store.get_meta(conn, "reseeding"))
        health["reseed_remaining"] = pending if reseeding else 0
        health["evidence_complete"] = {
            family.name: not (family.global_counts and reseeding)
            for family in dreamer_families.REGISTRY
        }
        with store.write(conn):
            store.set_health(conn, health)
    except Exception:  # noqa: BLE001 - health is best effort; the tick is recorded
        log.debug("dreamer: health not recorded", exc_info=True)


def _health_locked(now_mono: float) -> dict[str, Any]:
    state = _STATE
    return {
        "state": state.phase,
        "waiting_reason": state.waiting_reason,
        "waiting_since": state.waiting_since,
        "last_tick_at": state.last_tick_at,
        "last_stop_reason": state.last_stop_reason,
        "last_error_code": state.last_error_code,
        "consecutive_failures": state.consecutive_failures,
        "failed_since": state.failed_since,
        "hour_cpu_used": state.ledger.used(now_mono),
        "ticks": state.ticks,
        "pages_processed": state.pages_processed,
    }


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------


def status(vault_root: Path | None = None) -> dict[str, Any]:
    """The worker's posture, with no path and no content. Allocates nothing.

    In the serving process this is the live in-memory state. Given a vault, it
    also reports the sidecar's last recorded health, which is what a separate
    process (the `exomem dreamer status` CLI) can see; it never creates the
    sidecar.
    """
    current = setting()
    with _LOCK:
        state = _STATE
        out: dict[str, Any] = {
            "setting": current,
            "running": _thread is not None and _thread.is_alive(),
            "state": "off" if current == "off" else state.phase,
            "waiting_reason": state.waiting_reason,
            "waiting_since": state.waiting_since,
            "consecutive_failures": state.consecutive_failures,
            "failed_since": state.failed_since,
            "last_tick_at": state.last_tick_at,
            "last_stop_reason": state.last_stop_reason,
            "last_error_code": state.last_error_code,
            "hour_cpu_used": state.ledger.used(_time.monotonic()),
            "ticks": state.ticks,
            "pages_processed": state.pages_processed,
        }
    if vault_root is not None:
        view = dreamer_store.read_view(Path(vault_root))
        out["sidecar"] = dict(view.health) if view is not None else None
    return out
