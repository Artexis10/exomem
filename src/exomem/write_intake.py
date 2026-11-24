"""Durable intake for saves that a busy mutation boundary would refuse.

A save that meets a busy boundary used to return ``MUTATION_BUSY`` and write
nothing, so it survived only if the agent remembered to send it again. In the
serving HTTP process an eligible save is accepted instead: its arguments go to
``.write-intake.sqlite`` in the vault's state directory and the caller gets a
``queued`` receipt. One drainer thread applies the queued saves in acceptance
order, through ``writer_lease.invoke_command``, as the principal that sent each
one, so every ordinary check still runs.

Exactly once: the drainer replays a save under its own explicit identity,
``intake:<id>``, which the idempotency store keeps for 24 hours. Before the
replay writes its first canonical byte, the commit gate
(``writer_lease.validate_active_write_fence`` and the collection store's
transaction) records durably that this identity reached its commit, or refuses
the attempt uncommitted. After a stop, a row whose identity reached its commit
replays under the same identity, so the idempotency store resumes it or answers
``MUTATION_OUTCOME_UNKNOWN``; a row whose identity never reached it replays
under a fresh one. While a save is open, a resend with the original identity
returns the same receipt and runs nothing.

A save that fails for any reason other than contention stays ``failed`` with
its refusal and its arguments until the owner dismisses it
(``exomem intake dismiss``). An applied save keeps its compact outcome and
drops its arguments; applied rows are pruned after seven days.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from . import reserved_paths, state_paths
from .cli_ops import OpError

log = logging.getLogger(__name__)

STORE_ID = "write-intake-store"
OWNER = "write_intake"
REF_PREFIX = "exomem://queued-write/"
MODE = "queued-write"

QUEUED = "queued"
APPLYING = "applying"
APPLIED = "applied"
FAILED = "failed"
_OPEN_STATES = (QUEUED, APPLYING)

#: Refusals that prove nothing committed because the boundary or the store was
#: busy. Closed protocol codes: only these turn into a queued receipt.
ACCEPTED_REFUSALS = frozenset({"MUTATION_BUSY", "MUTATION_WARMING", "COLLECTION_STORE_BUSY"})  # nosemgrep: ep-word-set -- closed public error codes of the mutation protocol.
#: Replay outcomes that are not final: the drainer tries the same identity again
#: later. An outcome-unknown or acknowledgement-pending replay resolves on a
#: same-identity retry, which the idempotency store answers.
#: `MUTATION_OUTCOME_UNKNOWN` is not here: the idempotency store keeps it for good, so it is final.
RETRY_REFUSALS = ACCEPTED_REFUSALS | {  # nosemgrep: ep-word-set -- closed public error codes of the mutation protocol.
    "MUTATION_ACKNOWLEDGEMENT_PENDING",
    # Committed, terminal not persisted: its own remediation is a same-identity retry.
    "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN",
}
#: Retry outcomes whose work may still commit after the replay ends; their requeue marks the
#: identity as reaching its commit, so a reclaim reuses it instead of replaying under a fresh one.
UNCERTAIN_REFUSALS = frozenset(RETRY_REFUSALS - ACCEPTED_REFUSALS)
#: The login behind a save no longer vouches for it: the save fails, with its arguments.
LOGIN_REVOKED = "INTAKE_LOGIN_REVOKED"
#: The session store could not say whether the login still holds: the save waits and is retried.
LOGIN_UNREADABLE = "INTAKE_LOGIN_UNREADABLE"
_REQUEST_NAMESPACE = uuid.UUID("5d1f3c62-8a7e-4f0b-9c2d-6e4b1a7f3c90")

MAX_ARGUMENT_BYTES = 4 * 1024 * 1024
APPLIED_RETENTION_SECONDS = 7 * 24 * 60 * 60.0
FIRST_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
_DEFAULT_INTAKE_WAIT_SECONDS = 15.0
_MAX_INTAKE_WAIT_SECONDS = 600.0
#: How often a drainer looks again at a row that another live process is applying.
_OTHER_OWNER_POLL_SECONDS = 5.0
#: An idle drainer waits for an acceptance; this read-only re-check only covers a missed one.
_IDLE_RECHECK_SECONDS = 300.0
_OUTCOME_LIMIT_BYTES = 16 * 1024
_LIST_LIMIT = 50
_STABLE_READ_ATTEMPTS = 3
#: How long a resend waits for an in-flight acceptance of its own origin; the acceptance itself is
#: bounded by the store retries, well inside this.
_ACCEPT_WAIT_SECONDS = 60.0

StoreMode = Literal["read", "write", "create"]


def enabled() -> bool:
    """Whether acceptance is on. Off still drains what is queued."""
    return os.environ.get("EXOMEM_BUSY_INTAKE", "1").strip().lower() not in {"0", "false"}  # nosemgrep: ep-word-membership -- the env switch's closed off spellings.


def intake_wait_seconds() -> float:
    """How long an eligible save waits for the boundary before it is queued."""
    try:
        wait = float(os.environ.get("EXOMEM_BUSY_INTAKE_AFTER_SECONDS", _DEFAULT_INTAKE_WAIT_SECONDS))
    except ValueError:
        return _DEFAULT_INTAKE_WAIT_SECONDS
    if not math.isfinite(wait):
        return _DEFAULT_INTAKE_WAIT_SECONDS
    return min(max(0.0, wait), _MAX_INTAKE_WAIT_SECONDS)


def store_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / ".write-intake.sqlite"


def ref_for(intake_id: str) -> str:
    return f"{REF_PREFIX}{intake_id}"


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    """Canonical JSON of a JSON-mode value; raises on anything JSON cannot hold."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _report_json(value: Any) -> str:
    """JSON of a reply or refusal kept for the owner to read; a value JSON cannot hold reads as text."""
    return _json(to_jsonable_python(value, fallback=str))


# --------------------------------------------------------------------------- store


@contextmanager
def _store(vault_root: Path, *, mode: StoreMode) -> Iterator[sqlite3.Connection]:
    """Open the intake store under the private-state ceremony.

    ``create`` makes the store and may change the vault's private identities, so it advances
    the identity generation; only the first acceptance on a vault needs it. ``write`` opens
    the existing store read-write and ``read`` opens it read-only; neither changes an identity,
    and both raise ``FileNotFoundError`` when no store exists yet.
    """
    root = Path(vault_root).resolve()
    path = store_path(root)
    create = mode == "create"
    with reserved_paths._subsystem_authority_scope(OWNER):  # noqa: SLF001
        with reserved_paths._identity_coordination_scope(  # noqa: SLF001
            root, descriptor_ids=(STORE_ID,), identity_may_change=create
        ):
            with reserved_paths._sqlite_owner_target_scope(  # noqa: SLF001
                root, path, STORE_ID, create=create
            ) as retained:
                conn = _open(root, path, retained, mode)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write(vault_root: Path, *, create: bool = False) -> Iterator[sqlite3.Connection]:
    with _store(vault_root, mode="create" if create else "write") as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _open(root: Path, path: Path, retained: Path, mode: StoreMode) -> sqlite3.Connection:
    if mode == "create":
        retained.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(retained, timeout=5.0, check_same_thread=False)
    else:
        if not retained.exists():
            raise FileNotFoundError(retained)
        # `mode=rw` opens the existing file and never creates one.
        # A replay's connection is used by the commit gate on the store thread, under the replay's lock.
        conn = sqlite3.connect(
            f"{retained.as_uri()}?mode={'rw' if mode == 'write' else 'ro'}", uri=True, timeout=5.0,
            check_same_thread=False,
        )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if mode != "read":
            # A queued receipt promises custody: it must survive a power loss, not only a kill.
            conn.execute("PRAGMA synchronous=FULL")
        if mode == "create":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS intake ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, origin TEXT, origin_ttl REAL, "
                "command TEXT NOT NULL, arguments TEXT, principal TEXT NOT NULL, surface TEXT, "
                "state TEXT NOT NULL, owner TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
                "accepted_at REAL NOT NULL, updated_at REAL NOT NULL, next_attempt_at REAL NOT NULL, "
                "error_code TEXT, error TEXT, outcome TEXT, finished_at REAL, "
                "generation INTEGER NOT NULL DEFAULT 0, commit_reached INTEGER NOT NULL DEFAULT 0, named_at REAL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS intake_state ON intake(state, seq)")
            # One open save per original identity: a resend while it is open gets its receipt.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS intake_open_origin ON intake(origin) "
                "WHERE origin IS NOT NULL AND state IN ('queued', 'applying')"
            )
            conn.commit()
        reserved_paths._publish_sqlite_owner_family(root, path, STORE_ID, conn)  # noqa: SLF001
    except BaseException:
        conn.close()
        raise
    return conn


# --------------------------------------------------------------------------- identity and eligibility


def principal_record(principal: Any) -> dict[str, Any] | None:
    """The stored form of an owner principal, or ``None`` for a caller whose authority may not be replayed.

    Only the owner (`raw_protection.is_owner`) is stored. A delegated authorization session can
    be narrowed or revoked after the save is accepted, so replaying it later could exceed what
    the owner still grants; such a caller keeps the busy refusal. The origin-session binding
    of a local or remote client is kept, so the replay rechecks the login that sent the save.
    The same test gates who reads queued saves: they hold exactly these callers' arguments.
    """
    from .governance.raw_protection import is_owner

    if principal is None or not is_owner(principal):
        return None
    if principal.verified_authorization_session is not None or principal.authorization_session_id is not None:
        return None
    binding = principal.client_binding
    origin = principal.origin_session
    return {
        "audience_id": principal.audience_id,
        "surface": principal.surface,
        "session_id": principal.session_id,
        "purpose": principal.purpose,
        "issuer_family": principal.issuer_family,
        "remote_owner": bool(principal.remote_owner),
        "local_owner": bool(principal.local_owner),
        "administrative_ingress": bool(principal.administrative_ingress),
        "client_binding": (
            {"issuer": binding.issuer, "client_id": binding.client_id} if binding is not None else None
        ),
        "origin_session": (
            {"session_id": origin.session_id, "generation": origin.generation, "audience": origin.audience}
            if origin is not None
            else None
        ),
    }


def _restore_principal(record: Mapping[str, Any]) -> Any:
    from .governance import principal as principal_module

    binding = record.get("client_binding")
    origin = record.get("origin_session")
    return principal_module.RequestPrincipal(
        audience_id=record["audience_id"],
        surface=record.get("surface") or "library",
        session_id=record.get("session_id"),
        purpose=record.get("purpose"),
        issuer_family=record.get("issuer_family"),
        remote_owner=bool(record.get("remote_owner")),
        local_owner=bool(record.get("local_owner")),
        administrative_ingress=bool(record.get("administrative_ingress")),
        client_binding=(
            principal_module.ClientBinding(issuer=binding["issuer"], client_id=binding["client_id"])
            if isinstance(binding, Mapping)
            else None
        ),
        origin_session=(
            principal_module.OriginSessionBinding(
                session_id=origin["session_id"], generation=origin["generation"], audience=origin["audience"]
            )
            if isinstance(origin, Mapping)
            else None
        ),
    )


def _login_state(principal: Any) -> Any:
    """The background recheck of the login behind a save, as the collection importer runs it."""
    from . import auth_sessions, cloud_cell
    from .governance import principal as principal_module

    login = auth_sessions.OriginLogin.VALID
    # A Cloud cell's gateway proves its login per request, so outside one only the binding below applies.
    if auth_sessions.origin_revalidates_in_background(principal):
        login = auth_sessions.origin_login_state_sync(principal)
    issuer = principal.issuer_family
    if login is not auth_sessions.OriginLogin.NOT_VALID and cloud_cell.is_cloud_issuer_family(issuer):
        # A Cloud owner's authority holds only while this process serves the cell that granted it.
        if not principal_module.cloud_owner_binding_matches(issuer.removeprefix(cloud_cell.issuer_family(""))):
            return auth_sessions.OriginLogin.NOT_VALID
    return login


def _surface_record() -> dict[str, Any] | None:
    from . import capabilities

    descriptor = capabilities.current_active_surface()
    if descriptor is None:
        return None
    return {
        "surface": descriptor.surface,
        "profile": descriptor.profile,
        "tier2_enabled": descriptor.tier2_enabled,
        "tool_shape": descriptor.tool_shape,
    }


def _restore_surface(record: Mapping[str, Any] | None, command_name: str) -> Any:
    from . import capabilities

    if record is None:
        return None
    return capabilities.ActiveSurfaceDescriptor(
        surface=record["surface"],
        profile=record["profile"],
        tier2_enabled=bool(record["tier2_enabled"]),
        product_commands=(command_name,),
        tool_shape=record.get("tool_shape") or "grouped",
    )


@dataclass(frozen=True)
class _Claim:
    id: str
    command: str
    arguments: dict[str, Any]
    principal: dict[str, Any]
    surface: dict[str, Any] | None
    attempts: int
    generation: int
    origin: str | None
    origin_ttl: float | None


@dataclass
class _Replay:
    """One replay attempt of a claimed save, visible to the commit gate through `_REPLAY`."""

    vault_root: Path
    claim: _Claim
    commit_reached: bool = False
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Opened before the dispatcher runs, so the commit gate's marker is one commit on an open
    #: connection, not an open, a coordination and a close under the vault boundary.
    conn: sqlite3.Connection | None = None
    #: The drainer's stop. Once set, the dispatcher's waits end, so a stop never waits out a busy
    #: boundary or a warm-up, and a refusal that proves nothing committed requeues the save.
    stop: threading.Event | None = None


_REPLAY: contextvars.ContextVar[_Replay | None] = contextvars.ContextVar("exomem_intake_replay", default=None)
_WAIT_CAP: contextvars.ContextVar[float | None] = contextvars.ContextVar("exomem_intake_wait_cap", default=None)


def busy_wait_cap() -> float | None:
    """The deadline (monotonic) by which an eligible save stops waiting and is queued, if any."""
    return _WAIT_CAP.get()


def replay_stop_event() -> threading.Event | None:
    """The drainer's stop when this thread replays a queued save, else ``None``.

    The dispatcher's waits end once it is set, so stopping the drainer never waits for them.
    """
    replay = _REPLAY.get()
    return None if replay is None else replay.stop


def replay_stopping() -> bool:
    """Whether this thread replays a queued save whose drainer is stopping; every dispatcher wait ends then."""
    stop = replay_stop_event()
    return stop is not None and stop.is_set()


def mark_replay_reached_commit() -> None:
    """Record, before a queued save's replay writes its first canonical byte, that it reached its commit.

    The commit gates call this; outside a replay it does nothing. The first call of an attempt
    writes `commit_reached` for the row's current replay identity. A marker that cannot be
    written refuses the attempt as `MUTATION_BUSY` with `committed: false`, so nothing commits
    and the drainer tries again: an unmarked commit could be replayed under a fresh identity
    after a stop, and so land twice.
    """
    replay = _REPLAY.get()
    if replay is None:
        return
    with replay.lock:
        # Work that outlived its replay commits as designed: the requeue of an uncertain
        # outcome already marked its identity, so no reclaim replays it under a fresh one.
        if replay.commit_reached or replay.closed:
            return
        try:
            _mark_commit_reached(replay)
        except Exception as error:
            log.warning("write intake could not mark %s as reaching its commit", replay.claim.id, exc_info=True)
            raise OpError(
                "MUTATION_BUSY",
                "the queued save could not record that it reached its commit, so nothing was written",
                "Nothing to do: the service applies the queued save again.",
                details={"status": "retryable", "committed": False},
            ) from error
        replay.commit_reached = True


def _mark_uncertain(replay: _Replay) -> None:
    """Mark an identity whose work may still commit; the requeue marks it again if this fails."""
    with replay.lock:
        if replay.commit_reached:
            return
        try:
            _mark_commit_reached(replay)
        except Exception:  # noqa: BLE001 - the requeue's own mark is the fallback
            log.warning("write intake could not mark %s before its requeue", replay.claim.id, exc_info=True)
            return
        replay.commit_reached = True


def _mark_commit_reached(replay: _Replay) -> None:
    conn, claim = replay.conn, replay.claim
    if conn is None:
        raise RuntimeError("the replay has no open intake connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE intake SET commit_reached = 1 WHERE id = ? AND generation = ? AND state = ?",
            (claim.id, claim.generation, APPLYING),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("the queued save is no longer claimed by this replay")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# --------------------------------------------------------------------------- drainers


_DRAINERS: dict[Path, Drainer] = {}
_DRAINERS_LOCK = threading.Lock()


def drainer_for(vault_root: Any) -> Drainer | None:
    try:
        key = Path(vault_root).resolve()
    except (TypeError, OSError):
        return None
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.get(key)
    return drainer if drainer is not None and drainer.running else None


def start(vault_root: Path) -> Drainer:
    """Start this process's drainer for one vault. The serving HTTP runtime calls it once."""
    key = Path(vault_root).resolve()
    with _DRAINERS_LOCK:
        existing = _DRAINERS.get(key)
        if existing is not None and existing.running:
            return existing
        drainer = Drainer(key)
        _DRAINERS[key] = drainer
    drainer.start()
    return drainer


def stop(vault_root: Path, *, timeout: float = 10.0) -> None:
    key = Path(vault_root).resolve()
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.pop(key, None)
    if drainer is not None:
        drainer.stop(timeout=timeout)


def _note_finished(vault_root: Path, origin: str | None, intake_id: str, expires_at: float | None) -> None:
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.get(Path(vault_root).resolve())
    if drainer is not None:
        drainer.note_finished(origin, intake_id, expires_at)


def _note_failed(vault_root: Path) -> None:
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.get(Path(vault_root).resolve())
    if drainer is not None:
        drainer.note_failed()


def _owner_identity() -> tuple[str | None, Path]:
    from . import writer_lease

    store = writer_lease.get_manager().idempotency
    return store.owner_id, store.state_dir


def _owner_alive(owner: str | None, mine: str | None, state_dir: Path) -> bool:
    from . import writer_lease

    if owner is None:
        return False
    if owner == mine:
        return True
    return writer_lease._probe_owner_liveness(state_dir, owner)  # noqa: SLF001


class Drainer:
    """The one thread that applies one vault's queued saves, oldest first.

    It also keeps the in-memory index that lets a save that never met contention skip the
    store: the origins of open saves, and of applied saves while their original identity's
    retry window lasts. The index is loaded once at start and kept current on acceptance
    and finish.
    """

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Open saves, or None while unknown: a store that could not be read counts as non-empty.
        self._open: int | None = None
        self._indexed = False
        self._open_origins: dict[str, str] = {}
        self._applied_origins: dict[str, tuple[str, float]] = {}
        #: Origins with a send in flight (waiting for the boundary or being queued), with a count;
        #: a resend with one of them waits instead of running.
        self._accepting: dict[str, int] = {}
        #: Acceptances between their durable insert and `note_accepted`.
        self._inserting = 0
        #: Acceptances so far; a store read that an acceptance overlapped is read again.
        self._accepts = 0
        #: A save may have failed since an owner reply last looked for unnamed failures. True at
        #: start, so the first eligible save after a restart looks once (`failure_notice`).
        self._failure_unannounced = True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def alive(self) -> bool:
        """Whether the drainer thread still runs, a stopped drainer's replay in flight included."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        try:
            self._load()
        except Exception:  # noqa: BLE001 - the drainer must run even when its store cannot be read
            log.warning("write intake could not read its store at start; the backlog is unknown", exc_info=True)
        thread = threading.Thread(
            target=contextvars.Context().run, args=(self._loop,), name="exomem-write-intake", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float) -> None:
        self._stop.set()
        self.notify()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def notify(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def note_failed(self) -> None:
        with self._condition:
            self._failure_unannounced = True

    def take_failure_flag(self) -> bool:
        """Whether a failure may be unnamed, clearing the flag: a failure during the look raises it again."""
        with self._condition:
            flagged, self._failure_unannounced = self._failure_unannounced, False
            return flagged

    def backlog(self) -> int | None:
        """Open saves, or None while the store has not been read."""
        with self._condition:
            return self._open

    def _backlog_unsettled(self) -> bool:
        # A save between its durable insert and its count is already queued: a later save waits behind it.
        return self._open is None or self._open > 0 or self._inserting > 0

    def has_backlog(self) -> bool:
        with self._condition:
            return self._backlog_unsettled()

    def wait_for_empty_queue(self, deadline: float) -> bool:
        """Wait until no save is open or `deadline` (monotonic) passes; return whether it emptied."""
        with self._condition:
            while self._backlog_unsettled():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return False
                self._condition.wait(min(remaining, 1.0))
            return True

    def enter(self, origin: str) -> tuple[str, str] | None:
        """Admit one send of the save with this origin, atomically.

        While another send of the same save is in flight (waiting for the boundary or being
        queued), wait for it. Then return the intake id and state (open or applied) when the save
        is known, or mark this send in flight and return None; the caller ends that mark with
        `end_accept`. So a resend never runs beside the send it repeats.
        """
        from . import request_budget

        deadline = time.monotonic() + _ACCEPT_WAIT_SECONDS
        budget = request_budget.current()
        if budget is not None:
            # Never wait past the time the caller can still receive a reply.
            deadline = min(deadline, budget.deadline - request_budget.DELIVERY_RESERVE_SECONDS)
        with self._condition:
            while origin in self._accepting and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"a send of origin {origin[:12]} did not finish")
                self._condition.wait(min(remaining, 1.0))
            known = self._known(origin)
            if known is None:
                self._accepting[origin] = self._accepting.get(origin, 0) + 1
            return known

    def _known(self, origin: str) -> tuple[str, str] | None:
        """The intake id and state of the open save, or of the applied save still in its retry window."""
        intake_id = self._open_origins.get(origin)
        if intake_id is not None:
            return intake_id, QUEUED
        applied = self._applied_origins.get(origin)
        if applied is None:
            return None
        if applied[1] <= time.time():
            del self._applied_origins[origin]
            return None
        return applied[0], APPLIED

    def begin_insert(self) -> None:
        with self._condition:
            self._inserting += 1

    def end_insert(self) -> None:
        with self._condition:
            self._inserting -= 1
            self._condition.notify_all()

    def hold(self, origin: str) -> None:
        with self._condition:
            self._accepting[origin] = self._accepting.get(origin, 0) + 1

    def end_accept(self, origin: str) -> None:
        with self._condition:
            remaining = self._accepting.get(origin, 1) - 1
            if remaining > 0:
                self._accepting[origin] = remaining
            else:
                self._accepting.pop(origin, None)
            self._condition.notify_all()

    def note_accepted(self, origin: str | None, intake_id: str) -> None:
        with self._condition:
            self._accepts += 1
            if self._open is not None:
                self._open += 1
            if origin is not None:
                self._open_origins[origin] = intake_id
            self._condition.notify_all()

    def note_finished(self, origin: str | None, intake_id: str, expires_at: float | None) -> None:
        now = time.time()
        with self._condition:
            if origin is not None and self._open_origins.get(origin) == intake_id:
                del self._open_origins[origin]
            self._applied_origins = {
                key: value for key, value in self._applied_origins.items() if value[1] > now
            }
            if origin is not None and expires_at is not None and expires_at > now:
                self._applied_origins[origin] = (intake_id, expires_at)

    def _load(self) -> None:
        """Read the open count and the origin index in one query."""
        for _attempt in range(_STABLE_READ_ATTEMPTS):
            with self._condition:
                seen = self._accepts
            now = time.time()
            try:
                with _store(self.vault_root, mode="read") as conn:
                    rows = conn.execute(
                        "SELECT id, origin, state, finished_at, origin_ttl FROM intake WHERE state IN (?, ?) "
                        "OR (state = ? AND origin IS NOT NULL AND finished_at + origin_ttl > ?)",
                        (*_OPEN_STATES, APPLIED, now),
                    ).fetchall()
            except FileNotFoundError:
                rows = []
            open_count = 0
            open_origins: dict[str, str] = {}
            applied_origins: dict[str, tuple[str, float]] = {}
            for row in rows:
                if row["state"] == APPLIED:
                    applied_origins[row["origin"]] = (row["id"], row["finished_at"] + row["origin_ttl"])
                    continue
                open_count += 1
                if row["origin"] is not None:
                    open_origins[row["origin"]] = row["id"]
            with self._condition:
                if self._accepts != seen:
                    continue
                self._open, self._indexed = open_count, True
                self._open_origins, self._applied_origins = open_origins, applied_origins
                self._condition.notify_all()
                return
        raise RuntimeError("acceptances kept overlapping the intake index read")

    def _refresh_open(self) -> None:
        """Recount open saves read-only; a failed read keeps the last known count."""
        if not self._indexed:
            self._load()
            return
        for attempt in range(_STABLE_READ_ATTEMPTS):
            with self._condition:
                seen = self._accepts
            try:
                with _store(self.vault_root, mode="read") as conn:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM intake WHERE state IN (?, ?)", _OPEN_STATES
                    ).fetchone()[0]
            except FileNotFoundError:
                count = 0
            with self._condition:
                # An acceptance during the read may be missing from it; its own count stands.
                if self._accepts == seen or attempt == _STABLE_READ_ATTEMPTS - 1:
                    self._open = int(count) if self._accepts == seen else max(int(count), self._open or 0)
                    self._condition.notify_all()
                    return

    def _loop(self) -> None:
        backoff_failures = 0
        while not self._stop.is_set():
            try:
                due_in = self._drain_one()
                backoff_failures = 0
            except Exception:  # noqa: BLE001 - the drainer must outlive a store fault
                log.warning("write intake drain pass failed", exc_info=True)
                backoff_failures += 1
                due_in = min(MAX_BACKOFF_SECONDS, FIRST_BACKOFF_SECONDS * 2**backoff_failures)
            if due_in is None:
                continue
            with self._condition:
                if self._stop.is_set():
                    return
                if due_in != math.inf:
                    self._condition.wait(due_in)
                elif self._open == 0:
                    # Nothing open: an acceptance notifies this wait. Checked under the condition,
                    # so an acceptance that came first is not missed.
                    self._condition.wait(_IDLE_RECHECK_SECONDS)

    def _drain_one(self) -> float | None:
        """Apply the oldest open save if it is due.

        Return seconds to wait, ``inf`` when nothing is open, or None to go again at once.
        While nothing is open the drainer only reads the store, so an idle drainer never
        takes the store's write path.
        """
        if self.backlog() == 0:
            self._refresh_open()
            if self.backlog() == 0:
                return math.inf
        claimed = _with_store_retry(lambda: _claim_head(self.vault_root))
        if claimed is None:
            self._refresh_open()
            return math.inf
        if isinstance(claimed, float):
            return claimed
        _apply(self.vault_root, claimed, stop=self._stop)
        self._refresh_open()
        return None


def _replay_identity(claim: _Claim) -> str:
    return f"intake:{claim.id}" if claim.generation == 0 else f"intake:{claim.id}:{claim.generation}"


def _decoded(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    arguments = json.loads(row["arguments"])
    principal = json.loads(row["principal"])
    surface = json.loads(row["surface"]) if row["surface"] else None
    if not isinstance(arguments, dict) or not isinstance(principal, dict) or not (
        surface is None or isinstance(surface, dict)
    ):
        raise ValueError("a stored queued save is not a JSON object")
    return arguments, principal, surface


def _claim_head(vault_root: Path) -> _Claim | float | None:
    """Claim the oldest open save. A float is the wait until it is due; None means nothing is open.

    Order is strict: a save in back-off holds the saves behind it, so a later edit never
    overtakes the create it depends on. A save left applying by a live other process is
    that process's; this drainer waits for it. A row that does not decode fails with
    ``INTERNAL`` in the same transaction, keeps its arguments, and the claim moves on.
    """
    mine, state_dir = _owner_identity()
    now = time.time()
    undecodable: list[tuple[str | None, str]] = []
    claimed: _Claim | float | None = None
    try:
        with _write(vault_root) as conn:
            while True:
                row = conn.execute(
                    "SELECT * FROM intake WHERE state IN (?, ?) ORDER BY seq LIMIT 1", _OPEN_STATES
                ).fetchone()
                if row is None:
                    break
                if row["state"] == APPLYING and row["owner"] != mine and _owner_alive(row["owner"], mine, state_dir):
                    claimed = _OTHER_OWNER_POLL_SECONDS
                    break
                if row["state"] == QUEUED and row["next_attempt_at"] > now:
                    claimed = max(0.05, row["next_attempt_at"] - now)
                    break
                try:
                    arguments, principal, surface = _decoded(row)
                except (TypeError, ValueError) as error:
                    log.warning("write intake save %s does not decode", row["id"], exc_info=True)
                    conn.execute(
                        "UPDATE intake SET state = ?, owner = NULL, error_code = ?, error = ?, updated_at = ?, "
                        "finished_at = ? WHERE id = ?",
                        (FAILED, "INTERNAL", _json({"code": "INTERNAL", "message": type(error).__name__}),
                         now, now, row["id"]),
                    )
                    undecodable.append((row["origin"], row["id"]))
                    continue
                generation = int(row["generation"])
                if row["state"] == APPLYING and not row["commit_reached"]:
                    # No attempt under this replay identity reached the commit gate, so none wrote a
                    # canonical byte. The attempt that left the row applying may still hold the
                    # identity pending under a dead owner, which the idempotency store keeps as
                    # outcome-unknown for good; a fresh identity replays the save instead of leaving
                    # it stuck. `commit_reached` is sticky per identity, so an identity that ever
                    # reached its commit (and so may have committed) is never abandoned this way.
                    generation += 1
                conn.execute(
                    "UPDATE intake SET state = ?, owner = ?, attempts = attempts + 1, updated_at = ?, "
                    "commit_reached = CASE WHEN generation = ? THEN commit_reached ELSE 0 END, "
                    "generation = ? WHERE id = ?",
                    (APPLYING, mine, now, generation, generation, row["id"]),
                )
                claimed = _Claim(
                    id=row["id"],
                    command=row["command"],
                    arguments=arguments,
                    principal=principal,
                    surface=surface,
                    attempts=int(row["attempts"]) + 1,
                    generation=generation,
                    origin=row["origin"],
                    origin_ttl=row["origin_ttl"],
                )
                break
    except FileNotFoundError:
        return None
    for origin, intake_id in undecodable:
        _note_finished(vault_root, origin, intake_id, None)
    if undecodable:
        _note_failed(vault_root)
    return claimed


def _command_named(name: str) -> Any:
    from .commands import PRODUCT_COMMANDS

    for command in PRODUCT_COMMANDS:
        if command.name == name:
            return command
    return None


def _apply(vault_root: Path, claim: _Claim, *, stop: threading.Event | None = None) -> None:
    """Replay one claimed save in a fresh context and record its outcome."""
    contextvars.Context().run(_apply_in_context, vault_root, claim, stop)


def _apply_in_context(vault_root: Path, claim: _Claim, stop: threading.Event | None = None) -> None:
    outcome = _replay(vault_root, claim, stop=stop)
    if outcome[0] == APPLIED:
        _with_store_retry(lambda: _finish_applied(vault_root, claim, outcome[1]))
    elif outcome[0] == QUEUED:
        _with_store_retry(lambda: _requeue(vault_root, claim, outcome[1]))
    else:
        _with_store_retry(lambda: _finish_failed(vault_root, claim, outcome[1], outcome[2]))


def _replay(vault_root: Path, claim: _Claim, *, stop: threading.Event | None = None) -> tuple[Any, ...]:
    """Run one replay; return ``(applied, result)``, ``(queued, code)`` or ``(failed, code, refusal)``.

    Every step from the command lookup to the dispatcher sits in one try, so any error ends
    in a recorded outcome and the row never stays applying behind the queue.
    """
    from . import auth_sessions, capabilities, cli_ops, writer_lease
    from .governance import principal as principal_module

    replay = _Replay(vault_root, claim, stop=stop)
    token = _REPLAY.set(replay)
    stack = ExitStack()
    try:
        command = _command_named(claim.command)
        if command is None:
            raise OpError("INTAKE_COMMAND_UNKNOWN", f"no command named {claim.command}",
                          "Send the save again through a current tool.")
        principal = _restore_principal(claim.principal)
        login = _login_state(principal)
        if login is auth_sessions.OriginLogin.UNREADABLE:
            return (QUEUED, LOGIN_UNREADABLE)
        if login is auth_sessions.OriginLogin.NOT_VALID:
            return (FAILED, LOGIN_REVOKED, {
                "code": LOGIN_REVOKED,
                "message": "the login that sent this save has ended, so the service did not apply it",
                "remediation": "Sign in again and send the save again with its arguments below.",
            })
        descriptor = _restore_surface(claim.surface, command.name)
        if command.needs_schema:
            from . import schema as schema_module

            injected: tuple[Any, ...] = (vault_root, schema_module.load_source_schema(vault_root))
        else:
            injected = (vault_root,)
        try:
            replay.conn = stack.enter_context(_store(vault_root, mode="write"))
        except Exception:  # noqa: BLE001 - without its marker connection the replay waits for the next pass
            log.warning("write intake could not open its store for the replay of %s", claim.id, exc_info=True)
            return (QUEUED, "MUTATION_BUSY")
        with (
            principal_module.request_scope(principal),
            capabilities.active_surface(descriptor) if descriptor is not None else _nothing(),
            principal_module.invocation_scope(),
            writer_lease.caller_holds_no_request_worker(),
        ):
            result = writer_lease.invoke_command(
                command,
                *injected,
                idempotency_key=_replay_identity(claim),
                idempotency_principal_scope=principal.audience_id,
                # Receipts require a UUID request id; one per attempt, derived from the save.
                mutation_request_id=str(uuid.uuid5(_REQUEST_NAMESPACE, f"{claim.id}:{claim.attempts}")),
                **claim.arguments,
            )
    except Exception as error:  # noqa: BLE001 - every replay ends in a recorded outcome
        refusal = cli_ops.public_refusal(error)
        code = str(refusal.get("code")) if refusal else "INTERNAL"
        if code in RETRY_REFUSALS:
            if code in UNCERTAIN_REFUSALS:
                # Before the replay closes: its work may still commit late, and must find the mark.
                _mark_uncertain(replay)
            return (QUEUED, code)
        if (
            stop is not None and stop.is_set() and refusal is not None
            and refusal.get("committed") is False and refusal.get("status") == "retryable"
        ):
            # The stop ended a wait the save needed, such as episode reference re-planning: the
            # refusal proves nothing committed, so the save stays queued for the next process.
            return (QUEUED, code)
        if refusal is None:
            log.warning("write intake replay of %s failed", claim.command, exc_info=True)
            refusal = {"code": "INTERNAL", "message": type(error).__name__}
        return (FAILED, code, refusal)
    finally:
        with replay.lock:
            replay.closed = True
            stack.close()
        _REPLAY.reset(token)
    return (APPLIED, result)


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def _requeue(vault_root: Path, claim: _Claim, code: str) -> None:
    delay = min(MAX_BACKOFF_SECONDS, FIRST_BACKOFF_SECONDS * 2 ** max(0, claim.attempts - 1))
    now = time.time()
    with _write(vault_root) as conn:
        conn.execute(
            "UPDATE intake SET state = ?, owner = NULL, updated_at = ?, next_attempt_at = ?, error_code = ?, "
            "commit_reached = CASE WHEN ? THEN 1 ELSE commit_reached END "
            "WHERE id = ? AND state = ? AND generation = ?",
            (QUEUED, now, now + delay, code, code in UNCERTAIN_REFUSALS, claim.id, APPLYING, claim.generation),
        )


def _bounded_outcome(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"result": str(result)[:1024]}
    text = _report_json(dict(result))
    if len(text.encode("utf-8")) <= _OUTCOME_LIMIT_BYTES:
        return json.loads(text)
    # A large reply keeps its top-level scalars: the summary, state, title and ids.
    return {
        key: value
        for key, value in result.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }


def _finish_applied(vault_root: Path, claim: _Claim, result: Any) -> None:
    now = time.time()
    with _write(vault_root) as conn:
        # Like every other transition, only the claim that still holds the row may finish it.
        finished = conn.execute(
            "UPDATE intake SET state = ?, owner = NULL, arguments = NULL, outcome = ?, error_code = NULL, "
            "updated_at = ?, finished_at = ? WHERE id = ? AND state = ? AND generation = ?",
            (APPLIED, _report_json(_bounded_outcome(result)), now, now, claim.id, APPLYING, claim.generation),
        ).rowcount
        conn.execute(
            "DELETE FROM intake WHERE state = ? AND finished_at < ?",
            (APPLIED, now - APPLIED_RETENTION_SECONDS),
        )
    if not finished:
        log.warning("write intake dropped a stale finish of %s", claim.id)
        return
    expires_at = now + claim.origin_ttl if claim.origin_ttl is not None else None
    _note_finished(vault_root, claim.origin, claim.id, expires_at)
    log.info("write intake applied %s", claim.id)


def _finish_failed(vault_root: Path, claim: _Claim, code: str, refusal: Mapping[str, Any]) -> None:
    now = time.time()
    with _write(vault_root) as conn:
        finished = conn.execute(
            "UPDATE intake SET state = ?, owner = NULL, error_code = ?, error = ?, updated_at = ?, "
            "finished_at = ? WHERE id = ? AND state = ? AND generation = ?",
            (FAILED, code, _report_json(dict(refusal)), now, now, claim.id, APPLYING, claim.generation),
        ).rowcount
    if not finished:
        log.warning("write intake dropped a stale finish of %s", claim.id)
        return
    _note_finished(vault_root, claim.origin, claim.id, None)
    _note_failed(vault_root)
    log.warning("write intake save %s failed with %s", claim.id, code)


# --------------------------------------------------------------------------- acceptance


@dataclass
class Admission:
    """One eligible save on its way through the dispatcher."""

    drainer: Drainer
    command: Any
    #: The arguments as JSON text, the form the drainer replays.
    encoded: str
    control: dict[str, Any]
    principal: dict[str, Any]
    surface: dict[str, Any] | None
    #: The dispatcher's own idempotency identity and its retry window; a save without one is not admitted.
    origin: str
    origin_ttl: float

    def run(self, call: Callable[[], Any]) -> Any:
        """Run the save, or queue it when the boundary stays busy.

        A save that never meets contention reads no store: a resend is recognised from the
        drainer's in-memory index. Any intake fault before `call()` runs the save as if the
        intake were off, and a fault while accepting keeps the original busy refusal.
        """
        started = time.monotonic()
        deadline = started + intake_wait_seconds()
        in_flight = False
        try:
            known = self.drainer.enter(self.origin)
            in_flight = known is None
        except TimeoutError as error:
            # The same save is still in flight; running it now could land it twice.
            raise OpError(
                "MUTATION_BUSY",
                "the same save is still being sent",
                "Send it again in a moment with the same idempotency key.",
                details={"status": "retryable", "committed": False},
            ) from error
        except Exception:  # noqa: BLE001 - an intake fault must not stop an ordinary save
            log.warning("write intake could not check a resend of a %s save", self.command.name, exc_info=True)
            known = None
        if known is not None:
            answer = self._known_answer(*known)
            if answer is not None:
                return answer
            # Its row is gone or past its retry window: this is a new send of the save.
            self.drainer.hold(self.origin)
            in_flight = True
        try:
            return self._run_in_flight(call, started, deadline)
        finally:
            if in_flight:
                self.drainer.end_accept(self.origin)

    def _run_in_flight(self, call: Callable[[], Any], started: float, deadline: float) -> Any:
        try:
            behind = self.drainer.has_backlog() and not self.drainer.wait_for_empty_queue(deadline)
        except Exception:  # noqa: BLE001 - an intake fault must not stop an ordinary save
            log.warning("write intake could not wait behind its queue", exc_info=True)
            behind = False
        if behind:
            try:
                # Behind earlier queued saves: run after them, never before.
                return self._accept(reason="queue_not_empty", started=started)
            except Exception:  # noqa: BLE001 - an intake that cannot write runs the save now
                log.warning("write intake could not queue a %s save behind its queue", self.command.name,
                            exc_info=True)
        token = _WAIT_CAP.set(deadline)
        try:
            return call()
        except OpError as error:
            if error.code not in ACCEPTED_REFUSALS or error.details.get("committed") is not False:
                raise
            try:
                return self._accept(reason=error.code, started=started)
            except Exception:  # noqa: BLE001 - an intake that cannot write leaves today's refusal
                log.warning("write intake could not accept a %s save", self.command.name, exc_info=True)
                raise error from None
        finally:
            _WAIT_CAP.reset(token)

    def _known_answer(self, intake_id: str, state: str) -> dict[str, Any] | None:
        try:
            return _with_store_retry(lambda: self._stored_answer(intake_id))
        except Exception:  # noqa: BLE001 - a known save is answered from the index, never run again
            log.warning("write intake answered a resend from its index", exc_info=True)
            return _index_answer(intake_id, state)

    def _stored_answer(self, intake_id: str) -> dict[str, Any] | None:
        try:
            with _store(self.drainer.vault_root, mode="read") as conn:
                row = conn.execute(
                    "SELECT * FROM intake WHERE id = ? AND origin = ?", (intake_id, self.origin)
                ).fetchone()
                if row is None:
                    return None
                if row["state"] in _OPEN_STATES:
                    return _receipt(conn, row)
                if row["state"] == APPLIED and row["finished_at"] + (row["origin_ttl"] or 0.0) > time.time():
                    # The original identity never committed; the queued replay did. Answer the
                    # resend with that outcome, within the original identity's retry window.
                    outcome = json.loads(row["outcome"]) if row["outcome"] else {}
                    return {**outcome, "queued_write": {"ref": ref_for(row["id"]), "state": APPLIED}}
                return None
        except FileNotFoundError:
            return None

    def _accept(self, *, reason: str, started: float) -> dict[str, Any]:
        return _with_store_retry(lambda: self._accept_once(reason=reason, started=started))

    def _accept_once(self, *, reason: str, started: float) -> dict[str, Any]:
        # `run` holds this send in flight, so no resend runs between the insert and the index entry;
        # the insert count keeps a later save behind it in the same gap.
        self.drainer.begin_insert()
        try:
            try:
                receipt, inserted = self._insert(reason, create=False)
            except FileNotFoundError:
                # The first acceptance on this vault creates the store.
                receipt, inserted = self._insert(reason, create=True)
            if inserted is not None:
                self.drainer.note_accepted(self.origin, inserted)
        finally:
            self.drainer.end_insert()
        if inserted is not None:
            _journal_queued(self, inserted, reason, started)
        return receipt

    def _insert(self, reason: str, *, create: bool) -> tuple[dict[str, Any], str | None]:
        intake_id = uuid.uuid4().hex
        now = time.time()
        with _write(self.drainer.vault_root, create=create) as conn:
            row = conn.execute(
                "SELECT * FROM intake WHERE origin = ? AND state IN (?, ?)", (self.origin, *_OPEN_STATES)
            ).fetchone()
            if row is not None:
                return _receipt(conn, row), None
            conn.execute(
                "INSERT INTO intake (id, origin, origin_ttl, command, arguments, principal, surface, state, "
                "accepted_at, updated_at, next_attempt_at, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intake_id, self.origin, self.origin_ttl, self.command.name, self.encoded,
                    _json(self.principal), _json(self.surface) if self.surface else None,
                    QUEUED, now, now, now, reason,
                ),
            )
            row = conn.execute("SELECT * FROM intake WHERE id = ?", (intake_id,)).fetchone()
            return _receipt(conn, row), intake_id


_STORE_RETRIES = 5


def _with_store_retry(operation: Callable[[], Any]) -> Any:
    """Run one intake-store operation, retrying a busy private-state identity domain briefly.

    Every open of the store takes its reserved-identity domain for milliseconds; a drainer
    pass and an acceptance can meet there. That contention must not refuse a save.
    """
    for attempt in range(_STORE_RETRIES):
        try:
            return operation()
        except OpError as error:
            if error.code != "MUTATION_BUSY" or attempt == _STORE_RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _journal_queued(admission: Admission, intake_id: str, reason: str, started: float) -> None:
    from . import writer_lease

    try:
        writer_lease.get_manager()._record_mutation_journal(  # noqa: SLF001
            request_id=str(admission.control.get("mutation_request_id") or intake_id),
            command=admission.command.name,
            receipt=intake_id[:16],
            outcome="queued",
            error_code=reason,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            scope=admission.control.get("implicit_idempotency_scope")
            or admission.control.get("idempotency_principal_scope"),
            targets=[str(admission.drainer.vault_root)],
            boundary_timing=(None, None),
        )
    except Exception:  # noqa: BLE001 - the journal is observability; the save is already held
        log.debug("write intake journal row failed", exc_info=True)


def _position(conn: sqlite3.Connection, seq: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM intake WHERE state IN (?, ?) AND seq <= ?", (*_OPEN_STATES, seq)
        ).fetchone()[0]
    )


def _receipt(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    ref = ref_for(row["id"])
    position = _position(conn, row["seq"])
    return {
        "success": True,
        "state": QUEUED,
        "committed": False,
        "user_summary": (
            "Queued, not saved yet: Evermatter was busy, so it is holding this save durably and will "
            f"apply it in arrival order (position {position}). Do not resend it; check its outcome "
            "with the route below."
        ),
        "queued_write": {"ref": ref, "position": position, "accepted_at": _iso(row["accepted_at"])},
        "route": {"tool": "review_memory", "args": {"mode": MODE, "ref": ref}},
    }


def _index_answer(intake_id: str, state: str) -> dict[str, Any]:
    """The answer to a resend of a known save when its row cannot be read: its ref, never a rerun."""
    ref = ref_for(intake_id)
    route = {"tool": "review_memory", "args": {"mode": MODE, "ref": ref}}
    if state == APPLIED:
        return {
            "success": True,
            "committed": True,
            "user_summary": "Saved earlier from the queue; its outcome could not be read just now. "
            "Do not resend it; check it with the route below.",
            "queued_write": {"ref": ref, "state": APPLIED},
            "route": route,
        }
    return {
        "success": True,
        "state": QUEUED,
        "committed": False,
        "user_summary": "Queued, not saved yet: this save is already held and will be applied in "
        "arrival order. Do not resend it; check its outcome with the route below.",
        "queued_write": {"ref": ref},
        "route": route,
    }


def admission(command: Any, injected: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Admission | None:
    """The intake admission for one dispatched call, or ``None`` when the intake does not apply."""
    if _REPLAY.get() is not None or not getattr(command, "busy_intake", False) or not injected or not enabled():
        return None
    drainer = drainer_for(injected[0])
    if drainer is None:
        return None
    from . import writer_lease
    from .commands import busy_intake_applies, invocation_is_read_only
    from .governance import principal as principal_module
    from .governance.egress import SelectorCoverageError

    control_names = writer_lease.invoke_control_arguments()
    arguments = {key: value for key, value in kwargs.items() if key not in control_names}
    try:
        if invocation_is_read_only(command, arguments):
            return None
    except SelectorCoverageError:
        return None
    principal = principal_record(principal_module.current_principal())
    if principal is None:
        return None
    try:
        # JSON mode: a typed argument (an MCP operation model) is stored as the JSON it came from.
        jsonable = to_jsonable_python(arguments)
        encoded = _json(jsonable)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        return None
    if not busy_intake_applies(command, jsonable):
        return None
    control = {key: kwargs[key] for key in control_names if key in kwargs}
    try:
        origin = writer_lease.replay_origin(command, injected, {**jsonable, **control})
    except Exception:  # noqa: BLE001 - a call whose identity cannot be derived runs as before
        log.debug("write intake could not derive the identity of a %s save", command.name, exc_info=True)
        return None
    if origin is None:
        # Without an idempotency identity a resend cannot be told from a new save, so each resend
        # would queue another copy; such a caller keeps today's busy refusal.
        return None
    return Admission(
        drainer=drainer,
        command=command,
        encoded=encoded,
        control=control,
        principal=principal,
        surface=_surface_record(),
        origin=origin[0],
        origin_ttl=origin[1],
    )


# --------------------------------------------------------------------------- failure notice


def failure_notice(drainer: Drainer) -> int | None:
    """How many failed queued saves no reply has named yet, marking them named, or ``None``.

    The drainer flags each failure in memory, so a reply reads the store only after one, and
    once after a restart. Each failed row is named once: `named_at` is set in the same
    transaction that counts it. A store that cannot be read leaves the flag set.
    """
    if not drainer.take_failure_flag():
        return None
    try:
        with _write(drainer.vault_root) as conn:
            # A store made before `named_at` existed gains the column here.
            if "named_at" not in {row[1] for row in conn.execute("PRAGMA table_info(intake)")}:
                conn.execute("ALTER TABLE intake ADD COLUMN named_at REAL")
            named = conn.execute(
                "UPDATE intake SET named_at = ? WHERE state = ? AND named_at IS NULL", (time.time(), FAILED)
            ).rowcount
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - the notice must never fail the save it rides on
        log.warning("write intake could not read its failed saves for a reply", exc_info=True)
        drainer.note_failed()
        return None
    return named or None


def with_failure_notice(admission: Admission, result: Any) -> Any:
    """`result` of an owner's eligible save, naming queued saves that failed and no reply has named yet.

    Only an admitted save carries the notice: a replay, a read, a delegated caller or an unkeyed
    save has no admission, so none of them uses a failure up where the owner would not see it.
    """
    if not isinstance(result, Mapping) or result.get("success") is False or isinstance(result.get("error"), Mapping):
        return result
    summary = result.get("user_summary")
    if not isinstance(summary, str) or not summary:
        return result
    count = failure_notice(admission.drainer)
    if not count:
        return result
    from . import capabilities

    route = capabilities.active_call_text("review_memory", {"mode": MODE})
    sentence = (
        f"{count} queued saves sent earlier failed and were not saved; {route} lists them with their "
        "text to correct and send again."
        if count > 1 else
        f"1 queued save sent earlier failed and was not saved; {route} shows it with its text to "
        "correct and send again."
    )
    return {
        **result,
        "queued_writes_failed": count,
        "user_summary": f"{summary} {sentence}" if summary.endswith(".") else f"{summary}. {sentence}",
    }


# --------------------------------------------------------------------------- reads and owner operations


def _view(row: sqlite3.Row, *, full: bool, position: int | None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "ref": ref_for(row["id"]),
        "command": row["command"],
        "state": row["state"],
        "attempts": row["attempts"],
        "accepted_at": _iso(row["accepted_at"]),
        "age_seconds": round(time.time() - row["accepted_at"], 1),
    }
    if row["state"] in _OPEN_STATES:
        view["position"] = position
        if row["error_code"]:
            view["last_wait_reason"] = row["error_code"]
    if row["state"] == FAILED:
        view["error"] = json.loads(row["error"]) if row["error"] else {"code": row["error_code"]}
    if row["state"] == APPLIED:
        view["finished_at"] = _iso(row["finished_at"])
        if full:
            view["outcome"] = json.loads(row["outcome"]) if row["outcome"] else None
    if full and row["arguments"] is not None:
        view["arguments"] = json.loads(row["arguments"])
    return view


def _summary_line(state_counts: Mapping[str, int]) -> str:
    queued = state_counts.get(QUEUED, 0) + state_counts.get(APPLYING, 0)
    failed = state_counts.get(FAILED, 0)
    return f"{queued} queued save(s) waiting, {failed} failed save(s) to review."


def read(vault_root: Path, ref: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """`review_memory(mode="queued-write")`: one save by `ref`, or the open and failed saves."""
    if ref is not None:
        if not ref.startswith(REF_PREFIX) or not ref[len(REF_PREFIX):]:
            raise OpError(
                "INVALID_REVIEW",
                f"queued-write mode takes a ref of the form {REF_PREFIX}<id>",
                "Pass the queued_write.ref from the receipt, or omit ref to list saves.",
            )
        try:
            with _store(vault_root, mode="read") as conn:
                row = conn.execute("SELECT * FROM intake WHERE id = ?", (ref[len(REF_PREFIX):],)).fetchone()
                if row is None:
                    raise OpError(
                        "QUEUED_WRITE_NOT_FOUND",
                        "no queued save has this ref; an applied save is kept for 7 days",
                        "Check the page itself, or list the open saves without ref.",
                    )
                position = _position(conn, row["seq"]) if row["state"] in _OPEN_STATES else None
                view = _view(row, full=True, position=position)
        except FileNotFoundError:
            raise OpError(
                "QUEUED_WRITE_NOT_FOUND",
                "no save has been queued on this vault",
                "Check the page itself.",
            ) from None
        summaries = {  # nosemgrep: ep-word-dict -- keyed by this store's own closed row states.
            QUEUED: "Queued, not saved yet.",
            APPLYING: "Being applied now, not confirmed yet.",
            APPLIED: "Saved: the queued save was applied.",
            FAILED: "Not saved: the queued save failed. Its arguments are below; correct and send it again.",
        }
        summary = summaries[view["state"]]
        if view["state"] == FAILED and view.get("error", {}).get("code") == "MUTATION_OUTCOME_UNKNOWN":
            summary = (
                "Not confirmed: the service stopped while applying this save, so it may have landed. "
                "Check the page before sending it again; its arguments are below."
            )
        return {"user_summary": summary, "queued_write": view}
    cap = max(1, min(int(limit or _LIST_LIMIT), _LIST_LIMIT))
    try:
        with _store(vault_root, mode="read") as conn:
            counts = {
                row["state"]: row["n"]
                for row in conn.execute("SELECT state, COUNT(*) AS n FROM intake GROUP BY state")
            }
            rows = conn.execute(
                "SELECT * FROM intake WHERE state IN (?, ?, ?) ORDER BY seq LIMIT ?",
                (*_OPEN_STATES, FAILED, cap),
            ).fetchall()
    except FileNotFoundError:
        counts, rows = {}, []
    # The listing starts at the oldest row, so a running count of open rows is each one's position.
    items = []
    position = 0
    for row in rows:
        if row["state"] in _OPEN_STATES:
            position += 1
            items.append(_view(row, full=False, position=position))
        else:
            items.append(_view(row, full=False, position=None))
    listed = sum(counts.get(state, 0) for state in (*_OPEN_STATES, FAILED))
    return {
        "user_summary": _summary_line(counts),
        "items": items,
        "counts": {state: counts.get(state, 0) for state in (QUEUED, APPLYING, FAILED, APPLIED)},
        "truncated": listed > len(items),
    }


def status(vault_root: Path) -> dict[str, Any]:
    """Counts for `coordination_status`; ``unknown`` when the store cannot be read."""
    try:
        with _store(vault_root, mode="read") as conn:
            counts = {
                row["state"]: row["n"]
                for row in conn.execute("SELECT state, COUNT(*) AS n FROM intake GROUP BY state")
            }
            oldest = conn.execute(
                "SELECT MIN(accepted_at) FROM intake WHERE state IN (?, ?)", _OPEN_STATES
            ).fetchone()[0]
    except FileNotFoundError:
        counts, oldest = {}, None
    except Exception:  # noqa: BLE001 - an unreadable store is a visible unknown, never zero
        log.warning("write intake status read failed", exc_info=True)
        return {"state": "unknown"}
    return {
        "state": "ready",
        "queued": counts.get(QUEUED, 0),
        "applying": counts.get(APPLYING, 0),
        "failed": counts.get(FAILED, 0),
        "oldest_open_age_seconds": None if oldest is None else round(time.time() - oldest, 1),
    }


def dismiss(vault_root: Path, intake_id: str) -> dict[str, Any]:
    """Remove one failed save. Open or applied saves are not dismissed."""
    if intake_id.startswith(REF_PREFIX):
        intake_id = intake_id[len(REF_PREFIX):]
    try:
        with _write(vault_root) as conn:
            row = conn.execute("SELECT state FROM intake WHERE id = ?", (intake_id,)).fetchone()
            if row is None:
                raise OpError(
                    "QUEUED_WRITE_NOT_FOUND", f"no queued save {intake_id}", "List saves with `exomem intake list`."
                )
            if row["state"] != FAILED:
                raise OpError(
                    "QUEUED_WRITE_NOT_FAILED",
                    f"save {intake_id} is {row['state']}; only a failed save can be dismissed",
                    "Wait for it to be applied, or list saves with `exomem intake list`.",
                )
            conn.execute("DELETE FROM intake WHERE id = ?", (intake_id,))
    except FileNotFoundError:
        raise OpError(
            "QUEUED_WRITE_NOT_FOUND", f"no queued save {intake_id}", "List saves with `exomem intake list`."
        ) from None
    return {"dismissed": ref_for(intake_id)}
