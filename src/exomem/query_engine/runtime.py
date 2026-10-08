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
from ..collection_store import connection, governance, typed_storage
from ..governance import membership
from ..governance.principal import effective_principal
from .errors import QueryError

_READERS_GUARD = threading.Lock()
_READERS: dict[Path, int] = {}
#: Analytics sessions per store. Each holds one of the store's two readers for up to two
#: seconds, so at most one runs at a time and an interactive reader always remains.
_ANALYTICS: dict[Path, int] = {}
MAX_ANALYTICS_SESSIONS = 1
_ADMISSION_SEAL = object()
_CURRENT_SESSION: ContextVar[ReadSession | None] = ContextVar("collection_query_session", default=None)
_MAX_DECODE_BYTES = 256 * 1024
#: The whole serialized v1 result, rows or groups (design §8).
MAX_RESULT_BYTES = 64 * 1024


def wire_bytes(value) -> int:
    """Bytes of ``value`` as REST and the CLI's JSON output serialize a result.

    Both use ``json.dumps(..., ensure_ascii=False)`` with spaced separators. MCP's
    compact UTF-8 JSON (FastMCP's pydantic serializer) is never larger, and no
    result transport escapes non-ASCII text. The CLI's human output re-indents a
    result with ``indent=2`` for reading; that display, like the envelope around a
    result, is outside the cap.
    """
    return len(json.dumps(value, ensure_ascii=False).encode())


_MIB = 1024 * 1024
#: Each execution profile's bounds, which are also the widest a caller may set (design §12).
#: Analytics is explicit and synchronous: a whole-call deadline, no partial
#: aggregate and no continuation.
PROFILES = {
    "interactive": {"timeout_ms": 200, "max_row_visits": 100_000, "max_temp_bytes": 64 * _MIB,
                    "max_groups": 1000, "max_state_bytes": 8 * _MIB},
    "analytics": {"timeout_ms": 2000, "max_row_visits": 10_000_000, "max_temp_bytes": 256 * _MIB,
                  "max_groups": 1000, "max_state_bytes": 8 * _MIB},
}


@dataclass(frozen=True, slots=True)
class QueryLimits:
    """One session's bounds: its profile's, or tighter ones a caller sets; never wider."""

    timeout_ms: int | None = None
    max_row_visits: int | None = None
    max_temp_bytes: int | None = None
    fetch_size: int = 128
    profile: str = "interactive"
    max_groups: int | None = None
    max_state_bytes: int | None = None

    def __post_init__(self):
        maxima = PROFILES.get(self.profile) if type(self.profile) is str else None
        if maxima is None:
            raise ValueError(f"profile must be one of {sorted(PROFILES)}")
        for name, maximum in (*maxima.items(), ("fetch_size", 128)):
            value = getattr(self, name)
            if value is None:
                object.__setattr__(self, name, maximum)
            elif type(value) is not int or not 0 < value <= maximum:
                raise ValueError(f"{name} must be a positive integer at most {maximum}")

    def bounds(self) -> dict[str, int]:
        """The bounds this session enforces, as reported with a reduction."""
        return {name: getattr(self, name) for name in PROFILES[self.profile]}


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
    layout: typed_storage.Layout | None
    _seal: object

    @property
    def values_sql(self) -> str:
        self.check()
        # Typed rows decode by row_id, so SQL function arity never grows with field count.
        raw = "i.values_json" if self.layout is None else "exomem_typed_values(i.collection_id,i.row_id)"
        return f"CASE WHEN {self.membership_sql} THEN exomem_query_values(i.collection_id,{raw}) END"

    def check(self) -> None:
        if self._seal is not _ADMISSION_SEAL or self.session._admitted.get(self.collection_id) is not self:
            raise QueryError("COLLECTION_NOT_FOUND")
        self.session.check()

    def __reduce__(self):
        raise TypeError("query admission is request-local")


class ReadSession:
    def __init__(self, root, conn, limits, cancelled, deadline):
        self.connection = conn
        self.limits = limits
        self._root = root
        self._cancelled = cancelled
        self._project_values = None
        self._principal = effective_principal()
        self._thread = threading.get_ident()
        self._deadline = deadline
        self._active = True
        self._failure = None
        self._authorization = None
        self._admitted = {}
        self._field_plans = {}
        self._queries = {}
        self._cursors = set()
        self._estimated_visits = 0
        # Fields the projection can change; `project_with` is the only way to install one.
        self._projected_fields = frozenset()

    def __reduce__(self):
        raise TypeError("query sessions are request-local")

    def project_with(self, project_values, *, fields) -> None:
        """Install the projection of ``fields`` that this session's own manifest calls for, before any query runs.

        ``project_values`` sees only the values of ``fields``, and only those come back from it; every
        other value passes as stored. A rollup, which never projects, and a base scan therefore agree
        on every field outside ``fields``, which is what the reduction planner relies on.
        """
        if self._project_values is not None or self._admitted or self._queries or self._estimated_visits:
            raise QueryError("QUERY_UNAVAILABLE", "a session's value projection is fixed before its first query")
        fields = frozenset(fields)

        def project(values):
            chosen = {name: value for name, value in values.items() if name in fields}
            if not chosen:
                return values
            projected = project_values(chosen)
            return {name: projected[name] if name in fields else value for name, value in values.items()
                    if name not in fields or name in projected}

        self._project_values = project
        self._projected_fields = fields

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
            from io import BytesIO

            from .selected_values import read_selected_tree

            plan = self._field_plans[collection_id]
            if plan.owner:
                if len(raw) > maximum or len(raw.encode()) > maximum:
                    raise QueryError("QUERY_COST_LIMIT")
                stored = json.loads(raw)
            else:
                stored = read_selected_tree(BytesIO(raw.encode()), plan.fields,
                                            max_bytes=maximum, check=self.check)
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

    def _typed_values(self, collection_id, row_id):
        """Decode one admitted typed row to its canonical JSON text for the value UDF."""
        try:
            self.check()
            admitted = self._admitted.get(collection_id)
            if admitted is None or admitted.layout is None:
                raise QueryError("COLLECTION_NOT_FOUND")
            encoded = typed_storage.canonical_json(self.selected_values(
                row_id, admitted.layout, admitted.fields,
                max_bytes=min(_MAX_DECODE_BYTES, self.limits.max_temp_bytes), check=self.check))
            if len(encoded.encode()) > min(_MAX_DECODE_BYTES, self.limits.max_temp_bytes):
                raise QueryError("QUERY_COST_LIMIT")
            self.check()
            return encoded
        except QueryError as error:
            self._failure = error
            raise
        except (typed_storage.TypedStorageError, ValueError, TypeError, RecursionError) as error:
            self._failure = QueryError("QUERY_UNAVAILABLE")
            raise self._failure from error

    def selected_values(self, row_id, layout, fields, *, max_bytes, check):
        """Selected top-level values of one admitted row under either encoding."""
        from .selected_values import read_selected_tree

        cid = layout.collection_id if layout is not None else self.connection.execute(
            "SELECT collection_id FROM items WHERE row_id=?", (row_id,)).fetchone()[0]
        plan = self._field_plans[cid]
        fields = frozenset(fields)
        selection = {name: tree for name, tree in plan.fields.items() if name in fields}
        if set(fields) - set(selection):
            raise QueryError("QUERY_FIELD_UNAVAILABLE")

        if layout is None:
            with self.connection.blobopen("items", "values_json", row_id, readonly=True) as blob:
                return read_selected_tree(blob, selection, max_bytes=max_bytes, check=check)
        check()
        try:
            values = typed_storage.selected_current_values(self.connection, layout, row_id, selection,
                                                          max_bytes=max_bytes, check=check)
        except typed_storage.TypedStorageError as error:
            raise QueryError("QUERY_UNAVAILABLE") from error
        fields = frozenset(fields)
        selected = {name: value for name, value in values.items() if name in fields}
        if len(typed_storage.canonical_json(selected).encode()) > max_bytes:
            raise QueryError("QUERY_RESULT_TOO_LARGE")
        check()
        return selected

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
            with self._manifest(collection_id) as (manifest, _, subjects, uniform):
                fields = tuple(manifest.schema.fields)
                grammar = record_formats.log_grammar_tokens(manifest)
                if grammar is not None and grammar.note_field is not None and grammar.note_field not in fields:
                    fields += (grammar.note_field,)
                order = "i.view_path ASC, i.row_id ASC"
                if manifest.storage.strategy == "markdown-log":
                    direction = "DESC" if manifest.storage.descriptor.get("insertion") == "newest-first" else "ASC"
                    order = f"i.created_txn {direction}, i.row_id {direction}"
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
                layout = self.typed_layout(collection_id)
                result = AdmittedCollection(self, collection_id, fields, count, order, predicate, layout,
                                            _ADMISSION_SEAL)
                self._admitted[collection_id] = result
                return result
        except (ValueError, TypeError, StopIteration, collections.CollectionError) as error:
            raise QueryError("COLLECTION_NOT_FOUND") from error

    def typed_layout(self, collection_id) -> typed_storage.Layout | None:
        """The published typed layout of an admitted collection, or None for json-v1."""
        try:
            if typed_storage.collection_encoding(self.connection, collection_id) == typed_storage.JSON_V1:
                return None
            return typed_storage.require_layout(self.connection, collection_id)
        except typed_storage.TypedStorageError as error:
            raise QueryError("QUERY_UNAVAILABLE") from error

    @contextmanager
    def _manifest(self, collection_id):
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
                manifest = collections.parse_manifest_bytes(self._root, subject.basis.subject.path, row[0].encode())
                if manifest.collection_id != collection_id or manifest.manifest_version.hash != subject.basis.manifest_hash:
                    raise QueryError("COLLECTION_NOT_FOUND")
                plan = operation.field_plan(manifest)
                self._field_plans[collection_id] = plan
                manifest = plan.manifest
                # An admitted manifest proves uniformity only without any
                # row-varying policy, grant, exclusion or session context, or
                # for a summary collection by its one representative decision.
                uniform = operation.uniform_release() or _summary_released(operation, collection_id)
                yield manifest, subject.basis, subjects, uniform
        except (ValueError, TypeError, StopIteration, collections.CollectionError) as error:
            raise QueryError("COLLECTION_NOT_FOUND") from error

    def admit_query(self, query, *, as_of: str):
        from .typed_rows import admit_query

        return admit_query(self, query, as_of=as_of)

    def reduce(self, query, *, as_of: str | None = None) -> dict:
        """Run one page of a grouped reduction under this session's profile, or refuse."""
        from .reductions import reduce

        return reduce(self, query, as_of=as_of)


def _summary_released(operation, collection_id: str) -> bool:
    """Whether every row of a summary collection is released, decided without row subjects.

    Past the summary release bound the typed limit is the answer, not a longer
    admission stream over the same row decisions.
    """
    try:
        release = operation.summary_release(collection_id)
    except collections.CollectionError as error:
        if error.code == governance.RELEASE_LIMIT:
            raise QueryError(error.code, "summary rows vary by row-level policy past the bound") from error
        raise
    return release is not None and release.complete


@contextmanager
def read_session(root: Path, store_path: Path, *, limits: QueryLimits | None = None,
                 cancelled: Callable[[], bool] | None = None) -> Iterator[ReadSession]:
    """Own a fresh connection, snapshot, evaluator and admission until return."""
    limits = limits or QueryLimits()
    deadline = time.monotonic() + limits.timeout_ms / 1000
    target = Path(store_path).resolve()
    analytics = limits.profile == "analytics"
    with _READERS_GUARD:
        if _READERS.get(target, 0) >= 2 or analytics and _ANALYTICS.get(target, 0) >= MAX_ANALYTICS_SESSIONS:
            raise QueryError("QUERY_BUSY", "the store's query readers are in use; retry shortly")
        _READERS[target] = _READERS.get(target, 0) + 1
        if analytics:
            _ANALYTICS[target] = _ANALYTICS.get(target, 0) + 1
    conn = session = token = None
    try:
        conn = connection.open_query_reader(target, busy_timeout_ms=limits.timeout_ms)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        session = ReadSession(Path(root), conn, limits, cancelled, deadline)
        token = _CURRENT_SESSION.set(session)
        conn.set_progress_handler(session._progress, 1000)
        conn.create_function("exomem_query_values", 2, session._values)
        conn.create_function("exomem_typed_values", 2, session._typed_values)
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
                session._queries.clear()
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
                if analytics:
                    _ANALYTICS[target] -= 1
                    if not _ANALYTICS[target]:
                        del _ANALYTICS[target]
