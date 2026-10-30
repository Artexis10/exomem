"""Governed maintenance jobs: a remote maintenance write that outlives its request.

A remote ``backfill-ids`` or ``fix`` write (later ``reconcile``) is admitted by
``writer_lease.invoke_command`` with a ``plan_id`` and a ``why``. The request
thread claims one job row, starts a daemon thread and returns a handle at once.
The thread dispatches the real leaf through ``invoke_command`` as the starting
principal on the internal ``maintenance-job`` surface, so every ordinary check
still applies. The leaf commits its pages in chunks (``commit_pages``), each
under its own short mutation boundary hold, and reports through ``checkpoint``,
``note`` and ``record_paths``. A request thread never holds the boundary for a
job's work.

State lives in ``.maintenance-jobs.sqlite`` in the vault's state directory. A
job belongs to the process that runs it (the idempotency owner identity). A row
whose owner is gone reads as ``interrupted``; the next claim and the service
activation record it.

The receipt of a finished job is bounded however large the vault is: a status
read returns the totals, the chunk count, the leaf's small notes and a reduced
leaf report. The paths, with the IDs a job minted, are read page by page
(``job_status`` with a ``cursor``), each page at most half of the 64 KiB response bound.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
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
#: Longest one chunk holds the mutation boundary before it lets a waiting writer in. A foreground
#: writer waits ``EXOMEM_MUTATION_TIMEOUT`` (5 s by default) for the boundary, so about one second
#: leaves it a wide margin. A chunk always writes at least one batch, so a job always progresses.
HOLD_SECONDS = 1.0
#: Most pages one chunk writes: the contract bounds a chunk by pages as well as by time.
CHUNK_PAGES = 100
#: Pages one atomic write commits inside a hold. A batch costs a fixed index and graph fan-out plus
#: a per-page share, and the hold cannot end between the pages of a batch, so a small batch keeps
#: one hold near ``HOLD_SECONDS``. Four is a judgment, not a measured optimum: review runs saw
#: 4-page batches take 2.6-6.0 s on a loaded development host.
BATCH_PAGES = 4
#: Largest field of a leaf report that the receipt keeps; a larger one is reduced to its size.
RESULT_FIELD_BYTES = 1024
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
    #: Checkpoints the leaf has passed; a dispatch that passed none has written nothing.
    checkpoints: int = 0
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


@contextmanager
def _write(vault_root: Path) -> Iterator[sqlite3.Connection]:
    """One write transaction on the job store: committed on a clean exit, rolled back on any exception."""
    with _store(vault_root, create=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


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
                "cancel_why TEXT, started_at REAL NOT NULL, ended_at REAL, error_code TEXT, notes TEXT, outcome TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, started_at)")
            # One row per page a job wrote or tried to write, in the order the job reached it.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, chunk INTEGER NOT NULL, "
                "kind TEXT NOT NULL, path TEXT NOT NULL, value TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS entries_run ON entries(run_id, seq)")
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

#: What an entry of a job's receipt says about its page. ``PENDING`` is a write that was
#: about to start: it reads as ``outcome_unknown`` once the job has ended with it unsettled.
_COMMITTED = "committed"
_SKIPPED = "skipped_changed"
_PENDING = "pending"
_UNKNOWN = "outcome_unknown"


def _totals(conn: sqlite3.Connection, run_id: str | None = None) -> dict[str, dict[str, int]]:
    """Entry counts per job and kind, in one query."""
    rows = conn.execute(
        "SELECT run_id, kind, COUNT(*) AS n FROM entries "
        + ("WHERE run_id = ? " if run_id is not None else "")
        + "GROUP BY run_id, kind",
        (run_id,) if run_id is not None else (),
    ).fetchall()
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        totals.setdefault(row["run_id"], {})[row["kind"]] = row["n"]
    return totals


def _view(
    conn: sqlite3.Connection, row: sqlite3.Row, counts: Mapping[str, int], *, full: bool
) -> dict[str, Any]:
    mine, state_dir = _owner()
    state = _effective_state(row, mine, state_dir)
    swept = state != row["state"]
    totals = {
        "committed": counts.get(_COMMITTED, 0),
        "skipped_changed": counts.get(_SKIPPED, 0),
        # A write that was about to start in a job that is no longer running may have happened.
        _UNKNOWN: counts.get(_PENDING, 0) if state != RUNNING else 0,
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
        entries = sum(counts.values())
        view["receipt"] = {
            **totals,
            "chunks": conn.execute(
                "SELECT COUNT(DISTINCT chunk) FROM entries WHERE run_id = ?", (row["run_id"],)
            ).fetchone()[0],
            "notes": json.loads(row["notes"]) if row["notes"] else {},
            "result": stored.get("result"),
            "error": stored.get("error"),
            # The paths and minted IDs are not in this read; pass this as `cursor` to read them.
            "next_cursor": "0" if entries else None,
        }
    return view


def _job_view(conn: sqlite3.Connection, row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    return _view(conn, row, _totals(conn, row["run_id"]).get(row["run_id"], {}), full=full)


def _not_found() -> OpError:
    return OpError("NOT_FOUND", "no maintenance job has this run_id", "List jobs with mode maintenance-job.")


def job_status(vault_root: Path, run_id: str, cursor: str | None = None) -> dict[str, Any]:
    """One job's state, or with a `cursor` one page of the paths its receipt lists."""
    require_owner(vault_root)
    try:
        with _store(vault_root, create=False) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise _not_found()
            view = _job_view(conn, row, full=cursor is None)
            return view if cursor is None else _page(conn, view, cursor)
    except FileNotFoundError:
        raise _not_found() from None


def _page(conn: sqlite3.Connection, view: Mapping[str, Any], cursor: str) -> dict[str, Any]:
    """The entries of a finished job after `cursor`, as many as fit in half the response bound."""
    from . import query_data

    if view["state"] == RUNNING:
        raise OpError(
            "MAINTENANCE_JOB_RUNNING",
            "a running job has no receipt to page yet",
            "Read the job with mode maintenance-job until its state is not running.",
        )
    try:
        after = int(cursor)
    except ValueError:
        after = -1
    if after < 0:
        raise OpError(
            "INVALID_ARGUMENTS",
            "cursor is not a next_cursor of this job's receipt",
            "Pass the receipt's next_cursor, or the next_cursor of the previous page.",
        )
    limit = query_data.MAX_RESPONSE_BYTES // 2
    entries: list[dict[str, Any]] = []
    size = 0
    last = after
    more = False
    for row in conn.execute(
        "SELECT seq, chunk, kind, path, value FROM entries WHERE run_id = ? AND seq > ? ORDER BY seq",
        (view["run_id"], after),
    ):
        entry = {
            "chunk": row["chunk"],
            "kind": _UNKNOWN if row["kind"] == _PENDING else row["kind"],
            "path": row["path"],
        }
        if row["value"] is not None:
            entry["value"] = row["value"]
        cost = len(_canonical(entry)) + 1
        if entries and size + cost > limit:
            more = True
            break
        entries.append(entry)
        size += cost
        last = row["seq"]
    return {
        "run_id": view["run_id"],
        "state": view["state"],
        "entries": entries,
        "next_cursor": str(last) if more else None,
    }


def job_list(vault_root: Path) -> dict[str, Any]:
    require_owner(vault_root)
    try:
        with _store(vault_root, create=False) as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY started_at DESC, seq DESC").fetchall()
            totals = _totals(conn)
            return {"jobs": [_view(conn, row, totals.get(row["run_id"], {}), full=False) for row in rows]}
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
                return _job_view(conn, row, full=True)
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
                    return _job_view(conn, row, full=False), False
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
        return _job_view(conn, row, full=False), True


def _prune(conn: sqlite3.Connection) -> None:
    """Drop terminal jobs older than the retention window, beyond the newest ``RETAIN_NEWEST``."""
    cutoff = time.time() - RETAIN_SECONDS
    stale = "state != ? AND ended_at < ? AND run_id NOT IN (SELECT run_id FROM jobs ORDER BY started_at DESC, seq DESC LIMIT ?)"
    params = (RUNNING, cutoff, RETAIN_NEWEST)
    conn.execute(f"DELETE FROM entries WHERE run_id IN (SELECT run_id FROM jobs WHERE {stale})", params)
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
    from . import capabilities, foreground_priority
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
            result = _dispatch(live, command, injected, kwargs)
            if isinstance(result, Mapping) and "graph_sync" in result:
                # The dispatcher joined the graph after the leaf, so its outcome replaces the leaf's "pending".
                note(graph=result["graph_sync"])
        state, error_code, outcome = COMPLETED, None, {"result": _bounded(result)}
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


def _dispatch(live: _Live, command: Any, injected: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Run the job's leaf, retrying a whole-run dispatch that met another writer's turn.

    A whole-run mode takes the manager's own wide guard, as the CLI does, so the guard releases
    before the graph join that follows the leaf. That guard gives up after seconds and refuses
    before anything commits, so the job tries again until ``BUSY_BUDGET_SECONDS`` and ends at
    once on a stop. A chunked job holds the boundary per chunk, through ``chunk_hold``.
    """
    from . import writer_lease

    deadline = time.monotonic() + BUSY_BUDGET_SECONDS
    while True:
        raise_if_stopped()
        passed = live.checkpoints
        try:
            return writer_lease.invoke_command(command, *injected, mutation_request_id=live.run_id, **kwargs)
        except OpError as error:
            if (
                kwargs.get("mode") not in WHOLE_RUN_MODES
                or error.code != "MUTATION_BUSY"
                or error.details.get("committed") is not False
                # A leaf that reached a checkpoint may have purged derived rows; a retry would misreport it.
                or live.checkpoints != passed
                or time.monotonic() >= deadline
            ):
                raise


def _error_code(error: BaseException) -> str:
    return str(getattr(error, "code", None) or type(error).__name__)


def _error_record(error: BaseException) -> dict[str, Any]:
    details = getattr(error, "details", None)
    return {
        "code": _error_code(error),
        "message": str(getattr(error, "message", None) or getattr(error, "reason", None) or error),
        "remediation": getattr(error, "remediation", None),
        "details": _bounded(dict(details)) if isinstance(details, Mapping) else {},
    }


def _bounded(report: Any) -> Any:
    """A leaf report as the receipt keeps it: a field too large to read in one response becomes its size.

    The paths a job wrote are in the receipt's entries, so a report that lists them again loses nothing.
    """
    if not isinstance(report, Mapping):
        return report
    return {
        key: {"omitted": len(value)}
        if isinstance(value, (list, dict)) and len(_canonical(value)) > RESULT_FIELD_BYTES
        else value
        for key, value in report.items()
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
    live.checkpoints += 1
    raise_if_stopped()
    with _write(live.vault_root) as conn:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE run_id = ?", (live.run_id,)).fetchone()
        conn.execute(
            "UPDATE jobs SET phase = ?, done = COALESCE(?, done), total = COALESCE(?, total) "
            "WHERE run_id = ? AND state = ?",
            (phase, done, total, live.run_id, RUNNING),
        )
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


def note(**fields: Any) -> None:
    """Add small leaf-specific fields to the job's receipt; a field named again replaces the earlier one."""
    live = _JOB.get()
    if live is None:
        return
    with _write(live.vault_root) as conn:
        row = conn.execute("SELECT notes FROM jobs WHERE run_id = ?", (live.run_id,)).fetchone()
        merged = {**(json.loads(row["notes"]) if row is not None and row["notes"] else {}), **fields}
        conn.execute("UPDATE jobs SET notes = ? WHERE run_id = ?", (_canonical(_bounded(merged)), live.run_id))


def record_paths(paths: Sequence[str], *, committed: bool) -> None:
    """Record pages a leaf wrote outside `commit_pages`, as one chunk, after its write ended.

    ``committed=False`` records a write that failed, so its outcome is unknown.
    """
    live = _JOB.get()
    if live is None or not paths:
        return
    live.chunks += 1
    _insert(live, live.chunks, _COMMITTED if committed else _PENDING, [(path, None) for path in paths])
    if committed:
        live.committed_paths.extend(paths)


def _insert(live: _Live, chunk: int, kind: str, items: Sequence[tuple[str, str | None]]) -> dict[str, int]:
    """Add entries to the job's receipt; return the sequence number of each path."""
    with _write(live.vault_root) as conn:
        return {
            path: conn.execute(
                "INSERT INTO entries (run_id, chunk, kind, path, value) VALUES (?, ?, ?, ?, ?)",
                (live.run_id, chunk, kind, path, value),
            ).lastrowid
            for path, value in items
        }


def _settle(
    live: _Live,
    intended: Mapping[str, int],
    committed: Mapping[str, str | None],
    skipped: Collection[str],
    unknown: Collection[str],
) -> None:
    """Turn a chunk's intent entries into what happened; an entry never attempted is dropped.

    ``unknown`` stay pending: a write that raised may have reached the disk.
    """
    with _write(live.vault_root) as conn:
        for path, seq in intended.items():
            if path in committed:
                conn.execute(
                    "UPDATE entries SET kind = ?, value = ? WHERE seq = ?", (_COMMITTED, committed[path], seq)
                )
            elif path in skipped:
                conn.execute("UPDATE entries SET kind = ? WHERE seq = ?", (_SKIPPED, seq))
            elif path not in unknown:
                conn.execute("DELETE FROM entries WHERE seq = ?", (seq,))


@contextmanager
def chunk_hold(vault_root: Path, operation: str, *, stoppable: bool = True) -> Iterator[Any]:
    """Hold the mutation boundary for one chunk, waiting out a busy boundary for a bounded time.

    A busy boundary is another writer's turn, not a failure: the job checks for
    a stop between attempts and gives up only after ``BUSY_BUDGET_SECONDS``.
    ``stoppable=False`` is for settling work that a stop must not skip, such as
    rebuilding an index over chunks that already committed. Yields the boundary,
    which says how many writers wait for it (``waiter_count``).
    """
    from . import writer_lease

    deadline = time.monotonic() + BUSY_BUDGET_SECONDS
    with ExitStack() as stack:
        while True:
            if stoppable:
                raise_if_stopped()
            try:
                boundary = stack.enter_context(
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
        yield boundary


def commit_pages(
    vault_root: Path,
    operation: str,
    entries: Sequence[tuple[str, Path, str]],
    rewrite: Callable[[str, str], str],
    *,
    receipt_value: Callable[[str], str | None] | None = None,
) -> tuple[list[str], list[str]]:
    """Rewrite a plan's pages in short holds of the mutation boundary; return `(committed, skipped)`.

    An entry is `(rel, path, content hash the plan was made from)`. Each hold is one chunk of at most
    `CHUNK_PAGES` pages that ends after `HOLD_SECONDS`, or as soon as a writer waits for the boundary,
    and the job lets that writer in before it takes the boundary again. Under a hold each page is read
    again: one that cannot be read, or whose hash moved, is skipped and never overwritten.
    `rewrite(rel, text)` gives a kept page's new text, and `receipt_value(rel)` what the receipt lists
    beside the page after `rewrite` ran (a minted ID). A failed write may have reached the disk, so
    its pages stay `outcome_unknown` in the receipt and the failure is raised.
    """
    from . import foreground_priority

    committed: list[str] = []
    skipped: list[str] = []
    position = 0
    while position < len(entries):
        checkpoint("committing", done=position, total=len(entries))
        # A writer that queued behind the last hold takes its turn before this job takes the next.
        foreground_priority.yield_to_foreground()
        position += _commit_chunk(
            vault_root, operation, entries[position : position + CHUNK_PAGES], rewrite, receipt_value, committed, skipped
        )
    checkpoint("committing", done=position, total=len(entries))
    return committed, skipped


def _commit_chunk(
    vault_root: Path,
    operation: str,
    window: Sequence[tuple[str, Path, str]],
    rewrite: Callable[[str, str], str],
    receipt_value: Callable[[str], str | None] | None,
    committed: list[str],
    skipped: list[str],
) -> int:
    """Write as many pages of `window` as one hold allows; return how many it reached.

    The window's pages are entered in the receipt as pending before the hold, outside the boundary,
    so a crash inside the hold leaves them `outcome_unknown`. They are settled after the hold.
    """
    live = _JOB.get()
    intended: dict[str, int] = {}
    if live is not None:
        live.chunks += 1
        intended = _insert(live, live.chunks, _PENDING, [(rel, None) for rel, _, _ in window])
    wrote: dict[str, str | None] = {}
    moved: list[str] = []
    unknown: list[str] = []
    reached = 0
    failure: BaseException | None = None
    try:
        with chunk_hold(vault_root, operation) as boundary:
            started = time.monotonic()
            slowest = 0.0
            while reached < len(window):
                batch = window[reached : reached + BATCH_PAGES]
                began = time.monotonic()
                unknown = [rel for rel, _, _ in batch]
                _write_batch(vault_root, batch, rewrite, receipt_value, wrote, moved)
                unknown = []
                reached += len(batch)
                slowest = max(slowest, time.monotonic() - began)
                # The next batch could run past the budget, a writer waits, or the job was told to stop.
                if (
                    boundary.waiter_count() > 0
                    or (live is not None and live.stop.is_set())
                    or time.monotonic() - started + slowest >= HOLD_SECONDS
                ):
                    break
    except BaseException as error:
        failure = error
        raise
    finally:
        if live is not None:
            try:
                _settle(live, intended, wrote, moved, unknown)
            except Exception:
                if failure is None:
                    raise
    committed.extend(wrote)
    skipped.extend(moved)
    return reached


def _write_batch(
    vault_root: Path,
    batch: Sequence[tuple[str, Path, str]],
    rewrite: Callable[[str, str], str],
    receipt_value: Callable[[str], str | None] | None,
    wrote: dict[str, str | None],
    moved: list[str],
) -> None:
    """Read each page of `batch` again under the hold, then write the unchanged ones in one atomic batch."""
    from . import vault

    writes: list[vault.PlannedWrite] = []
    kept: list[str] = []
    for rel, path, digest in batch:
        try:
            text, guard = vault.read_guarded_text(vault_root, path)
        except (OSError, UnicodeDecodeError, vault.PathGuardError):
            moved.append(rel)
            continue
        if vault.content_hash(text) != digest:
            moved.append(rel)
            continue
        kept.append(rel)
        writes.append(vault.PlannedWrite(path=path, content=rewrite(rel, text), guard=guard, expected_hash=digest))
    if writes:
        vault.batch_atomic_write(writes, vault_root=vault_root)
    for rel in kept:
        wrote[rel] = receipt_value(rel) if receipt_value is not None else None
    live = _JOB.get()
    if live is not None:
        live.committed_paths.extend(kept)
