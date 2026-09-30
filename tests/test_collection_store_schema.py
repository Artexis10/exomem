"""Collection store schema contract (move-structured-collections-to-sqlite P1a.1-P1a.2b).

The store is dark: nothing routes to it yet. These pins hold the storage
contract the later writer, importer and projector slices build on:

- every table is STRICT;
- the audit, version, provenance, manifest-history and type-history tables are
  append-only by trigger, and item rows are never deleted;
- natural keys are a partial unique index and view paths are unique;
- readiness refuses an old SQLite or a state root where WAL does not take effect;
- the store-wide chain (``commit_seq`` / ``store_head_hash``, A3) is unique,
  contiguous and verifiable, and forks are decided by head, never by ``txn_id``.

All data is invented.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem.collection_store import chain, connection, schema, tokens

COLLECTION_ID = "00000000-0000-4000-8000-000000000001"
OTHER_COLLECTION_ID = "00000000-0000-4000-8000-000000000002"
H = "a" * 64

EXPECTED_TABLES = {
    "store_meta",
    "collection_types",
    "collection_type_versions",
    "collection_manifests",
    "collections",
    "items",
    "item_versions",
    "item_sources",
    "txns",
    "audit_effects",
    "held_candidates",
    "projection_state",
}
APPEND_ONLY = (
    "txns",
    "audit_effects",
    "item_versions",
    "item_sources",
    "collection_manifests",
    "collection_type_versions",
)


def _allow() -> bool:
    return True


@pytest.fixture
def store(tmp_path: Path) -> Iterator[connection.WriterConnection]:
    writer = connection.open_writer(tmp_path / "state" / "collections.sqlite", lease_check=_allow)
    try:
        yield writer
    finally:
        writer.close()


def _txn(
    conn: sqlite3.Connection,
    *,
    txn_id: int,
    commit_seq: int,
    event_hash: str,
    prev_head: str | None,
    collection_id: str = COLLECTION_ID,
    request_id: str | None = None,
) -> str:
    head = tokens.store_head_hash(prev_head, commit_seq, event_hash)
    conn.execute(
        "INSERT INTO txns(txn_id, transition_id, collection_id, operation, generation_before,"
        " generation_after, actor, why, request_id, receipt_json, committed_at, event_hash,"
        " commit_seq, store_head_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            txn_id,
            f"{txn_id:024x}",
            collection_id,
            "append",
            txn_id - 1,
            txn_id,
            "owner",
            "invented",
            request_id,
            "{}",
            "2026-09-30T00:00:00Z",
            event_hash,
            commit_seq,
            head,
        ),
    )
    return head


def _seed_collection(conn: sqlite3.Connection, collection_id: str = COLLECTION_ID) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO collection_types(name, current_version, builtin) VALUES (?,?,?)",
        ("records", 1, 1),
    )
    conn.execute(
        "INSERT OR IGNORE INTO collection_type_versions(name, version, declaration_json,"
        " declaration_hash, change_class, txn_id) VALUES (?,?,?,?,?,?)",
        ("records", 1, "{}", H, "builtin", 1),
    )
    conn.execute(
        "INSERT INTO collection_manifests(collection_id, manifest_version, manifest_text,"
        " manifest_hash, schema_json, txn_id) VALUES (?,?,?,?,?,?)",
        (collection_id, 1, "---\ntitle: Invented\n---\n", H, "{}", 1),
    )
    conn.execute(
        "INSERT INTO collections(collection_id, type_name, type_version, manifest_path,"
        " source_path, layout, manifest_version, generation, audit_reader_version,"
        " created_txn, updated_txn) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            collection_id,
            "records",
            1,
            f"Knowledge Base/Records/{collection_id}/_collection.md",
            f"Knowledge Base/Records/{collection_id}/items",
            "markdown-items",
            1,
            1,
            2,
            1,
            1,
        ),
    )


def _item(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    natural_key: str | None,
    view_path: str,
    collection_id: str = COLLECTION_ID,
) -> int:
    cursor = conn.execute(
        "INSERT INTO items(collection_id, item_key, natural_key, row_version, schema_version,"
        " values_json, payload_hash, view_path, created_txn, updated_txn)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (collection_id, item_key, natural_key, 1, 1, "{}", H, view_path, 1, 1),
    )
    return int(cursor.lastrowid)


def _populated(writer: connection.WriterConnection) -> dict[str, int]:
    """One committed row in every append-only table, created in one transaction."""
    with writer.transaction() as conn:
        _seed_collection(conn)
        row_id = _item(conn, item_key="k1", natural_key='[1,[["title","a"]]]', view_path="v/a.md")
        conn.execute(
            "INSERT INTO item_versions(row_id, row_version, values_json, body, payload_hash,"
            " txn_id) VALUES (?,?,?,?,?,?)",
            (row_id, 1, "{}", "", H, 1),
        )
        conn.execute(
            "INSERT INTO item_sources(row_id, row_version, ordinal, source_ref) VALUES (?,?,?,?)",
            (row_id, 1, 0, "Knowledge Base/Sources/invented.md"),
        )
        conn.execute(
            "INSERT INTO audit_effects(txn_id, ordinal, row_id, item_key, effect, version_after,"
            " hash_after) VALUES (?,?,?,?,?,?,?)",
            (1, 0, row_id, "k1", "insert", 1, H),
        )
        _txn(conn, txn_id=1, commit_seq=1, event_hash="1" * 64, prev_head=None)
    return {"row_id": row_id}


# --- P1a.1: STRICT tables ---------------------------------------------------


def test_every_store_table_is_strict(store: connection.WriterConnection) -> None:
    rows = store.connection.execute(
        "SELECT name, strict FROM pragma_table_list WHERE schema='main'"
        " AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {name: strict for name, strict in rows}
    assert set(tables) == EXPECTED_TABLES
    assert {name for name, strict in tables.items() if not strict} == set()


def test_strict_tables_refuse_a_mistyped_value(store: connection.WriterConnection) -> None:
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
        _seed_collection(conn)
        conn.execute(
            "INSERT INTO items(collection_id, item_key, row_version, schema_version, values_json,"
            " payload_hash, view_path, created_txn, updated_txn) VALUES (?,?,?,?,?,?,?,?,?)",
            (COLLECTION_ID, "k", "not-a-version", 1, "{}", H, "v.md", 1, 1),
        )


# --- P1a.1: append-only history ---------------------------------------------


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_append_only_tables_abort_update_and_delete(
    store: connection.WriterConnection, table: str
) -> None:
    _populated(store)
    conn = store.connection
    before = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed names
    assert before, f"{table} has no committed row to protect"
    column = conn.execute(f"SELECT name FROM pragma_table_info('{table}') LIMIT 1").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"), store.transaction() as tx:
        tx.execute(f"UPDATE {table} SET {column} = {column}")  # noqa: S608
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), store.transaction() as tx:
        tx.execute(f"DELETE FROM {table}")  # noqa: S608

    assert conn.execute(f"SELECT * FROM {table}").fetchall() == before  # noqa: S608


def test_item_rows_are_never_deleted(store: connection.WriterConnection) -> None:
    _populated(store)
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"), store.transaction() as tx:
        tx.execute("DELETE FROM items")
    assert store.connection.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_item_rows_remain_updatable(store: connection.WriterConnection) -> None:
    ids = _populated(store)
    with store.transaction() as tx:
        tx.execute("UPDATE items SET row_version = 2 WHERE row_id = ?", (ids["row_id"],))
    assert store.connection.execute("SELECT row_version FROM items").fetchone()[0] == 2


# --- P1a.1: keys ---------------------------------------------------------------


def test_natural_key_is_a_partial_unique_index(store: connection.WriterConnection) -> None:
    key = '[1,[["title","Invented"]]]'
    with store.transaction() as tx:
        _seed_collection(tx)
        _seed_collection(tx, OTHER_COLLECTION_ID)
        _item(tx, item_key="a", natural_key=key, view_path="v/a.md")
        # Same serialization in another collection is a different identity.
        _item(
            tx,
            item_key="a",
            natural_key=key,
            view_path="w/a.md",
            collection_id=OTHER_COLLECTION_ID,
        )
        # Incomplete natural keys are NULL and never collide.
        _item(tx, item_key="b", natural_key=None, view_path="v/b.md")
        _item(tx, item_key="c", natural_key=None, view_path="v/c.md")

    with pytest.raises(sqlite3.IntegrityError, match="natural_key"), store.transaction() as tx:
        _item(tx, item_key="d", natural_key=key, view_path="v/d.md")

    index = store.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='items_natural_key'"
    ).fetchone()
    assert index is not None
    assert "UNIQUE" in index[0].upper() and "WHERE NATURAL_KEY IS NOT NULL" in index[0].upper()


def test_view_path_is_unique_across_collections(store: connection.WriterConnection) -> None:
    with store.transaction() as tx:
        _seed_collection(tx)
        _seed_collection(tx, OTHER_COLLECTION_ID)
        _item(tx, item_key="a", natural_key=None, view_path="shared/view.md")
    with pytest.raises(sqlite3.IntegrityError, match="view_path"), store.transaction() as tx:
        _item(
            tx,
            item_key="b",
            natural_key=None,
            view_path="shared/view.md",
            collection_id=OTHER_COLLECTION_ID,
        )


def test_item_key_is_unique_within_a_collection(store: connection.WriterConnection) -> None:
    with store.transaction() as tx:
        _seed_collection(tx)
        _item(tx, item_key="a", natural_key=None, view_path="v/a.md")
    with pytest.raises(sqlite3.IntegrityError, match="item_key"), store.transaction() as tx:
        _item(tx, item_key="a", natural_key=None, view_path="v/other.md")


# --- P1a.1: readiness ------------------------------------------------------------


def test_readiness_refuses_sqlite_older_than_3_38(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
    target = tmp_path / "state" / "collections.sqlite"

    with pytest.raises(connection.CollectionStoreUnavailable) as refused:
        connection.open_writer(target, lease_check=_allow)

    assert refused.value.code == "COLLECTION_STORE_UNAVAILABLE"
    assert refused.value.reason == "sqlite_version"
    assert "3.38" in refused.value.remediation
    assert not target.exists(), "a refused engine must not create the store"


def test_readiness_accepts_the_minimum_version(monkeypatch: pytest.MonkeyPatch) -> None:
    connection.check_sqlite_version((3, 38, 0))
    with pytest.raises(connection.CollectionStoreUnavailable):
        connection.check_sqlite_version((3, 37, 99))


class _NoWalConnection(sqlite3.Connection):
    """A connection on a filesystem where WAL journaling does not take effect."""

    def execute(self, sql, *args):  # noqa: ANN001, ANN201
        normalized = " ".join(str(sql).upper().split())
        if normalized.startswith("PRAGMA JOURNAL_MODE") and "WAL" in normalized:
            return super().execute("SELECT 'delete'")
        return super().execute(sql, *args)


def test_readiness_refuses_a_state_root_where_wal_does_not_take_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "_CONNECTION_FACTORY", _NoWalConnection)

    with pytest.raises(connection.CollectionStoreUnavailable) as refused:
        connection.open_writer(tmp_path / "state" / "collections.sqlite", lease_check=_allow)

    assert refused.value.code == "COLLECTION_STORE_UNAVAILABLE"
    assert refused.value.reason == "wal_unavailable"
    assert refused.value.remediation


# --- P1a.2: connection model and forward migrations ---------------------------------


def test_writer_applies_the_store_pragmas(store: connection.WriterConnection) -> None:
    conn = store.connection
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_a_new_store_is_schema_version_1_with_identity(store: connection.WriterConnection) -> None:
    meta = dict(store.connection.execute("SELECT key, value FROM store_meta").fetchall())
    assert meta["schema_version"] == "1"
    assert schema.SCHEMA_VERSION == 1
    assert meta["store_id"] != meta["instance_id"]
    for key in ("store_id", "instance_id"):
        assert len(meta[key]) == 36 and meta[key].count("-") == 4
    assert meta["commit_seq"] == "0"
    assert json.loads(meta["forks"]) == []
    lineage = json.loads(meta["lineage"])
    assert lineage == [
        {
            "instance_id": meta["instance_id"],
            "adopted_from": None,
            "adopted_at_commit_seq": 0,
            "head_hash": None,
        }
    ]
    assert meta["created_at"].endswith("Z") and len(meta["created_at"]) == 20
    # Values the design leaves unset until they happen stay absent, never faked.
    for absent in ("store_head_hash", "last_published_replica_sha256", "migrated_from"):
        assert absent not in meta


def test_reopening_a_store_keeps_its_identity(tmp_path: Path) -> None:
    target = tmp_path / "state" / "collections.sqlite"
    first = connection.open_writer(target, lease_check=_allow)
    identity = dict(first.connection.execute("SELECT key, value FROM store_meta").fetchall())
    first.close()
    second = connection.open_writer(target, lease_check=_allow)
    try:
        again = dict(second.connection.execute("SELECT key, value FROM store_meta").fetchall())
    finally:
        second.close()
    assert again == identity


def test_a_store_newer_than_this_release_refuses(tmp_path: Path) -> None:
    target = tmp_path / "state" / "collections.sqlite"
    writer = connection.open_writer(target, lease_check=_allow)
    with writer.transaction() as tx:
        tx.execute(
            "UPDATE store_meta SET value = ? WHERE key = 'schema_version'",
            (str(schema.SCHEMA_VERSION + 1),),
        )
    writer.close()

    with pytest.raises(connection.CollectionStoreError) as refused:
        connection.open_writer(target, lease_check=_allow)
    assert refused.value.code == "COLLECTION_STORE_SCHEMA_NEWER"


def test_forward_migrations_run_in_order_from_the_recorded_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state" / "collections.sqlite"
    connection.open_writer(target, lease_check=_allow).close()

    applied: list[int] = []

    def migrate_to_2(conn: sqlite3.Connection) -> None:
        applied.append(2)
        conn.execute("CREATE TABLE migration_probe(x INTEGER) STRICT")

    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(schema.MIGRATIONS, 2, migrate_to_2)
    writer = connection.open_writer(target, lease_check=_allow)
    try:
        version = writer.connection.execute(
            "SELECT value FROM store_meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        writer.close()
    assert applied == [2], "only the missing step runs; version 1 is not re-applied"
    assert version == "2"


def test_writes_require_the_writer_lease(tmp_path: Path) -> None:
    held = {"lease": False}
    writer = connection.open_writer(
        tmp_path / "state" / "collections.sqlite", lease_check=lambda: held["lease"]
    )
    try:
        with pytest.raises(connection.CollectionStoreError) as refused, writer.transaction():
            pass
        assert refused.value.code == "COLLECTION_STORE_LEASE_REQUIRED"
        held["lease"] = True
        with writer.transaction() as tx:
            _seed_collection(tx)
    finally:
        writer.close()


def test_a_failed_transaction_rolls_back_every_statement(
    store: connection.WriterConnection,
) -> None:
    with pytest.raises(RuntimeError, match="invented failure"), store.transaction() as tx:
        _seed_collection(tx)
        raise RuntimeError("invented failure")
    assert store.connection.execute("SELECT count(*) FROM collections").fetchone()[0] == 0
    assert not store.connection.in_transaction


def test_readers_see_committed_rows_and_cannot_write(store: connection.WriterConnection) -> None:
    _populated(store)
    reader = connection.open_reader(store.path)
    try:
        assert reader.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO store_meta(key, value) VALUES ('probe', 'x')")
    finally:
        reader.close()


def test_a_reader_never_creates_a_store(tmp_path: Path) -> None:
    target = tmp_path / "state" / "collections.sqlite"
    target.parent.mkdir(parents=True)
    with pytest.raises(connection.CollectionStoreError) as refused:
        connection.open_reader(target)
    assert refused.value.code == "COLLECTION_STORE_ABSENT"
    assert not target.exists()


# --- P1a.2b: amendment columns ---------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, int]]:
    return {
        name: (declared.upper(), notnull)
        for _cid, name, declared, notnull, _default, _pk in conn.execute(
            f"PRAGMA table_info('{table}')"
        )
    }


def test_amendment_columns_exist_with_their_types(store: connection.WriterConnection) -> None:
    conn = store.connection
    txns = _columns(conn, "txns")
    assert txns["commit_seq"] == ("INTEGER", 1)
    assert txns["store_head_hash"] == ("TEXT", 1)
    projection = _columns(conn, "projection_state")
    for name in ("published_sha256", "pending_sha256", "stat_identity"):
        assert projection[name][0] == "TEXT"
    for name in ("published_row_version", "pending_row_version"):
        assert projection[name][0] == "INTEGER"
    held = _columns(conn, "held_candidates")
    assert held["code"][0] == "TEXT"
    assert held["held_bytes"][0] == "BLOB"
    assert _columns(conn, "collections")["verified_through_txn"][0] == "INTEGER"


def test_projection_state_kind_and_state_are_closed(store: connection.WriterConnection) -> None:
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO projection_state(path, kind, state, pending_sha256, pending_row_version)"
            " VALUES ('Knowledge Base/_Schema/collection-types/invented.md', 'type', 'pending',"
            " ?, 1)",
            (H,),
        )
    for column, value in (("kind", "note"), ("state", "stale")):
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as tx:
            values = {"kind": "item", "state": "current", column: value}
            tx.execute(
                "INSERT INTO projection_state(path, kind, state) VALUES (?, ?, ?)",
                (f"x/{column}.md", values["kind"], values["state"]),
            )


def test_held_bytes_round_trip_exactly(store: connection.WriterConnection) -> None:
    raw = b"---\ntitle: invented\n---\n\xef\xbb\xbfnot utf-8 safe \xff\n"
    with store.transaction() as tx:
        _seed_collection(tx)
        tx.execute(
            "INSERT INTO held_candidates(held_id, collection_id, kind, code, held_bytes)"
            " VALUES ('h1', ?, 'view-correction', 'VIEW_CONFLICT', ?)",
            (COLLECTION_ID, raw),
        )
    stored = store.connection.execute(
        "SELECT code, held_bytes FROM held_candidates WHERE held_id='h1'"
    ).fetchone()
    assert stored == ("VIEW_CONFLICT", raw)


def test_commit_seq_and_store_head_are_unique(store: connection.WriterConnection) -> None:
    with store.transaction() as tx:
        head = _txn(tx, txn_id=1, commit_seq=1, event_hash="1" * 64, prev_head=None)
    # A second row reusing the sequence number is refused.
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as tx:
        tx.execute(
            "INSERT INTO txns(txn_id, transition_id, collection_id, operation,"
            " generation_before, generation_after, actor, why, receipt_json, committed_at,"
            " event_hash, commit_seq, store_head_hash)"
            " VALUES (2, ?, ?, 'append', 1, 2, 'owner', 'x', '{}', 'now', ?, 1, ?)",
            ("2" * 24, COLLECTION_ID, "2" * 64, "b" * 64),
        )
    # So is a row reusing the head.
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as tx:
        tx.execute(
            "INSERT INTO txns(txn_id, transition_id, collection_id, operation,"
            " generation_before, generation_after, actor, why, receipt_json, committed_at,"
            " event_hash, commit_seq, store_head_hash)"
            " VALUES (2, ?, ?, 'append', 1, 2, 'owner', 'x', '{}', 'now', ?, 2, ?)",
            ("2" * 24, COLLECTION_ID, "2" * 64, head),
        )


def test_commit_seq_is_contiguous_and_advances_the_store_head(
    store: connection.WriterConnection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="contiguous"), store.transaction() as tx:
        _txn(tx, txn_id=1, commit_seq=2, event_hash="1" * 64, prev_head=None)
    with store.transaction() as tx:
        first = _txn(tx, txn_id=1, commit_seq=1, event_hash="1" * 64, prev_head=None)
        second = _txn(tx, txn_id=2, commit_seq=2, event_hash="2" * 64, prev_head=first)
    meta = dict(store.connection.execute("SELECT key, value FROM store_meta").fetchall())
    assert meta["commit_seq"] == "2"
    assert meta["store_head_hash"] == second
    assert chain.verify_store_chain(store.connection) == (2, second)


def test_a_broken_chain_is_detected(store: connection.WriterConnection) -> None:
    with store.transaction() as tx:
        first = _txn(tx, txn_id=1, commit_seq=1, event_hash="1" * 64, prev_head=None)
        # The second head is not derived from the first.
        _txn(tx, txn_id=2, commit_seq=2, event_hash="2" * 64, prev_head="f" * 64)
    assert first
    with pytest.raises(chain.StoreChainError) as broken:
        chain.verify_store_chain(store.connection)
    assert broken.value.commit_seq == 2


def test_forks_are_decided_by_head_never_by_colliding_txn_ids(tmp_path: Path) -> None:
    origin = connection.open_writer(tmp_path / "a" / "collections.sqlite", lease_check=_allow)
    with origin.transaction() as tx:
        shared = _txn(tx, txn_id=1, commit_seq=1, event_hash="1" * 64, prev_head=None)

    fork_path = tmp_path / "b" / "collections.sqlite"
    fork_path.parent.mkdir()
    copy = sqlite3.connect(fork_path)
    origin.connection.backup(copy)
    copy.close()
    fork = connection.open_writer(fork_path, lease_check=_allow)

    try:
        # Both sides commit their own transaction 2: the txn_id integers collide.
        with origin.transaction() as tx:
            origin_head = _txn(tx, txn_id=2, commit_seq=2, event_hash="a" * 64, prev_head=shared)
        with fork.transaction() as tx:
            fork_head = _txn(tx, txn_id=2, commit_seq=2, event_hash="b" * 64, prev_head=shared)

        def txn_ids(writer: connection.WriterConnection) -> list[int]:
            return [r[0] for r in writer.connection.execute("SELECT txn_id FROM txns")]

        assert txn_ids(origin) == txn_ids(fork) == [1, 2]
        assert origin_head != fork_head

        assert chain.head_relation(origin.connection, 2, origin_head) == "same"
        assert chain.head_relation(origin.connection, 2, fork_head) == "fork"
        assert chain.head_relation(fork.connection, 2, origin_head) == "fork"
        assert chain.head_relation(origin.connection, 1, shared) == "ancestor"
        assert chain.head_relation(origin.connection, 1, "c" * 64) == "fork"
        assert chain.head_relation(origin.connection, 3, "d" * 64) == "ahead"
        # Both copies still carry one store lineage identity.
        store_ids = {
            w.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
            for w in (origin, fork)
        }
        assert len(store_ids) == 1
    finally:
        origin.close()
        fork.close()
