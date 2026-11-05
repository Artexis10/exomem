"""Partial graph repairs retain the rows of the last complete publication."""

from contextlib import closing
from pathlib import Path

from test_relation_fragment_targets import DUP_TARGET, SOURCE, _built

from exomem import deferred_index, find, graph_publication, index_sync


def test_split_drain_keeps_published_units_until_the_remaining_work_finishes(
    tmp_path: Path,
) -> None:
    root, index = _built(tmp_path)
    query = "SELECT node_key, text, source_hash FROM graph_nodes ORDER BY node_key"
    with closing(index._connect_existing(readonly=True)) as conn:
        baseline = conn.execute(query).fetchall()
        metadata = dict(conn.execute("SELECT key, value FROM graph_meta"))

    source = root / SOURCE
    source.write_text(source.read_text().replace("Source body.", "Repaired source body."))
    other = root / DUP_TARGET
    other.write_text(other.read_text() + "\nA later repair remains queued.\n")
    assert index_sync.publish_corpus_delta(root, changed=[source, other])
    find.on_resolver_files_changed(root, [SOURCE, DUP_TARGET], [])
    receipts = deferred_index.add_graph_receipts(root, [SOURCE, DUP_TARGET])

    partial = index.drain_paths([source])
    assert partial["published"] is False
    assert partial["indexed_files"] >= 1
    with closing(index._connect_existing(readonly=True)) as conn:
        assert conn.execute(query).fetchall() != baseline
        conn.execute("BEGIN")
        assert graph_publication.open_view(conn) == metadata
        assert conn.execute(query).fetchall() == baseline

    deferred_index.clear_graph_receipts(root, [row for row in receipts if row.rel_path == SOURCE])
    complete = index.drain_paths([other])
    assert complete["published"] is True
    with closing(index._connect_existing(readonly=True)) as conn:
        successor = conn.execute(query).fetchall()
        current = dict(conn.execute("SELECT key, value FROM graph_meta"))
        assert successor != baseline
        conn.execute("BEGIN")
        assert graph_publication.open_view(conn) == current
        assert conn.execute(query).fetchall() == successor


def test_integrity_purge_withdraws_the_saved_publication(tmp_path: Path) -> None:
    _root, index = _built(tmp_path)

    assert index.purge_exact_persisted_rows(node_paths=[SOURCE], edge_values={}) > 0

    with closing(index._connect_existing(readonly=True)) as conn:
        conn.execute("BEGIN")
        assert graph_publication.open_view(conn) is None
