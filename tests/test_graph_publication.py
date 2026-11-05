"""Row publications preserve the maintained graph while repairs change its rows."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from exomem import epistemic_graph, graph_publication


def _put(conn: sqlite3.Connection, table: str, **values: str | int) -> None:
    fields = list(conn.execute(f'PRAGMA main.table_info("{table}")'))
    row = {field[1]: values.get(field[1], 0 if field[2] == "INTEGER" else "") for field in fields}
    columns = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f'INSERT OR REPLACE INTO "{table}" ({columns}) VALUES ({marks})',
        tuple(row.values()),
    )


def _rows(conn: sqlite3.Connection, *, main: bool = False) -> dict[str, list[tuple]]:
    rows = {}
    for table in graph_publication.ROW_TABLES:
        columns = ", ".join(
            f'"{field[1]}"' for field in conn.execute(f'PRAGMA main.table_info("{table}")')
        )
        source = f'main."{table}"' if main else f'"{table}"'
        rows[table] = sorted(conn.execute(f"SELECT rowid, {columns} FROM {source}"))
    return rows


def _reader(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.execute("BEGIN")
    return conn


@pytest.fixture
def graph_db(tmp_path: Path):
    path = tmp_path / "graph.sqlite"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        index = object.__new__(epistemic_graph.EpistemicGraphIndex)
        index._create_graph_schema(writer)
        target = "notes/target.md"
        target_key = epistemic_graph._file_key(target)
        _put(writer, "graph_nodes", node_key=target_key, path=target, text="published")
        _put(writer, "graph_nodes", node_key="author", path="notes/author.md")
        for number in range(4):
            author = f"notes/author-{number}.md"
            _put(writer, "graph_nodes", node_key=f"unit:{number}", path=author)
            _put(
                writer,
                "graph_edges",
                edge_key=f"incoming:{number}",
                source_path=author,
                src_key=f"unit:{number}",
                dst_page_key=target_key,
            )
            _put(
                writer,
                "graph_edges",
                edge_key=f"other:{number}",
                source_path=author,
                src_key=f"unit:{number}",
                dst_page_key="other",
            )
        for source in (target, "notes/author.md"):
            _put(writer, "graph_parent_refs", path=source, parent_ref="parent")
            _put(writer, "graph_dependency_coverage", source_path=source, source_hash="hash")
            _put(
                writer, "graph_dependencies", source_path=source, lookup_key="key", raw_target="one"
            )
        _put(writer, "graph_dependencies", source_path=target, lookup_key="key", raw_target="two")
        writer.execute("INSERT INTO graph_meta VALUES ('proof', 'complete')")
        graph_publication.install(writer)
        writer.commit()
        yield writer, path, index


def test_unpublished_storage_installs_without_owning_a_transaction(graph_db):
    writer, path, _ = graph_db
    assert not writer.in_transaction
    graph_publication.install(writer)
    assert not writer.in_transaction
    writer.execute("BEGIN")
    graph_publication.install(writer)
    assert writer.in_transaction
    writer.rollback()
    with closing(_reader(path)) as reader:
        assert graph_publication.open_view(reader) is None
        assert reader.execute("SELECT name FROM sqlite_temp_master").fetchall() == []


@pytest.mark.parametrize("recursive", [0, 1])
def test_replacements_and_key_moves_preserve_first_rows_and_order(graph_db, recursive):
    writer, path, _ = graph_db
    writer.execute(f"PRAGMA recursive_triggers={recursive}")
    baseline = _rows(writer, main=True)
    proof = dict(writer.execute("SELECT key, value FROM graph_meta"))
    with writer:
        graph_publication.publish(writer)
    target_key = epistemic_graph._file_key("notes/target.md")
    with writer:
        for text in ("first repair", "second repair"):
            _put(writer, "graph_nodes", node_key=target_key, path="notes/target.md", text=text)
            _put(writer, "graph_edges", edge_key="incoming:0", source_path="notes/changed.md")
        writer.execute(
            "UPDATE OR REPLACE graph_nodes SET node_key='author' WHERE node_key=?", (target_key,)
        )
        writer.execute("DELETE FROM graph_parent_refs WHERE path='notes/target.md'")
        _put(writer, "graph_parent_refs", path="notes/target.md", parent_ref="replacement")
        writer.execute(
            "UPDATE graph_dependencies SET raw_target='moved' "
            "WHERE source_path='notes/target.md' AND raw_target='one'"
        )
        _put(
            writer,
            "graph_dependencies",
            source_path="notes/target.md",
            lookup_key="key",
            raw_target="one",
        )
        writer.execute("DELETE FROM graph_dependencies WHERE raw_target='moved'")
        writer.execute("UPDATE graph_dependency_coverage SET source_hash='repair'")
    with closing(_reader(path)) as reader:
        assert graph_publication.open_view(reader) == proof
        assert _rows(reader) == baseline
    assert _rows(writer, main=True) != baseline


@pytest.mark.parametrize("recursive", [0, 1])
def test_born_row_churn_retires_markers_after_replace_and_key_moves(graph_db, recursive):
    writer, path, _ = graph_db
    writer.execute(f"PRAGMA recursive_triggers={recursive}")
    baseline = _rows(writer, main=True)
    with writer:
        graph_publication.publish(writer)
    for number in range(6):
        with writer:
            key = f"born:{number}"
            _put(writer, "graph_nodes", node_key=key, path="notes/new.md")
            _put(writer, "graph_nodes", node_key=key, path="notes/new.md", text="replacement")
            _put(writer, "graph_nodes", node_key="destination", path="notes/new.md")
            writer.execute(
                "UPDATE OR REPLACE graph_nodes SET node_key='destination' WHERE node_key=?", (key,)
            )
        with closing(_reader(path)) as reader:
            assert graph_publication.open_view(reader) is not None
            assert _rows(reader) == baseline
        with writer:
            writer.execute("DELETE FROM graph_nodes WHERE node_key='destination'")
        assert writer.execute("SELECT COUNT(*) FROM graph_saved_nodes").fetchone()[0] == 0


def test_target_deletion_captures_only_changed_rows_and_publishes_atomically(graph_db):
    writer, path, index = graph_db
    baseline = _rows(writer, main=True)
    proof = dict(writer.execute("SELECT key, value FROM graph_meta"))
    with writer:
        graph_publication.publish(writer)
    with writer:
        assert index._delete_path(writer, "notes/target.md", commit=False) == 1
    assert {
        table: writer.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in (
            "graph_saved_nodes",
            "graph_saved_edges",
            "graph_saved_parent_refs",
            "graph_saved_dependencies",
            "graph_saved_dependency_coverage",
        )
    } == {
        "graph_saved_nodes": 1,
        "graph_saved_edges": 4,
        "graph_saved_parent_refs": 1,
        "graph_saved_dependencies": 2,
        "graph_saved_dependency_coverage": 1,
    }
    successor = _rows(writer, main=True)
    with closing(_reader(path)) as one, closing(_reader(path)) as two:
        assert one is not two
        assert graph_publication.open_view(one) == proof
        assert graph_publication.open_view(two) == proof
        assert _rows(one) == _rows(two) == baseline
        with writer:
            writer.execute("UPDATE graph_meta SET value='successor' WHERE key='proof'")
            graph_publication.publish(writer)
            assert writer.in_transaction
        assert _rows(one) == _rows(two) == baseline
        with closing(_reader(path)) as reader:
            assert graph_publication.open_view(reader) == dict(
                writer.execute("SELECT * FROM graph_meta")
            )
            assert _rows(reader) == successor
            assert _rows(reader) != baseline


def test_publication_and_withdrawal_rollback_with_the_callers_transaction(graph_db):
    writer, path, _ = graph_db
    baseline = _rows(writer, main=True)
    proof = dict(writer.execute("SELECT key, value FROM graph_meta"))
    with writer:
        graph_publication.publish(writer)
    with writer:
        writer.execute("UPDATE graph_nodes SET text='partial repair' WHERE node_key='author'")
    partial = _rows(writer, main=True)
    writer.execute("BEGIN")
    writer.execute("UPDATE graph_meta SET value='uncommitted' WHERE key='proof'")
    writer.execute("DELETE FROM graph_nodes WHERE node_key='author'")
    graph_publication.publish(writer)
    assert writer.in_transaction
    writer.rollback()
    assert _rows(writer, main=True) == partial
    with closing(_reader(path)) as reader:
        assert graph_publication.open_view(reader) == proof
        assert _rows(reader) == baseline
    writer.execute("BEGIN")
    graph_publication.withdraw(writer)
    assert writer.in_transaction
    writer.rollback()
    with closing(_reader(path)) as reader:
        assert graph_publication.open_view(reader) == proof
        assert _rows(reader) == baseline
    with writer:
        graph_publication.withdraw(writer)
    with closing(_reader(path)) as reader:
        assert graph_publication.open_view(reader) is None
        assert reader.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
    assert writer.execute("SELECT COUNT(*) FROM graph_saved_nodes").fetchone()[0] == 0
    assert _rows(writer, main=True) == partial
