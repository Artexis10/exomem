"""The collection replica is the live store's collection data, without what store mode adds to it."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest
from test_collection_store_snapshot import genesis  # noqa: F401 - fixture
from test_collection_store_typed_storage import create, migrate
from test_collection_store_writer import CID
from test_records_bulk_upsert import EVIDENCE

from exomem.collection_store import connection, schema, snapshot
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope

AT = "2026-11-08T00:00:00Z"
STORE_MODE_ROWS = (
    ("store_mode", "INSERT INTO store_mode(id, mode, revision, changed_at, release) VALUES (1, 'store', 7, ?, 'test')", (AT,)),
    ("store_pages", "INSERT INTO store_pages(path, body, sha256, size, revision) VALUES ('Knowledge Base/a.md', x'6869', 'f', 2, 7)", ()),
    ("store_audit", "INSERT INTO store_audit(revision, path, operation, origin, at) VALUES (7, 'a.md', 'write', 'test', ?)", (AT,)),
)


@pytest.fixture
def live(tmp_path):
    """A vault whose live store sits at the vault's own store path, as the replica reads it."""
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle, library_scope():
        yield CollectionWriter(root, handle)


def _stage(root, tmp_path, **kwargs):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700, exist_ok=True)
    return snapshot.staged_collection_snapshot(
        root, directory=stage, deadline=time.monotonic() + 30, **kwargs)


def _objects(database):
    return dict(database.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))


def _rows(database, table):
    """Every row, with its rowid where the table has one, so a renumbered row is a difference."""
    has_rowid = database.execute("SELECT wr FROM pragma_table_list WHERE name = ?", (table,)).fetchone() == (0,)
    return sorted(database.execute(f'SELECT {"rowid, " if has_rowid else ""}* FROM "{table}"').fetchall(), key=repr)


def test_the_copy_holds_every_collection_row_and_none_of_store_modes(live, tmp_path):
    """Defect: the replica carries pages, lexical rows or graph rows, or drops a collection or typed row."""
    create(live)
    for number in range(3):
        live.append_record(CID, item={"title": f"Row {number}", "amount": number}, why="observe", sources=(EVIDENCE,))
    assert migrate(live) == "ready"
    before = set(_objects(live.connection))
    live.connection.execute("CREATE VIRTUAL TABLE lexical_pages USING fts5(body)")
    live.connection.execute("INSERT INTO lexical_pages(body) VALUES ('pump pressure')")
    lexical = set(_objects(live.connection)) - before
    for name in sorted(lexical):
        live.connection.execute("INSERT INTO store_mode_objects(name, type) VALUES (?, 'table')", (name,))
    for _table, insert, arguments in STORE_MODE_ROWS:
        live.connection.execute(insert, arguments)
    assert lexical > {"lexical_pages", "lexical_pages_data"}

    with _stage(live.root, tmp_path) as artifact, closing(sqlite3.connect(artifact.path)) as copy:
        copied = _objects(copy)
        source = _objects(live.connection)
        assert set(copied) == set(source) - lexical
        assert all(copied[name] == source[name] for name in copied)
        for name in copied:
            if source[name].startswith("CREATE TABLE") and name not in (*schema.VAULT_STORE_TABLES, "sqlite_sequence"):
                assert _rows(copy, name) == _rows(live.connection, name), name
        assert live.connection.execute("SELECT count(*) FROM items").fetchone()[0] == 3
        # The copy rebuilds AUTOINCREMENT counters from its rows: only a table that the copy leaves empty may use one.
        assert {name for (name,) in live.connection.execute("SELECT name FROM sqlite_sequence")} <= set(schema.VAULT_STORE_TABLES)
        assert [copy.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in schema.VAULT_STORE_TABLES] == [0] * len(schema.VAULT_STORE_TABLES)
        assert [live.connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] > 0
                for name, _insert, _args in STORE_MODE_ROWS] == [True] * len(STORE_MODE_ROWS)


def test_a_virtual_table_that_store_mode_does_not_own_refuses_the_copy(live, tmp_path):
    """Defect: a virtual table is copied as its definition alone, leaving a replica whose index is empty."""
    live.connection.execute("CREATE VIRTUAL TABLE unowned USING fts5(body)")
    with pytest.raises(connection.CollectionStoreError) as refused, _stage(live.root, tmp_path):
        pass
    assert refused.value.code == "COLLECTION_SNAPSHOT_INVALID"


def test_commits_during_the_copy_leave_every_table_at_one_head(genesis):  # noqa: F811
    """Defect: tables read at different heads give a replica whose rows disagree with its commit chain."""
    from test_collection_store_snapshot import _commit

    root, stage, writer = genesis
    _commit(writer, initial=True)

    def commit_between_tables():
        _commit(writer)  # the copy checks after each table, so some commits land between two tables
        return False

    with snapshot.staged_collection_snapshot(
            root, directory=stage, deadline=time.monotonic() + 30, cancelled=commit_between_tables) as artifact:
        with closing(sqlite3.connect(artifact.path)) as copy:
            assert copy.execute("SELECT max(updated_txn) FROM items").fetchone()[0] == artifact.commit_seq
            assert copy.execute("SELECT max(commit_seq) FROM txns").fetchone()[0] == artifact.commit_seq
    live_head = int(dict(writer.connection.execute("SELECT key, value FROM store_meta"))["commit_seq"])
    assert live_head > artifact.commit_seq + 3
