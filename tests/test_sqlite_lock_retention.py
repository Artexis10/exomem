"""Exomem's own file opens leave this process's SQLite locks in place.

Closing any ordinary descriptor for a file drops every POSIX record lock that
the process holds on that file. A WAL reader whose locks are gone no longer
stops another process from checkpointing over the pages that its snapshot still
needs, so the reader sees rows newer than its transaction, or a malformed file.
A writer whose locks are gone no longer stops a writer in another process, so
one of the two committed transactions can disappear.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_vocabulary_authority import NOW, _Control, _Custody, _floor, _owner_decision, _store

from exomem import audit, claims, deferred_index, held_fs, index_paths

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="exercises Linux POSIX lock retention in the held filesystem backend",
)

# Another process, as an online-upgrade standby would be: it writes, checkpoints and closes.
_WRITE_CHECKPOINT_CLOSE = """
import sqlite3, sys
writer = sqlite3.connect(sys.argv[1], timeout=0)
writer.execute("INSERT INTO probe(n) VALUES (2)")
writer.commit()
writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
writer.close()
"""

_TRY_WRITE = """
import sqlite3, sys
writer = sqlite3.connect(sys.argv[1], timeout=0)
try:
    writer.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as error:
    if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
        raise
else:
    writer.rollback()
    raise AssertionError("another process acquired the retained writer's reservation")
finally:
    writer.close()
"""

def _assert_retained_snapshot(database: Path, reader: sqlite3.Connection) -> None:
    subprocess.run(
        [sys.executable, "-c", _WRITE_CHECKPOINT_CLOSE, os.fspath(database)],
        check=True,
        timeout=60,
    )
    wal = Path(f"{database}-wal")
    assert wal.exists() and wal.stat().st_size > 0, "another process discarded the reader's WAL"
    assert reader.execute("SELECT n FROM probe ORDER BY n").fetchall() == [(1,)]


def _locks_held_here(path: Path) -> set[str]:
    """The process's lock coverage; duplicate proc records add no protection."""
    info = path.stat()
    file_id = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    held = set()
    for line in Path("/proc/locks").read_text().splitlines():
        # "7: POSIX  ADVISORY  READ 4242 08:30:1234 128 128"; a blocked waiter's line adds "->".
        fields = line.split()
        if fields[1] == "POSIX" and fields[4] == str(os.getpid()) and fields[5] == file_id:
            held.add(" ".join((fields[3], *fields[6:8])))
    return held


def _wal_reader(database: Path) -> sqlite3.Connection:
    """A WAL connection inside an open read transaction on `database`."""
    database.parent.mkdir(parents=True, exist_ok=True)
    setup = sqlite3.connect(database)
    assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    setup.execute("CREATE TABLE chunks (file_path TEXT, chunk_idx INTEGER, file_mtime REAL)")
    setup.execute("INSERT INTO chunks VALUES ('Knowledge Base/note.md', 0, 0)")
    setup.execute("CREATE TABLE probe (n INTEGER)")
    setup.execute("INSERT INTO probe(n) VALUES (1)")
    setup.commit()
    reader = sqlite3.connect(database, isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT n FROM probe").fetchall() == [(1,)]
    setup.close()
    return reader


def test_owner_bound_connect_keeps_an_open_readers_snapshot(tmp_path: Path) -> None:
    """Opening another owned connection must not let a checkpoint invalidate an active reader."""
    index = claims.ClaimIndex(tmp_path)
    database = index.path
    setup = index._connect()
    setup.execute("CREATE TABLE probe (n INTEGER)")
    setup.execute("INSERT INTO probe(n) VALUES (1)")
    setup.commit()
    setup.close()

    reader = index._connect()
    reader.isolation_level = None
    reader.execute("BEGIN")
    assert reader.execute("SELECT n FROM probe").fetchall() == [(1,)]
    second = index._connect()
    try:
        _assert_retained_snapshot(database, reader)
    finally:
        second.close()
        reader.close()


@pytest.mark.parametrize("operation", ["identity file", "children", "enumerate"])
def test_held_identity_reads_keep_this_process_sqlite_locks(
    tmp_path: Path, operation: str
) -> None:
    """Identity reads and directory scans must not expose a live reader to checkpoint truncation."""
    database = tmp_path / "held" / "store.sqlite"
    reader = _wal_reader(database)
    try:
        with held_fs.acquire(tmp_path).require() as filesystem:
            with filesystem.parent("held").require() as parent:
                if operation == "identity file":
                    held_file = filesystem.file(parent, database.name, access="identity")
                    with held_file.require() as held:
                        assert held.identity.kind == "file"
                        assert held.identity.link_count == 1
                elif operation == "children":
                    assert filesystem.children(parent).ok
                else:
                    assert filesystem.enumerate(parent).ok

        _assert_retained_snapshot(database, reader)
    finally:
        reader.close()


def test_audit_census_read_keeps_this_process_sqlite_locks(tmp_path: Path) -> None:
    """Reading a census must not let another process checkpoint over an existing reader."""
    database = tmp_path / "state" / "census.sqlite"
    reader = _wal_reader(database)
    try:
        rows, _truncated, _last, failure = audit._sidecar_rows(
            tmp_path,
            database,
            "SELECT file_path FROM chunks WHERE file_path > ? ORDER BY file_path",
            after="",
            limit=10,
        )

        assert (rows, failure) == ([("Knowledge Base/note.md",)], None)
        _assert_retained_snapshot(database, reader)
    finally:
        reader.close()


def test_embedding_freshness_snapshot_keeps_this_process_sqlite_locks(tmp_path: Path) -> None:
    """Copying the freshness snapshot must not let a checkpoint invalidate another active reader."""
    note = tmp_path / "Knowledge Base" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# note\n", encoding="utf-8")
    database = index_paths.sidecar_path(tmp_path)
    reader = _wal_reader(database)
    try:
        freshness = deferred_index.inspect_embedding_freshness(
            tmp_path, ["Knowledge Base/note.md"]
        )

        # Only a completed read of the snapshot copy answers other than UNVERIFIABLE.
        unverifiable = deferred_index.EmbeddingFreshness.UNVERIFIABLE
        assert freshness["Knowledge Base/note.md"] is not unverifiable
        _assert_retained_snapshot(database, reader)
    finally:
        reader.close()


def test_vocabulary_authority_connect_keeps_a_writers_reserved_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second authority connection must not let an independent writer bypass a reservation."""
    # This database runs in rollback-journal mode: its file locks are its only
    # guard against a writer in another process.
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    custody = _Custody(
        control=_Control(vocabulary_authority_floor=2),
        control_path=root.parent / "custody" / "control.json",
    )
    marker = authority._create_marker(custody, floor=_floor(), owner=_owner_decision(), now=NOW)
    writer = authority._connect(custody, create=True, marker=marker)
    assert writer is not None
    database = authority._database_path(custody)
    writer.execute("BEGIN IMMEDIATE")
    try:
        second = authority._connect(custody, create=False, marker=marker)
        assert second is not None
        second.close()

        subprocess.run(
            [sys.executable, "-c", _TRY_WRITE, os.fspath(database)],
            check=True,
            timeout=60,
        )
    finally:
        writer.execute("ROLLBACK")
        writer.close()
