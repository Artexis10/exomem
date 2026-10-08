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

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from . import schema

STORE_FILENAME = "collections.sqlite"
MINIMUM_SQLITE_VERSION = (3, 38, 0)
BUSY_TIMEOUT_MS = 5000

#: Connection class used for every store connection (a test seam for engines
#: whose journal mode does not take effect).
_CONNECTION_FACTORY: type[sqlite3.Connection] = sqlite3.Connection

# Process-local connection ownership; the existing vault lease remains the
# cross-process authority. Reserve before opening, release on failure or close.
_WRITERS_GUARD = threading.Lock()
_WRITER_PATHS: set[Path] = set()


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
    return sqlite3.connect(
        database,
        isolation_level=None,
        factory=_CONNECTION_FACTORY,
        uri=uri,
        timeout=busy_timeout_ms / 1000,
        check_same_thread=False,
    )


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
    engine = create_engine("sqlite+pysqlite://", creator=lambda: _connect(database), poolclass=NullPool)

    @event.listens_for(engine, "begin")
    def begin(conn):
        # Explicit SQLite transaction control also makes DDL atomic.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


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
        # Host-local import job facts (``importer``), keyed by job id: this handle's
        # proofs of bound source bytes, and the store refusal blocking a running job.
        # A new handle, as after a takeover, starts with neither.
        self.import_proofs: dict[str, Any] = {}
        self.import_blocked: dict[str, str] = {}

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
                "COLLECTION_STORE_WRITER_THREAD", "the opening thread owns the writer connection"
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
        self.require_write_authority(allow_diverged=resolve_divergence)
        cache = self.release_cache
        cache.check()
        try:
            self.core.begin()
        except DBAPIError as error:
            raise error.orig from error
        cache.begin()
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

    def execute(self, statement, parameters=None):
        """Execute Core statements only within this handle's mutation scope."""
        self.require_owner_thread()
        if not self.core.in_transaction() or not self.connection.in_transaction:
            raise RuntimeError("Core writes require the writer transaction")
        try:
            return self.core.execute(statement, parameters)
        except DBAPIError as error:
            raise error.orig from error

    def close(self) -> None:
        if not self._closed:
            self._release_cache = None
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
) -> WriterConnection:
    """Open the vault's single writer, under its mutation boundary.

    ``vault_root`` binds the default authority and store placement to the same
    vault. Opening (including schema repair/migration) and each transaction
    require that boundary. The opening thread owns this connection until close.
    ``lease_check`` is a trusted adapter/test seam: its caller must supply the
    exact store's scoped authority, never the process-wide scheduler predicate.
    """
    check_sqlite_version()
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
            schema.ensure_schema(conn)
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
    if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be a non-negative integer")
    check_sqlite_version()
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
        if found < schema.SCHEMA_VERSION:
            raise CollectionStoreError(
                "COLLECTION_STORE_SCHEMA_PENDING",
                "the collection store has not been migrated by its writer yet",
            )
    except BaseException:
        conn.close()
        raise
    return conn


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
