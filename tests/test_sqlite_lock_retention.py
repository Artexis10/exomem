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
    reason="reads this process's POSIX locks from /proc/locks, which only Linux provides",
)

# Another process, as an online-upgrade standby would be: it writes, checkpoints and closes.
_WRITE_CHECKPOINT_CLOSE = """
import sqlite3, sys
writer = sqlite3.connect(sys.argv[1])
writer.execute("INSERT INTO probe(n) VALUES (2)")
writer.commit()
writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
writer.close()
"""


def _locks_held_here(path: Path) -> list[str]:
    """The POSIX locks that this process holds on `path`, as /proc/locks lists them."""
    info = path.stat()
    file_id = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    held = []
    for line in Path("/proc/locks").read_text().splitlines():
        # "7: POSIX  ADVISORY  READ 4242 08:30:1234 128 128"; a blocked waiter's line adds "->".
        fields = line.split()
        if fields[1] == "POSIX" and fields[4] == str(os.getpid()) and fields[5] == file_id:
            held.append(" ".join((fields[3], *fields[6:8])))
    return sorted(held)


def _wal_reader(database: Path) -> sqlite3.Connection:
    """A WAL connection inside an open read transaction on `database`."""
    database.parent.mkdir(parents=True, exist_ok=True)
    setup = sqlite3.connect(database)
    assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    setup.execute("CREATE TABLE chunks (file_path TEXT, chunk_idx INTEGER, file_mtime REAL)")
    setup.execute("INSERT INTO chunks VALUES ('Knowledge Base/note.md', 0, 0)")
    setup.commit()
    reader = sqlite3.connect(database, isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM chunks").fetchone() == (1,)
    setup.close()
    return reader


def test_owner_bound_connect_keeps_an_open_readers_snapshot(tmp_path: Path) -> None:
    index = claims.ClaimIndex(tmp_path)
    database = index.path
    setup = index._connect()
    setup.execute("CREATE TABLE probe (n INTEGER)")
    setup.execute("INSERT INTO probe(n) VALUES (1)")
    setup.commit()
    query = "SELECT group_concat(n) FROM probe"
    committed = setup.execute(query).fetchone()
    setup.close()

    reader = index._connect()
    reader.isolation_level = None
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
    second = index._connect()
    try:
        reader_locks = _locks_held_here(database)
        subprocess.run(
            [sys.executable, "-c", _WRITE_CHECKPOINT_CLOSE, os.fspath(database)],
            check=True,
            timeout=60,
        )
        wal_kept = Path(f"{database}-wal").exists()
        view = reader.execute(query).fetchone()
    finally:
        second.close()
        reader.close()

    # SQLite 3.53 refuses that close checkpoint while the reader keeps its -shm
    # locks; 3.50 and 3.45 checkpoint, delete the WAL and change the view.
    assert reader_locks, "the reader no longer holds its lock on the database file"
    assert wal_kept, "another process checkpointed over the reader and removed the WAL"
    assert view == committed


@pytest.mark.parametrize("operation", ["identity file", "children", "enumerate"])
def test_held_identity_reads_keep_this_process_sqlite_locks(
    tmp_path: Path, operation: str
) -> None:
    database = tmp_path / "held" / "store.sqlite"
    reader = _wal_reader(database)
    shm = Path(f"{database}-shm")
    try:
        before = {path: _locks_held_here(path) for path in (database, shm)}
        assert all(before.values())
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

        assert {path: _locks_held_here(path) for path in before} == before
    finally:
        reader.close()


def test_audit_census_read_keeps_this_process_sqlite_locks(tmp_path: Path) -> None:
    database = tmp_path / "state" / "census.sqlite"
    reader = _wal_reader(database)
    shm = Path(f"{database}-shm")
    try:
        before = {path: _locks_held_here(path) for path in (database, shm)}
        assert all(before.values())

        rows, _truncated, _last, failure = audit._sidecar_rows(
            tmp_path,
            database,
            "SELECT file_path FROM chunks WHERE file_path > ? ORDER BY file_path",
            after="",
            limit=10,
        )

        assert (rows, failure) == ([("Knowledge Base/note.md",)], None)
        assert {path: _locks_held_here(path) for path in before} == before
    finally:
        reader.close()


def test_embedding_freshness_snapshot_keeps_this_process_sqlite_locks(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# note\n", encoding="utf-8")
    database = index_paths.sidecar_path(tmp_path)
    reader = _wal_reader(database)
    shm = Path(f"{database}-shm")
    try:
        before = {path: _locks_held_here(path) for path in (database, shm)}
        assert all(before.values())

        freshness = deferred_index.inspect_embedding_freshness(
            tmp_path, ["Knowledge Base/note.md"]
        )

        # Only a completed read of the snapshot copy answers other than UNVERIFIABLE.
        unverifiable = deferred_index.EmbeddingFreshness.UNVERIFIABLE
        assert freshness["Knowledge Base/note.md"] is not unverifiable
        assert {path: _locks_held_here(path) for path in before} == before
    finally:
        reader.close()


def test_vocabulary_authority_connect_keeps_a_writers_reserved_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        before = _locks_held_here(database)
        assert before
        second = authority._connect(custody, create=False, marker=marker)
        assert second is not None
        second.close()

        assert _locks_held_here(database) == before
    finally:
        writer.execute("ROLLBACK")
        writer.close()
