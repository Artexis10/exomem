"""The catalogue's change journal names every path a write touched, so recall rereads only those rows.

After a write, recall read every page row, page identity and unit revision of the catalogue again. Now
it keeps the rows of the last snapshot it read and patches them from the paths the journal names. These
tests pin what that patch must never change: a patched state equals a whole read, and a gap the journal
cannot vouch for reads every row again.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
from pathlib import Path

import pytest

from exomem import lexstore


def _catalogue(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    lexstore.create_catalogue_schema(conn)
    return conn


def _write(conn: sqlite3.Connection, *paths: str) -> None:
    for path in paths:
        conn.execute(
            "INSERT INTO pages(path, mtime_ns) VALUES(?, 1) "
            "ON CONFLICT(path) DO UPDATE SET mtime_ns = mtime_ns + 1",
            (path,),
        )


def test_the_journal_names_every_path_that_a_write_touched_since_a_position(tmp_path: Path) -> None:
    conn = _catalogue(tmp_path / "catalogue.sqlite")
    _write(conn, "a.md", "c.md")
    since = lexstore._journal_position(conn)

    _write(conn, "b.md", "a.md")
    conn.execute("DELETE FROM pages WHERE path = 'c.md'")
    conn.execute("UPDATE pages SET path = 'd.md' WHERE path = 'b.md'")
    now = lexstore._journal_position(conn)
    assert sorted(lexstore._changed_paths(conn, since, now)) == ["a.md", "b.md", "c.md", "d.md"]
    assert lexstore._changed_paths(conn, now, now) == []

    # A full-text row or a unit revision moves without its page row.
    conn.execute("INSERT INTO fts(rowid, stemmed) SELECT rowid, 'harbor' FROM pages WHERE path = 'a.md'")
    conn.execute("INSERT INTO unit_revisions(parent_path, revision) VALUES('e.md', 'r1')")
    assert sorted(lexstore._changed_paths(conn, now, lexstore._journal_position(conn))) == ["a.md", "e.md"]


def test_a_copy_vouches_for_a_gap_only_from_a_position_before_it_diverged(tmp_path: Path) -> None:
    live = _catalogue(tmp_path / "live.sqlite")
    _write(live, "a.md")
    before_copy = lexstore._journal_position(live)
    # A copy of the file's bytes keeps its schema version; SQLite's backup API would change it.
    shutil.copyfile(tmp_path / "live.sqlite", tmp_path / "copy.sqlite")
    copy = sqlite3.connect(tmp_path / "copy.sqlite", isolation_level=None)

    # Each catalogue writes its own page at the same journal position.
    _write(live, "b.md")
    after_copy = lexstore._journal_position(live)
    _write(copy, "c.md")
    replaced = lexstore._journal_position(copy)
    assert (after_copy.schema, after_copy.seq) == (replaced.schema, replaced.seq)

    # The copy holds every row up to its copy point, so a reader positioned there misses nothing.
    assert lexstore._changed_paths(copy, before_copy, replaced) == ["c.md"]
    # A reader positioned on the live catalogue after the copy would miss `b.md` there.
    assert lexstore._changed_paths(copy, after_copy, replaced) is None
    _write(copy, "d.md")
    assert lexstore._changed_paths(copy, after_copy, lexstore._journal_position(copy)) is None


def test_a_schema_change_ends_what_the_journal_vouches_for(tmp_path: Path) -> None:
    conn = _catalogue(tmp_path / "catalogue.sqlite")
    _write(conn, "a.md")
    since = lexstore._journal_position(conn)

    # A table replaced by DDL loses its rows without a journal row, so no gap across DDL holds.
    conn.execute("CREATE INDEX pages_title ON pages(title)")
    _write(conn, "b.md")
    assert lexstore._changed_paths(conn, since, lexstore._journal_position(conn)) is None

    # A write without a journal trigger names nothing, so a catalogue that lacks one has no position.
    conn.execute(f"DROP TRIGGER {lexstore._change_trigger('pages', 'UPDATE')}")
    assert lexstore._journal_position(conn) is None


def test_a_position_that_the_journal_bound_dropped_cannot_vouch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lexstore, "_CHANGE_JOURNAL_ROWS", 4)
    conn = _catalogue(tmp_path / "catalogue.sqlite")
    _write(conn, "a.md")
    since = lexstore._journal_position(conn)

    # Each new page journals its page row and its unit revision.
    _write(conn, "b.md")
    assert lexstore._changed_paths(conn, since, lexstore._journal_position(conn)) == ["b.md"]
    _write(conn, "c.md", "d.md")
    assert conn.execute(f"SELECT count(*) FROM {lexstore.CHANGE_JOURNAL}").fetchone() == (4,)
    assert lexstore._changed_paths(conn, since, lexstore._journal_position(conn)) is None


def test_rows_patched_after_seeded_writes_equal_a_whole_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edits, renames, deletions and moves in and out of the knowledge base and its raw material.

    After each write the long-lived store patches its rows from the journal. A store that kept nothing
    reads the whole catalogue at the same snapshot, and both must hold the same rows in the same order.
    """
    folders = ["Knowledge Base/Notes", "Knowledge Base/Sources", "Journal"]
    names = [f"page-{index}" for index in range(8)]
    chosen = random.Random(11)
    words = ["harbor", "tide", "gauge", "chain", "buoy", "reef"]
    written: dict[str, Path] = {}

    def write(name: str) -> None:
        if name in written:
            written.pop(name).unlink()
        path = tmp_path / chosen.choice(folders) / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        units = "\n".join(f"- [fact] {' '.join(chosen.choices(words, k=3))}" for _ in range(chosen.randint(0, 3)))
        path.write_text(f"# {chosen.choice(words).title()} {name}\n\n{' '.join(chosen.choices(words, k=12))}\n\n{units}\n")
        written[name] = path

    reads: list[int | None] = []
    real_read = lexstore._read_catalogue_rows

    def counted_read(conn, paths, context, indexed):
        reads.append(None if paths is None else len(paths))
        return real_read(conn, paths, context, indexed)

    monkeypatch.setattr(lexstore, "_read_catalogue_rows", counted_read)

    def compare() -> None:
        store = lexstore.get_store(tmp_path)
        conn = store._connect()
        try:
            conn.execute("BEGIN")
            generation = store._snapshot_generation(conn)
            kept = store._catalogue_rows(conn, generation)
            whole = lexstore.LexicalStore(tmp_path)._catalogue_rows(conn, generation)
        finally:
            conn.close()
        assert list(kept.pages.items()) == list(whole.pages.items())
        assert kept.revisions == whole.revisions

    for name in names[:5]:
        write(name)
    lexstore.ensure_fresh(tmp_path)
    compare()
    for _step in range(14):
        change = chosen.choice(["edit", "edit", "move", "add", "delete"])
        if change == "delete" and len(written) > 2:
            written.pop(chosen.choice(sorted(written))).unlink()
        elif change == "add" and len(written) < len(names):
            write(chosen.choice([name for name in names if name not in written]))
        else:
            write(chosen.choice(sorted(written)))
        lexstore.ensure_fresh(tmp_path)
        compare()
    # The kept store read the whole catalogue once; the store that kept nothing reads it at every step.
    kept_reads = reads[0::2]
    assert kept_reads[0] is None
    assert all(paths is not None and paths > 0 for paths in kept_reads[1:]), kept_reads
