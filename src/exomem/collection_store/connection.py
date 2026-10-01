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


def _connect(database: str, *, uri: bool = False) -> sqlite3.Connection:
    return sqlite3.connect(
        database,
        isolation_level=None,
        factory=_CONNECTION_FACTORY,
        uri=uri,
        timeout=BUSY_TIMEOUT_MS / 1000,
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
        self, path: Path, conn: sqlite3.Connection, lease_check: Callable[[], bool]
    ) -> None:
        self.path = path
        self.connection = conn
        self._lease_check = lease_check
        self._owner_thread = threading.get_ident()
        self._closed = False

    def require_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise CollectionStoreError(
                "COLLECTION_STORE_WRITER_THREAD", "the opening thread owns the writer connection"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction: commit on success, else roll back."""
        self.require_owner_thread()
        _require_lease(self._lease_check)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        if not self._closed:
            self.connection.close()
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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(str(target))
        _apply_writer_pragmas(conn)
        _require_lease(check)
        try:
            schema.ensure_schema(conn)
        except schema.SchemaVersionError as error:
            raise CollectionStoreError("COLLECTION_STORE_SCHEMA_NEWER", str(error)) from error
        return WriterConnection(target, conn, check)
    except BaseException:
        if conn is not None:
            conn.close()
        with _WRITERS_GUARD:
            _WRITER_PATHS.remove(target)
        raise


def open_reader(path: Path) -> sqlite3.Connection:
    """Open an existing store read-only at the current schema version."""
    check_sqlite_version()
    target = Path(path)
    if not target.is_file():
        raise CollectionStoreError("COLLECTION_STORE_ABSENT", "the collection store does not exist")
    conn = _connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        found = schema.schema_version(conn)
        if found > schema.SCHEMA_VERSION:
            raise CollectionStoreError(
                "COLLECTION_STORE_SCHEMA_NEWER",
                f"collection store schema {found} is newer than this release supports",
            )
        if found < schema.SCHEMA_VERSION:
            raise CollectionStoreError(
                "COLLECTION_STORE_SCHEMA_PENDING",
                "the collection store has not been migrated by its writer yet",
            )
    except BaseException:
        conn.close()
        raise
    return conn
