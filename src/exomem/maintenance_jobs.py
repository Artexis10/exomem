"""Governed maintenance jobs: a remote maintenance write that outlives its request.

A remote ``backfill-ids`` or ``fix`` write (later ``reconcile``) is admitted by
``writer_lease.invoke_command`` with a ``plan_id`` and a ``why``. The request
thread claims one job row, starts a daemon thread and returns a handle at once.
The thread dispatches the real leaf through ``invoke_command`` as the starting
principal on the internal ``maintenance-job`` surface, so every ordinary check
still applies. The leaf commits in chunks, each under its own short mutation
boundary hold (``chunk_hold``), and reports through ``checkpoint`` and
``record_chunk``. A request thread never holds the boundary for a job's work.

State lives in ``.maintenance-jobs.sqlite`` in the vault's state directory. A
job belongs to the process that runs it (the idempotency owner identity). A row
whose owner is gone reads as ``interrupted``; the next claim and the service
activation record it.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import reserved_paths, state_paths
from .cli_ops import OpError

#: A job's state is one of these; a job is terminal once it leaves ``RUNNING``.
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

#: The internal surface a job's dispatch runs on. It is not request-bound, so the
#: remote admission gate in ``invoke_command`` does not run a second time.
SURFACE = "maintenance-job"

#: Stop codes a checkpoint raises, and the error code a swept row carries.
CANCEL_CODE = "CANCELLED"
SHUTDOWN_CODE = "SERVICE_STOPPING"
MEMORY_PRESSURE_CODE = "MEMORY_PRESSURE"
OWNER_GONE_CODE = "OWNER_GONE"

# Closed maintain_memory mode literals: protocol names, not a vocabulary a vault defines.
#: Job kinds, each with the arguments its plan binds as flags. A request-bound surface admits a write
#: of one of these modes only as a job, and only with the plan_id of its preview and a why.
JOB_FLAGS: dict[str, tuple[str, ...]] = {  # nosemgrep: ep-word-set -- closed maintain_memory mode and argument literals
    "backfill-ids": (),
    "fix": ("rebuild_embeddings",),
    "reconcile": ("rebuild_graph",),
}
JOB_MODES = frozenset(JOB_FLAGS)
#: Job kinds whose leaf is not chunked. Their job dispatch keeps the wide mutation boundary for the whole run,
#: as the CLI does, and a stop lands between phases. A reconcile phase writes derived state that other
#: phases read, and the CLI's whole-run hold is the measured behaviour to keep; narrowing it is separate work.
WHOLE_RUN_MODES = frozenset({"reconcile"})  # nosemgrep: ep-word-set -- closed maintain_memory mode literal
MODE_STATUS = "maintenance-job"
MODE_CANCEL = "maintenance-job-cancel"

#: Terminal rows older than this are pruned, beyond the newest ``RETAIN_NEWEST``.
RETAIN_SECONDS = 30 * 24 * 3600
RETAIN_NEWEST = 200
#: Longest a chunk waits for a busy mutation boundary before the job fails.
BUSY_BUDGET_SECONDS = 600.0
#: Longest service shutdown waits for a job to reach its next checkpoint.
SHUTDOWN_JOIN_SECONDS = 5.0

_STORE_ID = "maintenance-jobs-store"


class JobStop(BaseException):
    """Stop a job at a checkpoint.

    A ``BaseException`` so a leaf's ``except Exception`` cannot swallow it. The
    runner reads ``state`` and ``code`` and records them as the job's terminal.
    """

    def __init__(self, code: str, state: str) -> None:
        super().__init__(code)
        self.code = code
        self.state = state


@dataclass
class _Live:
    """What this process holds for one running job."""

    run_id: str
    vault_root: Path
    stop: threading.Event = field(default_factory=threading.Event)
    #: Why ``stop`` was set: ``CANCEL_CODE`` or ``SHUTDOWN_CODE``.
    reason: str | None = None
    thread: threading.Thread | None = None
    chunks: int = 0
    committed_paths: list[str] = field(default_factory=list)


_JOB: contextvars.ContextVar[_Live | None] = contextvars.ContextVar("exomem_maintenance_job", default=None)
_LIVE: dict[str, _Live] = {}
#: Held across claim and registration, and by the sweep, so a row this process
#: just inserted is never read as an orphan before its thread is registered.
_LIVE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- store


def store_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / ".maintenance-jobs.sqlite"


@contextmanager
def _store(vault_root: Path, *, create: bool) -> Iterator[sqlite3.Connection]:
    """Open the job store under the private-state ceremony; raise ``FileNotFoundError`` for a read of none."""
    root = Path(vault_root).resolve()
    path = store_path(root)
    with reserved_paths._subsystem_authority_scope("maintenance_jobs"):
        with reserved_paths._identity_coordination_scope(
            root, descriptor_ids=(_STORE_ID,), identity_may_change=create
        ):
            with reserved_paths._sqlite_owner_target_scope(root, path, _STORE_ID, create=create) as retained:
                conn = _open(root, path, retained, create)
    try:
        yield conn
    finally:
        conn.close()


def _open(root: Path, path: Path, retained: Path, create: bool) -> sqlite3.Connection:
    if create:
        retained.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(retained, timeout=5.0)
    else:
        conn = sqlite3.connect(f"{retained.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Make the whole WAL family reachable while the private-state boundary is held.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE, mode TEXT NOT NULL, "
                "flags TEXT NOT NULL, plan_id TEXT NOT NULL, why TEXT NOT NULL, surface TEXT NOT NULL, "
                "audience TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL, phase TEXT, "
                "done INTEGER NOT NULL DEFAULT 0, total INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0, "
                "cancel_why TEXT, started_at REAL NOT NULL, ended_at REAL, error_code TEXT, outcome TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, started_at)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chunks ("
                "run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, committed INTEGER NOT NULL, "
                "skipped_changed INTEGER NOT NULL, outcome_unknown INTEGER NOT NULL, detail TEXT, "
                "PRIMARY KEY (run_id, ordinal))"
            )
            conn.commit()
        reserved_paths._publish_sqlite_owner_family(root, path, _STORE_ID, conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- ownership and liveness


def _owner() -> tuple[str | None, Path]:
    """This process's job-owner identity and the directory its liveness is probed in."""
    from . import writer_lease

    store = writer_lease.get_manager().idempotency
    return store.owner_id, store.state_dir


def _owner_gone(row: sqlite3.Row, mine: str | None, state_dir: Path) -> bool:
    from . import writer_lease

    if row["owner"] == mine:
        # The probe cannot tell this process from a dead one; the live registry can.
        return row["run_id"] not in _LIVE
    return not writer_lease._probe_owner_liveness(state_dir, row["owner"])  # noqa: SLF001


def _effective_state(row: sqlite3.Row, mine: str | None, state_dir: Path) -> str:
    if row["state"] == RUNNING and _owner_gone(row, mine, state_dir):
        return INTERRUPTED
    return str(row["state"])


def _record_orphans(conn: sqlite3.Connection) -> None:
    """Mark every running row whose owner is gone as interrupted. Call inside a write transaction."""
    mine, state_dir = _owner()
    now = time.time()
    for row in conn.execute("SELECT * FROM jobs WHERE state = ?", (RUNNING,)).fetchall():
        if _owner_gone(row, mine, state_dir):
            conn.execute(
                "UPDATE jobs SET state = ?, ended_at = ?, error_code = ? WHERE run_id = ? AND state = ?",
                (INTERRUPTED, now, OWNER_GONE_CODE, row["run_id"], RUNNING),
            )


def sweep_dead(vault_root: Path) -> int:
    """Record the running jobs whose process is gone as interrupted; touch nothing if there is no store."""
    with _LIVE_LOCK:
        try:
            with _store(vault_root, create=False) as probe:
                if probe.execute("SELECT 1 FROM jobs WHERE state = ? LIMIT 1", (RUNNING,)).fetchone() is None:
                    return 0
        except FileNotFoundError:
            return 0
        with _store(vault_root, create=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                before = conn.total_changes
                _record_orphans(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return conn.total_changes - before


# --------------------------------------------------------------------------- owner-only access


def require_owner(vault_root: Path) -> None:
    """Refuse every caller but the vault owner, naming no job."""
    from .governance import raw_protection
    from .governance.principal import effective_principal

    who = effective_principal()
    if not (raw_protection.is_owner(who) and raw_protection.has_unrestricted_access(Path(vault_root), who)):
        raise OpError(
            "MAINTENANCE_OWNER_REQUIRED",
            "maintenance jobs are owner-only",
            "Sign in as the vault owner to start, read or cancel a maintenance job.",
            details={"status": "terminal", "committed": False},
        )


def require_plan(mode: str, kwargs: Mapping[str, Any]) -> None:
    """Refuse a remote plan-bound write that carries no reviewed plan and reason."""
    if kwargs.get("plan_id") and kwargs.get("why"):
        return
    raise OpError(
        "MAINTENANCE_PLAN_REQUIRED",
        f"a remote {mode} write needs the plan_id of its preview and a one-line why",
        f"Preview {mode} with dry_run=true, then repeat it with dry_run=false, that plan_id and a why.",
        details={"status": "terminal", "committed": False},
    )


def plan_identity(mode: str, flags: Mapping[str, Any], entries: Iterable[Sequence[str]]) -> str:
    """The `plan_id` of one plan: its mode, the flags that change its work, and its sorted entries.

    A preview and a job's re-plan both compute it, so a `plan_id` names exactly the work the job would do.
    An entry is the strings that say what the plan does to one page, for example its path and content hashes.
    """
    payload = json.dumps(
        {"mode": mode, "flags": dict(flags), "pages": sorted(list(entry) for entry in entries)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_current_plan(mode: str, planned: str, fresh: str, what: str) -> None:
    """Refuse a job whose plan changed since the preview, before it writes anything.

    `planned` is the `plan_id` the owner reviewed, `fresh` the one this run computed again, and
    `what` names what the plan covers, for the owner reading the refusal.
    """
    if planned == fresh:
        return
    raise OpError(
        "STALE_MAINTENANCE_PLAN",
        f"{what} changed since the preview",
        f"Preview {mode} again, then start the new plan_id.",
        details={"status": "terminal", "committed": False, "plan_id": fresh},
    )


def is_job_write(kwargs: Mapping[str, Any]) -> bool:
    """Whether these arguments run a job kind's committing leaf (the dispatch a job thread makes)."""
    return (
        kwargs.get("mode") in JOB_MODES
        and kwargs.get("plan_id") is not None
        and kwargs.get("dry_run") is False
    )


def narrows_boundary(kwargs: Mapping[str, Any], surface: str | None) -> bool:
    """Whether this maintain_memory call holds the narrow boundary in the writer manager.

    A job start only claims a row and launches a thread, and a cancel only flags one; neither touches
    the vault. A chunked job's own dispatch holds the boundary per chunk in its leaf. A whole-run job's
    dispatch is the one that keeps the wide boundary.
    """
    mode = kwargs.get("mode")
    if not is_job_write(kwargs):
        return False
    return not (surface == SURFACE and mode in WHOLE_RUN_MODES)


# --------------------------------------------------------------------------- views


def _chunks(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM chunks WHERE run_id = ? ORDER BY ordinal", (run_id,)).fetchall()


def _view(conn: sqlite3.Connection, row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    mine, state_dir = _owner()
    state = _effective_state(row, mine, state_dir)
    swept = state != row["state"]
    chunks = _chunks(conn, row["run_id"])
    totals = {
        "committed": sum(chunk["committed"] for chunk in chunks),
        "skipped_changed": sum(chunk["skipped_changed"] for chunk in chunks),
        "outcome_unknown": sum(chunk["outcome_unknown"] for chunk in chunks),
    }
    view: dict[str, Any] = {
        "run_id": row["run_id"],
        "mode": row["mode"],
        "flags": json.loads(row["flags"]),
        "plan_id": row["plan_id"],
        "why": row["why"],
        "surface": row["surface"],
        "principal": row["audience"],
        "state": state,
        "phase": row["phase"],
        # `total` stays None until the leaf knows it; 0 would read as "nothing to do".
        "progress": {"done": row["done"], "total": row["total"]},
        "cancel_requested": bool(row["cancel_requested"]),
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "error_code": OWNER_GONE_CODE if swept else row["error_code"],
        **totals,
    }
    if full and state != RUNNING:
        stored = json.loads(row["outcome"]) if row["outcome"] else {}
        view["receipt"] = {
            **totals,
            "chunks": [
                {
                    "ordinal": chunk["ordinal"],
                    "committed": chunk["committed"],
                    "skipped_changed": chunk["skipped_changed"],
                    "outcome_unknown": chunk["outcome_unknown"],
                    **(json.loads(chunk["detail"]) if chunk["detail"] else {}),
                }
                for chunk in chunks
            ],
            "result": stored.get("result"),
            "error": stored.get("error"),
        }
    return view


def _not_found() -> OpError:
    return OpError("NOT_FOUND", "no maintenance job has this run_id", "List jobs with mode maintenance-job.")


def job_status(vault_root: Path, run_id: str) -> dict[str, Any]:
    require_owner(vault_root)
    try:
        with _store(vault_root, create=False) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise _not_found()
            return _view(conn, row, full=True)
    except FileNotFoundError:
        raise _not_found() from None


def job_list(vault_root: Path) -> dict[str, Any]:
    require_owner(vault_root)
    try:
        with _store(vault_root, create=False) as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY started_at DESC, seq DESC").fetchall()
            return {"jobs": [_view(conn, row, full=False) for row in rows]}
    except FileNotFoundError:
        return {"jobs": []}


def request_cancel(vault_root: Path, run_id: str, why: str) -> dict[str, Any]:
    """Ask a running job to stop at its next checkpoint; a terminal job is returned unchanged.

    A replay of the same cancel within the dispatcher's implicit retry window (600 s) returns the
    view stored with the first call, not a fresh read of the job.
    """
    require_owner(vault_root)
    with _LIVE_LOCK:
        try:
            with _store(vault_root, create=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _record_orphans(conn)
                    row = conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
                    if row is None:
                        raise _not_found()
                    if row["state"] == RUNNING:
                        conn.execute(
                            "UPDATE jobs SET cancel_requested = 1, cancel_why = ? WHERE run_id = ?",
                            (why, run_id),
                        )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                live = _LIVE.get(run_id)
                if live is not None and row["state"] == RUNNING:
                    live.reason = live.reason or CANCEL_CODE
                    live.stop.set()
                row = conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
                return _view(conn, row, full=True)
        except FileNotFoundError:
            raise _not_found() from None


# --------------------------------------------------------------------------- start


def start_leaf(command: Any, injected: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Callable[..., Any]:
    """The leaf that replaces a plan-bound command's own on a request-bound surface.

    Captures the caller at admission, so the job runs as the principal that was
    admitted. The request that calls it only claims the job and returns a handle.
    """
    from . import capabilities
    from .governance import principal as principal_module

    principal = principal_module.effective_principal()
    admitted_surface = capabilities.current_active_surface()
    arguments = dict(kwargs)

    def start(*_ignored: Any, **_also_ignored: Any) -> dict[str, Any]:
        return _start(command, injected, arguments, principal, admitted_surface)

    return start


def _start(
    command: Any,
    injected: tuple[Any, ...],
    kwargs: dict[str, Any],
    principal: Any,
    admitted_surface: Any,
) -> dict[str, Any]:
    from . import capabilities

    vault_root = Path(injected[0])
    mode = str(kwargs["mode"])
    # Only the arguments the mode's plan binds: defaults that every surface fills differently stay out of the identity.
    flags = {name: kwargs[name] for name in JOB_FLAGS[mode] if kwargs.get(name) is not None}
    job, started = _claim(
        vault_root,
        mode=mode,
        flags=flags,
        plan_id=str(kwargs["plan_id"]),
        why=str(kwargs["why"]),
        surface=admitted_surface.surface if admitted_surface is not None else SURFACE,
        audience=principal.audience_id,
        launch=lambda live: _launch(live, command, injected, kwargs, principal, admitted_surface),
    )
    handle = {
        "run_id": job["run_id"],
        "state": job["state"],
        "mode": job["mode"],
        "plan_id": job["plan_id"],
        "started": started,
        "next": capabilities.active_call_route(command.name, {"mode": MODE_STATUS, "run_id": job["run_id"]}),
    }
    return handle


def _claim(
    vault_root: Path,
    *,
    mode: str,
    flags: dict[str, Any],
    plan_id: str,
    why: str,
    surface: str,
    audience: str,
    launch: Callable[[_Live], threading.Thread],
) -> tuple[dict[str, Any], bool]:
    """Record the one running job, or join an identical one, and launch a new job's thread.

    Runs under ``BEGIN IMMEDIATE``: orphans are recorded first, a running job
    with the same mode, flags and plan is returned, and any other running job
    refuses. Registration of the thread happens before ``_LIVE_LOCK`` is released.
    """
    mine, _ = _owner()
    if mine is None:
        raise OpError(
            "MAINTENANCE_JOB_UNAVAILABLE",
            "this service has no job owner identity, so a job could not be told apart from an orphan",
            "Restart the service, then repeat the start.",
            details={"status": "terminal", "committed": False},
        )
    flags_json = _canonical(flags)
    run_id = uuid.uuid4().hex
    with _LIVE_LOCK, _store(vault_root, create=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _record_orphans(conn)
            running = conn.execute("SELECT * FROM jobs WHERE state = ? ORDER BY seq", (RUNNING,)).fetchall()
            for row in running:
                if row["mode"] == mode and row["flags"] == flags_json and row["plan_id"] == plan_id:
                    conn.commit()
                    return _view(conn, row, full=False), False
            if running:
                raise OpError(
                    "MAINTENANCE_JOB_ACTIVE",
                    "another maintenance job is running",
                    "Read it with mode maintenance-job, wait for it to finish, or cancel it, then start again.",
                    details={"status": "terminal", "committed": False, "run_id": running[0]["run_id"]},
                )
            _prune(conn)
            conn.execute(
                "INSERT INTO jobs (run_id, mode, flags, plan_id, why, surface, audience, owner, state, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, mode, flags_json, plan_id, why, surface, audience, mine, RUNNING, time.time()),
            )
            live = _Live(run_id=run_id, vault_root=Path(vault_root).resolve())
            _LIVE[run_id] = live
            conn.commit()
        except BaseException:
            conn.rollback()
            _LIVE.pop(run_id, None)
            raise
        try:
            live.thread = launch(live)
        except BaseException:
            _LIVE.pop(run_id, None)
            conn.execute(
                "UPDATE jobs SET state = ?, ended_at = ?, error_code = ? WHERE run_id = ?",
                (FAILED, time.time(), "JOB_NOT_STARTED", run_id),
            )
            conn.commit()
            raise
        row = conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
        return _view(conn, row, full=False), True


def _prune(conn: sqlite3.Connection) -> None:
    """Drop terminal jobs older than the retention window, beyond the newest ``RETAIN_NEWEST``."""
    cutoff = time.time() - RETAIN_SECONDS
    stale = "state != ? AND ended_at < ? AND run_id NOT IN (SELECT run_id FROM jobs ORDER BY started_at DESC, seq DESC LIMIT ?)"
    params = (RUNNING, cutoff, RETAIN_NEWEST)
    conn.execute(f"DELETE FROM chunks WHERE run_id IN (SELECT run_id FROM jobs WHERE {stale})", params)
    conn.execute(f"DELETE FROM jobs WHERE {stale}", params)


# --------------------------------------------------------------------------- runner


def _launch(
    live: _Live,
    command: Any,
    injected: tuple[Any, ...],
    kwargs: dict[str, Any],
    principal: Any,
    admitted_surface: Any,
) -> threading.Thread:
    # A fresh context: the request's write fence, mutation trace, post-terminal lists and
    # time budget belong to a request that has ended by the time most of the job runs.
    context = contextvars.Context()
    thread = threading.Thread(
        target=context.run,
        args=(_run, live, command, injected, kwargs, principal, admitted_surface),
        name=f"maintenance-job-{live.run_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread


def _run(
    live: _Live,
    command: Any,
    injected: tuple[Any, ...],
    kwargs: dict[str, Any],
    principal: Any,
    admitted_surface: Any,
) -> None:
    from . import capabilities, foreground_priority, writer_lease
    from .governance import principal as principal_module

    descriptor = (
        replace(admitted_surface, surface=SURFACE)
        if admitted_surface is not None
        else capabilities.ActiveSurfaceDescriptor(SURFACE, "full", True, (command.name,))
    )
    state, error_code, outcome = FAILED, "INTERNAL", {}
    try:
        with (
            principal_module.request_scope(principal),
            capabilities.active_surface(descriptor),
            principal_module.invocation_scope(),
            foreground_priority.background_job(),
            foreground_priority.bulk(stop=live.stop),
        ):
            _JOB.set(live)
            with _whole_run_hold(live, command.name, kwargs.get("mode")):
                result = writer_lease.invoke_command(command, *injected, mutation_request_id=live.run_id, **kwargs)
        state, error_code, outcome = COMPLETED, None, {"result": result}
    except JobStop as stop:
        state, error_code, outcome = stop.state, stop.code, {}
    except foreground_priority.BulkCancelled:
        shutting_down = live.reason == SHUTDOWN_CODE
        state = INTERRUPTED if shutting_down else CANCELLED
        error_code = SHUTDOWN_CODE if shutting_down else CANCEL_CODE
        outcome = {}
    except Exception as error:  # noqa: BLE001 - every leaf failure becomes the job's recorded terminal
        state, error_code, outcome = FAILED, _error_code(error), {"error": _error_record(error)}
    finally:
        _finish(live, state, error_code, outcome)


@contextmanager
def _whole_run_hold(live: _Live, operation: str, mode: Any) -> Iterator[None]:
    """Wait out a busy boundary before a whole-run job's dispatch, which then nests in this hold.

    The manager's own wide guard gives up after seconds and would fail a job that only met another
    writer's turn. Waiting here is bounded by ``BUSY_BUDGET_SECONDS`` and ends at once on a stop.
    A chunked job needs none: it holds the boundary per chunk, through ``chunk_hold``.
    """
    if mode not in WHOLE_RUN_MODES:
        yield
        return
    with chunk_hold(live.vault_root, operation):
        yield


def _error_code(error: BaseException) -> str:
    return str(getattr(error, "code", None) or type(error).__name__)


def _error_record(error: BaseException) -> dict[str, Any]:
    details = getattr(error, "details", None)
    return {
        "code": _error_code(error),
        "message": str(getattr(error, "message", None) or getattr(error, "reason", None) or error),
        "remediation": getattr(error, "remediation", None),
        "details": dict(details) if isinstance(details, Mapping) else {},
    }


def _finish(live: _Live, state: str, error_code: str | None, outcome: dict[str, Any]) -> None:
    """Record the terminal, then leave the live registry, so a sweep never sees a finished job as an orphan."""
    try:
        with _store(live.vault_root, create=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE jobs SET state = ?, ended_at = ?, error_code = ?, outcome = ? WHERE run_id = ? AND state = ?",
                (state, time.time(), error_code, _canonical(outcome), live.run_id, RUNNING),
            )
            conn.commit()
    finally:
        with _LIVE_LOCK:
            _LIVE.pop(live.run_id, None)


def stop_all(*, wait: float = SHUTDOWN_JOIN_SECONDS) -> None:
    """Ask every live job to stop at its next checkpoint and wait for them, for a service stopping."""
    with _LIVE_LOCK:
        jobs = list(_LIVE.values())
    for live in jobs:
        live.reason = live.reason or SHUTDOWN_CODE
        live.stop.set()
    deadline = time.monotonic() + wait
    for live in jobs:
        if live.thread is not None:
            live.thread.join(max(0.0, deadline - time.monotonic()))


# --------------------------------------------------------------------------- leaf-side hooks


def active() -> bool:
    return _JOB.get() is not None


def current_run_id() -> str | None:
    live = _JOB.get()
    return live.run_id if live is not None else None


def committed_paths() -> list[str]:
    """Paths the current job's chunks committed, for a leaf that settles derived state after a stop."""
    live = _JOB.get()
    return list(live.committed_paths) if live is not None else []


def raise_if_stopped() -> None:
    """Stop the job if cancel or shutdown was requested. In-process only, so it is safe inside a hold."""
    live = _JOB.get()
    if live is not None and live.stop.is_set():
        if live.reason == SHUTDOWN_CODE:
            raise JobStop(SHUTDOWN_CODE, INTERRUPTED)
        raise JobStop(CANCEL_CODE, CANCELLED)


def checkpoint(phase: str, done: int | None = None, total: int | None = None) -> None:
    """Stop if asked, fail under memory pressure, and publish progress. Call between units, outside a hold.

    ``total`` stays unknown (null) until the leaf passes it.
    """
    live = _JOB.get()
    if live is None:
        return
    raise_if_stopped()
    with _store(live.vault_root, create=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE run_id = ?", (live.run_id,)).fetchone()
            conn.execute(
                "UPDATE jobs SET phase = ?, done = COALESCE(?, done), total = COALESCE(?, total) "
                "WHERE run_id = ? AND state = ?",
                (phase, done, total, live.run_id, RUNNING),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    if row is not None and row["cancel_requested"]:
        # Another process (the CLI) asked; this process's own cancel set the event already.
        live.reason = live.reason or CANCEL_CODE
        live.stop.set()
        raise_if_stopped()
    _fail_under_pressure()


def _fail_under_pressure() -> None:
    from . import media_brakes

    if not media_brakes.enabled():
        return
    cell = media_brakes.read_cell()
    if media_brakes.pressure_exceeded(cell, media_brakes.settings(cell=cell)):
        raise JobStop(MEMORY_PRESSURE_CODE, FAILED)


def record_chunk(
    *,
    committed: int = 0,
    skipped_changed: int = 0,
    outcome_unknown: int = 0,
    committed_paths: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one chunk's counts and leaf-specific receipt fields (``detail``) to the job's receipt."""
    live = _JOB.get()
    if live is None:
        return
    live.chunks += 1
    live.committed_paths.extend(committed_paths or ())
    with _store(live.vault_root, create=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO chunks (run_id, ordinal, committed, skipped_changed, outcome_unknown, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (live.run_id, live.chunks, committed, skipped_changed, outcome_unknown,
                 _canonical(detail) if detail else None),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


@contextmanager
def chunk_hold(vault_root: Path, operation: str, *, stoppable: bool = True) -> Iterator[None]:
    """Hold the mutation boundary for one chunk, waiting out a busy boundary for a bounded time.

    A busy boundary is another writer's turn, not a failure: the job checks for
    a stop between attempts and gives up only after ``BUSY_BUDGET_SECONDS``.
    ``stoppable=False`` is for settling work that a stop must not skip, such as
    rebuilding an index over chunks that already committed.
    """
    from . import writer_lease

    deadline = time.monotonic() + BUSY_BUDGET_SECONDS
    with ExitStack() as stack:
        while True:
            if stoppable:
                raise_if_stopped()
            try:
                stack.enter_context(
                    writer_lease.active_manager().mutation_guard(
                        vault_root,
                        request_id=current_run_id() or writer_lease.active_mutation_request_id(),
                        operation=operation,
                        holder_kind="maintenance-job",
                    )
                )
                break
            except OpError as error:
                if error.code != "MUTATION_BUSY" or time.monotonic() >= deadline:
                    raise
        # The boundary may have been busy for the whole wait: a stop that arrived then ends the job unwritten.
        if stoppable:
            raise_if_stopped()
        yield


def commit_chunk(
    vault_root: Path,
    operation: str,
    entries: Sequence[tuple[str, Path, str]],
    rewrite: Callable[[str, str], str],
    *,
    extra: Callable[[list[str]], dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Rewrite one chunk of a plan's pages under one hold, record it, and return `(committed, skipped)`.

    An entry is `(rel, path, content hash the plan was made from)`. Under the hold each page is read
    again: one that cannot be read, or whose hash moved, is skipped and never overwritten.
    `rewrite(rel, text)` gives a kept page's new text. A failed commit may have written some pages, so
    the chunk is recorded as `outcome_unknown` and the failure re-raised. ``extra(committed)`` adds receipt fields.
    """
    from . import vault

    writes: list[vault.PlannedWrite] = []
    kept: list[str] = []
    skipped: list[str] = []
    failure: BaseException | None = None
    with chunk_hold(vault_root, operation):
        for rel, path, digest in entries:
            try:
                text, guard = vault.read_guarded_text(vault_root, path)
            except (OSError, UnicodeDecodeError, vault.PathGuardError):
                skipped.append(rel)
                continue
            if vault.content_hash(text) != digest:
                skipped.append(rel)
                continue
            kept.append(rel)
            writes.append(vault.PlannedWrite(path=path, content=rewrite(rel, text), guard=guard, expected_hash=digest))
        if writes:
            try:
                vault.batch_atomic_write(writes, vault_root=vault_root)
            except BaseException as error:  # noqa: BLE001 - recorded, then re-raised below
                failure = error
    if failure is not None:
        record_chunk(
            skipped_changed=len(skipped),
            outcome_unknown=len(writes),
            detail={"skipped_changed": skipped, "outcome_unknown": kept},
        )
        raise failure
    record_chunk(
        committed=len(kept),
        skipped_changed=len(skipped),
        committed_paths=kept,
        detail={"paths": kept, "skipped_changed": skipped, **(extra(kept) if extra is not None else {})},
    )
    return kept, skipped
