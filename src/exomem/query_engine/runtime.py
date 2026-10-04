"""Request-bound collection admission and bounded, read-only SQL sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .. import record_formats
from .. import structured_collections as collections
from ..collection_store import connection, governance
from ..governance import membership
from ..governance.principal import effective_principal

_READERS_GUARD = threading.Lock()
_READERS: dict[Path, int] = {}
_ADMISSION_SEAL = object()
_CURRENT_SESSION: ContextVar[ReadSession | None] = ContextVar("collection_query_session", default=None)
_MAX_DECODE_BYTES = 256 * 1024


class QueryError(RuntimeError):
    """A bounded query refusal without physical store diagnostics."""

    def __init__(self, code: str, message: str = "collection query could not complete") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class QueryLimits:
    timeout_ms: int = 200
    max_row_visits: int = 100_000
    max_temp_bytes: int = 64 * 1024 * 1024
    fetch_size: int = 128

    def __post_init__(self):
        for name, maximum in (("timeout_ms", 200), ("max_row_visits", 100_000),
                              ("max_temp_bytes", 64 * 1024 * 1024), ("fetch_size", 128)):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(f"{name} must be a positive integer at most {maximum}")


class _UncachedRelease(governance.ReleaseCache):
    """Reuse canonical point resolution without retaining per-row bases."""

    retain_points = False

    @staticmethod
    def _remember(cache, key, value, maximum):
        return value

    def scopes(self, subject, candidate):
        return tuple(sorted(membership.evaluate_metadata(subject.basis.subject, candidate)))


@dataclass(frozen=True, slots=True)
class AdmittedCollection:
    session: ReadSession
    collection_id: str
    fields: tuple[str, ...]
    visible_count: int
    input_order: str
    membership_sql: str
    _seal: object

    @property
    def values_sql(self) -> str:
        self.check()
        return f"CASE WHEN {self.membership_sql} THEN exomem_query_values(i.collection_id,i.values_json) END"

    def check(self) -> None:
        if self._seal is not _ADMISSION_SEAL or self.session._admitted.get(self.collection_id) is not self:
            raise QueryError("COLLECTION_NOT_FOUND")
        self.session.check()

    def __reduce__(self):
        raise TypeError("query admission is request-local")


class ReadSession:
    def __init__(self, root, conn, limits, cancelled, project_values, deadline):
        self.connection = conn
        self.limits = limits
        self._root = root
        self._cancelled = cancelled
        self._project_values = project_values
        self._principal = effective_principal()
        self._thread = threading.get_ident()
        self._deadline = deadline
        self._active = True
        self._failure = None
        self._authorization = None
        self._admitted = {}
        self._cursors = set()
        self._estimated_visits = 0

    def __reduce__(self):
        raise TypeError("query sessions are request-local")

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure
        if (not self._active or _CURRENT_SESSION.get() is not self
                or threading.get_ident() != self._thread or effective_principal() != self._principal):
            raise QueryError("QUERY_CANCELLED")
        if self._cancelled is not None and self._cancelled():
            self._failure = QueryError("QUERY_CANCELLED")
        elif time.monotonic() >= self._deadline:
            self._failure = QueryError("QUERY_TIMEOUT")
        if self._failure is not None:
            raise self._failure

    def _progress(self):
        try:
            self.check()
        except QueryError as error:
            self._failure = error
            return 1
        return 0

    def _values(self, collection_id, raw):
        try:
            self.check()
            admitted = self._admitted.get(collection_id)
            if admitted is None:
                raise QueryError("COLLECTION_NOT_FOUND")
            # Bound an individual decode independently of total row count.
            maximum = min(_MAX_DECODE_BYTES, self.limits.max_temp_bytes)
            if len(raw) > maximum or len(raw.encode()) > maximum:
                raise QueryError("QUERY_COST_LIMIT")
            stored = json.loads(raw)
            self.check()
            values = {name: stored[name] for name in admitted.fields if name in stored}
            if self._project_values is not None:
                values = self._project_values(values)
            self.check()
            encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode()) > maximum:
                raise QueryError("QUERY_COST_LIMIT")
            self.check()
            return encoded
        except QueryError as error:
            self._failure = error
            raise
        except (ValueError, TypeError, RecursionError) as error:
            self._failure = QueryError("QUERY_COST_LIMIT")
            raise self._failure from error

    def check_temp(self) -> None:
        self.check()
        pages = self.connection.execute("PRAGMA temp.page_count").fetchone()[0]
        size = self.connection.execute("PRAGMA temp.page_size").fetchone()[0]
        if pages * size > self.limits.max_temp_bytes:
            raise QueryError("QUERY_COST_LIMIT")

    def fetch(self, cursor: sqlite3.Cursor) -> Iterator[list[tuple]]:
        self._cursors.add(cursor)
        try:
            while True:
                self.check()
                # Keep payload batches below the general 128-row ceiling;
                # Python strings and decoded containers also consume memory.
                batch = cursor.fetchmany(min(self.limits.fetch_size, 16))
                self.check_temp()
                if not batch:
                    return
                yield batch
        finally:
            self._cursors.discard(cursor)
            if self._active:
                cursor.close()

    def admit(self, collection_id: str) -> AdmittedCollection:
        self.check()
        if collection_id in self._admitted:
            result = self._admitted[collection_id]
            return result
        operation = self._authorization
        try:
            with closing(governance.iter_subjects(self.connection, collection_id,
                         operation.logical_vault_id, include_held=False, query_order=True,
                         batch_size=min(self.limits.fetch_size, 16))) as subjects:
                subject = next(subjects)
                operation._load_grants()
                self.check()
                if operation.decision(subject).level < 6:
                    raise QueryError("COLLECTION_NOT_FOUND")
                row = self.connection.execute(
                    "SELECT manifest_text FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
                    (collection_id, subject.basis.manifest_version),
                ).fetchone()
                manifest = collections.parse_manifest_bytes(
                    self._root, subject.basis.subject.path, row[0].encode(),
                )
                if manifest.collection_id != collection_id or manifest.manifest_version.hash != subject.basis.manifest_hash:
                    raise QueryError("COLLECTION_NOT_FOUND")
                fields = tuple(manifest.schema.fields)
                grammar = record_formats.log_grammar_tokens(manifest)
                if grammar is not None and grammar.note_field is not None and grammar.note_field not in fields:
                    fields += (grammar.note_field,)
                order = "i.view_path ASC, i.row_id ASC"
                if manifest.storage.strategy == "markdown-log":
                    direction = "DESC" if manifest.storage.descriptor.get("insertion") == "newest-first" else "ASC"
                    order = f"i.created_txn {direction}, i.row_id {direction}"
                # The evaluated manifest has the same default audience as every
                # row; with no row-varying inputs its decision proves uniformity.
                uniform = (operation.policy.empty and not operation.policy.scopes
                           and not operation.policy.rules and not operation.policy.grants
                           and not operation.tombstones and not operation.access["excluded"]
                           and operation.context is None and not operation.failed)
                if uniform:
                    count = self.connection.execute(
                        "SELECT count(*) FROM (SELECT 1 FROM items WHERE collection_id=? LIMIT ?)",
                        (collection_id, self.limits.max_row_visits + 1),
                    ).fetchone()[0]
                    predicate = "1"
                else:
                    self.connection.execute("CREATE TEMP TABLE IF NOT EXISTS query_admitted(row_id INTEGER PRIMARY KEY)")
                    count = 0
                    for subject in subjects:
                        self.check()
                        if operation.decision(subject).level >= 6:
                            count += 1
                            if count + self._estimated_visits > self.limits.max_row_visits:
                                raise QueryError("QUERY_COST_LIMIT")
                            self.connection.execute("INSERT INTO query_admitted VALUES(?)", (subject.row_id,))
                    predicate = "EXISTS (SELECT 1 FROM temp.query_admitted AS admitted WHERE admitted.row_id=i.row_id)"
                self._estimated_visits += count
                if self._estimated_visits > self.limits.max_row_visits:
                    raise QueryError("QUERY_COST_LIMIT")
                self.check_temp()
                result = AdmittedCollection(self, collection_id, fields, count, order, predicate, _ADMISSION_SEAL)
                self._admitted[collection_id] = result
                return result
        except (ValueError, TypeError, StopIteration, collections.CollectionError) as error:
            raise QueryError("COLLECTION_NOT_FOUND") from error


@contextmanager
def read_session(root: Path, store_path: Path, *, limits: QueryLimits | None = None,
                 cancelled: Callable[[], bool] | None = None, project_values=None) -> Iterator[ReadSession]:
    """Own a fresh connection, snapshot, evaluator and admission until return."""
    limits = limits or QueryLimits()
    deadline = time.monotonic() + limits.timeout_ms / 1000
    target = Path(store_path).resolve()
    with _READERS_GUARD:
        if _READERS.get(target, 0) >= 2:
            raise QueryError("QUERY_BUSY")
        _READERS[target] = _READERS.get(target, 0) + 1
    conn = session = token = None
    try:
        conn = connection.open_query_reader(target, busy_timeout_ms=limits.timeout_ms)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        session = ReadSession(Path(root), conn, limits, cancelled, project_values, deadline)
        token = _CURRENT_SESSION.set(session)
        conn.set_progress_handler(session._progress, 1000)
        conn.create_function("exomem_query_values", 2, session._values)
        page_size = conn.execute("PRAGMA temp.page_size").fetchone()[0]
        conn.execute(f"PRAGMA temp.max_page_count={max(1, limits.max_temp_bytes // page_size)}")
        conn.execute("BEGIN")
        session.check()
        session._authorization = governance.OperationAuthorization(
            Path(root), conn, mutation=False, cache=_UncachedRelease(conn),
        )
        session.check()
        yield session
        session.check_temp()
    except sqlite3.Error as error:
        if session is not None:
            session.check()
        code = getattr(error, "sqlite_errorcode", 0) & 255
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise QueryError("QUERY_BUSY") from error
        if code in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_TOOBIG, sqlite3.SQLITE_NOMEM):
            raise QueryError("QUERY_COST_LIMIT") from error
        raise QueryError("QUERY_UNAVAILABLE") from error
    except (connection.CollectionStoreError, connection.CollectionStoreUnavailable) as error:
        raise QueryError("QUERY_UNAVAILABLE") from error
    finally:
        try:
            if conn is not None:
                conn.set_progress_handler(None, 0)
            if session is not None:
                session._active = False
                session._admitted.clear()
                for cursor in session._cursors:
                    cursor.close()
                try:
                    if session._authorization is not None:
                        session._authorization.close()
                finally:
                    conn.close()
            elif conn is not None:
                conn.close()
        finally:
            if token is not None:
                _CURRENT_SESSION.reset(token)
            with _READERS_GUARD:
                _READERS[target] -= 1
                if not _READERS[target]:
                    del _READERS[target]
