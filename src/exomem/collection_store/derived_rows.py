"""Derived import collections: rows kept beside the store and rebuilt from its import log.

OpenSpec bring-in-large-exports design §4. A collection declared ``derived: true`` takes
rows only from imports of export members. Its rows live in ``collections-derived.sqlite``
beside ``collections.sqlite``, in a lean current-state layout: one table per collection,
which is its query projection with the typed columns added, so table data holds each row
and its item key once, and its rollup mirrors. The file keeps no version history, sources,
audit effects or receipts. The store keeps what is canonical: the manifest, saved
mappings, rollup buckets and the append-only import log, which names the preserved
members. Snapshots, backups and the replica never open this file, so they exclude the
rows unless an owner backup asks for them.

SQLite does not commit atomically across attached files in WAL mode, so a member commits
in three transactions on two connections:

1. each batch: rows, rollup mirrors and the in-member progress record, here;
2. the member's end: the import log row, the touched rollup buckets, the job checkpoint and
   one ``import_member`` transition, in the store (``importer``);
3. then: the applied log sequence, and the progress record cleared, here.

``assess`` decides a collection's state from both files. A writer handle reconciles on
first use, so after a restart and after a takeover: a file from another store is
discarded, a finished store commit is finalised, an open member resumes at its recorded
row, a file behind the log replays the tail, and any other state drops the collection's
rows and rebuilds from the first log entry. A rebuild replays the log member by member
through the importer's own apply path, checks each member's counts, digest and row count
against the log, then checks the rollup mirrors against the store's buckets. Row queries
refuse with ``QUERY_REBUILDING`` and the rebuild's progress until it ends; rollups and the
summary page answer from the store throughout.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import structured_collections as collections
from ..reserved_paths import _SQLITE_SUFFIXES  # the SQLite file family, defined once there
from . import connection, index_migrations, rollups, summary, typed_storage
from .query_indexes import ProjectionPlan

FILENAME = "collections-derived.sqlite"
COLLECTION_DERIVED = "COLLECTION_DERIVED"
QUERY_REBUILDING = "QUERY_REBUILDING"
REBUILD_MISMATCH = "DERIVED_REBUILD_MISMATCH"
SOURCE_MISSING = "DERIVED_SOURCE_MISSING"
#: Rows one rebuild step applies in one transaction, so requests are served between steps.
REBUILD_STEP_ROWS = 2000
#: This file's own layout; a file of another one is discarded, and its rows rebuild from the log.
LAYOUT = 2

_DDL = (
    "CREATE TABLE IF NOT EXISTS derived_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
    # contract: what the rows were built under (layout, projection, rollups); a change rebuilds them.
    # cursor_json: a rebuild's position, committed with the rows it applied; stop_json: why it stopped.
    """CREATE TABLE IF NOT EXISTS derived_collections(
      collection_id TEXT PRIMARY KEY,
      contract TEXT NOT NULL,
      applied_seq INTEGER NOT NULL CHECK (applied_seq >= 0),
      state TEXT NOT NULL CHECK (state IN ('ready', 'rebuilding')),
      cursor_json TEXT,
      stop_json TEXT
    ) STRICT""",
    # The open member of the one import job that holds the collection, committed with its rows.
    """CREATE TABLE IF NOT EXISTS derived_progress(
      collection_id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      record_json TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE IF NOT EXISTS derived_rejections(
      collection_id TEXT NOT NULL, ordinal INTEGER NOT NULL, byte_offset INTEGER NOT NULL,
      code TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (collection_id, ordinal)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS derived_touched(
      collection_id TEXT NOT NULL, rollup_id INTEGER NOT NULL, bucket TEXT NOT NULL, groups TEXT NOT NULL,
      PRIMARY KEY (collection_id, rollup_id, bucket, groups)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS derived_flagged(
      rollup_id INTEGER PRIMARY KEY, collection_id TEXT NOT NULL, flagged INTEGER NOT NULL
    ) STRICT""",
    # Rollup mirrors under the store's own table names and rollup ids, so ``rollups`` maintains them.
    """CREATE TABLE IF NOT EXISTS rollup_buckets(
      rollup_id INTEGER NOT NULL, bucket TEXT NOT NULL, groups TEXT NOT NULL, state_json TEXT NOT NULL,
      PRIMARY KEY (rollup_id, bucket, groups)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS rollup_members(
      rollup_id INTEGER NOT NULL, bucket TEXT NOT NULL, groups TEXT NOT NULL, row_id INTEGER NOT NULL,
      PRIMARY KEY (rollup_id, bucket, groups, row_id)
    ) STRICT, WITHOUT ROWID""",
)


def path(store_path: Path) -> Path:
    """The derived file of the store at ``store_path``: beside it, in the same state directory."""
    return Path(store_path).with_name(FILENAME)


def is_derived(conn: sqlite3.Connection, collection_id: str) -> bool:
    row = conn.execute("SELECT derived FROM collections WHERE collection_id=?", (collection_id,)).fetchone()
    return row is not None and row[0] == 1


def logged(conn: sqlite3.Connection, collection_id: str) -> tuple[int, int]:
    """The import log's last sequence and the collection's row count after it; (0, 0) before any member."""
    row = conn.execute("SELECT seq,row_count_after FROM import_members WHERE collection_id=? "
                       "ORDER BY seq DESC LIMIT 1", (collection_id,)).fetchone()
    return (0, 0) if row is None else (row[0], row[1])


def refused(action: str, *, repair: str | None = None) -> collections.CollectionError:
    """The one refusal of a row mutation, a row-history read or a held edit of a derived collection."""
    return collections.CollectionError(
        COLLECTION_DERIVED,
        f"a derived collection keeps its rows outside the store and takes them only from imports, "
        f"so {action} is refused",
        {"action": action, "history": "the collection's import log, one entry per imported member",
         "repair": repair or "import into this collection; to edit rows, create a NEW collection without derived"},
    )


def conversion_refused(current: bool) -> collections.CollectionError:
    """A revise that would change ``derived``: the existing create-a-NEW-collection guidance."""
    return collections.CollectionError(
        summary.MODE_CHANGE_UNSUPPORTED,
        f"a collection is derived only at creation; this one stays derived: {str(current).lower()}",
        {"derived": current, "requested_derived": not current,
         "migration": (f"Create a NEW collection with derived: {str(not current).lower()} and import or copy "
                       "the rows into it. This collection, its import log and its sources stay as they are.")},
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# What a collection's rows are built under


@dataclass(frozen=True, slots=True)
class Target:
    """A derived collection's typed layout, query projection and rollups, all from the store."""

    collection_id: str
    layout: typed_storage.Layout
    projection: ProjectionPlan
    projection_hash: str
    rollups: tuple[tuple[int, rollups.Rollup], ...]

    @property
    def table(self) -> str:
        """The rows table, which is the query projection's own table: the engine reads it by that name."""
        return self.projection.table_name

    @property
    def contract(self) -> str:
        """The identity of what the rows were built under; another one means a rebuild."""
        return _json({"file": LAYOUT, "layout": [self.layout.generation, self.layout.encoded],
                      "projection": [self.projection.generation, self.projection_hash],
                      "rollups": [[rollup_id, rollup.encoded()] for rollup_id, rollup in self.rollups]})

    def reads(self, schema: str) -> typed_storage.Layout:
        """The layout a reader joins: the one rows table stands for ``items`` and the current table."""
        table = f"{schema}.{self.table}"
        return replace(self.layout, items=table, current=table)


def target(conn: sqlite3.Connection, collection_id: str) -> Target | None:
    """The store's current contract for a derived collection; None while its projection is not ready."""
    found = conn.execute("SELECT plan_hash FROM query_projection_mappings WHERE collection_id=? AND state='ready'",
                         (collection_id,)).fetchone()
    plan = index_migrations.ready_plan(conn, collection_id)
    if found is None or plan is None:
        return None
    defined = tuple((rollup_id, rollup) for rollup_id, rollup, _, _ in rollups.definitions(conn, collection_id))
    return Target(collection_id, typed_storage.require_layout(conn, collection_id), plan, found[0], defined)


def _rows_ddl(goal: Target) -> tuple[str, ...]:
    """The projection as its plan declares it, then the typed columns and the ``items`` columns readers use.

    Adding columns keeps the projection's own columns and indexes exactly as the query
    engine compiles them. A tag column takes no NOT NULL, which an added column needs a
    default for; ``typed_storage.decode_value`` refuses a NULL tag.
    """
    added = [column for n in range(len(goal.layout.fields)) for column in (f"t{n} INTEGER", f"v{n} ANY", f"k{n} ANY")]
    # The store's items columns a reader filters or reads, computed so they cost no bytes per row.
    added += ["r TEXT", f"collection_id TEXT GENERATED ALWAYS AS ('{goal.collection_id}') VIRTUAL",
              f"encoding TEXT GENERATED ALWAYS AS ('{typed_storage.TYPED_V1}') VIRTUAL",
              "view_path TEXT GENERATED ALWAYS AS (NULL) VIRTUAL", "created_txn INTEGER GENERATED ALWAYS AS (row_id) VIRTUAL",
              "values_json TEXT GENERATED ALWAYS AS (NULL) VIRTUAL"]
    return (*goal.projection.create_ddl(), *(f"ALTER TABLE {goal.table} ADD COLUMN {column}" for column in added))


# State


@dataclass(frozen=True, slots=True)
class State:
    """What ``assess`` found: an action and the log positions it compared."""

    action: str  # empty, ready, live, finalize, replay, rebuild, rebuilding
    applied: int
    logged: int
    stop: dict | None = None

    @property
    def serves(self) -> bool:
        """Whether the file holds the rows the log names, so a row query can read them."""
        return self.action in ("empty", "ready", "live", "finalize")

    def progress(self) -> dict[str, Any]:
        return {"state": "stopped" if self.stop else "rebuilding", "members_applied": self.applied,
                "members_logged": self.logged, "stop": self.stop}


# A paused job keeps its open member, as a running one does, so its continuation resumes it.
_HOLDING = "state='running' OR (state='partial' AND reason IN ('authority_lost','time_cap'))"


def _holds(main: sqlite3.Connection, record: dict[str, Any]) -> bool:
    """Whether the progress record's job still holds its member: alive, and not checkpointed past it."""
    row = main.execute(f"SELECT checkpoint_json FROM import_jobs WHERE job_id=? AND ({_HOLDING})",
                       (record["job"],)).fetchone()
    return row is not None and json.loads(row[0]).get("member", 0) <= record["member"]


def _logs(main: sqlite3.Connection, collection_id: str, seq: int, record: dict[str, Any]) -> bool:
    """Whether the store's log entry ``seq`` is the member the progress record finished."""
    row = main.execute("SELECT job_id,member_sha256,rows_digest,accepted,rejected FROM import_members "
                       "WHERE collection_id=? AND seq=?", (collection_id, seq)).fetchone()
    tally = record["tally"]
    return row == (record["job"], record["sha256"], tally["rows_digest"], tally["accepted"], tally["rejected"])


def assess(main: sqlite3.Connection, derived: sqlite3.Connection | None, schema: str, collection_id: str,
           contract: str | None) -> State:
    """A derived collection's state from the store's log and this file's bookkeeping under ``schema``."""
    seq = logged(main, collection_id)[0]
    row = None if derived is None else derived.execute(
        f"SELECT contract,applied_seq,state,stop_json FROM {schema}.derived_collections WHERE collection_id=?",
        (collection_id,)).fetchone()
    if row is None:
        return State("empty" if seq == 0 else "rebuild", 0, seq)
    stored, applied, state, stop = row
    if contract is None or stored != contract or applied > seq:
        return State("rebuild", 0, seq)
    if state == "rebuilding":
        return State("rebuilding", applied, seq, None if stop is None else json.loads(stop))
    held = derived.execute(f"SELECT record_json FROM {schema}.derived_progress WHERE collection_id=?",
                           (collection_id,)).fetchone()
    if held is not None:
        record = json.loads(held[0])
        if record["done"] and seq == applied + 1 and _logs(main, collection_id, seq, record):
            return State("finalize", applied, seq)
        if seq == applied and _holds(main, record):
            return State("live", applied, seq)
        return State("rebuild", applied, seq)
    return State("ready" if seq == applied else "replay", applied, seq)


# The writer's connection


class Store:
    """One writer handle's connection to the derived file; ``open_store`` opens and reconciles it."""

    def __init__(self, store_path: Path) -> None:
        self.path = path(store_path)
        # Opened on the writer handle's thread and closed with the handle (``WriterConnection.close``).
        self.thread, self.closed = threading.get_ident(), False
        self.conn = self._open()
        self.readers: dict[str, Any] = {}  # a rebuild's open member reader per collection

    def _open(self) -> sqlite3.Connection:
        for attempt in (0, 1):
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                # Every state this file can lose by a power cut is one reconcile repairs.
                conn.execute("PRAGMA synchronous=NORMAL")
                if conn.execute("PRAGMA user_version").fetchone()[0] != LAYOUT and conn.execute(
                        "SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                    raise sqlite3.DatabaseError("the derived file has another layout")
                for statement in _DDL:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={LAYOUT}")
                return conn
            except sqlite3.DatabaseError:
                conn.close()
                if attempt:
                    raise
                # An unreadable file, or one of another layout, is only a projection: discard it and
                # rebuild from the log.
                for suffix in _SQLITE_SUFFIXES:
                    Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        raise AssertionError("unreachable")

    def close(self) -> None:
        for reader in self.readers.values():
            reader.close()
        self.readers.clear()
        self.conn.close()
        self.closed = True

    @contextmanager
    def transaction(self):
        # This host's projection needs its writer handle's thread, not the vault's writer lease: a
        # step counts only once the store's log agrees with it, so a stale write is replayed over.
        if self.closed:
            raise connection.CollectionStoreError("COLLECTION_STORE_WRITER_CLOSED", "the writer connection is closed")
        if threading.get_ident() != self.thread:
            raise connection.CollectionStoreError("COLLECTION_STORE_WRITER_THREAD",
                                                  "the opening thread owns the writer connection")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def state(self, main: sqlite3.Connection, collection_id: str) -> tuple[State, Target | None]:
        goal = target(main, collection_id)
        return assess(main, self.conn, "main", collection_id, None if goal is None else goal.contract), goal

    def progress(self, collection_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT record_json FROM derived_progress WHERE collection_id=?",
                                (collection_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def _drop(self, collection_id: str) -> None:
        """Remove a collection's rows, projection, mirrors and bookkeeping; the caller holds the transaction."""
        hexed = uuid.UUID(collection_id).hex
        for (name,) in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '!'",
                (f"cq!_{hexed}!_%",)).fetchall():
            self.conn.execute(f"DROP TABLE {name}")  # noqa: S608 - names from the collection UUID
        for (rollup_id,) in self.conn.execute("SELECT rollup_id FROM derived_flagged WHERE collection_id=?",
                                              (collection_id,)).fetchall():
            self.conn.execute("DELETE FROM rollup_buckets WHERE rollup_id=?", (rollup_id,))
            self.conn.execute("DELETE FROM rollup_members WHERE rollup_id=?", (rollup_id,))
        for table in ("derived_flagged", "derived_touched", "derived_rejections", "derived_progress",
                      "derived_collections"):
            self.conn.execute(f"DELETE FROM {table} WHERE collection_id=?", (collection_id,))  # noqa: S608
        reader = self.readers.pop(collection_id, None)
        if reader is not None:
            reader.close()

    def _install(self, goal: Target, *, state: str, applied: int, cursor: dict | None) -> None:
        """Create the empty rows table and mirrors for ``goal``; the caller holds the transaction."""
        self._drop(goal.collection_id)
        for statement in _rows_ddl(goal):
            self.conn.execute(statement)
        self.conn.executemany("INSERT INTO derived_flagged(rollup_id,collection_id,flagged) VALUES (?,?,0)",
                              [(rollup_id, goal.collection_id) for rollup_id, _ in goal.rollups])
        self.conn.execute("INSERT INTO derived_collections(collection_id,contract,applied_seq,state,cursor_json) "
                          "VALUES (?,?,?,?,?)", (goal.collection_id, goal.contract, applied, state,
                                                 None if cursor is None else _json(cursor)))

    def reconcile(self, main: sqlite3.Connection) -> None:
        """Bring every derived collection to a state an import or a rebuild continues from."""
        store_id = main.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
        found = self.conn.execute("SELECT value FROM derived_meta WHERE key='store_id'").fetchone()
        derived = {cid for (cid,) in main.execute("SELECT collection_id FROM collections WHERE derived=1")}
        with self.transaction():
            if found is None or found[0] != store_id:
                # A file from another store holds nothing this store logged.
                for (cid,) in self.conn.execute("SELECT collection_id FROM derived_collections").fetchall():
                    self._drop(cid)
                self.conn.execute("INSERT OR REPLACE INTO derived_meta(key,value) VALUES ('store_id',?)", (store_id,))
            for (cid,) in self.conn.execute("SELECT collection_id FROM derived_collections").fetchall():
                if cid not in derived:
                    self._drop(cid)
        for cid in sorted(derived):
            self.settle(main, cid)

    def settle(self, main: sqlite3.Connection, collection_id: str) -> State:
        """Act on one collection's assessed state: create, finalise, replay the tail or rebuild."""
        state, goal = self.state(main, collection_id)
        if goal is None or state.action in ("ready", "live", "rebuilding"):
            return state
        with self.transaction():
            if state.action == "empty":
                self._install(goal, state="ready", applied=0, cursor=None)
            elif state.action == "finalize":
                self._finish(collection_id, state.logged)
            elif state.action == "replay":
                self.conn.execute("UPDATE derived_collections SET state='rebuilding',cursor_json=?,stop_json=NULL "
                                  "WHERE collection_id=?", (_json(_cursor(main, collection_id, state.applied + 1)),
                                                            collection_id))
                self._release(collection_id)
            else:
                self._install(goal, state="rebuilding", applied=0, cursor=_cursor(main, collection_id, 1))
        return self.state(main, collection_id)[0]

    def _release(self, collection_id: str) -> None:
        for table in ("derived_touched", "derived_rejections", "derived_progress"):
            self.conn.execute(f"DELETE FROM {table} WHERE collection_id=?", (collection_id,))  # noqa: S608

    def _finish(self, collection_id: str, seq: int) -> None:
        self.conn.execute("UPDATE derived_collections SET applied_seq=? WHERE collection_id=?", (seq, collection_id))
        self._release(collection_id)

    def finalize(self, main: sqlite3.Connection, collection_id: str) -> None:
        """Step 3: the store logged the member, so record its sequence and clear the progress record."""
        with self.transaction():
            self._finish(collection_id, logged(main, collection_id)[0])

    # Step 1 and step 2's reads

    def stage(self, collection_id: str, job_id: str, work) -> dict[str, Any]:
        """Step 1: one batch's rows, its rejections and the member's progress record, in one transaction.

        ``work(conn)`` applies the rows and returns the new progress record and the rejections.
        """
        with self.transaction():
            record, rejections = work(self.conn)
            self.conn.executemany(
                "INSERT OR REPLACE INTO derived_rejections(collection_id,ordinal,byte_offset,code,at) "
                "VALUES (?,?,?,?,?)", [(collection_id, *rejection) for rejection in rejections])
            self.conn.execute("INSERT OR REPLACE INTO derived_progress(collection_id,job_id,record_json) "
                              "VALUES (?,?,?)", (collection_id, job_id, _json(record)))
        return record

    def rejections(self, collection_id: str) -> list[tuple[int, int, str, str]]:
        return self.conn.execute("SELECT ordinal,byte_offset,code,at FROM derived_rejections "
                                 "WHERE collection_id=? ORDER BY ordinal", (collection_id,)).fetchall()

    def publish_rollups(self, execute, goal: Target, *, whole: tuple[int, ...] = ()) -> None:
        """Copy the mirror's touched buckets, or the ``whole`` rollups, into the store's, with flagged counts.

        ``execute`` is the writer's accounted statement runner, inside its transaction.
        """
        cid = goal.collection_id
        for rollup_id in whole:
            execute("DELETE FROM rollup_buckets WHERE rollup_id=?", (rollup_id,))
            execute("INSERT INTO rollup_buckets(rollup_id,bucket,groups,state_json) VALUES (?,?,?,?)",
                    self.conn.execute("SELECT rollup_id,bucket,groups,state_json FROM rollup_buckets "
                                      "WHERE rollup_id=?", (rollup_id,)).fetchall(), many=True)
        touched = () if whole else self.conn.execute(
            "SELECT rollup_id,bucket,groups FROM derived_touched WHERE collection_id=?", (cid,)).fetchall()
        for rollup_id, bucket, groups in touched:
            state = self.conn.execute("SELECT state_json FROM rollup_buckets WHERE rollup_id=? AND bucket=? "
                                      "AND groups=?", (rollup_id, bucket, groups)).fetchone()
            if state is None:
                execute("DELETE FROM rollup_buckets WHERE rollup_id=? AND bucket=? AND groups=?",
                        (rollup_id, bucket, groups))
            else:
                execute("INSERT INTO rollup_buckets(rollup_id,bucket,groups,state_json) VALUES (?,?,?,?) "
                        "ON CONFLICT(rollup_id,bucket,groups) DO UPDATE SET state_json=excluded.state_json",
                        (rollup_id, bucket, groups, state[0]))
        flagged = self.conn.execute("SELECT flagged,rollup_id FROM derived_flagged WHERE collection_id=?",
                                    (cid,)).fetchall()
        execute("UPDATE rollup_definitions SET flagged=? WHERE rollup_id=?",
                [row for row in flagged if not whole or row[1] in whole], many=True)

    # Rebuild

    def rebuilding(self) -> list[str]:
        return [cid for (cid,) in self.conn.execute(
            "SELECT collection_id FROM derived_collections WHERE state='rebuilding' ORDER BY collection_id")]

    def _stop(self, collection_id: str, stop: dict[str, Any]) -> str:
        reader = self.readers.pop(collection_id, None)
        if reader is not None:
            reader.close()
        with self.transaction():
            self.conn.execute("UPDATE derived_collections SET stop_json=? WHERE collection_id=?",
                              (_json(stop), collection_id))
        return "stopped"


def _cursor(main: sqlite3.Connection, collection_id: str, seq: int) -> dict[str, Any]:
    """A rebuild's start at log entry ``seq``, with the row count the log records before it."""
    before = main.execute("SELECT row_count_after FROM import_members WHERE collection_id=? AND seq=?",
                          (collection_id, seq - 1)).fetchone()
    return {"seq": seq, "member_row": 0, "fold": None, "tally": None, "rows": 0 if before is None else before[0]}


def open_store(writer) -> Store:
    """This writer handle's derived file, opened and reconciled on the handle's first use."""
    handle = writer.handle
    if handle.derived is None:
        store = Store(handle.path)
        try:
            store.reconcile(writer.connection)
        except BaseException:
            store.close()
            raise
        handle.derived = store
    return handle.derived


# The apply function: one batch of rows into the derived layout


def _decoder(goal: Target):
    width = 3 * len(goal.layout.fields) + 1

    def decode(columns) -> dict[str, Any]:
        return typed_storage.decode_row(goal.layout, columns[:width])

    return decode


def _members(goal: Target):
    """Read a mirror bucket's indexed members from the collection's own rows."""
    columns = ",".join(f"d.{column}" for column in (*goal.layout.value_columns, "r"))
    decode = _decoder(goal)

    def members(conn, rollup_id, bucket, groups):
        for row_id, key, *values in conn.execute(
                f"SELECT d.row_id,d.item_key,{columns} FROM rollup_members m JOIN {goal.table} d "  # noqa: S608
                "ON d.row_id=m.row_id WHERE m.rollup_id=? AND m.bucket=? AND m.groups=?",
                (rollup_id, bucket, groups)).fetchall():
            yield row_id, key, decode(values)

    return members


def apply(conn: sqlite3.Connection, goal: Target, items) -> list[dict[str, Any]]:
    """Upsert ``items`` (``(index, item_key, values)``, in source order) into the collection's rows.

    A row's typed columns and its projection keys are one row of the collection's table.
    Every rollup mirror moves with the batch, each touched bucket read and written once,
    and each touched bucket is recorded for the member's store commit. Returns one outcome
    per item: ``inserted``, ``updated`` or ``unchanged``, with its index and item key. The
    caller holds the transaction.
    """
    items = list(items)
    if not items:
        return []
    layout, table = goal.layout, goal.table
    columns = (*layout.value_columns, "r")
    found: dict[str, tuple[int, int, tuple]] = {}
    keys = list(dict.fromkeys(key for _, key, _ in items))
    for offset in range(0, len(keys), 500):
        chunk = keys[offset:offset + 500]
        for key, row_id, version, *encoded in conn.execute(
                f"SELECT item_key,row_id,row_version,{','.join(columns)} FROM {table} "  # noqa: S608
                f"WHERE item_key IN ({','.join('?' for _ in chunk)})", chunk):
            found[key] = (row_id, version, tuple(encoded))
    insert = (f"INSERT INTO {table}(item_key,row_version,keys_json,{','.join(columns)}) "  # noqa: S608
              f"VALUES (?,?,?,{','.join('?' for _ in columns)})")
    update = (f"UPDATE {table} SET row_version=?,keys_json=?,"  # noqa: S608
              f"{','.join(c + '=?' for c in columns)} WHERE row_id=?")
    decode, upkeep = _decoder(goal), rollups.Upkeep(conn, members=_members(goal))
    flagged: dict[int, int] = {}
    touched: set[tuple[int, str, str]] = set()
    outcomes = []
    for index, key, values in items:
        encoded = typed_storage.encode_row(layout, values)
        before = found.get(key)
        if before is not None and before[2] == encoded:
            outcomes.append({"index": index, "item_key": key, "outcome": "unchanged"})
            continue
        projected = goal.projection.encode(values)
        if before is None:
            row_id, version, previous = conn.execute(insert, (key, 1, projected, *encoded)).lastrowid, 1, None
        else:
            row_id, version, previous = before[0], before[1] + 1, decode(before[2])
            conn.execute(update, (version, projected, *encoded, row_id))
        found[key] = (row_id, version, encoded)
        for rollup_id, rollup in goal.rollups:
            change, places = upkeep.move(rollup_id, rollup, row_id, key, values, previous)
            if change:
                flagged[rollup_id] = flagged.get(rollup_id, 0) + change
            touched.update((rollup_id, *place) for place in places)
        outcomes.append({"index": index, "item_key": key, "outcome": "inserted" if before is None else "updated"})
    upkeep.flush()
    conn.executemany("UPDATE derived_flagged SET flagged=flagged+? WHERE rollup_id=?",
                     [(change, rollup_id) for rollup_id, change in flagged.items()])
    conn.executemany("INSERT OR IGNORE INTO derived_touched(collection_id,rollup_id,bucket,groups) VALUES (?,?,?,?)",
                     [(goal.collection_id, *place) for place in touched])
    return outcomes


# Query readers


@dataclass(frozen=True, slots=True)
class RowSource:
    """Where a query reads one collection's rows: the relation standing for ``items``, the schema of its
    projection table, and the typed layout whose reads join them (None: the store's own)."""

    items: str
    schema: str
    layout: typed_storage.Layout | None = None


STORE = RowSource("main.items", "main")


class Rebuilding(Exception):
    """A derived collection's row basis lags the store's log, so a row query must refuse."""

    def __init__(self, state: State) -> None:
        super().__init__(QUERY_REBUILDING)
        self.progress = state.progress()


def attach(conn: sqlite3.Connection, store_path: Path) -> bool:
    """Attach the derived file read-only as ``derived`` when it exists; only query readers call this."""
    found = path(store_path)
    if not found.is_file():
        return False
    try:
        conn.execute("ATTACH DATABASE ? AS derived", (f"{found.resolve().as_uri()}?mode=ro",))
        conn.execute("SELECT 1 FROM derived.derived_collections LIMIT 1")
    except sqlite3.DatabaseError:
        # An unreadable projection reads as absent; the writer's reconcile replaces it.
        if conn.execute("SELECT 1 FROM pragma_database_list WHERE name='derived'").fetchone() is not None:
            conn.execute("DETACH DATABASE derived")
        return False
    return True


def row_source(conn: sqlite3.Connection, collection_id: str, *, attached: bool) -> RowSource:
    """A query's row source for one collection; raises ``Rebuilding`` when a derived basis lags the log."""
    if not is_derived(conn, collection_id):
        return STORE
    goal = target(conn, collection_id)
    state = assess(conn, conn if attached else None, "derived", collection_id,
                   None if goal is None else goal.contract)
    if state.action == "empty" and goal is not None:
        # Nothing logged: the store's own empty relations are the whole, current basis.
        return STORE
    if not state.serves or goal is None:
        raise Rebuilding(state)
    return RowSource(f"derived.{goal.table}", "derived", goal.reads("derived"))


def rebuilds_pending(store_path: Path) -> bool:
    """Whether the derived file records a rebuild still running, from a plain read; absent means none."""
    found = path(store_path)
    if not found.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"{found.resolve().as_uri()}?mode=ro", uri=True)) as reader:
            return reader.execute("SELECT 1 FROM derived_collections WHERE state='rebuilding' LIMIT 1").fetchone() \
                is not None
    except sqlite3.DatabaseError:
        return True  # an unreadable file is the writer's to replace


def rebuild_status(conn: sqlite3.Connection, store_path: Path, collection_id: str) -> dict[str, Any]:
    """A derived collection's rows state for inspection: ready, or the rebuild's progress."""
    goal = target(conn, collection_id)
    found = path(store_path)
    reader = None
    try:
        if found.is_file():
            reader = sqlite3.connect(f"{found.resolve().as_uri()}?mode=ro", uri=True)
        state = assess(conn, reader, "main", collection_id, None if goal is None else goal.contract)
    except sqlite3.DatabaseError:
        state = assess(conn, None, "main", collection_id, None if goal is None else goal.contract)
    finally:
        if reader is not None:
            reader.close()
    seq, rows = logged(conn, collection_id)
    result = {"rows": rows, "members_logged": seq, "rows_source": "import log"}
    return {**result, "state": "ready"} if state.serves else {**result, **state.progress()}


def snapshot(store_path: Path, destination: Path) -> dict[str, Any] | None:
    """Copy the derived file to ``destination`` through SQLite's backup; None when there is none."""
    import hashlib

    found = path(store_path)
    if not found.is_file():
        return None
    staged = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    staged.unlink(missing_ok=True)
    with closing(sqlite3.connect(f"{found.resolve().as_uri()}?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(staged)) as copy:
        source.backup(copy)
        intact = copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    if not intact:
        staged.unlink(missing_ok=True)
        raise sqlite3.DatabaseError("the derived snapshot failed its integrity check")
    os.replace(staged, destination)
    with open(destination, "rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    return {"path": str(destination), "sha256": digest, "size_bytes": destination.stat().st_size}


# Rebuild


class _Missing(Exception):
    """A source the log names is absent or no longer the bytes it bound; the argument says why."""


def step(root: Path, writer) -> str:
    """Advance one rebuilding derived collection by one bounded step on the store writer's thread.

    Returns ``advanced``, ``done`` when a rebuild finished, ``stopped`` when every rebuild
    waits on a missing source or a mismatch, or ``idle``.
    """
    store = open_store(writer)
    for cid in sorted({*store.rebuilding(), *(
            found for (found,) in writer.connection.execute("SELECT collection_id FROM collections WHERE derived=1")
            if store.state(writer.connection, found)[0].action in ("rebuild", "replay", "finalize", "empty"))}):
        store.settle(writer.connection, cid)
    stopped = False
    for cid in store.rebuilding():
        outcome = _rebuild(Path(root), writer, store, cid)
        if outcome != "stopped":
            return outcome
        stopped = True
    return "stopped" if stopped else "idle"


def _bound_members(root: Path, job, manifest_sha256: str) -> tuple:
    """The job's bound members, read only through the trusted manifest reader under the logged digest.

    A replay has no principal, so it trusts exactly what a live import trusts: a manifest
    kept under raw protection whose bytes hash to its binding, which must name the log's
    ``manifest_sha256``.
    """
    from . import importer

    bound = job.binding["source"]
    try:
        members = importer._export(root, bound["ref"], manifest_sha256, bound["members"]["select"])
    except collections.CollectionError as error:
        raise _Missing(_manifest_refusal(root, bound["ref"], manifest_sha256)) from error
    if [member.sha256 for member in members] != bound["members"]["sha256"]:
        raise _Missing("its selector picks other members than the import bound")
    return members


def _manifest_refusal(root: Path, ref: str, manifest_sha256: str) -> str:
    """Why the trusted reader refused the logged manifest, asked of the same helpers it uses."""
    from .. import archive_members
    from ..governance import raw_protection

    found = raw_protection.binding(root, ref)
    if found is None:
        return "it lost its raw-protection binding"
    if found[0]["artifact_sha256"] != manifest_sha256 or archive_members.read_manifest(root, ref) is None:
        return "it no longer hashes to the logged manifest_sha256"
    return "its selector no longer picks the members the import bound"


def _rebuild(root: Path, writer, store: Store, collection_id: str) -> str:
    """Replay one bounded step of a collection's import log into its rows, through the importer's apply path."""
    from .. import __version__
    from . import import_time, importer

    main = writer.connection
    cursor_json, stop_json = store.conn.execute(
        "SELECT cursor_json,stop_json FROM derived_collections WHERE collection_id=?", (collection_id,)).fetchone()
    cursor, stop = json.loads(cursor_json), None if stop_json is None else json.loads(stop_json)
    goal = target(main, collection_id)
    if goal is None or (stop is not None and stop["code"] == REBUILD_MISMATCH):
        return "stopped"
    if cursor["seq"] > logged(main, collection_id)[0]:
        return _finish(root, writer, store, goal)
    job_id, index, sha256, manifest_sha256, accepted, rejected, digest, rows_after, version, zone = main.execute(
        "SELECT job_id,member_index,member_sha256,manifest_sha256,accepted,rejected,rows_digest,row_count_after,"
        "importer_version,zone_rules FROM import_members WHERE collection_id=? AND seq=?",
        (collection_id, cursor["seq"])).fetchone()
    named = {"seq": cursor["seq"], "index": index, "sha256": sha256}
    job = importer._load(main, job_id)
    manifest = writer._collection_manifest(writer._collection_row(collection_id))[0]
    binding = job.binding
    plan = importer.compile_mapping(binding["mapping"]["declared"], manifest, binding["mapping"]["format"])
    position = {"row": 0, "byte": 0, "member": index, "member_row": cursor["member_row"]}
    reader = store.readers.get(collection_id)
    if reader is None or reader.marked != (cursor["seq"], cursor["member_row"]):
        if reader is not None:
            reader.close()
        try:
            members = _bound_members(root, job, manifest_sha256)
        except _Missing as missing:
            return store._stop(collection_id, {"code": SOURCE_MISSING, "member": named, "missing": "export manifest",
                                               "reason": missing.args[0]})
        streams = tuple(importer._Stream(member.sha256, member.bytes,
                                         lambda member=member: importer._open_member(root, member))
                        for member in members)
        reader = store.readers[collection_id] = importer._Streams(
            streams, binding["mapping"]["format"], plan.rows, position, None, bounded=True)
    order = import_time.Fold(cursor["fold"]) if plan.ordered else None
    tally, rows = dict(cursor["tally"] or importer._tally()), cursor["rows"]
    shas, taken, ended, stopped = binding["source"]["members"]["sha256"], 0, False, False
    try:
        with store.transaction():
            while taken < REBUILD_STEP_ROWS and not ended:
                batch = importer._next_batch(reader, plan, reader.pending, order)
                if batch.stop is not None:
                    stopped = True  # a logged member never stopped its import
                    break
                applied = importer._derived_apply(store.conn, goal, batch, manifest, binding["source"]["ref"], shas)
                for _, key, payload in applied.entries:
                    importer._extend(tally, key, payload)
                rows += sum(outcome["outcome"] == "inserted" for outcome in applied.outcomes)
                taken += len(batch.rows) + len(batch.rejections)
                ended = batch.eof or batch.checkpoint["member"] != index
                reached = batch.checkpoint["member_row"]
            if ended and not stopped and (tally["accepted"], tally["rejected"], tally["rows_digest"], rows) == (
                    accepted, rejected, digest, rows_after):
                cursor = {"seq": cursor["seq"] + 1, "member_row": 0, "fold": None, "tally": None, "rows": rows}
                store.conn.execute("UPDATE derived_collections SET applied_seq=?,cursor_json=?,stop_json=NULL "
                                   "WHERE collection_id=?", (named["seq"], _json(cursor), collection_id))
            elif not ended and not stopped:
                cursor = {**cursor, "member_row": reached, "fold": None if order is None else order.state(),
                          "tally": tally, "rows": rows}
                store.conn.execute("UPDATE derived_collections SET cursor_json=?,stop_json=NULL WHERE collection_id=?",
                                   (_json(cursor), collection_id))
            else:
                raise _Mismatch
    except importer._Lost:
        return store._stop(collection_id, {"code": SOURCE_MISSING, "member": named, "missing": "member"})
    except _Mismatch:
        return store._stop(collection_id, {
            "code": REBUILD_MISMATCH, "member": named,
            "logged": {"accepted": accepted, "rejected": rejected, "rows_digest": digest, "rows": rows_after},
            "rebuilt": {"accepted": tally["accepted"], "rejected": tally["rejected"],
                        "rows_digest": tally["rows_digest"], "rows": rows},
            "importer": {"logged": version, "running": __version__},
            "zone_rules": {"logged": zone, "running": import_time.version() if zone else None}})
    if ended:
        store.readers.pop(collection_id).close()
    else:
        reader.marked = (cursor["seq"], cursor["member_row"])
    return "advanced"


class _Mismatch(Exception):
    """A replayed member's counts, digest or row count differ from its log entry."""


def _finish(root: Path, writer, store: Store, goal: Target) -> str:
    """End a rebuild: check the rollup mirrors against the store's buckets, and publish rollups still building."""
    from .preview import _mutate

    main, cid = writer.connection, goal.collection_id
    building = []
    for rollup_id, rollup in goal.rollups:
        state, flagged = main.execute("SELECT state,flagged FROM rollup_definitions WHERE rollup_id=?",
                                      (rollup_id,)).fetchone()
        if state != "ready":
            building.append(rollup_id)
            continue
        query = "SELECT bucket,groups,state_json FROM rollup_buckets WHERE rollup_id=? ORDER BY bucket,groups"
        mirror = store.conn.execute(query, (rollup_id,)).fetchall()
        mirrored = store.conn.execute("SELECT flagged FROM derived_flagged WHERE rollup_id=?", (rollup_id,)).fetchone()
        if main.execute(query, (rollup_id,)).fetchall() != mirror or (mirrored or (0,))[0] != flagged:
            return store._stop(cid, {"code": REBUILD_MISMATCH, "member": None, "rollup": rollup.name})
    if building:

        def publish():
            with writer._mutation():
                store.publish_rollups(writer._execute, goal, whole=tuple(building))
                writer._execute("UPDATE rollup_definitions SET state='ready' WHERE rollup_id=?",
                                [(rollup_id,) for rollup_id in building], many=True)

        _mutate(root, publish)
    with store.transaction():
        store.conn.execute("DELETE FROM derived_touched WHERE collection_id=?", (cid,))
        store.conn.execute("UPDATE derived_collections SET state='ready',cursor_json=NULL,stop_json=NULL "
                           "WHERE collection_id=?", (cid,))
    return "done"
