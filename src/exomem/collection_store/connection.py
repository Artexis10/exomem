"""Connections to the structured-collection store (design §1, §2).

The live store is one SQLite file per vault, ``collections.sqlite`` under the
vault's external state root (placement class ``external-canonical``). It uses
``journal_mode=WAL``, ``synchronous=FULL``, ``foreign_keys=ON`` and a busy
timeout.

- One writer connection per store. Every write is one ``BEGIN IMMEDIATE``
  transaction, and only a caller inside the writer lease may open one: the
  lease stays the cross-process write authority.
- Readers open read-only and never create or migrate a store.
- Readiness refuses rather than degrades. SQLite older than 3.38 (STRICT
  tables and JSON) or a state root where WAL does not take effect makes the
  store unavailable, and collection writes refuse; nothing falls back to
  file-canonical writes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from . import schema

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

STORE_FILENAME = "collections.sqlite"
MINIMUM_SQLITE_VERSION = (3, 38, 0)
BUSY_TIMEOUT_MS = 5000
#: What `describe` and the doctor probe tell the owner about writers without the lease (design §15 item 5).
UNSUPPORTED_WRITERS = (
    "Store collections take every write under the vault's writer lease. Running more than one collection "
    "writer on one vault without that lease, such as a Windows and a WSL service on one synced vault, is "
    "unsupported: once one finds the other's replica, its collection writes refuse with "
    "COLLECTION_STORE_DIVERGED until the owner runs adopt-local, while reads and knowledge writes continue."
)

#: Connection class used for every store connection (a test seam for engines
#: whose journal mode does not take effect).
_CONNECTION_FACTORY: type[sqlite3.Connection] = sqlite3.Connection

# Process-local connection ownership; the existing vault lease remains the
# cross-process authority. Reserve before opening, release on failure or close.
_WRITERS_GUARD = threading.Lock()
_WRITER_PATHS: set[Path] = set()
_LIBRARIES_LOCK = threading.Lock()


def load_store_libraries() -> None:
    """Import SQLAlchemy and Alembic, one thread at a time, before store work.

    The CLI imports this module without them. Two threads importing their submodules
    for the first time at once can see a partially initialised module. Threads start
    store work only through ``runtime.route``, ``open_writer`` or ``open_reader``,
    which call this first, or after ``server.run`` has called it on the main thread.
    Module-level imports in ``tables``, ``typed_storage`` and ``query_indexes`` are
    safe only under that rule.
    """
    with _LIBRARIES_LOCK:
        import alembic.command  # noqa: F401
        import alembic.script  # noqa: F401
        import sqlalchemy  # noqa: F401


class CollectionStoreUnavailable(RuntimeError):
    """The engine or the state root cannot host the store; nothing was written."""

    code = "COLLECTION_STORE_UNAVAILABLE"

    def __init__(self, reason: str, remediation: str) -> None:
        super().__init__(f"{self.code}: {reason}")
        self.reason = reason
        self.remediation = remediation


class CollectionStoreError(RuntimeError):
    """A store refusal with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.reason = message


def busy(message: str):
    """The one shape of every COLLECTION_STORE_BUSY a caller sees: retryable, nothing committed."""
    from ..cli_ops import OpError

    return OpError("COLLECTION_STORE_BUSY", message, "Retry shortly.",
                   details={"status": "retryable", "committed": False})


def store_path(vault_root: Path) -> Path:
    """The live store for one vault, under its external state root."""
    from .. import state_paths

    return state_paths.vault_state_dir(Path(vault_root)) / STORE_FILENAME


def check_sqlite_version(version_info: tuple[int, ...] | None = None) -> None:
    """Refuse a runtime SQLite older than the store's floor."""
    found = tuple(sqlite3.sqlite_version_info if version_info is None else version_info)
    if found < MINIMUM_SQLITE_VERSION:
        floor = ".".join(str(part) for part in MINIMUM_SQLITE_VERSION[:2])
        raise CollectionStoreUnavailable(
            "sqlite_version",
            f"The collection store needs SQLite {floor} or newer (found "
            f"{'.'.join(str(part) for part in found)}). Install a Python build that "
            "bundles a newer SQLite.",
        )


def _connect(
    database: str, *, uri: bool = False, busy_timeout_ms: int = BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    conn = sqlite3.connect(
        database,
        isolation_level=None,
        factory=_CONNECTION_FACTORY,
        uri=uri,
        timeout=busy_timeout_ms / 1000,
        check_same_thread=False,
    )
    try:
        from .native_operands import FileRole, register_native_operands

        register_native_operands(conn, databases={FileRole.STORE: "main", FileRole.DERIVED: "derived"})
        return conn
    except BaseException:
        conn.close()
        raise


def _apply_writer_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        raise CollectionStoreUnavailable(
            "wal_unavailable",
            "WAL journaling did not take effect on the state root. Keep the Exomem "
            "state root on a local filesystem (not a network or synced share).",
        )
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")


def _writer_engine(database: str):
    # Store libraries load with the first store, not with every CLI import.
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    engine = create_engine("sqlite+pysqlite://", creator=lambda: _connect(database), poolclass=NullPool)

    @event.listens_for(engine, "begin")
    def begin(conn):
        # Explicit SQLite transaction control also makes DDL atomic.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


@contextmanager
def checked_sql(conn, check):
    """Keep the caller's interruption when SQLite or a migration wraps its progress error."""
    if check is None:
        yield
        return
    from sqlalchemy.exc import DBAPIError

    interrupted = None

    def progress():
        nonlocal interrupted
        try:
            check()
        except BaseException as error:  # noqa: BLE001 - SQLite otherwise discards the caller's interruption
            interrupted = error
            return 1
        return 0

    conn.set_progress_handler(progress, 1000)
    try:
        check()
        yield
    except (sqlite3.DatabaseError, CollectionStoreError, DBAPIError) as error:
        if interrupted is not None:
            raise interrupted from error
        raise
    finally:
        conn.set_progress_handler(None, 0)


@contextmanager
def staging_store(path: Path, *, check: Callable[[], None] | None = None) -> Iterator[sqlite3.Connection]:
    """A private, unpublished store at ``path`` with the current schema, for a proof that never publishes.

    It takes no writer lease: only its caller opens it, and the caller removes it.
    """
    check_sqlite_version()
    load_store_libraries()
    engine = _writer_engine(str(path))
    try:
        with engine.connect() as core:
            conn = core.connection.driver_connection
            with checked_sql(conn, check):
                conn.execute("PRAGMA recursive_triggers=ON")
                conn.execute("PRAGMA foreign_keys=ON")
                schema.ensure_schema(core)
                yield conn
    finally:
        engine.dispose()


def rollback(conn: Connection) -> None:
    """Clear both transaction states after an unsuccessful mutation."""
    conn.rollback()
    raw = conn.connection.driver_connection
    # A deferred-constraint COMMIT failure deactivates Core before SQLite rolls back.
    if raw.in_transaction:
        raw.rollback()


def _require_lease(lease_check: Callable[[], bool]) -> None:
    if not lease_check():
        raise CollectionStoreError(
            "COLLECTION_STORE_LEASE_REQUIRED",
            "collection store writes run only inside this vault's writer lease",
        )


def _vault_lease_check(path: Path, vault_root: Path | None) -> Callable[[], bool]:
    from ..mutation_lock import VaultMutationCoordinator
    from ..writer_lease import LeaseConfig

    if vault_root is None:
        raise CollectionStoreError(
            "COLLECTION_STORE_LEASE_REQUIRED", "a writer must be bound to its vault"
        )
    if path != store_path(vault_root).resolve():
        raise CollectionStoreError(
            "COLLECTION_STORE_VAULT_MISMATCH", "the store path does not belong to this vault"
        )
    coordinator = VaultMutationCoordinator(LeaseConfig.from_env().state_dir, Path(vault_root))
    return coordinator.current_thread_holds_boundary


class WriterConnection:
    """The one write connection to a store."""

    def __init__(
        self, path: Path, conn: Connection, lease_check: Callable[[], bool]
    ) -> None:
        self.path = path
        self.core = conn
        self.connection = conn.connection.driver_connection
        self._lease_check = lease_check
        self._owner_thread = threading.get_ident()
        self._closed = False
        self._release_cache = None
        self._inspection_identity = object()
        self._write_scope, self._owns_transaction = None, False
        # Host-local import job facts (``importer``), keyed by job id: this handle's
        # proofs of bound source bytes, the store refusal blocking a running job, and
        # a streamed job's open reader. A new handle, as after a takeover, starts with none.
        self.import_proofs: dict[str, Any] = {}
        self.import_blocked: dict[str, str] = {}
        self.import_readers: dict[str, Any] = {}
        # This handle's connection to the derived-rows file (``derived_rows``), opened and
        # reconciled on first use, so a new handle, as after a takeover, reconciles again.
        self.derived: Any = None
        self.view_census: Any = None
        # Read-only connections this tenure lends to any thread; closed first by ``close``.
        self.readers = ReaderPool(path)

    def census(self, root):
        """This writer tenure's disposable view evidence; replacing the handle starts a fresh sweep."""
        self.require_owner_thread()
        if self.view_census is None:
            from .census import Census

            self.view_census = Census(self)
            self.view_census.bind_root(root)
        return self.view_census

    @property
    def release_cache(self):
        self.require_owner_thread()
        if self._release_cache is None:
            from .governance import ReleaseCache

            self._release_cache = ReleaseCache(self.connection)
        return self._release_cache

    def require_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise CollectionStoreError(
                "COLLECTION_STORE_WRITER_THREAD", "only the current owner thread may use the writer connection"
            )

    def require_write_authority(self, *, allow_diverged: bool = False) -> None:
        """Recheck authority before SQL or a publication filesystem effect.

        The publisher may preserve a detected foreign file after recording
        divergence. That trusted filesystem-only caller can opt out of the
        divergence fence, never the opening-thread, open-handle or lease checks.
        Transactions retain the fence, except owner adopt-local's, which clears it.
        """
        self.require_owner_thread()
        if self._closed:
            raise CollectionStoreError(
                "COLLECTION_STORE_WRITER_CLOSED", "the writer connection is closed"
            )
        _require_lease(self._lease_check)
        if not allow_diverged and self.connection.execute(
            "SELECT 1 FROM store_meta WHERE key=?", (schema.META_REPLICA_DIVERGENCE,)
        ).fetchone() is not None:
            raise CollectionStoreError(
                "COLLECTION_STORE_DIVERGED", "the replica requires owner reconciliation"
            )

    @contextmanager
    def transaction(self, *, resolve_divergence: bool = False) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction: commit on success, else roll back."""
        from sqlalchemy.exc import DBAPIError

        self.require_write_authority(allow_diverged=resolve_divergence)
        cache = self.release_cache
        cache.check()
        try:
            self.core.begin()
        except DBAPIError as error:
            raise error.orig from error
        cache.begin()
        self._owns_transaction = True
        try:
            yield self.connection
            cache.prepare()
            self.core.commit()
            cache.finish(True)
        except BaseException as error:
            rollback(self.core)
            cache.finish(False)
            if isinstance(error, DBAPIError):
                raise error.orig from error
            raise
        finally:
            self._close_write_scope()

    @property
    def write_scope(self):
        """This transaction's native metadata scope, opened on first use and closed when the transaction ends."""
        if not self._owns_transaction:
            raise RuntimeError("a native metadata scope belongs to this handle's open transaction")
        if self._write_scope is None:
            from .native_metadata import WriteScope

            self._write_scope = WriteScope(self.connection)
        return self._write_scope

    def _close_write_scope(self) -> None:
        self._owns_transaction = False
        if self._write_scope is not None:
            self._write_scope.close()
            self._write_scope = None

    def execute(self, statement, parameters=None):
        """Execute Core statements only within this handle's mutation scope."""
        from sqlalchemy.exc import DBAPIError

        self.require_owner_thread()
        if not self.core.in_transaction() or not self.connection.in_transaction:
            raise RuntimeError("Core writes require the writer transaction")
        try:
            return self.core.execute(statement, parameters)
        except DBAPIError as error:
            raise error.orig from error

    def checkpoint(self) -> bool:
        """Copy every WAL commit into the store file and empty the WAL (design §11).

        Closing the writer does this only as the last connection: another open reader,
        even an idle one, keeps the commits in the WAL beside a store file that lacks
        them. It never waits, so a release or shutdown keeps its deadline: a reader's
        open read transaction holds the copy back at once, and it returns False with
        every commit still in the WAL.
        """
        self.require_write_authority()
        conn = self.connection
        conn.execute("PRAGMA busy_timeout=0")
        try:
            blocked, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return blocked == 0

    def close(self) -> None:
        if not self._closed:
            self._release_cache = None
            # Before the writer's own connection: an open reader keeps the last commits in the WAL.
            self.readers.close()
            for reader in self.import_readers.values():
                reader.close()
            self.import_readers.clear()
            if self.derived is not None:
                self.derived.close()
                self.derived = None
            if self.view_census is not None:
                self.view_census.close()
                self.view_census = None
            self.core.close()
            self.core.engine.dispose()
            self._closed = True
            with _WRITERS_GUARD:
                _WRITER_PATHS.remove(self.path)

    def __enter__(self) -> WriterConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_writer(
    path: Path, *, vault_root: Path | None = None,
    lease_check: Callable[[], bool] | None = None,
    schema_guard: Callable[[int], None] | None = None,
) -> WriterConnection:
    """Open the vault's single writer, under its mutation boundary.

    ``vault_root`` binds the default authority and store placement to the same
    vault. Opening (including schema repair/migration) and each transaction
    require that boundary. The opening thread owns this connection except during
    the runtime's exclusive coalesced-publication loan (design A3).
    ``lease_check`` is a trusted adapter/test seam: its caller must supply the
    exact store's scoped authority, never the process-wide scheduler predicate.
    ``schema_guard(version)`` runs under that authority around the migration of a
    store that an earlier release wrote. A vault's writer passes it to make the
    vault state manifest record what a store at ``version`` requires of a release:
    the current schema before migrating, so a crash between the two never leaves a
    migrated store without that record, and the earlier schema again after a
    migration that rolled back, so the release that wrote the store may open it.
    """
    check_sqlite_version()
    load_store_libraries()
    from sqlalchemy.exc import DBAPIError

    target = Path(path).resolve()
    check = lease_check if lease_check is not None else _vault_lease_check(target, vault_root)
    _require_lease(check)
    with _WRITERS_GUARD:
        if target in _WRITER_PATHS:
            raise CollectionStoreError(
                "COLLECTION_STORE_WRITER_OPEN", "this store already has an open writer connection"
            )
        _WRITER_PATHS.add(target)
    conn = None
    engine = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        engine = _writer_engine(str(target))
        conn = engine.connect()
        _apply_writer_pragmas(conn.connection.driver_connection)
        _require_lease(check)
        try:
            raw = conn.connection.driver_connection
            found = schema.schema_version(raw, ceiling=schema.SCHEMA_VERSION)
            guarded = schema_guard is not None and found < schema.SCHEMA_VERSION
            if guarded:
                schema_guard(schema.SCHEMA_VERSION)
                _require_lease(check)
            try:
                schema.ensure_schema(conn)
            except BaseException:
                # A guard that cannot lower the record keeps it: refusing an earlier release is the safe side.
                with suppress(Exception):
                    if guarded and schema.schema_version(raw) == found:
                        schema_guard(found)
                raise
        except schema.SchemaVersionError as error:
            raise CollectionStoreError("COLLECTION_STORE_SCHEMA_NEWER", str(error)) from error
        except schema.SchemaMetadataError as error:
            raise CollectionStoreError("COLLECTION_STORE_SCHEMA_INVALID", str(error)) from error
        return WriterConnection(target, conn, check)
    except BaseException as error:
        if conn is not None:
            conn.close()
        if engine is not None:
            engine.dispose()
        with _WRITERS_GUARD:
            _WRITER_PATHS.remove(target)
        if isinstance(error, DBAPIError):
            raise error.orig from error
        raise


def open_reader(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open an existing store read-only at the current schema version."""
    conn, found = _open_read_only(path, busy_timeout_ms)
    if found < schema.SCHEMA_VERSION:
        conn.close()
        raise CollectionStoreError(
            "COLLECTION_STORE_SCHEMA_PENDING",
            "the collection store has not been migrated by its writer yet",
        )
    return conn


@contextmanager
def reader_snapshot(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS):
    """Keep related canonical reads in one read-only transaction, then close it."""
    conn = open_reader(path, busy_timeout_ms=busy_timeout_ms)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


# What every schema version keeps unchanged: the store's metadata and the columns of its commit chain.
_HEAD_COLUMNS = {
    "store_meta": frozenset({"key", "value"}),
    "txns": frozenset({"commit_seq", "event_hash", "store_head_hash"}),  # nosemgrep: ep-word-set -- chain columns
}
_HEAD_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_FUNCTION})


def _head_only(action: int, table: str | None, column: str | None, *_: object) -> int:
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if column in _HEAD_COLUMNS.get(table, ()) else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _HEAD_ACTIONS else sqlite3.SQLITE_DENY


def open_head_reader(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a store at its current or an earlier schema, able to read only its head.

    A store that an earlier release wrote keeps ``store_meta`` and the ``txns`` chain in
    every schema, so its identity, head, chain and pending intent read the same before its
    writer migrates it. SQLite refuses any other table or column, so no row reader can see
    a store that its writer has not migrated yet. A newer schema refuses as in ``open_reader``.
    """
    return _open_head(path, busy_timeout_ms)[0]


def _open_head(path: Path, busy_timeout_ms: int) -> tuple[sqlite3.Connection, int]:
    conn, found = _open_read_only(path, busy_timeout_ms)
    conn.set_authorizer(_head_only)
    return conn, found


# These closed SQL columns let a file writer verify routing without reading canonical item payloads.
_OWNERSHIP_COLUMNS = {
    **_HEAD_COLUMNS,
    "collections": frozenset({"collection_id", "manifest_path", "manifest_version", "source_path", "layout"}),
    "collection_manifests": frozenset({"collection_id", "manifest_version", "manifest_text"}),
}


def _ownership_only(action: int, table: str | None, column: str | None, *_: object) -> int:
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if column in _OWNERSHIP_COLUMNS.get(table, ()) else sqlite3.SQLITE_DENY
    return _head_only(action, table, column)


def open_ownership_reader(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Read head and canonical ownership metadata at the supported schema ceiling, without migration."""
    conn, _ = _open_read_only(path, busy_timeout_ms)
    conn.set_authorizer(_ownership_only)
    return conn


def schema_pending(path: Path) -> bool:
    """Whether the store an earlier release wrote still awaits its writer's migration."""
    conn, found = _open_read_only(path, BUSY_TIMEOUT_MS)
    conn.close()
    return found < schema.SCHEMA_VERSION


def recorded_meta(path: Path, key: str) -> str | None:
    """One ``store_meta`` value that a store records, or None if it is absent or unreadable.

    It reads one row with the standard library, so a launcher can ask without the store libraries.
    It neither validates nor migrates the store.
    """
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                     timeout=BUSY_TIMEOUT_MS / 1000)) as conn:
            row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]
    except (sqlite3.Error, OSError):
        return None


#: The reserved-path family of the migration's retained staging directories (``migration`` owns them).
MIGRATION_STAGING = "collection-migration"


def migration_staging_directories(vault_root: Path) -> list[Path]:
    """The migration staging directories retained beside this vault's live store, by name.

    The launcher reads them here, without the store libraries, to learn whether a handoff
    has a pre-import to finish; ``migration`` reads the same list for its own steps.
    """
    from .. import reserved_paths, state_paths

    state = state_paths.vault_state_dir(Path(vault_root))
    if not state.is_dir():
        return []
    return sorted(path for path in state.iterdir()
                  if reserved_paths.classify_logical(path.name).descriptor_id == MIGRATION_STAGING
                  and path.is_dir() and not path.is_symlink())


def recorded_schema(path: Path) -> int | None:
    """The schema that a store records, also one newer than this release opens, or None if unreadable."""
    value = recorded_meta(path, schema.META_SCHEMA_VERSION)
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _open_read_only(path: Path, busy_timeout_ms: int) -> tuple[sqlite3.Connection, int]:
    """An existing store opened read-only at any schema up to the current one, with that schema."""
    if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be a non-negative integer")
    check_sqlite_version()
    load_store_libraries()
    target = Path(path)
    if not target.is_file():
        raise CollectionStoreError("COLLECTION_STORE_ABSENT", "the collection store does not exist")
    conn = _connect(
        f"{target.resolve().as_uri()}?mode=ro", uri=True, busy_timeout_ms=busy_timeout_ms
    )
    try:
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            found = schema.schema_version(conn, ceiling=schema.SCHEMA_VERSION)
        except schema.SchemaMetadataError as error:
            raise CollectionStoreError("COLLECTION_STORE_SCHEMA_INVALID", str(error)) from error
        except schema.SchemaVersionError as error:
            raise CollectionStoreError("COLLECTION_STORE_SCHEMA_NEWER", str(error)) from error
    except BaseException:
        conn.close()
        raise
    return conn, found


def _file_identity(path: Path) -> tuple[int, int]:
    status = os.stat(path)
    return status.st_dev, status.st_ino


def _recorded_version(conn: sqlite3.Connection) -> int:
    """The schema version a store records, read from ``store_meta`` alone so a head reader may ask."""
    rows = conn.execute("SELECT value FROM store_meta WHERE key = ?", (schema.META_SCHEMA_VERSION,)).fetchall()
    try:
        return int(rows[0][0]) if rows else 0
    except (TypeError, ValueError) as error:
        raise CollectionStoreError("COLLECTION_STORE_SCHEMA_INVALID", "the recorded schema version is invalid") from error


def _close_quietly(conn: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        conn.close()


class _Idle(NamedTuple):
    conn: sqlite3.Connection
    identity: tuple[int, int]


class ReaderPool:
    """Read-only connections that one writer tenure lends to any thread, one borrower at a time.

    A fresh open pays a connect and a schema parse (about 3.5 ms) on every read, and the cached writer
    already outlives those reads. The pool belongs to its ``WriterConnection`` and closes with it: an idle
    reader outliving the writer would keep the last commits in the WAL, block ``journal_mode=DELETE``
    and, on Windows, a replacement of the file. A lend that finds no idle reader opens one, and a lend
    that no longer fits (the pool is closed or full, or the file changed) falls back to what every read
    did before the pool: a reader opened for this call alone.

    Each lend checks that the path still names the reader's file and re-reads the recorded schema
    version, so it refuses exactly as the matching opener would. A reader returns to idle only with no
    open transaction and no active statement. The lock covers bookkeeping only, never a read or a close.
    Lock order: manager locks, then this lock; this lock is a leaf.
    """

    #: Idle readers kept per protection class; extra concurrent demand opens one more for its call.
    IDLE_PER_CLASS = 2

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._closed = False
        # Two protection classes, fixed by code and not a vocabulary: True is the head reader (authorizer
        # ``_head_only``), False the plain one (no authorizer). A reader keeps its class for life.
        self._idle: dict[bool, list[_Idle]] = {True: [], False: []}

    @contextmanager
    def lend(self, *, head: bool, current: bool) -> Iterator[sqlite3.Connection]:
        """Lend a head or plain reader; ``current`` also refuses a store that awaits its writer's migration."""
        conn, version, identity = self._take(head)
        healthy = False
        try:
            if version > schema.SCHEMA_VERSION:
                raise CollectionStoreError(
                    "COLLECTION_STORE_SCHEMA_NEWER", str(schema.SchemaVersionError(version, schema.SCHEMA_VERSION)))
            if current and version < schema.SCHEMA_VERSION:
                raise CollectionStoreError(
                    "COLLECTION_STORE_SCHEMA_PENDING", "the collection store has not been migrated by its writer yet")
            yield conn
            healthy = identity is not None and _quiet(conn)
        finally:
            self._give_back(head, conn, identity if healthy else None)

    def close(self) -> None:
        """Close the idle readers now; a lent reader closes when its lend ends."""
        with self._lock:
            self._closed = True
            idle = [reader for readers in self._idle.values() for reader in readers]
            for readers in self._idle.values():
                readers.clear()
        for reader in idle:
            _close_quietly(reader.conn)

    def _take(self, head: bool) -> tuple[sqlite3.Connection, int, tuple[int, int] | None]:
        """An idle reader that still names the store's file, else a fresh one; the identity is None for one-call readers."""
        with self._lock:
            poolable = not self._closed
            idle = self._idle[head].pop() if poolable and self._idle[head] else None
        if idle is not None:
            try:
                same = _file_identity(self.path) == idle.identity
            except OSError:
                same = False
            if same:
                try:
                    return idle.conn, _recorded_version(idle.conn), idle.identity
                except BaseException:
                    _close_quietly(idle.conn)
                    raise
            # The path names another file or none, so this call opens its own reader and pools nothing.
            _close_quietly(idle.conn)
            poolable = False
        try:
            before = _file_identity(self.path)
        except OSError:
            before = None
        conn, version = (_open_head if head else _open_read_only)(self.path, BUSY_TIMEOUT_MS)
        try:
            after = _file_identity(self.path)
        except OSError:
            after = None
        return conn, version, after if poolable and before == after else None

    def _give_back(self, head: bool, conn: sqlite3.Connection, identity: tuple[int, int] | None) -> None:
        if identity is not None:
            with self._lock:
                if not self._closed and len(self._idle[head]) < self.IDLE_PER_CLASS:
                    self._idle[head].append(_Idle(conn, identity))
                    return
        _close_quietly(conn)


def _quiet(conn: sqlite3.Connection) -> bool:
    """Whether a returned reader holds no transaction and no active statement, so the next lend sees the latest commit."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        # A cursor left half-read pins its snapshot while ``in_transaction`` is False, and only closing the
        # reader releases it. ``interrupt`` takes effect only while a statement is active, so then this
        # SELECT fails as interrupted; with none active it is a no-op.
        conn.interrupt()
        conn.execute("SELECT 1").fetchall()
        return True
    except sqlite3.Error:
        return False


def head_reader(path: Path, pool: ReaderPool | None = None):
    """A head reader for one ``with`` block: lent by ``pool`` when given, else freshly opened and closed."""
    return closing(open_head_reader(path)) if pool is None else pool.lend(head=True, current=False)


def ownership_reader(path: Path, pool: ReaderPool | None = None):
    """A plain reader at any supported schema (ownership declarations ride the unchanged metadata wire)."""
    return closing(_open_read_only(path, BUSY_TIMEOUT_MS)[0]) if pool is None else pool.lend(head=False, current=False)


def store_reader(path: Path, pool: ReaderPool | None = None):
    """A plain reader at the current schema, as ``open_reader`` opens it."""
    return closing(open_reader(path)) if pool is None else pool.lend(head=False, current=True)


def open_query_reader(path: Path, *, busy_timeout_ms: int = 200) -> sqlite3.Connection:
    """A dedicated read-only main database with private, disk-backed TEMP."""
    # This is a new connection, never a pooled reader whose protection changes.
    conn = open_reader(path, busy_timeout_ms=busy_timeout_ms)
    try:
        conn.execute("PRAGMA query_only=OFF")
        conn.execute("PRAGMA cache_size=-8192")
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA temp.cache_size=-1024")
    except BaseException:
        conn.close()
        raise
    return conn
