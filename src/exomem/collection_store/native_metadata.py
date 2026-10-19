"""Append-only reconstruction metadata for the typed-v2 wire.

Epochs and format IDs reconstruct retained payloads. Rewriting either would
change old values, so these tables reject replacement, update and deletion.
"""

import json
from dataclasses import dataclass

from . import schema

# These names and columns belong to the fixed storage wire, not source vocabulary.
TABLES = ("typed_field_epochs", "typed_value_formats")
#: The most formats one scan or one writer transaction retains, with at most MEMO_BYTES charged for them.
MEMO_ENTRIES, MEMO_BYTES = 128, 64 * 1024


def install(conn, *, canonical: bool = True) -> None:
    """Install in the canonical store, or the replayable derived file itself."""
    # Derived metadata commits with its rows in one file, never across WAL files.
    reference = (", FOREIGN KEY(collection_id,generation) REFERENCES typed_encoding_mappings(collection_id,generation)"
                 if canonical else "")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS typed_field_epochs(
      collection_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
      field_ordinal INTEGER NOT NULL CHECK(field_ordinal>=0), epoch INTEGER NOT NULL,
      PRIMARY KEY(collection_id,generation,field_ordinal){reference}
    ) STRICT, WITHOUT ROWID""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS typed_value_formats(
      collection_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
      field_ordinal INTEGER NOT NULL CHECK(field_ordinal>=0), format_id INTEGER NOT NULL CHECK(format_id>0),
      format_json TEXT NOT NULL CHECK(json_valid(format_json)),
      PRIMARY KEY(collection_id,generation,field_ordinal,format_id),
      UNIQUE(collection_id,generation,field_ordinal,format_json){reference}
    ) STRICT, WITHOUT ROWID""")
    repair_triggers(conn)


def repair_triggers(conn) -> None:
    """Restore the same immutable metadata contract during schema ensure."""
    for table in TABLES:
        for statement in schema._append_only_triggers(table):
            conn.execute(statement)


def _relation(table: str, database: str) -> str:
    """Quote an attached database identifier; table names come only from this module."""
    return '"' + database.replace('"', '""') + '".' + table


def read_epoch(conn, collection_id: str, generation: int, ordinal: int, *, database: str = "main") -> int | None:
    """Read within the caller's snapshot; absence is never cached across snapshots."""
    row = conn.execute(f"SELECT epoch FROM {_relation('typed_field_epochs', database)} "
                       "WHERE collection_id=? AND generation=? AND field_ordinal=?",
                       (collection_id, generation, ordinal)).fetchone()
    return None if row is None else row[0]


def ensure_epoch(conn, collection_id: str, generation: int, ordinal: int, initial: int) -> int:
    """Initialize an origin once in the same transaction as the first payload."""
    if not conn.in_transaction:
        raise RuntimeError("native reconstruction metadata requires a writer transaction")
    epoch = read_epoch(conn, collection_id, generation, ordinal)
    if epoch is not None:
        return epoch
    conn.execute("INSERT INTO typed_field_epochs(collection_id,generation,field_ordinal,epoch) VALUES(?,?,?,?)",
                 (collection_id, generation, ordinal, initial))
    return initial


def intern_format(conn, collection_id: str, generation: int, ordinal: int, value: object) -> int:
    """Intern exact JSON bytes, preserving numeric subtype and signed zero."""
    from .typed_storage import canonical_json

    if not conn.in_transaction:
        raise RuntimeError("native reconstruction metadata requires a writer transaction")
    encoded = canonical_json(value)
    identity = (collection_id, generation, ordinal)
    row = conn.execute("SELECT format_id FROM typed_value_formats WHERE collection_id=? AND generation=? "
                       "AND field_ordinal=? AND format_json=?", (*identity, encoded)).fetchone()
    if row is not None:
        return row[0]
    next_id = conn.execute("SELECT COALESCE(MAX(format_id),0)+1 FROM typed_value_formats "
                           "WHERE collection_id=? AND generation=? AND field_ordinal=?", identity).fetchone()[0]
    conn.execute("INSERT INTO typed_value_formats(collection_id,generation,field_ordinal,format_id,format_json) "
                 "VALUES(?,?,?,?,?)", (*identity, next_id, encoded))
    return next_id


def _format_json(conn, collection_id: str, generation: int, ordinal: int, format_id: int, *,
                 max_bytes: int, database: str) -> str:
    from .typed_storage import TypedStorageError

    if type(max_bytes) is not int or max_bytes < 0:
        raise TypedStorageError("native reconstruction requires a decoding budget")
    row = conn.execute(f"SELECT length(CAST(format_json AS BLOB)), "
                       f"CASE WHEN length(CAST(format_json AS BLOB))<=? THEN format_json END "
                       f"FROM {_relation('typed_value_formats', database)} "
                       "WHERE collection_id=? AND generation=? AND field_ordinal=? AND format_id=?",
                       (max_bytes, collection_id, generation, ordinal, format_id)).fetchone()
    if row is None:
        raise TypedStorageError("native reconstruction format is absent")
    if row[0] > max_bytes:
        raise TypedStorageError("native reconstruction metadata exceeds the decoding budget")
    return row[1]


def _parse_format(raw: str) -> object:
    from .typed_storage import TypedStorageError

    try:
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError) as error:
        raise TypedStorageError("native reconstruction format is invalid") from error


def read_format(conn, collection_id: str, generation: int, ordinal: int, format_id: int, *,
                max_bytes: int, database: str = "main") -> object:
    """Resolve a format ID from its own generation, never from a current layout."""
    return _parse_format(_format_json(conn, collection_id, generation, ordinal, format_id,
                                     max_bytes=max_bytes, database=database))


class _Memo:
    """Successful reads of one scan's snapshot or one writer transaction, never cached longer."""

    __slots__ = ("epochs", "formats", "ids", "keyed", "retained")

    def __init__(self):
        self.epochs, self.formats, self.ids, self.keyed, self.retained = {}, {}, {}, {}, 0

    def admit(self, size: int) -> bool:
        # Reserve for raw text and a closed four-field temporal format, including Python overhead.
        charge = 8 * size + 512
        if len(self.formats) + len(self.ids) + len(self.keyed) >= MEMO_ENTRIES or self.retained + charge > MEMO_BYTES:
            return False
        self.retained += charge
        return True

    def epoch(self, conn, namespace: tuple, database: str) -> int | None:
        found = self.epochs.get(namespace)
        if found is None:
            found = read_epoch(conn, *namespace, database=database)
            if found is not None:
                self.epochs[namespace] = found
        return found

    def entry(self, conn, namespace: tuple, format_id: int, max_bytes: int, database: str) -> list:
        # Imports stay on the cold branches: a scan calls this once per frame.
        if type(max_bytes) is not int or max_bytes < 0:
            from .typed_storage import TypedStorageError

            raise TypedStorageError("native reconstruction requires a decoding budget")
        key = namespace, format_id
        found = self.formats.get(key)
        if found is None:
            raw = _format_json(conn, *namespace, format_id, max_bytes=max_bytes, database=database)
            size = len(raw.encode("utf-8"))
            found = [raw, size, None]
            if self.admit(size):
                self.formats[key] = found
        elif found[1] > max_bytes:
            from .typed_storage import TypedStorageError

            raise TypedStorageError("native reconstruction metadata exceeds the decoding budget")
        return found

    def temporal_format(self, conn, namespace: tuple, format_id: int, max_bytes: int, database: str):
        entry = self.entry(conn, namespace, format_id, max_bytes, database)
        if entry[2] is None:
            from .native_temporal import _compile_format

            entry[2] = _compile_format(_parse_format(entry[0]))
        return entry[2]


def snapshot_fields(conn, collection_id: str, generation: int, ordinals, *, database: str):
    """Memoize successful reads only for one executing scan's immutable namespace."""
    memo = _Memo()

    class ReadMetadata:
        __slots__ = ("namespace",)

        def __init__(self, ordinal):
            self.namespace = collection_id, generation, ordinal

        def epoch(self):
            return memo.epoch(conn, self.namespace, database)

        def format(self, format_id, *, max_bytes):
            return _parse_format(memo.entry(conn, self.namespace, format_id, max_bytes, database)[0])

        def temporal_format(self, format_id, *, max_bytes):
            return memo.temporal_format(conn, self.namespace, format_id, max_bytes, database)

    return {ordinal: ReadMetadata(ordinal) for ordinal in ordinals}


def sqlite_connection(connection):
    """The sqlite3 connection under the writer's accounting wrapper, for blob handles and identity checks."""
    return getattr(connection, "connection", connection)


class WriteScope:
    """Memoize one writer transaction's metadata across its rows; reads and writes stay in that transaction.

    Only a transaction's owner opens one, bound to its sqlite3 connection, and closes it when that transaction
    ends; use after the close, or with another connection's rows, refuses.
    """

    __slots__ = ("_memo", "_canonical", "_sqlite", "_open", "_checked")

    def __init__(self, connection):
        from .typed_storage import canonical_json

        if not connection.in_transaction:
            raise RuntimeError("native metadata scope requires a writer transaction")
        self._memo, self._canonical, self._sqlite = _Memo(), canonical_json, sqlite_connection(connection)
        self._open, self._checked = True, None

    def close(self) -> None:
        self._open = False

    def field(self, connection, collection_id: str, generation: int, ordinal: int) -> "_ScopedField":
        # Triggers keep these rows append-only and collection_store opens no SAVEPOINT, so what this transaction
        # read or wrote stays valid until it ends; its owner closes the scope then, so a later one cannot reuse it.
        if not self._open or not connection.in_transaction:
            raise RuntimeError("native metadata scope outlived its writer transaction")
        if connection is not self._checked:
            if sqlite_connection(connection) is not self._sqlite:
                raise RuntimeError("native metadata scope belongs to another connection's transaction")
            self._checked = connection
        return _ScopedField(self._memo, self._canonical, connection, (collection_id, generation, ordinal))


class _ScopedField:
    """The FieldMetadata contract for one field of a writer transaction, served from its scope's memo."""

    __slots__ = ("memo", "canonical", "connection", "namespace")

    def __init__(self, memo, canonical, connection, namespace):
        self.memo, self.canonical, self.connection, self.namespace = memo, canonical, connection, namespace

    def epoch(self) -> int | None:
        return self.memo.epoch(self.connection, self.namespace, "main")

    def format(self, format_id: int, *, max_bytes: int) -> object:
        return _parse_format(self.memo.entry(self.connection, self.namespace, format_id, max_bytes, "main")[0])

    def temporal_format(self, format_id: int, *, max_bytes: int):
        return self.memo.temporal_format(self.connection, self.namespace, format_id, max_bytes, "main")

    def ensure_epoch(self, initial: int) -> int:
        epoch = ensure_epoch(self.connection, *self.namespace, initial)
        self.memo.epochs[self.namespace] = epoch
        return epoch

    def intern(self, value: object) -> int:
        key = self.namespace, self.canonical(value)
        found = self.memo.ids.get(key)
        if found is None:
            found = intern_format(self.connection, *self.namespace, value)
            if self.memo.admit(len(key[1].encode("utf-8"))):
                self.memo.ids[key] = found
        return found

    def intern_as(self, key, make) -> int:
        """Intern ``make(key)``; remember its ID by ``key``, which must identify that value exactly."""
        memo_key = self.namespace, key
        found = self.memo.keyed.get(memo_key)
        if found is None:
            value = make(key)
            found = intern_format(self.connection, *self.namespace, value)
            if self.memo.admit(len(self.canonical(value).encode("utf-8"))):
                self.memo.keyed[memo_key] = found
        return found


@dataclass(frozen=True, slots=True)
class FieldMetadata:
    """One immutable namespace; reads use the caller's current SQLite snapshot."""

    connection: object
    collection_id: str
    generation: int
    ordinal: int
    database: str = "main"

    def epoch(self) -> int | None:
        return read_epoch(self.connection, self.collection_id, self.generation, self.ordinal, database=self.database)

    def format(self, format_id: int, *, max_bytes: int) -> object:
        return read_format(self.connection, self.collection_id, self.generation, self.ordinal, format_id,
                           max_bytes=max_bytes, database=self.database)

    def temporal_format(self, format_id: int, *, max_bytes: int):
        from .native_temporal import _compile_format

        return _compile_format(self.format(format_id, max_bytes=max_bytes))

    def ensure_epoch(self, initial: int) -> int:
        if self.database != "main":
            raise RuntimeError("native metadata writes use their file's writer connection")
        return ensure_epoch(self.connection, self.collection_id, self.generation, self.ordinal, initial)

    def intern(self, value: object) -> int:
        if self.database != "main":
            raise RuntimeError("native metadata writes use their file's writer connection")
        return intern_format(self.connection, self.collection_id, self.generation, self.ordinal, value)

    def intern_as(self, key, make) -> int:
        return self.intern(make(key))
