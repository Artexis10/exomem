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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from . import reserved_paths, state_paths, write_intake_access
from .cli_ops import OpError
from .governance.principal import RequestPrincipal

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
#: Long enough for an ordinary boundary wait (99% of direct commits waited under 1.6 s on a busy
#: personal service), short enough that a busy caller gets its receipt at once.
_DEFAULT_INTAKE_WAIT_SECONDS = 2.0
_MAX_INTAKE_WAIT_SECONDS = 600.0
_STORE_TIMEOUT_SECONDS = 5.0
#: How often a drainer looks again at a row that another live process is applying.
_OTHER_OWNER_POLL_SECONDS = 5.0
#: An idle drainer waits for an acceptance; this read-only re-check only covers a missed one.
_IDLE_RECHECK_SECONDS = 300.0
_OUTCOME_LIMIT_BYTES = 16 * 1024
_LIST_LIMIT = 50
#: Bound the number of active caller scopes whose empty notice check we remember.
_NOTICE_SCOPE_CACHE_LIMIT = 128
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
def _store(
    vault_root: Path, *, mode: StoreMode, deadline: float | None = None
) -> Iterator[sqlite3.Connection]:
    """Open the intake store under the private-state ceremony.

    ``create`` makes the store and may change the vault's private identities, so it advances
    the identity generation; only the first acceptance on a vault needs it. ``write`` opens
    the existing store read-write and ``read`` opens it read-only; neither changes an identity,
    and both raise ``FileNotFoundError`` when no store exists yet. A ``deadline`` (a
    ``time.monotonic()`` value) bounds the waits for the identity coordination and for SQLite's
    locks: a wait that would pass it raises instead.
    """
    root = Path(vault_root).resolve()
    path = store_path(root)
    create = mode == "create"
    with reserved_paths._subsystem_authority_scope(OWNER):  # noqa: SLF001
        with reserved_paths._identity_coordination_scope(  # noqa: SLF001
            root, descriptor_ids=(STORE_ID,), identity_may_change=create, deadline=deadline
        ):
            with reserved_paths._sqlite_owner_target_scope(  # noqa: SLF001
                root, path, STORE_ID, create=create
            ) as retained:
                conn = _open(root, path, retained, mode, deadline=deadline)
    try:
        yield conn
    finally:
        conn.close()


def _limit_sqlite_work(conn: sqlite3.Connection, deadline: float | None) -> None:
    if deadline is not None:
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)


@contextmanager
def _write(
    vault_root: Path, *, create: bool = False, deadline: float | None = None, work_deadline: float | None = None,
) -> Iterator[sqlite3.Connection]:
    with _store(vault_root, mode="create" if create else "write", deadline=deadline) as conn:
        _limit_sqlite_work(conn, deadline)
        conn.execute("BEGIN IMMEDIATE")
        _limit_sqlite_work(conn, work_deadline)
        try:
            yield conn
            conn.commit()
        except BaseException:
            if deadline is not None or work_deadline is not None:
                conn.set_progress_handler(None, 0)
            conn.rollback()
            raise


def _open(
    root: Path, path: Path, retained: Path, mode: StoreMode, *, deadline: float | None = None
) -> sqlite3.Connection:
    wait = _STORE_TIMEOUT_SECONDS if deadline is None else min(_STORE_TIMEOUT_SECONDS, max(0.0, deadline - time.monotonic()))
    if mode == "create":
        retained.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(retained, timeout=wait, check_same_thread=False)
    else:
        if not retained.exists():
            raise FileNotFoundError(retained)
        # `mode=rw` opens the existing file and never creates one.
        # A replay's connection is used by the commit gate on the store thread, under the replay's lock.
        conn = sqlite3.connect(
            f"{retained.as_uri()}?mode={'rw' if mode == 'write' else 'ro'}", uri=True, timeout=wait,
            check_same_thread=False,
        )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(wait * 1000)}")
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
                "generation INTEGER NOT NULL DEFAULT 0, commit_reached INTEGER NOT NULL DEFAULT 0, named_at REAL, "
                f"{_DECLARATION_COLUMN})"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS intake_state ON intake(state, seq)")
            # One open save per original identity: a resend while it is open gets its receipt.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS intake_open_origin ON intake(origin) "
                "WHERE origin IS NOT NULL AND state IN ('queued', 'applying')"
            )
            _add_declaration_marker(conn)
            conn.commit()
        reserved_paths._publish_sqlite_owner_family(root, path, STORE_ID, conn)  # noqa: SLF001
    except BaseException:
        conn.close()
        raise
    return conn


def may_change_declaration(command: Any, arguments: Any) -> bool:
    """Whether a queued save of `command` with these JSON `arguments` may change a declaration before a later save
    replays. It is decided once, when the save is accepted, and kept in `declaration_change`, because a replay
    drops the arguments.

    A command that declares `sets_any_frontmatter_field` may, whichever page it names: only its own resolution,
    when it applies, decides which page that is. A command of `operation_entries.manifest_text_commands` may when
    it carries a `MANIFEST_TEXT_FIELD` other than null; MCP stores a field the caller left unset as null. Arguments
    that are not an object may too.
    """
    from . import operation_entries

    if command.sets_any_frontmatter_field:
        return True
    if command.name not in operation_entries.manifest_text_commands():
        return False
    return not isinstance(arguments, Mapping) or arguments.get(operation_entries.MANIFEST_TEXT_FIELD) is not None


#: The column that keeps `may_change_declaration` for each save, one definition for a new store and an upgraded
#: one. A writer that does not know the column, such as an earlier release after a rollback, leaves it NULL, and
#: NULL counts as may change: only a 0 that an acceptance wrote says that a save cannot.
_DECLARATION_COLUMN = "declaration_change INTEGER"
def _add_declaration_marker(conn: sqlite3.Connection) -> None:
    """Start the declaration marker with a single seek, in the caller's write transaction."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='intake_declaration_state'"
    ).fetchone()
    if present is not None:
        return
    if "declaration_change" not in {row[1] for row in conn.execute("PRAGMA table_info(intake)")}:
        conn.execute(f"ALTER TABLE intake ADD COLUMN {_DECLARATION_COLUMN}")
    conn.execute("CREATE TABLE intake_declaration_state ("
                 "id INTEGER PRIMARY KEY CHECK(id=1), last_seq INTEGER NOT NULL)")
    # A legacy save may change a declaration. The primary-key seek covers all legacy saves at once.
    conn.execute("INSERT INTO intake_declaration_state VALUES (1, COALESCE((SELECT MAX(seq) FROM intake), 0))")
    # Older writers run this trigger too. Their omitted column is NULL and must count as a possible change.
    conn.execute("""
        CREATE TRIGGER intake_declaration_on_insert AFTER INSERT ON intake
        WHEN NEW.declaration_change IS NOT 0
        BEGIN
            UPDATE intake_declaration_state SET last_seq = MAX(last_seq, NEW.seq) WHERE id = 1;
        END
    """)


# --------------------------------------------------------------------------- identity and eligibility


def principal_record(principal: Any) -> dict[str, Any] | None:
    """The stored form of an owner principal, or ``None`` for a caller whose authority may not be replayed.

    Only the owner (`raw_protection.is_owner`) is stored. A delegated authorization session can
    be narrowed or revoked after the save is accepted, so replaying it later could exceed what
    the owner still grants; such a caller keeps the busy refusal. The origin-session binding
    of a local or remote client is kept, so the replay rechecks the login that sent the save.
    The same test gates who reads queued saves: they hold exactly these callers' arguments.
    """
    from . import auth_sessions
    from .governance.raw_protection import is_owner

    if principal is None or not is_owner(principal):
        return None
    if principal.verified_authorization_session is not None or principal.authorization_session_id is not None:
        return None
    binding = principal.client_binding
    origin = principal.origin_session
    family = auth_sessions.current_receipt_family(principal)
    return {
        "audience_id": principal.audience_id,
        "surface": principal.surface,
        "session_id": principal.session_id,
        "purpose": principal.purpose,
        "issuer_family": principal.issuer_family,
        "remote_owner": bool(principal.remote_owner),
        "local_owner": bool(principal.local_owner),
        "administrative_ingress": bool(principal.administrative_ingress),
        **({"receipt_family": family} if family is not None else {}),
        "client_binding": (
            {"issuer": binding.issuer, "client_id": binding.client_id} if binding is not None else None
        ),
        "origin_session": (
            {"session_id": origin.session_id, "generation": origin.generation, "audience": origin.audience}
            if origin is not None
            else None
        ),
    }


def restore_principal(record: Mapping[str, Any]) -> Any:
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


def _same_legacy_family(current: Mapping[str, Any], stored: Mapping[str, Any]) -> bool:
    return write_intake_access.same_legacy_family(current, stored, restore=restore_principal)


def login_state(principal: Any) -> Any:
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


def surface_record() -> dict[str, Any] | None:
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


def restore_surface(record: Mapping[str, Any] | None, command_name: str) -> Any:
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
_WAIT_CAP: contextvars.ContextVar[tuple[float, float] | None] = contextvars.ContextVar(
    "exomem_intake_wait_cap", default=None
)


_QUEUEABLE: contextvars.ContextVar[bool] = contextvars.ContextVar("exomem_intake_queueable", default=False)


def queues_busy_refusal() -> bool:
    """Whether a busy refusal of this call's write, with nothing committed, is queued and lands later."""
    return _QUEUEABLE.get()


def busy_wait_cap(*, initial_attempt: bool = False) -> float | None:
    """The deadline (monotonic) by which an eligible save stops waiting and is queued, if any."""
    budget = _WAIT_CAP.get()
    if budget is None:
        return None
    started, deadline = budget
    # Zero skips the contention retries but still allows one ordinary store acquire.
    if initial_attempt and deadline == started:
        return None
    return deadline


def replay_stop_event() -> threading.Event | None:
    """The drainer's stop when this thread replays a queued save, else ``None``.

    The dispatcher's waits end once it is set, so stopping the drainer never waits for them.
    """
    replay = _REPLAY.get()
    return None if replay is None else replay.stop


def replay_stopping() -> bool:
    """Whether this thread replays a queued save whose drainer is stopping.

    The warm-up wait, the turn wait and the store queue wait end then. A guard wait inside one
    attempt still runs to its own short bound.
    """
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


def stop(vault_root: Path, *, worker: Drainer | None = None, timeout: float = 10.0) -> None:
    """Stop the current drainer, or only the named worker when its owner asks."""
    key = Path(vault_root).resolve()
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.get(key)
        if drainer is None or (worker is not None and drainer is not worker):
            return
        del _DRAINERS[key]
    drainer.stop(timeout=timeout)


def _note_finished(
    vault_root: Path, origin: str | None, intake_id: str, expires_at: float | None, *, state: str,
) -> None:
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.get(Path(vault_root).resolve())
    if drainer is not None:
        drainer.note_finished(origin, intake_id, expires_at, state=state)


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


@dataclass(frozen=True)
class _FinishedOrigin:
    intake_id: str
    state: str
    expires_at: float


class Drainer:
    """The one thread that applies one vault's queued saves, oldest first.

    It also keeps the in-memory index that lets a save that never met contention skip the
    store: the origins of open saves, and of finished saves while their original identity's
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
        self._retry_indexes_needed = False
        self._scope_index_needed = False
        self._retry_index_upgrade = threading.Lock()
        self._open_origins: dict[str, str] = {}
        self._finished_origins: dict[str, _FinishedOrigin] = {}
        #: Origins with a send in flight (waiting for the boundary or being queued), with a count;
        #: a resend with one of them waits instead of running.
        self._accepting: dict[str, int] = {}
        #: Acceptances between their durable insert and `note_accepted`.
        self._inserting = 0
        #: A store read repeats if a local origin changed while the read was in progress.
        self._origin_revision = 0
        #: A save may have failed since an owner reply last looked for unnamed failures. True at
        #: start, so the first eligible save after a restart looks once (`failure_notice`).
        self._failure_unannounced = True
        self._failure_revision = 0
        self._empty_notice_scopes: dict[tuple[str, ...], None] = {}

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
            self._failure_revision += 1
            self._empty_notice_scopes.clear()

    def unannounced_failure_revision(self, scopes: tuple[str, ...]) -> int | None:
        with self._condition:
            if self._failure_unannounced and scopes not in self._empty_notice_scopes:
                return self._failure_revision
            return None

    def no_unannounced_failures_in_scope(self, revision: int, scopes: tuple[str, ...]) -> None:
        with self._condition:
            if self._failure_revision != revision:
                return
            remembered = self._empty_notice_scopes
            if len(remembered) >= _NOTICE_SCOPE_CACHE_LIMIT:
                del remembered[next(iter(remembered))]
            remembered[scopes] = None

    def no_unannounced_failures(self, revision: int) -> None:
        with self._condition:
            if self._failure_revision == revision:
                self._failure_unannounced = False

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

    def prepare_retained_origins(self, deadline: float, *, work_deadline: float) -> None:
        """Qualify a legacy store on its first save, before choosing a new execution."""
        if not (self._retry_indexes_needed or self._scope_index_needed):
            return
        from . import request_budget

        budget = request_budget.current()
        if budget is not None:
            work_deadline = min(work_deadline, budget.deadline - request_budget.DELIVERY_RESERVE_SECONDS)
        deadline = min(deadline, work_deadline)
        try:
            if not self._retry_index_upgrade.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise TimeoutError("another save is preparing legacy receipts")
            try:
                if not (self._retry_indexes_needed or self._scope_index_needed):
                    return
                # The intake wait bounds lock contention. A short wait must still allow the
                # one-time build to finish after SQLite grants the transaction.
                if self._retry_indexes_needed:
                    with _write(self.vault_root, deadline=deadline, work_deadline=work_deadline) as conn:
                        _add_retry_indexes(conn)
                    self._load(expect_store=True, deadline=work_deadline)
                if self._scope_index_needed:
                    with _write(self.vault_root, deadline=deadline, work_deadline=work_deadline) as conn:
                        write_intake_access.ensure_index(conn)
                    self._scope_index_needed = False
            finally:
                self._retry_index_upgrade.release()
        except Exception as error:  # noqa: BLE001 - an unreadable legacy receipt may already own this save
            if not self._retry_indexes_needed:
                log.debug("receipt scope is not indexed yet", exc_info=True)
                return
            raise OpError("MUTATION_BUSY", "the earlier save receipts could not be checked",
                          "Retry with the same idempotency key once the intake is available.",
                          details={"status": "retryable", "committed": None}) from error

    def enter(self, origin: str) -> tuple[str, str] | None:
        """Admit one send of the save with this origin, atomically.

        While another send of the same save is in flight (waiting for the boundary or being
        queued), wait for it. Then return the intake id and state when the save
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
        """The intake id and state of an open save or a finished save still in its retry window."""
        intake_id = self._open_origins.get(origin)
        if intake_id is not None:
            return intake_id, QUEUED
        finished = self._finished_origins.get(origin)
        if finished is None:
            return None
        if finished.expires_at <= time.time():
            del self._finished_origins[origin]
            return None
        return finished.intake_id, finished.state

    def retained_origin(self, origin: str) -> tuple[str, str] | None:
        with self._condition:
            return self._known(origin)

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
            self._origin_revision += 1
            if self._open is not None:
                self._open += 1
            if origin is not None:
                self._open_origins[origin] = intake_id
            self._condition.notify_all()

    def note_finished(self, origin: str | None, intake_id: str, expires_at: float | None, *, state: str) -> None:
        now = time.time()
        with self._condition:
            self._origin_revision += 1
            if origin is not None and self._open_origins.get(origin) == intake_id:
                del self._open_origins[origin]
            self._finished_origins = {
                key: value for key, value in self._finished_origins.items() if value.expires_at > now
            }
            if origin is not None and expires_at is not None and expires_at > now:
                self._finished_origins[origin] = _FinishedOrigin(intake_id, state, expires_at)

    def _load(self, *, expect_store: bool = False, deadline: float | None = None) -> None:
        """Read the open count and the origin index in one query."""
        for _attempt in range(_STABLE_READ_ATTEMPTS):
            with self._condition:
                seen = self._origin_revision
            now = time.time()
            retry_indexes_needed = False
            scope_index_needed = False
            try:
                with _store(self.vault_root, mode="read", deadline=deadline) as conn:
                    _limit_sqlite_work(conn, deadline)
                    retry_indexes_needed = not _has_retry_index(conn)
                    scope_index_needed = not write_intake_access.indexed(conn)
                    rows = _load_origins(conn, now, include_failures=not retry_indexes_needed)
            except FileNotFoundError:
                if expect_store:
                    raise
                rows = []
            open_count = 0
            open_origins: dict[str, str] = {}
            finished_origins: dict[str, _FinishedOrigin] = {}
            # Sorting the selected rows preserves the expiry index scan and resolves legacy duplicates.
            for row in sorted(rows, key=lambda receipt: receipt["seq"]):
                if row["state"] not in _OPEN_STATES:
                    finished_origins[row["origin"]] = _FinishedOrigin(
                        row["id"], row["state"], row["finished_at"] + row["origin_ttl"],
                    )
                    continue
                open_count += 1
                if row["origin"] is not None:
                    open_origins[row["origin"]] = row["id"]
            with self._condition:
                if self._origin_revision != seen:
                    continue
                self._open, self._indexed = open_count, True
                self._open_origins, self._finished_origins = open_origins, finished_origins
                self._retry_indexes_needed = retry_indexes_needed
                self._scope_index_needed = scope_index_needed
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
                seen = self._origin_revision
            try:
                with _store(self.vault_root, mode="read") as conn:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM intake WHERE state IN (?, ?)", _OPEN_STATES
                    ).fetchone()[0]
            except FileNotFoundError:
                count = 0
            with self._condition:
                # An acceptance during the read may be missing from it; its own count stands.
                if self._origin_revision == seen or attempt == _STABLE_READ_ATTEMPTS - 1:
                    self._open = int(count) if self._origin_revision == seen else max(int(count), self._open or 0)
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
    undecodable: list[tuple[str | None, str, float | None]] = []
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
                    undecodable.append((row["origin"], row["id"], row["origin_ttl"]))
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
    for origin, intake_id, ttl in undecodable:
        _note_finished(vault_root, origin, intake_id, now + ttl if ttl is not None else None, state=FAILED)
    if undecodable:
        _note_failed(vault_root)
    return claimed


def command_named(name: str) -> Any:
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
    from . import auth_sessions, cli_ops

    replay = _Replay(vault_root, claim, stop=stop)
    token = _REPLAY.set(replay)
    stack = ExitStack()
    try:
        command = command_named(claim.command)
        if command is None:
            raise OpError("INTAKE_COMMAND_UNKNOWN", f"no command named {claim.command}",
                          "Send the save again through a current tool.")
        principal = restore_principal(claim.principal)
        login = login_state(principal)
        if login is auth_sessions.OriginLogin.UNREADABLE:
            return (QUEUED, LOGIN_UNREADABLE)
        if login is auth_sessions.OriginLogin.NOT_VALID:
            return (FAILED, LOGIN_REVOKED, {
                "code": LOGIN_REVOKED,
                "message": "the login that sent this save has ended, so the service did not apply it",
                "remediation": "Sign in again and send the save again with its arguments below.",
            })
        descriptor = restore_surface(claim.surface, command.name)
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
        result = invoke_in_background(
            principal,
            descriptor,
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


@contextmanager
def background_call(principal: Any, descriptor: Any) -> Iterator[None]:
    """Bind what a command needs when a background thread calls it for a stored principal.

    The write intake's replay and the import filer both run a command as the owner who asked for it,
    long after that request ended; they share this binding so that neither drifts from the other.
    """
    from . import capabilities, writer_lease
    from .governance import principal as principal_module

    with (
        principal_module.request_scope(principal),
        capabilities.active_surface(descriptor) if descriptor is not None else _nothing(),
        principal_module.invocation_scope(),
        writer_lease.caller_holds_no_request_worker(),
    ):
        yield


def invoke_in_background(principal: Any, descriptor: Any, command: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run one command for a stored principal from a background thread, bound as `background_call` binds it."""
    from . import writer_lease

    with background_call(principal, descriptor):
        return writer_lease.invoke_command(command, *args, **kwargs)


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
    _note_finished(vault_root, claim.origin, claim.id, expires_at, state=APPLIED)
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
    expires_at = now + claim.origin_ttl if claim.origin_ttl is not None else None
    _note_finished(vault_root, claim.origin, claim.id, expires_at, state=FAILED)
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
    #: The refusal that the arguments alone are certain to get. A save that would queue raises it
    #: instead, because a queued receipt for it would only fail later, at the replay.
    refusal: Exception | None = None
    #: The same, for a refusal that needs a read of the vault's declarations. Only a save that is
    #: about to queue runs it (`certain_refusal`), so a save that never meets contention reads nothing.
    declared_refusal: Callable[[], Exception | None] | None = None
    #: The call's arguments, whose own references the terminal filter leaves as sent.
    arguments: Mapping[str, Any] = field(default_factory=dict)
    _released: bool = field(default=False, init=False, repr=False)

    @property
    def receipt_scopes(self) -> tuple[str, ...]:
        return write_intake_access.scopes(self.principal)

    def _check_receipt_access(self) -> None:
        from .governance.principal import effective_principal

        _check_release(effective_principal())

    def run(self, call: Callable[[], Any]) -> Any:
        """Run the save, or queue it when the boundary stays busy.

        A save that never meets contention reads no store: a resend is recognised from the
        drainer's in-memory index. Any intake fault before `call()` runs the save as if the
        intake were off, and a fault while accepting keeps the original busy refusal.
        """
        started = time.monotonic()
        wait = intake_wait_seconds()
        deadline = started + wait
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
            # A known receipt must still answer if the legacy database is temporarily unavailable.
            self.drainer.prepare_retained_origins(
                started + (wait or _STORE_TIMEOUT_SECONDS), work_deadline=started + _STORE_TIMEOUT_SECONDS,
            )
            retained = self.drainer.retained_origin(self.origin)
            if retained is not None and retained != known:
                answer = self._known_answer(*retained)
                if answer is not None:
                    return answer
            return self._run_in_flight(call, started, deadline)
        finally:
            if in_flight:
                self.drainer.end_accept(self.origin)

    def certain_refusal(self) -> Exception | None:
        """The refusal this save is certain to get, read once and only for a save about to queue.

        It is raised in place of the call, outside `invoke_command`'s terminal filter, so it leaves through
        that filter here, as the same refusal from the call does. An argument refusal quotes only the
        caller's own arguments, so a filter fault still raises it, as before the declared check existed. A
        declared refusal quotes the vault's own declaration, so one that the filter cannot release is
        dropped: the save goes on as if none were known, and its own refusal comes from the call or the
        replay.
        """
        if self.refusal is not None:
            if not self._released:
                self._released = True
                self._filter(self.refusal)
            return self.refusal
        if self.declared_refusal is not None:
            read, self.declared_refusal = self.declared_refusal, None
            try:
                declared = read()
            except Exception:  # noqa: BLE001 - a declaration that cannot be read leaves the save queueable
                log.debug("write intake could not read the declarations of a %s save", self.command.name,
                          exc_info=True)
                declared = None
            if declared is not None and self._filter(declared):
                self._released = True
                self.refusal = declared
        return self.refusal

    def _filter(self, refusal: Exception) -> bool:
        """Pass `refusal` through the terminal egress filter in place; False when the filter faults."""
        from .governance import egress

        try:
            egress.postfilter_error(
                self.command.name, refusal, self.drainer.vault_root, request_kwargs=self.arguments
            )
        except Exception:  # noqa: BLE001 - the caller decides what an unfiltered refusal does
            log.warning("write intake could not filter the refusal of a %s save", self.command.name,
                        exc_info=True)
            return False
        return True

    def _run_in_flight(self, call: Callable[[], Any], started: float, deadline: float) -> Any:
        try:
            backlog = self.drainer.has_backlog()
        except Exception:  # noqa: BLE001 - an intake fault must not stop an ordinary save
            log.warning("write intake could not read its queue", exc_info=True)
            backlog = False
        if backlog and self.certain_refusal() is not None:
            # Refused whether it runs after the queue or never: waiting would only delay the answer.
            raise self.refusal
        if backlog:
            # A queue rarely empties within a caller's wait, so waiting for it only delays the receipt.
            try:
                # Behind earlier queued saves: run after them, never before.
                return self._accept(reason="queue_not_empty", started=started)
            except _ReceiptUnavailable:
                raise
            except Exception:  # noqa: BLE001 - an intake that cannot write runs the save now
                log.warning("write intake could not queue a %s save behind its queue", self.command.name,
                            exc_info=True)
        token = _WAIT_CAP.set((started, deadline))
        queueable = _QUEUEABLE.set(self.refusal is None)
        try:
            return call()
        except OpError as error:
            if error.code not in ACCEPTED_REFUSALS or error.details.get("committed") is not False:
                raise
            if self.certain_refusal() is not None:
                raise self.refusal from error
            try:
                # The refusal's cause when it names one (a spent call budget, a moved auxiliary), so
                # the queued row says why it waited and the cause can be counted.
                return self._accept(reason=str(error.details.get("cause") or error.code), started=started)
            except _ReceiptUnavailable:
                raise
            except Exception:  # noqa: BLE001 - an intake that cannot write leaves today's refusal
                log.warning("write intake could not accept a %s save", self.command.name, exc_info=True)
                raise error from None
        finally:
            _QUEUEABLE.reset(queueable)
            _WAIT_CAP.reset(token)

    def _known_answer(self, intake_id: str, state: str) -> dict[str, Any] | None:
        try:
            answer = _with_store_retry(lambda: self._stored_answer(intake_id))
        except _ReceiptUnavailable:
            raise
        except Exception:  # noqa: BLE001 - a known save is answered from the index, never run again
            log.warning("write intake answered a resend from its index", exc_info=True)
            if not self.principal.get("administrative_ingress"):
                raise _receipt_unavailable() from None
            return _index_answer(intake_id, state)
        if answer is not None:
            self._check_receipt_access()
        return answer

    def _stored_answer(self, intake_id: str) -> dict[str, Any] | None:
        # A missing database leaves the indexed receipt as the only available evidence.
        with _store(self.drainer.vault_root, mode="read") as conn:
            now = time.time()
            row = conn.execute("SELECT * FROM intake WHERE id = ? AND origin = ?", (intake_id, self.origin)).fetchone()
            if row is None:
                # The partial unique index matches these two closed intake states.
                row = conn.execute(
                    "SELECT * FROM intake WHERE origin = ? AND state IN ('queued', 'applying')",
                    (self.origin,),
                ).fetchone()
            if row is None:
                # Dismissing the latest legacy failure can leave an older receipt for this origin.
                row = conn.execute(
                    "SELECT * FROM intake WHERE origin = ? AND finished_at + origin_ttl > ? ORDER BY seq DESC LIMIT 1",
                    (self.origin, now),
                ).fetchone()
            if row is None:
                return None
            scopes = self.receipt_scopes
            exact = write_intake_access.row_matches(row, scopes)
            position = _position(conn, row["seq"], scopes) if exact and row["state"] in _OPEN_STATES else None
        if not exact and not write_intake_access.row_matches(
            row, scopes, prior_login=lambda stored: _same_legacy_family(self.principal, stored),
        ):
            raise _receipt_unavailable()
        if row["state"] in _OPEN_STATES:
            return _receipt(row, position=position)
        if row["finished_at"] is None or row["finished_at"] + (row["origin_ttl"] or 0.0) <= now:
            return None
        if row["state"] == APPLIED:
            # The original identity never committed; the queued replay did.
            outcome = json.loads(row["outcome"]) if row["outcome"] else {}
            return {**outcome, "queued_write": {"ref": ref_for(row["id"]), "state": APPLIED}}
        if row["state"] == FAILED:
            failure = json.loads(row["error"]) if row["error"] else {}
            committed = failure.get("committed")
            return _index_answer(row["id"], FAILED, committed=committed if isinstance(committed, bool) else None)
        return None

    def _accept(self, *, reason: str, started: float) -> dict[str, Any]:
        self._check_receipt_access()
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
                if not write_intake_access.row_matches(row, self.receipt_scopes):
                    raise _receipt_unavailable()
                return _receipt(row, position=_position(conn, row["seq"], self.receipt_scopes)), None
            # Only the create path runs the schema, so an existing store from before the marker gains it here.
            _add_declaration_marker(conn)
            _add_retry_indexes(conn)
            # A legacy backfill belongs to the budgeted preparation path, never to a busy save.
            if create and conn.execute("SELECT 1 FROM intake LIMIT 1").fetchone() is None:
                write_intake_access.ensure_index(conn)
            conn.execute(
                "INSERT INTO intake (id, origin, origin_ttl, command, arguments, principal, surface, state, "
                "accepted_at, updated_at, next_attempt_at, error_code, declaration_change) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intake_id, self.origin, self.origin_ttl, self.command.name, self.encoded,
                    _json(self.principal), _json(self.surface) if self.surface else None,
                    QUEUED, now, now, now, reason, int(may_change_declaration(self.command, self.arguments)),
                ),
            )
            row = conn.execute("SELECT * FROM intake WHERE id = ?", (intake_id,)).fetchone()
            return _receipt(row, position=_position(conn, row["seq"], self.receipt_scopes)), intake_id


_STORE_RETRIES = 5


def _add_retry_indexes(conn: sqlite3.Connection) -> None:
    # Startup seeks by expiry, while a resend after dismissal seeks by origin first.
    conn.execute("CREATE INDEX IF NOT EXISTS intake_retry_deadline ON intake(finished_at + origin_ttl)")
    conn.execute("CREATE INDEX IF NOT EXISTS intake_retry_origin ON intake(origin, finished_at + origin_ttl)")


def _has_retry_index(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'intake_retry_deadline'").fetchone() is not None


def _load_origins(conn: sqlite3.Connection, now: float, *, include_failures: bool) -> list[sqlite3.Row]:
    fields = "seq, id, origin, state, finished_at, origin_ttl"
    terminals = "finished_at + origin_ttl > ? AND origin IS NOT NULL"
    parameters = (*_OPEN_STATES, now)
    if not include_failures:
        # Legacy idle startup uses the state index. Its first save installs the expiry index.
        terminals = "state = ? AND " + terminals
        parameters = (*_OPEN_STATES, APPLIED, now)
    return conn.execute(
        f"SELECT {fields} FROM intake WHERE state IN (?, ?) "
        f"UNION ALL SELECT {fields} FROM intake WHERE {terminals}", parameters,
    ).fetchall()


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


def _position(conn: sqlite3.Connection, seq: int, scopes: write_intake_access.Scopes) -> int | None:
    if scopes is None:
        return conn.execute(
            "SELECT COUNT(*) FROM intake WHERE state IN (?, ?) AND seq <= ?", (*_OPEN_STATES, seq)
        ).fetchone()[0]
    if len(scopes) > 1 or not write_intake_access.indexed(conn):
        return None
    observed = sum(conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM intake WHERE receipt_scope = ? AND state IN (?, ?) "
        "AND seq <= ? LIMIT ?)", (scope, *_OPEN_STATES, seq, _LIST_LIMIT + 1),
    ).fetchone()[0] for scope in scopes)
    return observed if observed <= _LIST_LIMIT else None


def _receipt(row: sqlite3.Row, *, position: int | None) -> dict[str, Any]:
    ref = ref_for(row["id"])
    return {
        "success": True,
        "state": QUEUED,
        "committed": False,
        "user_summary": (
            "Queued, not saved yet: Evermatter was busy, so it is holding this save durably and will "
            "apply it in arrival order. Do not resend it; check its outcome "
            "with the route below."
        ),
        "queued_write": {"ref": ref, "position": position, "accepted_at": _iso(row["accepted_at"])},
        "route": {"tool": "review_memory", "args": {"mode": MODE, "ref": ref}},
    }


def _index_answer(intake_id: str, state: str, *, committed: bool | None = None) -> dict[str, Any]:
    """A resend of a known save returns its reference even when the outcome cannot be read."""
    ref = ref_for(intake_id)
    route = {"tool": "review_memory", "args": {"mode": MODE, "ref": ref}}
    if state == FAILED:
        return {
            "success": False,
            "state": FAILED,
            "committed": committed,
            "user_summary": "The earlier queued save failed. Check its receipt before sending it again.",
            "queued_write": {"ref": ref, "state": FAILED},
            "route": route,
        }
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
    from .commands import (
        busy_intake_applies,
        declared_refusal_before_queueing,
        invocation_is_read_only,
        refusal_before_queueing,
    )
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
    if not writer_lease.rerun_safe(command, jsonable):
        # A replay would fetch the caller's temporary file links again, or find them expired; such
        # a save keeps today's busy refusal and full wait, so only its caller retries it.
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
        surface=surface_record(),
        origin=origin[0],
        origin_ttl=origin[1],
        refusal=refusal_before_queueing(command, jsonable),
        declared_refusal=lambda: declared_refusal_before_queueing(command, jsonable, drainer.vault_root),
        arguments=jsonable,
    )


# --------------------------------------------------------------------------- failure notice


def failure_notice(admission: Admission) -> int | None:
    """How many failed queued saves no reply has named yet, marking them named, or ``None``.

    The drainer flags each failure in memory, so a reply reads the store only after one, and
    once after a restart. Each failed row is named once: `named_at` is set in the same
    transaction that counts it. A store that cannot be read leaves the flag set.
    """
    drainer, scopes = admission.drainer, admission.receipt_scopes
    revision = drainer.unannounced_failure_revision(scopes)
    if revision is None:
        return None
    try:
        with _store(drainer.vault_root, mode="read") as conn:
            if not write_intake_access.indexed(conn):
                return None
            waiting = any(conn.execute(
                "SELECT 1 FROM intake INDEXED BY intake_receipt_notice "
                "WHERE state = 'failed' AND named_at IS NULL AND receipt_scope = ? LIMIT 1",
                (scope,),
            ).fetchone() for scope in scopes)
            if not waiting:
                drainer.no_unannounced_failures_in_scope(revision, scopes)
                if _unnamed_failure(conn) is None:
                    drainer.no_unannounced_failures(revision)
                return None
        admission._check_receipt_access()
        with _write(drainer.vault_root) as conn:
            rows = [row[0] for scope in scopes for row in conn.execute(
                "SELECT seq FROM intake INDEXED BY intake_receipt_notice WHERE state = 'failed' AND named_at IS NULL "
                "AND receipt_scope = ? ORDER BY seq LIMIT ?", (scope, _LIST_LIMIT),
            )]
            now = time.time()
            named = conn.executemany("UPDATE intake SET named_at = ? WHERE seq = ?",
                                     ((now, seq) for seq in sorted(rows)[:_LIST_LIMIT])).rowcount
            unnamed = _unnamed_failure(conn)
    except FileNotFoundError:
        drainer.no_unannounced_failures(revision)
        return None
    except _ReceiptUnavailable:
        return None
    except Exception:  # noqa: BLE001 - the notice must never fail the save it rides on
        log.warning("write intake could not read its failed saves for a reply", exc_info=True)
        drainer.note_failed()
        return None
    if unnamed is None:
        drainer.no_unannounced_failures(revision)
    return named or None


def _unnamed_failure(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT 1 FROM intake INDEXED BY intake_receipt_notice WHERE state = 'failed' AND named_at IS NULL LIMIT 1"
    ).fetchone()


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
    count = failure_notice(admission)
    if not count:
        return result
    from . import capabilities

    route = capabilities.active_call_text("review_memory", {"mode": MODE})
    saves = "save" if count == 1 else "saves"
    sentence = f"{count} earlier queued {saves} failed. Check the recorded outcomes with {route} before resending."
    coverage = _scope_coverage(admission.receipt_scopes)
    if coverage:
        sentence += f" {coverage['older_saves']}"
    return {
        **result,
        "queued_writes_failed": count,
        **({"queued_writes_failed_coverage": coverage["coverage"]} if coverage else {}),
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


def declaration_mark(vault_root: Path, *, seconds: float, guard_sensitive: bool = False) -> int | None:
    """The lowest `seq` that a save open now, or accepted from now on, holds; None where that is unknown.

    A check that reads a declaration takes this mark before that read and, only once the read refuses, asks
    `declaration_changed_since`. A save that has ended never reopens, AUTOINCREMENT never reuses a `seq`, and an
    ended save stays a row until retention or dismissal removes it. So a change that commits between the two
    reads comes from a save that was open at the mark or was accepted after it, each at or above the mark.

    The mark is the first open save's `seq`, else one more than the last assigned `seq`. SQLite keeps that
    number even when the save is deleted. The lowest `seq` of each open state is a seek on `intake_state`.
    A refusal that depends on current row guards gives up when any save is open. Its replay can change a guard.
    """
    row = _declaration_query(
        vault_root, seconds,
        "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'intake'), 0), "
        "(SELECT MIN(seq) FROM intake WHERE state = ?), (SELECT MIN(seq) FROM intake WHERE state = ?)",
        _OPEN_STATES, empty=(0, None, None),
    )
    if row is None:
        return None
    last, *first_open = row
    marker = min((int(seq) for seq in first_open if seq is not None), default=int(last) + 1)
    return None if guard_sensitive and marker <= last else marker


def declaration_changed_since(
    vault_root: Path, marker: int, *, seconds: float, guard_sensitive: bool = False,
) -> bool | None:
    """Whether a save at or after `marker`, in whatever state it is now, may have changed a declaration; None
    where that is unknown. Read the last possible change from one row. A deleted save can still count until
    the first open save moves past it. A store that has not added the marker answers unknown.
    For a refusal that depends on row guards, any acceptance since the mark can change the guard.
    """
    sql = (
        "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'intake'), 0) >= ?"
        if guard_sensitive else "SELECT last_seq >= ? FROM intake_declaration_state WHERE id = 1"
    )
    row = _declaration_query(vault_root, seconds, sql, (marker,), empty=(0,))
    return None if row is None else bool(row[0])


def _declaration_query(
    vault_root: Path, seconds: float, sql: str, parameters: Sequence[Any], *, empty: tuple[Any, ...]
) -> tuple[Any, ...] | None:
    """The one row that `sql` answers within `seconds`, `empty` where no save was ever queued, None where it is
    unknown. The wait to open the store counts: the statement gets only the time that the open left, none at all
    once it is spent, and an answer that arrives later is unknown too."""
    if seconds <= 0:
        return None
    deadline = time.monotonic() + seconds
    try:
        with _store(vault_root, mode="read", deadline=deadline) as conn:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            conn.execute(f"PRAGMA busy_timeout={math.ceil(remaining * 1000)}")
            _limit_sqlite_work(conn, deadline)
            row = tuple(conn.execute(sql, parameters).fetchone())
    except FileNotFoundError:
        row = empty
    except Exception:  # noqa: BLE001 - a queue that cannot be read is an unknown, never an empty one
        log.debug("write intake could not read its queued saves", exc_info=True)
        return None
    return row if time.monotonic() < deadline else None


class _ReceiptUnavailable(OpError):
    """The receipt is missing or this request cannot disclose it."""


def _receipt_unavailable() -> _ReceiptUnavailable:
    return _ReceiptUnavailable(
        "QUEUED_WRITE_NOT_FOUND", "this queued save is unavailable",
        "Check the reference and sign in through the login that sent the save, or ask the local owner to check it.",
    )


def _read_scope(principal: RequestPrincipal | None) -> write_intake_access.Scopes:
    # Local intake administration passes no request principal and can recover any receipt.
    if principal is None or principal.administrative_ingress:
        return None
    record = principal_record(principal)
    if record is None:
        raise _receipt_unavailable()
    return write_intake_access.scopes(record)


def _check_release(principal: RequestPrincipal | None) -> None:
    if principal is not None and not write_intake_access.can_release(principal):
        raise _receipt_unavailable()


def _counts(conn: sqlite3.Connection, scopes: write_intake_access.Scopes) -> dict[str, int]:
    if scopes is None:
        rows = conn.execute("SELECT state, COUNT(*) AS n FROM intake GROUP BY state")
    else:
        if not write_intake_access.indexed(conn):
            raise OpError(
                "QUEUED_WRITE_STATUS_UNAVAILABLE", "the queued save list is unavailable",
                "Check one save by its receipt reference, or use local intake review for older saves.",
            )
        rows = (row for scope in scopes for row in conn.execute(
            "SELECT state, n FROM intake_receipt_counts WHERE scope = ?", (scope,)
        ))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + row["n"]
    return counts


def _list_receipts(conn: sqlite3.Connection, scopes: write_intake_access.Scopes, cap: int) -> list[sqlite3.Row]:
    selection = "SELECT id, command, state, attempts, accepted_at, error, error_code, finished_at, seq FROM intake"
    if scopes is None:
        return conn.execute(f"{selection} WHERE state IN (?, ?, ?) ORDER BY seq LIMIT ?",
                            (*_OPEN_STATES, FAILED, cap)).fetchall()
    # Each state uses a separate bounded seek, then the first results merge in arrival order.
    rows = [row for scope in scopes for state in (*_OPEN_STATES, FAILED) for row in conn.execute(
        f"{selection} WHERE receipt_scope = ? AND state = ? ORDER BY seq LIMIT ?", (scope, state, cap),
    )]
    return sorted(rows, key=lambda row: row["seq"])[:cap]


def read(
    vault_root: Path, ref: str | None = None, limit: int | None = None, *, principal: RequestPrincipal | None = None,
) -> dict[str, Any]:
    """`review_memory(mode="queued-write")`: one save by `ref`, or the open and failed saves."""
    scopes = _read_scope(principal)
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
                    raise _receipt_unavailable()
                exact = write_intake_access.row_matches(row, scopes)
                position = _position(conn, row["seq"], scopes) if exact and row["state"] in _OPEN_STATES else None
        except FileNotFoundError:
            raise _receipt_unavailable() from None
        if not exact:
            current = principal_record(principal)
            if not write_intake_access.row_matches(
                row, scopes, prior_login=lambda stored: current is not None and _same_legacy_family(current, stored),
            ):
                raise _receipt_unavailable()
        _check_release(principal)
        view = _view(row, full=True, position=position)
        summaries = {  # nosemgrep: ep-word-dict -- keyed by this store's own closed row states.
            QUEUED: "Queued, not saved yet.",
            APPLYING: "Being applied now, not confirmed yet.",
            APPLIED: "Saved: the queued save was applied.",
            FAILED: "The queued save failed. Check its outcome and the refusal below before sending it again.",
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
            counts = _counts(conn, scopes)
            rows = _list_receipts(conn, scopes, cap)
    except FileNotFoundError:
        counts, rows = {}, []
    except Exception:
        _check_release(principal)
        raise
    _check_release(principal)
    # The listing starts at the oldest row, so a running count of open rows is each one's position.
    items = []
    position = 0
    coverage = _scope_coverage(scopes)
    for row in rows:
        if row["state"] in _OPEN_STATES:
            position += 1
            items.append(_view(row, full=False, position=None if coverage else position))
        else:
            items.append(_view(row, full=False, position=None))
    listed = sum(counts.get(state, 0) for state in (*_OPEN_STATES, FAILED))
    return {
        "user_summary": _summary_line(counts),
        "items": items,
        "counts": {state: counts.get(state, 0) for state in (QUEUED, APPLYING, FAILED, APPLIED)},
        "truncated": listed > len(items),
        **coverage,
    }


def _scope_coverage(scopes: write_intake_access.Scopes) -> dict[str, Any]:
    if scopes is None or len(scopes) < 2:
        return {}
    return {"coverage": "partial", "older_saves": "Check an older save by its receipt reference."}


def status(vault_root: Path, *, principal: RequestPrincipal | None = None) -> dict[str, Any]:
    """Counts for `coordination_status`; ``unknown`` when the store cannot be read."""
    try:
        scopes = _read_scope(principal)
        try:
            with _store(vault_root, mode="read") as conn:
                counts = _counts(conn, scopes)
                if scopes is None:
                    oldest = conn.execute(
                        "SELECT MIN(accepted_at) FROM intake WHERE state IN (?, ?)", _OPEN_STATES
                    ).fetchone()[0]
                else:
                    rows = [row for scope in scopes for state in _OPEN_STATES for row in conn.execute(
                        "SELECT accepted_at FROM intake WHERE receipt_scope = ? AND state = ? ORDER BY seq LIMIT 1",
                        (scope, state),
                    )]
                    oldest = min((row[0] for row in rows), default=None)
        except FileNotFoundError:
            counts, oldest = {}, None
        _check_release(principal)
    except _ReceiptUnavailable:
        return {"state": "unknown"}
    except Exception:  # noqa: BLE001 - an unreadable store is a visible unknown, never zero
        log.warning("write intake status read failed", exc_info=True)
        return {"state": "unknown"}
    return {
        "state": "ready",
        "queued": counts.get(QUEUED, 0),
        "applying": counts.get(APPLYING, 0),
        "failed": counts.get(FAILED, 0),
        "oldest_open_age_seconds": None if oldest is None else round(time.time() - oldest, 1),
        **_scope_coverage(scopes),
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
                    "QUEUED_WRITE_NOT_FOUND", f"no queued save {intake_id}", "List saves with `evermatter intake list`."
                )
            if row["state"] != FAILED:
                raise OpError(
                    "QUEUED_WRITE_NOT_FAILED",
                    f"save {intake_id} is {row['state']}; only a failed save can be dismissed",
                    "Wait for it to be applied, or list saves with `evermatter intake list`.",
                )
            _add_retry_indexes(conn)
            conn.execute("DELETE FROM intake WHERE id = ?", (intake_id,))
    except FileNotFoundError:
        raise OpError(
            "QUEUED_WRITE_NOT_FOUND", f"no queued save {intake_id}", "List saves with `evermatter intake list`."
        ) from None
    return {"dismissed": ref_for(intake_id)}
