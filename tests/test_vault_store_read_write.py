"""Store mode's read interface and the page-table half of its write commit.

OpenSpec make-vault-store-canonical tasks 1.2 and 1.6. The vault is the migration suite's generated
vault, moved into store mode by ``store-mode migrate``; the writer is a real store writer on the
migrated store, as the store thread holds it. The interpreter that dies in the atomicity case is a
real child process, killed by the operating system.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import sqlite3
import sys
from contextlib import closing

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, DIAGRAM, migrate_apply, vault  # noqa: F401

from exomem import reserved_paths
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.vault_store import ownership, read, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

LIMIT = 1 << 20
NEW = "Knowledge Base/Notes/Gamma.md"
EDITED = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nAn edit that only the store holds.\n"


@pytest.fixture
def stored(vault):  # noqa: F811
    """The migration suite's vault, in store mode."""
    assert migrate_apply(vault) == 0
    return vault


@pytest.fixture
def writer(stored):
    """The store's writer, as the store thread holds it."""
    path = connection.store_path(stored)
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        yield CollectionWriter(stored, handle)


def put(rel, body, **keywords):
    return write.StoreChange(
        write.Origin.GOVERNED, "test", puts=(write.PagePut(rel, body),), **keywords
    )


def rows(root, sql, *arguments):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, arguments).fetchall()


def stored_body(root, rel):
    return rows(root, "SELECT body FROM store_pages WHERE path = ?", rel)[0][0]


def test_a_file_mode_vault_refuses_a_snapshot_instead_of_reading_empty(vault):  # noqa: F811
    """A reader that takes a vault whose mode it cannot confirm for an empty store."""
    with pytest.raises(connection.CollectionStoreError, match=read.NOT_ACTIVE):
        read.current_revision(vault)


def test_after_a_migration_each_owned_page_reads_from_the_store_as_it_reads_from_the_disk(stored):
    """A read interface that drifts from what the migration stored: different bytes, or a different
    hash, size, modification time or revision than the projection manifest records."""
    served = 0
    with read.snapshot(stored) as view:
        manifest = {
            path: tuple(rest)
            for path, *rest in view.connection.execute(
                "SELECT path, sha256, size, mtime_ns, revision FROM store_manifest"
            )
        }
        for owned in ownership.iter_owned(stored):
            if owned.ownership is not ownership.Ownership.PAGE:
                continue
            assert read.owned_bytes(stored, owned.rel) == (stored / owned.rel).read_bytes()
            meta = view.page_meta(owned.rel)
            assert (meta.sha256, meta.size, meta.mtime_ns, meta.revision) == manifest[owned.rel]
            served += 1
    assert served >= 2, "the vault must hold pages for this check to mean anything"


def test_the_resolver_entries_are_the_owned_pages_that_a_disk_walk_reaches(stored):
    """A store whose resolver entries add or drop a page that the disk walk reaches, so a link
    resolves to another page, or to none, in store mode."""
    walked = {
        (rel, vault_module.title_key_from_bytes(path.read_bytes(), path))
        for path in vault_module.walk_vault_md(stored)
        if ownership.classify(rel := path.relative_to(stored).as_posix())
        is ownership.Ownership.PAGE
    }
    with read.snapshot(stored) as view:
        assert set(view.resolver_entries()) == walked
    assert len(walked) >= 2, "the vault must hold pages for this check to mean anything"


def test_a_projection_that_lags_the_store_is_not_what_the_byte_readers_return(stored, writer):
    """A reader that returns the file that the projector has not yet rewritten, which is the
    previous version of a page that the service acknowledged."""
    write.commit(writer, put(ALPHA, EDITED))
    before = (stored / ALPHA).read_bytes()
    assert before != EDITED, "the disk must lag the store for this check to mean anything"
    assert vault_module.read_bounded_guarded_bytes(stored, ALPHA, limit=LIMIT)[0] == EDITED
    with_generation = vault_module.read_bounded_guarded_bytes_with_generation
    assert with_generation(stored, ALPHA, limit=LIMIT)[0] == EDITED
    assert vault_module.read_bytes_without_pinning(stored / ALPHA) == EDITED
    assert reserved_paths.read_generic_bytes(stored, ALPHA).data == EDITED


def test_a_page_the_store_deleted_reads_as_missing_though_its_file_is_still_on_disk(stored, writer):
    """A reader that brings a deleted page back from the file that the projector has not removed."""
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(BETA),)),
    )
    assert (stored / BETA).is_file()
    with pytest.raises(vault_module.PathGuardError) as guarded:
        vault_module.read_bounded_guarded_bytes(stored, BETA, limit=LIMIT)
    assert guarded.value.code == "PATH_GUARD_IO"
    with pytest.raises(reserved_paths.ReservedPathLeafError, match="MISSING"):
        reserved_paths.read_generic_bytes(stored, BETA)
    with pytest.raises(FileNotFoundError):
        vault_module.read_bytes_without_pinning(stored / BETA)


def test_a_store_guard_refuses_after_a_store_commit_and_ignores_a_change_to_the_projection(
    stored, writer
):
    """A guard that compares the projection with the page that it read: it fails every write
    whose projection lags, and it misses a store commit that changed the page under it."""
    (stored / ALPHA).write_text("A person edited the projection.\n")
    data, guard = vault_module.read_bounded_guarded_bytes(stored, ALPHA, limit=LIMIT)
    assert data == stored_body(stored, ALPHA)
    guard.recheck(stored)
    write.commit(writer, put(ALPHA, EDITED))
    with pytest.raises(vault_module.PathGuardError) as refused:
        guard.recheck(stored)
    assert refused.value.code == "PATH_GUARD_CONTENT"


def test_a_store_guard_of_an_absent_page_refuses_once_a_commit_creates_the_page(stored, writer):
    """A create guard that passes after another writer made the page."""
    guard = vault_module.PathGuard.capture_store(stored, NEW)
    guard.recheck(stored)
    write.commit(writer, put(NEW, b"# Gamma\n"))
    with pytest.raises(vault_module.PathGuardError) as refused:
        guard.recheck(stored)
    assert refused.value.code == "PATH_GUARD_CHANGED"


def test_a_commit_stamps_its_rows_with_one_new_revision_and_leaves_the_transaction_chain_alone(
    stored, writer
):
    """A commit that reuses the chain position, which republishes the replica on every page
    write, or that stamps its pages, audit rows and intents with different revisions."""
    chain = rows(stored, "SELECT max(commit_seq) FROM txns")
    before = read.current_revision(stored)
    result = write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "test",
            puts=(write.PagePut(ALPHA, EDITED), write.PagePut(NEW, b"# Gamma\n")),
        ),
    )
    assert result.revision == before + 1 == read.current_revision(stored)
    assert set(result.written) == {ALPHA, NEW}
    assert rows(stored, "SELECT max(commit_seq) FROM txns") == chain
    # The migration's own audit row is seq 1; the commit's rows follow it.
    stamped = {
        *(r for (r,) in rows(stored, "SELECT revision FROM store_pages WHERE path = ?", ALPHA)),
        *(r for (r,) in rows(stored, "SELECT revision FROM store_pages WHERE path = ?", NEW)),
        *(r for (r,) in rows(stored, "SELECT revision FROM store_projection_queue")),
        *(r for (r,) in rows(stored, "SELECT revision FROM store_audit WHERE seq > 1")),
    }
    assert stamped == {result.revision}
    audit = rows(
        stored, "SELECT path, operation, origin FROM store_audit WHERE seq > 1 ORDER BY path"
    )
    assert audit == [
        (ALPHA, "write", write.Origin.GOVERNED.value),
        (NEW, "write", write.Origin.GOVERNED.value),
    ]


def test_two_commits_to_one_page_leave_one_intent_with_the_oldest_age_and_the_latest_revision(
    stored, writer, monkeypatch
):
    """A queue that keeps one intent per commit, so the projector writes a page twice, or one that
    restarts the intent's age at each commit, so a backlog never shows how old it is."""
    stamps = iter(("2030-01-01T00:00:01Z", "2030-01-01T00:00:02Z"))
    monkeypatch.setattr(write, "_timestamp", lambda: next(stamps))
    first = write.commit(writer, put(ALPHA, EDITED))
    second = write.commit(writer, put(ALPHA, EDITED + b"More.\n"))
    queue = rows(stored, "SELECT path, revision, operation, queued_at FROM store_projection_queue")
    assert queue == [(ALPHA, second.revision, "write", "2030-01-01T00:00:01Z")]
    assert second.revision == first.revision + 1


def test_a_commit_whose_expectation_fails_changes_nothing(stored, writer):
    """A read-modify-write that overwrites a page that another commit changed after it read."""
    stale = hashlib.sha256(stored_body(stored, ALPHA)).hexdigest()
    write.commit(writer, put(ALPHA, EDITED))
    revision = read.current_revision(stored)
    pages = rows(stored, "SELECT path, sha256 FROM store_pages ORDER BY path")
    with pytest.raises(write.StoreConflict) as refused:
        write.commit(
            writer,
            write.StoreChange(
                write.Origin.GOVERNED,
                "test",
                puts=(write.PagePut(ALPHA, b"lost update"), write.PagePut(NEW, b"# Gamma\n")),
                expect={ALPHA: write.Expect(sha256=stale)},
            ),
        )
    assert refused.value.path == ALPHA
    assert read.current_revision(stored) == revision
    assert rows(stored, "SELECT path, sha256 FROM store_pages ORDER BY path") == pages


def test_a_moved_page_gives_each_side_an_audit_row_and_an_intent(stored, writer):
    """A move that leaves the old file in the vault, or that records only one of its two paths."""
    moved = "Knowledge Base/Notes/Moved.md"
    body = stored_body(stored, BETA)
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "tidy", moves=(write.PageMove(BETA, moved),)),
    )
    assert rows(stored, "SELECT path, body FROM store_pages WHERE path IN (?, ?)", BETA, moved) == [
        (moved, body)
    ]
    assert dict(rows(stored, "SELECT path, operation FROM store_projection_queue")) == {
        BETA: "delete",
        moved: "write",
    }
    audited = rows(stored, "SELECT path FROM store_audit WHERE operation = 'move' ORDER BY seq")
    assert [path for (path,) in audited] == [BETA, moved]


def test_a_put_of_bytes_already_on_disk_records_the_manifest_and_queues_nothing(stored, writer):
    """An import that queues a projection of bytes the file already holds, or that leaves the
    manifest on the previous revision so the file reads as an outside edit."""
    (stored / ALPHA).write_bytes(EDITED)
    mtime = (stored / ALPHA).stat().st_mtime_ns
    result = write.commit(
        writer,
        write.StoreChange(
            write.Origin.OUTSIDE_EDITOR, "adopted", puts=(write.PagePut(ALPHA, EDITED, mtime),)
        ),
    )
    manifest = rows(
        stored, "SELECT sha256, size, mtime_ns, revision FROM store_manifest WHERE path = ?", ALPHA
    )
    assert manifest == [(hashlib.sha256(EDITED).hexdigest(), len(EDITED), mtime, result.revision)]
    assert rows(stored, "SELECT count(*) FROM store_projection_queue") == [(0,)]
    assert rows(stored, "SELECT operation, origin FROM store_audit WHERE path = ?", ALPHA) == [
        ("import", write.Origin.OUTSIDE_EDITOR.value)
    ]


def test_a_path_the_store_does_not_own_is_refused_before_anything_is_written(stored, writer):
    """A commit that stores a file of another owner, or a binary as a page."""
    revision = read.current_revision(stored)
    for rel in ("Archive/Outside.md", DIAGRAM):
        with pytest.raises(OpError, match=write.NOT_OWNED):
            write.commit(writer, put(rel, b"x"))
    assert read.current_revision(stored) == revision


def _commit_and_die(environment, root, log):
    """One commit in a fresh interpreter that the operating system kills at the revision step."""
    os.environ.clear()
    os.environ.update(environment)
    path = connection.store_path(root)
    with connection.open_writer(path, lease_check=lambda: True) as handle:

        def trace(statement):
            with open(log, "a") as record:
                record.write(statement.split("(")[0].strip() + "\n")
            if statement.startswith("UPDATE store_mode SET revision"):
                os.kill(os.getpid(), signal.SIGKILL)

        handle.connection.set_trace_callback(trace)
        write.commit(
            CollectionWriter(root, handle),
            write.StoreChange(
                write.Origin.GOVERNED,
                "killed",
                puts=(write.PagePut(ALPHA, EDITED), write.PagePut(NEW, b"# Gamma\n")),
            ),
        )
    os._exit(0)


def test_a_kill_inside_a_commit_leaves_the_pages_audit_intents_and_revision_all_old(
    stored, tmp_path
):
    """A torn page-table commit: new pages with the old revision, or intents for pages that were
    never stored."""
    tables = (
        "SELECT path, sha256, revision FROM store_pages ORDER BY path",
        "SELECT seq, path, operation FROM store_audit ORDER BY seq",
        "SELECT path, revision, operation FROM store_projection_queue",
        "SELECT mode, revision FROM store_mode",
    )
    before = [rows(stored, sql) for sql in tables]
    log = tmp_path / "statements.log"
    child = multiprocessing.get_context("spawn").Process(
        target=_commit_and_die, args=(dict(os.environ), stored, str(log))
    )
    child.start()
    child.join(180)
    assert child.exitcode == -signal.SIGKILL
    ran = log.read_text().splitlines()
    assert any(line.startswith("INSERT INTO store_pages") for line in ran) and any(
        line.startswith("INSERT INTO store_audit") for line in ran
    ), "the kill must land after the commit wrote its rows for this check to mean anything"
    assert [rows(stored, sql) for sql in tables] == before
    with closing(sqlite3.connect(connection.store_path(stored))) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
