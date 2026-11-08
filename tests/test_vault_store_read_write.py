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
from test_vault_store_migration import (  # noqa: F401
    ALPHA,
    BETA,
    DIAGRAM,
    migrate_apply,
    rollback_apply,
    vault,
)

from exomem import reserved_paths, state_migration
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


def test_a_page_in_a_vault_with_no_wrapper_folder_still_reads_from_the_store(
    stored, writer, monkeypatch
):
    """A byte reader that finds the vault by a wrapper folder in the path serves the lagging
    projection of a vault whose pages sit at the vault root."""
    # Only the ownership rule changes for such a vault: a Markdown file below the root is a page.
    monkeypatch.setattr(ownership, "classify", lambda rel: ownership.Ownership.PAGE)
    flat = "Notes/Flat.md"
    (stored / "Notes").mkdir(exist_ok=True)
    (stored / flat).write_bytes(b"the projection, which lags\n")
    write.commit(writer, put(flat, EDITED))
    assert vault_module.read_bytes_without_pinning(stored / flat, stored) == EDITED


def test_a_page_committed_but_not_yet_projected_is_listed_among_its_folders_children(
    stored, writer
):
    """A collision check that reads the projection lets a new page take the name of a page that
    the store holds and the projector has not written."""
    write.commit(writer, put(NEW, EDITED))
    assert not (stored / NEW).exists()
    with read.snapshot(stored) as view:
        listed = {child.name: child.is_dir for child in view.children("Knowledge Base/Notes")}
    assert listed["Gamma.md"] is False


def test_a_folders_children_are_its_direct_pages_and_folders_each_listed_once(stored, writer):
    """A listing that shows a deep page as a child, repeats a folder once per page below it, or
    skips the sibling that sorts right after the folder."""
    pages = ("Deep/One.md", "Deep/Two.md", "Deep0.md", "Deep.md")
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "test",
            puts=tuple(write.PagePut(f"Knowledge Base/Notes/{page}", EDITED) for page in pages),
        ),
    )
    with read.snapshot(stored) as view:
        listed = [(child.name, child.is_dir) for child in view.children("Knowledge Base/Notes")]
    assert [item for item in listed if item[0].startswith("Deep")] == [
        ("Deep.md", False),
        ("Deep", True),
        ("Deep0.md", False),
    ]


def test_a_projection_that_lags_the_store_is_not_what_the_byte_readers_return(stored, writer):
    """A reader that returns the file that the projector has not yet rewritten, which is the
    previous version of a page that the service acknowledged."""
    write.commit(writer, put(ALPHA, EDITED))
    before = (stored / ALPHA).read_bytes()
    assert before != EDITED, "the disk must lag the store for this check to mean anything"
    assert vault_module.read_bounded_guarded_bytes(stored, ALPHA, limit=LIMIT)[0] == EDITED
    with_generation = vault_module.read_bounded_guarded_bytes_with_generation
    assert with_generation(stored, ALPHA, limit=LIMIT)[0] == EDITED
    assert vault_module.read_bytes_without_pinning(stored / ALPHA, stored) == EDITED
    assert reserved_paths.read_generic_bytes(stored, ALPHA).data == EDITED


def test_the_text_reader_bounds_a_stored_page_by_its_stored_size_not_its_files(stored, writer):
    """A text reader that takes its limit from the file: a stored page that grew refuses as over its
    limit, and a page that the projector has not written yet reads as missing."""
    grown = EDITED * 50
    assert len(grown) > (stored / ALPHA).stat().st_size
    write.commit(writer, put(ALPHA, grown))
    write.commit(writer, put(NEW, EDITED))
    assert not (stored / NEW).exists()
    assert vault_module.read_guarded_text(stored, stored / ALPHA)[0] == grown.decode()
    assert vault_module.read_guarded_text(stored, stored / NEW)[0] == EDITED.decode()


def test_a_dotdot_or_symlink_spelling_of_a_page_reads_the_stored_page_and_one_leading_out_reads_the_file(
    stored, writer, tmp_path
):
    """A reader that judges containment by the spelling: a path that reaches a page through `..` or
    a link reads the stale file in store mode, and one that leads out of the vault is taken for a page."""
    write.commit(writer, put(ALPHA, EDITED))
    notes = stored / "Knowledge Base/Notes"
    (notes / "Link.md").symlink_to("Alpha.md")
    outside = tmp_path / "Outside.md"
    outside.write_bytes(b"not a page of the vault\n")
    (notes / "Out.md").symlink_to(outside)
    read_bytes = vault_module.read_bytes_without_pinning
    assert read_bytes(notes / ".." / "Notes" / "Alpha.md", stored) == EDITED
    assert read_bytes(notes / "Link.md", stored) == EDITED
    assert read_bytes(notes / "Out.md", stored) == outside.read_bytes()


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
        vault_module.read_bytes_without_pinning(stored / BETA, stored)
    with pytest.raises(FileNotFoundError):
        vault_module.read_guarded_text(stored, stored / BETA)


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


MOVED = "Knowledge Base/Notes/Moved.md"


def move(src, dst):
    return write.StoreChange(write.Origin.GOVERNED, "tidy", moves=(write.PageMove(src, dst),))


def queue(root):
    return rows(root, "SELECT path, operation, from_path FROM store_projection_queue ORDER BY path")


def test_a_moved_page_gives_each_side_an_audit_row_and_one_move_intent(stored, writer):
    """A move that leaves the old file in the vault, records only one of its two paths, or queues
    the rename as a delete and a write that cannot keep the file's identity."""
    body = stored_body(stored, BETA)
    write.commit(writer, move(BETA, MOVED))
    assert rows(
        stored, "SELECT path, body FROM store_pages WHERE path IN (?, ?)", BETA, MOVED
    ) == [(MOVED, body)]
    assert queue(stored) == [(MOVED, "move", BETA)]
    audited = rows(
        stored, "SELECT path, from_path FROM store_audit WHERE operation = 'move' ORDER BY seq"
    )
    assert audited == [(BETA, None), (MOVED, BETA)]


def test_a_later_change_to_a_moved_page_leaves_its_old_file_to_be_deleted(stored, writer):
    """A queue that forgets the old path once the new page changes, so the old file survives."""
    write.commit(writer, move(BETA, MOVED))
    write.commit(writer, put(MOVED, EDITED))
    assert queue(stored) == [(BETA, "delete", None), (MOVED, "write", None)]


def test_a_new_page_at_a_moved_pages_old_path_is_never_renamed_away(stored, writer):
    """A queue that keeps the move, so the projector renames the page that now lives at the old path."""
    write.commit(writer, move(BETA, MOVED))
    write.commit(writer, put(BETA, EDITED))
    assert queue(stored) == [(BETA, "write", None), (MOVED, "write", None)]


def manifest(root):
    return rows(root, "SELECT path, sha256, revision FROM store_manifest ORDER BY path")


def adopt(root, writer, rel, body):
    """Commit a page whose file already holds ``body``, as the importer adopts a person's edit."""
    (root / rel).write_bytes(body)
    mtime = (root / rel).stat().st_mtime_ns
    return write.commit(
        writer,
        write.StoreChange(
            write.Origin.OUTSIDE_EDITOR, "adopted", puts=(write.PagePut(rel, body, mtime),)
        ),
    )


def move_on_disk(src, dst):
    return write.StoreChange(
        write.Origin.GOVERNED, "tidy", moves=(write.PageMove(src, dst, on_disk=True),)
    )


def test_a_binary_recorded_again_after_its_delete_is_not_unlinked_by_the_queued_removal(
    stored, writer
):
    """A queue that keeps the removal of a binary that the store records again, so the projector
    unlinks a file that exists."""
    sha, size = rows(stored, "SELECT sha256, size FROM store_binaries WHERE path = ?", DIAGRAM)[0]
    removal = write.StoreChange(
        write.Origin.GOVERNED, "test", binary_deletes=(write.BinaryDelete(DIAGRAM),)
    )
    write.commit(writer, removal)
    assert queue(stored) == [(DIAGRAM, "delete", None)]
    again = write.StoreChange(
        write.Origin.GOVERNED, "test", binaries=(write.BinaryRecord(DIAGRAM, sha, size),)
    )
    write.commit(writer, again)
    assert queue(stored) == []


def test_an_on_disk_move_of_a_page_whose_latest_bytes_are_not_projected_is_refused(stored, writer):
    """A move that carries the manifest row of old bytes onto a page that holds newer ones: the page
    then reads as projected, and an outside edit that expects a projected page overwrites bytes that
    the file never received."""
    write.commit(writer, put(ALPHA, EDITED))
    before = manifest(stored)
    (stored / ALPHA).rename(stored / MOVED)
    with pytest.raises(write.StoreConflict) as refused:
        write.commit(writer, move_on_disk(ALPHA, MOVED))
    assert refused.value.path == ALPHA
    assert queue(stored) == [(ALPHA, "write", None)]
    assert manifest(stored) == before


def test_an_on_disk_move_onto_a_path_with_a_queued_delete_cancels_that_delete(stored, writer):
    """A queue that keeps the delete of a path that the move just filled, so the projector unlinks
    the moved file."""
    write.commit(writer, put(MOVED, EDITED))
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(MOVED),)),
    )
    assert queue(stored) == [(MOVED, "delete", None)]
    (stored / BETA).rename(stored / MOVED)
    write.commit(writer, move_on_disk(BETA, MOVED))
    assert queue(stored) == []
    expect = {MOVED: write.Expect(projected=True)}
    write.commit(writer, put(MOVED, EDITED, expect=expect))


def test_an_on_disk_move_onto_a_path_that_the_manifest_records_is_a_conflict(stored, writer):
    """A commit that fails with a database integrity error, which no caller handles, in place of
    the conflict that tells it to adopt the file."""
    adopt(stored, writer, MOVED, EDITED)
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(MOVED),)),
    )
    with pytest.raises(write.StoreConflict) as refused:
        write.commit(writer, move_on_disk(BETA, MOVED))
    assert refused.value.path == MOVED


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


def test_a_path_that_spells_a_parent_a_backslash_or_an_empty_folder_is_refused_before_a_write(
    stored, writer
):
    """A commit that stores a page under a spelling that no reader can ask for, or that names
    another page: the page then exists twice, under two keys."""
    revision = read.current_revision(stored)
    for rel in (
        "Knowledge Base/Notes/../Notes/Alpha.md",
        "Knowledge Base\\Notes\\Gamma.md",
        "Knowledge Base/Notes//Gamma.md",
    ):
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


def test_a_reader_does_not_parse_the_state_manifest_again_while_it_is_unchanged(vault, monkeypatch):  # noqa: F811
    """A reader that parses the manifest on every page read: a file-mode vault then pays for the
    parse on each read, which is several times the cost of the read."""
    assert read.serves(vault, ALPHA) is False
    parsed = []
    real = state_migration._read_manifest_bytes

    def counting(path):
        parsed.append(path)
        return real(path)

    monkeypatch.setattr(state_migration, "_read_manifest_bytes", counting)
    for _ in range(20):
        assert read.serves(vault, ALPHA) is False
    assert parsed == []


def test_a_reader_follows_the_manifest_through_an_activation_and_a_rollback(vault, tmp_path):  # noqa: F811
    """A reader that keeps its answer past a change of the manifest: it serves the lagging file
    after an activation, or the store after a rollback."""
    assert read.serves(vault, ALPHA) is False
    assert migrate_apply(vault) == 0
    assert read.serves(vault, ALPHA) is True
    assert rollback_apply(vault, tmp_path / "export") == 0
    assert read.serves(vault, ALPHA) is False
