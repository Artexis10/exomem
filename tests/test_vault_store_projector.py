"""Store mode's projector: a committed page reaches the disk, and a person's file is never lost.

OpenSpec make-vault-store-canonical task 4.1. The vault is the migration suite's generated vault,
moved into store mode by ``store-mode migrate``. Commits go through the real ``write.commit`` on a
real store writer, and the projector runs its real passes on the real files. The interpreters that
die in the kill trial are real child processes, killed by the operating system.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tarfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hard_kill import KILLED, kill_self
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import (  # noqa: F401
    ALPHA,
    BETA,
    build_indexes,
    migrate_apply,
    rollback_apply,
    vault,
)

from exomem import held_fs, lexstore, server_runtime
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.vault_store import mode, projector, read, write

#: The two folder-flush trials wrap the POSIX filesystem class and name each folder through /proc/self/fd.
posix_only_spy = pytest.mark.skipif(
    sys.platform != "linux", reason="the spy names each flushed folder through /proc/self/fd"
)

NOTES = "Knowledge Base/Notes"
NEW = f"{NOTES}/Gamma.md"
MOVE_FROM = f"{NOTES}/Drifter.md"
MOVE_TO = f"{NOTES}/Archive/Drifter.md"
EDITED = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nAn edit that only the store holds.\n"
PERSON = b"A person edited this page in an outside editor.\n"


@pytest.fixture
def stored(vault):  # noqa: F811
    """The migration suite's vault, in store mode."""
    assert migrate_apply(vault) == 0
    return vault


@pytest.fixture
def writer(stored):
    """The store's writer, as the store thread holds it."""
    with connection.open_writer(connection.store_path(stored), lease_check=lambda: True) as handle:
        yield CollectionWriter(stored, handle)


@pytest.fixture
def projecting(stored, writer):
    """A projector whose bookkeeping runs on this thread's writer, as the store thread runs it."""
    return projector.Projector(stored, run=lambda work: work(writer))


def commit(writer, **operations):
    return write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", **operations))


def put(rel, body):
    return {"puts": (write.PagePut(rel, body),)}


def rows(root, sql, *arguments):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, arguments).fetchall()


def temporaries(root):
    """Every file that a held publication left beside a page, hidden by its name."""
    return sorted(
        str(found) for found in root.rglob("*") if held_fs.is_publish_temporary(found.name)
    )


def test_committed_pages_a_delete_and_a_move_reach_the_disk_byte_exact(stored, writer, projecting):
    """A projector that leaves a committed page, delete or move off the disk, writes other bytes
    than the store holds, or records a manifest that differs from the file it wrote."""
    drifter = b"# Drifter\n\nA page that moves to another folder.\n"
    commit(writer, **put(MOVE_FROM, drifter))
    assert projecting.run_pass().projected == 1
    commit(
        writer,
        puts=(
            write.PagePut(NEW, b"# Gamma\n\nA page with no file yet.\n"),
            write.PagePut(ALPHA, EDITED),
        ),
        deletes=(write.PageDelete(BETA),),
        moves=(write.PageMove(MOVE_FROM, MOVE_TO),),
    )

    report = projecting.run_pass()

    assert (report.projected, report.removed, report.held, report.deferred) == (3, 1, 0, {})
    with read.snapshot(stored) as view:
        stored_pages = {meta.path: meta for meta in view.pages(NOTES)}
    for rel, meta in stored_pages.items():
        on_disk = stored / rel
        assert on_disk.read_bytes() == read.owned_bytes(stored, rel)
        manifest = rows(
            stored,
            "SELECT sha256, size, mtime_ns, revision FROM store_manifest WHERE path = ?",
            rel,
        )
        stat = on_disk.stat()
        # The page's own mtime is what a stat of its file says, so a reader of either agrees.
        assert manifest == [(meta.sha256, stat.st_size, stat.st_mtime_ns, meta.revision)]
        assert meta.mtime_ns == stat.st_mtime_ns
    assert (stored / MOVE_TO).read_bytes() == drifter
    assert not (stored / MOVE_FROM).exists() and not (stored / BETA).exists()
    assert (
        rows(stored, "SELECT path FROM store_manifest WHERE path IN (?, ?)", BETA, MOVE_FROM) == []
    )
    assert rows(stored, "SELECT COUNT(*) FROM store_projection_queue") == [(0,)]
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert temporaries(stored) == []


def test_a_persons_edit_is_held_and_kept_while_newer_store_bytes_wait(stored, writer, projecting):
    """A projector that replaces the file a person edited with the store's newer bytes, projects a
    later revision of a held path, or lets one held path stop the projection of the others."""
    (stored / ALPHA).write_bytes(PERSON)
    commit(writer, **put(ALPHA, EDITED))
    commit(writer, **put(NEW, b"# Gamma\n"))

    report = projecting.run_pass()

    assert (report.held, report.projected) == (1, 1), "the held path must not block the other one"
    assert (stored / ALPHA).read_bytes() == PERSON
    held = rows(stored, "SELECT kind, body, sha256, base_revision, store_revision FROM store_held")
    manifest_revision = rows(stored, "SELECT revision FROM store_manifest WHERE path = ?", ALPHA)[
        0
    ][0]
    page_revision = rows(stored, "SELECT revision FROM store_pages WHERE path = ?", ALPHA)[0][0]
    assert held == [
        ("edit", PERSON, hashlib.sha256(PERSON).hexdigest(), manifest_revision, page_revision)
    ]
    assert rows(stored, "SELECT operation FROM store_projection_queue WHERE path = ?", ALPHA) == [
        ("write",)
    ]

    commit(writer, **put(ALPHA, EDITED + b"A later store revision.\n"))
    assert projecting.run_pass().settled == 0
    assert (stored / ALPHA).read_bytes() == PERSON
    assert len(rows(stored, "SELECT path FROM store_held")) == 1


def test_a_projected_file_that_a_person_deleted_is_held_not_recreated(stored, writer, projecting):
    """A projector that recreates a page the person deleted, or drops the page from the store."""
    (stored / BETA).unlink()
    commit(writer, **put(BETA, b"# Beta\n\nNewer store bytes.\n"))

    report = projecting.run_pass()

    assert report.held == 1
    assert not (stored / BETA).exists()
    revision = rows(stored, "SELECT revision FROM store_manifest WHERE path = ?", BETA)[0][0]
    assert rows(stored, "SELECT kind, body, base_revision FROM store_held") == [
        ("delete", None, revision)
    ]
    assert len(rows(stored, "SELECT path FROM store_pages WHERE path = ?", BETA)) == 1


def test_a_delete_never_removes_a_file_that_a_person_edited(stored, writer, projecting):
    """A projector that unlinks the file of a deleted page after the person changed its bytes."""
    (stored / BETA).write_bytes(PERSON)
    commit(writer, deletes=(write.PageDelete(BETA),))

    report = projecting.run_pass()

    assert (report.held, report.removed) == (1, 0)
    assert (stored / BETA).read_bytes() == PERSON
    assert rows(stored, "SELECT kind, body, store_revision FROM store_held") == [
        ("edit", PERSON, None)
    ]
    assert rows(stored, "SELECT operation FROM store_projection_queue WHERE path = ?", BETA) == [
        ("delete",)
    ]


def test_a_move_never_carries_a_file_that_a_person_edited_to_the_new_path(
    stored, writer, projecting
):
    """A projector that renames a person's edited file to the new path, or writes the new path while
    the person's file sits at the old one."""
    commit(writer, **put(MOVE_FROM, b"# Drifter\n"))
    projecting.run_pass()
    (stored / MOVE_FROM).write_bytes(PERSON)
    commit(writer, moves=(write.PageMove(MOVE_FROM, MOVE_TO),))

    report = projecting.run_pass()

    assert report.held == 1
    assert (stored / MOVE_FROM).read_bytes() == PERSON
    assert not (stored / MOVE_TO).exists()
    assert rows(stored, "SELECT path, kind FROM store_held") == [(MOVE_FROM, "edit")]
    assert projecting.run_pass().settled == 0, "the held old path blocks the move until it resolves"


def test_a_move_of_a_page_with_a_pending_write_ends_with_the_pages_new_bytes(
    stored, writer, projecting
):
    """A projector that renames the old file to the new path and stops, which leaves the bytes of a
    write that was queued when the move was committed behind at the new path."""
    commit(writer, **put(MOVE_FROM, b"# Drifter\n\nOld bytes.\n"))
    projecting.run_pass()
    newer = b"# Drifter\n\nNew bytes that were queued before the move.\n"
    commit(writer, **put(MOVE_FROM, newer))
    commit(writer, moves=(write.PageMove(MOVE_FROM, MOVE_TO),))

    projecting.run_pass()

    assert (stored / MOVE_TO).read_bytes() == newer
    assert not (stored / MOVE_FROM).exists()
    assert rows(stored, "SELECT COUNT(*) FROM store_projection_queue") == [(0,)]
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]


def test_a_move_whose_source_file_is_gone_writes_the_new_path_from_the_store(
    stored, writer, projecting
):
    """A projector that holds the move because its source file is missing, or leaves the new path
    unwritten."""
    drifter = b"# Drifter\n\nA page whose file a person already removed.\n"
    commit(writer, **put(MOVE_FROM, drifter))
    projecting.run_pass()
    (stored / MOVE_FROM).unlink()
    commit(writer, moves=(write.PageMove(MOVE_FROM, MOVE_TO),))

    report = projecting.run_pass()

    assert (report.held, report.projected) == (0, 1)
    assert (stored / MOVE_TO).read_bytes() == drifter
    assert rows(stored, "SELECT path FROM store_manifest WHERE path = ?", MOVE_FROM) == []
    assert rows(stored, "SELECT COUNT(*) FROM store_projection_queue") == [(0,)]
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]


def test_a_delete_of_a_page_that_was_never_projected_leaves_a_persons_file_alone(
    stored, writer, projecting
):
    """A projector that unlinks or holds a person's file at a path where the store's page was
    deleted before it had a projection."""
    commit(writer, **put(NEW, b"# Gamma\n"))
    commit(writer, deletes=(write.PageDelete(NEW),))
    (stored / NEW).write_bytes(PERSON)

    report = projecting.run_pass()

    assert (report.dropped, report.removed, report.held) == (1, 0, 0)
    assert (stored / NEW).read_bytes() == PERSON
    assert rows(stored, "SELECT COUNT(*) FROM store_projection_queue") == [(0,)]
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]


def test_the_status_reports_queue_age_and_held_kinds_and_unknown_for_an_unreadable_store(
    stored, writer, projecting, monkeypatch
):
    """A status that shows zeros for a store it could not read, hides the age of the oldest queued
    projection, or counts held items without their kind."""
    quiet = mode.status(stored)
    assert set(quiet) >= set(mode.STORE_FIGURES)
    assert (quiet["queued_projections"], quiet["oldest_projection_age_seconds"]) == (0, None)
    assert (quiet["held"], quiet["held_by_kind"]) == (0, {})

    commit(writer, **put(NEW, b"# Gamma\n"))
    backdated = (datetime.now(UTC) - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with writer.handle.transaction() as tx:
        tx.execute("UPDATE store_projection_queue SET queued_at = ?", (backdated,))
    waiting = mode.status(stored)
    assert waiting["queued_projections"] == 1
    assert 90 <= waiting["oldest_projection_age_seconds"] < 120
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with writer.handle.transaction() as tx:
        tx.execute("UPDATE store_projection_queue SET queued_at = ?", (future,))
    assert mode.status(stored)["oldest_projection_age_seconds"] == mode.UNKNOWN, "a clock skew"

    (stored / ALPHA).write_bytes(PERSON)
    (stored / BETA).unlink()
    commit(writer, **put(ALPHA, EDITED))
    commit(writer, **put(BETA, b"# Beta\n\nNewer.\n"))
    projecting.run_pass()
    held = mode.status(stored)
    assert (held["held"], held["held_by_kind"]) == (2, {"edit": 1, "delete": 1})
    # Two held paths are queued, and neither waits for the projector.
    assert (held["queued_projections"], held["oldest_projection_age_seconds"]) == (2, None)

    def unreadable(_root):
        raise OSError("the store cannot be read")

    monkeypatch.setattr(mode, "_store_figures", unreadable)
    figures = mode.status(stored)
    assert all(figures[name] == mode.UNKNOWN for name in mode.STORE_FIGURES)
    assert figures["mode"] == mode.STORE_MODE and figures["unreadable"] == "OSError"

    monkeypatch.setattr(mode, "_stored_mode", unreadable)
    unreadable_store = mode.status(stored)
    assert (unreadable_store["mode"], unreadable_store["unreadable"]) == (mode.UNKNOWN, "OSError")
    assert "pages" not in unreadable_store, "an unknown mode must not carry store figures"


def test_the_service_starts_the_projector_only_for_a_store_mode_vault(vault, monkeypatch):  # noqa: F811
    """A service that starts a projector thread for a file-mode vault, or one that never writes a
    committed page of a store-mode vault to its disk."""
    assert server_runtime._start_vault_projector(vault) is None

    assert migrate_apply(vault) == 0
    path = connection.store_path(vault)

    def on_a_writer_of_its_own(work):
        with connection.open_writer(path, lease_check=lambda: True) as handle:
            work(CollectionWriter(vault, handle))

    monkeypatch.setattr(
        projector, "store_thread_runner", lambda _root, **_options: on_a_writer_of_its_own
    )
    monkeypatch.setattr(projector, "POLL_SECONDS", 0.05)
    # The store has one writer connection, so the commit finishes before the projector thread
    # opens its own.
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        commit(CollectionWriter(vault, handle), **put(NEW, b"# Gamma\n"))
    worker = server_runtime._start_vault_projector(vault)
    try:
        assert worker is not None
        deadline = time.monotonic() + 60
        while not (vault / NEW).exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert (vault / NEW).read_bytes() == b"# Gamma\n"
    finally:
        worker.stop()
    assert not worker._thread.is_alive()


def test_the_status_ages_an_intent_that_the_projector_deferred(stored, writer, projecting):
    """A status that drops a deferred intent from the age of the queue, so that a projection
    which cannot proceed looks like a healthy, empty queue."""
    commit(writer, **put(NEW, b"# Gamma\n"))
    projecting.run_pass()
    os.link(stored / NEW, stored.parent / "another-name-for-gamma")
    commit(writer, **put(NEW, b"# Gamma\n\nA second version.\n"))

    report = projecting.run_pass()

    assert report.deferred == {NEW: "IDENTITY_CHANGED"}, "a file with two names is never replaced"
    status = mode.status(stored)
    assert status["queued_projections"] == 1
    assert isinstance(status["oldest_projection_age_seconds"], int)
    assert status["oldest_projection_age_seconds"] >= 0


@pytest.mark.parametrize("ending", ["delete", "move"])
def test_a_put_never_replaces_an_earlier_version_that_a_person_restored(
    stored, writer, projecting, ending
):
    """A projector that counts the audit rows of a path's earlier lives as Exomem's own, so that it
    replaces a person's restored file with the store's newer bytes."""
    first = b"# Gamma\n\nThe first life of this page.\n"
    commit(writer, **put(NEW, first))
    projecting.run_pass()
    if ending == "delete":
        commit(writer, deletes=(write.PageDelete(NEW),))
    else:
        commit(writer, moves=(write.PageMove(NEW, f"{NOTES}/Elsewhere/Gamma.md"),))
    projecting.run_pass()
    assert not (stored / NEW).exists()
    (stored / NEW).write_bytes(first)
    commit(writer, **put(NEW, b"# Gamma\n\nThe second life of this page.\n"))

    report = projecting.run_pass()

    assert (report.held, report.projected) == (1, 0)
    assert (stored / NEW).read_bytes() == first
    assert rows(stored, "SELECT kind, body FROM store_held") == [("edit", first)]


def test_a_delete_never_removes_an_earlier_version_that_a_person_restored(
    stored, writer, projecting
):
    """A projector that unlinks a file whose bytes match an earlier life of the path, which a
    person restored, because it counts every audit row of the path as Exomem's own."""
    first = b"# Gamma\n\nThe first life of this page.\n"
    commit(writer, **put(NEW, first))
    projecting.run_pass()
    commit(writer, deletes=(write.PageDelete(NEW),))
    projecting.run_pass()
    (stored / NEW).write_bytes(first)
    commit(writer, **put(NEW, b"# Gamma\n\nThe second life of this page.\n"))
    commit(writer, deletes=(write.PageDelete(NEW),))

    report = projecting.run_pass()

    assert (report.removed, report.held) == (0, 0)
    assert (stored / NEW).read_bytes() == first


def test_a_put_never_replaces_a_version_that_a_person_restored_across_a_rollback(stored, tmp_path):
    """A projector that counts the writes from before a rollback as Exomem's own, so that it
    replaces a restored file with the store's newer bytes, though the vault ran in file mode and a
    person changed it in between."""
    first = b"# Gamma\n\nThe first version, written before the rollback.\n"
    path = connection.store_path(stored)
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        writing = CollectionWriter(stored, handle)
        commit(writing, **put(NEW, first))
        projector.Projector(stored, run=lambda work: work(writing)).run_pass()
    assert rollback_apply(stored, tmp_path / "export") == 0
    (stored / NEW).unlink()  # a person's delete, in file mode
    lexstore.delete_after_remove(stored, [NEW])  # the indexes follow the delete, as in file mode
    build_indexes(stored)
    assert migrate_apply(stored) == 0
    (stored / NEW).write_bytes(first)  # the person restores the first version
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        writing = CollectionWriter(stored, handle)
        commit(writing, **put(NEW, b"# Gamma\n\nA third version, written after the migration.\n"))

        report = projector.Projector(stored, run=lambda work: work(writing)).run_pass()

    assert (report.held, report.projected) == (1, 0)
    assert (stored / NEW).read_bytes() == first
    assert rows(stored, "SELECT kind, body FROM store_held") == [("edit", first)]


def test_the_top_folder_of_the_vault_is_made_durable_without_a_refusal(stored, writer, projecting):
    """A projector that asks the held filesystem for the folder "" when it creates the top folder,
    which the filesystem refuses, so that a page is deferred as an unsafe path."""
    shutil.rmtree(stored / "Knowledge Base")
    commit(writer, **put(NEW, b"# Gamma\n"))

    report = projecting.run_pass()

    assert (report.projected, report.deferred) == (1, {})
    assert (stored / NEW).read_bytes() == b"# Gamma\n"


def test_a_delete_removes_a_file_that_a_kill_left_unrecorded(stored, writer, projecting):
    """A projector that leaves the file of a deleted page on disk because its write was killed
    before the manifest heard of it, so the deleted page stays in the vault."""
    body = b"# Gamma\n\nPublished, and then the kill came.\n"
    commit(writer, **put(NEW, body))
    (stored / NEW).write_bytes(body)  # the file that the killed projection had installed
    commit(writer, deletes=(write.PageDelete(NEW),))

    report = projecting.run_pass()

    assert (report.removed, report.held) == (1, 0)
    assert not (stored / NEW).exists()


def test_a_file_that_exomem_removed_before_a_kill_is_written_again_not_held(
    stored, writer, projecting
):
    """A projector that holds a page whose file its own removal took away just before a kill, so
    that a page deleted and put again never reaches the disk."""
    commit(writer, **put(NEW, b"# Gamma\n\nThe first life of this page.\n"))
    projecting.run_pass()
    commit(writer, deletes=(write.PageDelete(NEW),))
    (stored / NEW).unlink()  # the kill came after this unlink and before the manifest row went
    second = b"# Gamma\n\nThe second life of this page.\n"
    commit(writer, **put(NEW, second))

    report = projecting.run_pass()

    assert (report.held, report.projected) == (0, 1)
    assert (stored / NEW).read_bytes() == second


def test_a_file_above_the_hold_limit_is_held_by_its_hash_without_a_copy_of_its_bytes(
    stored, writer, projecting, monkeypatch
):
    """A projector that reads an outside file whole into memory and into the store, or one that
    replaces a file that is too large to keep."""
    monkeypatch.setattr(projector, "HELD_BODY_LIMIT", len(PERSON) - 1)
    (stored / ALPHA).write_bytes(PERSON)
    commit(writer, **put(ALPHA, EDITED))

    assert projecting.run_pass().held == 1

    assert (stored / ALPHA).read_bytes() == PERSON
    assert rows(stored, "SELECT kind, body, sha256 FROM store_held") == [
        ("edit", None, hashlib.sha256(PERSON).hexdigest())
    ]


def test_a_move_over_a_persons_file_holds_the_new_path_and_keeps_the_old_file(
    stored, writer, projecting
):
    """A projector that removes the old file of a move before it has judged the new path, which
    leaves neither the page's old file nor the move's result."""
    drifter = b"# Drifter\n\nA page that moves.\n"
    commit(writer, **put(MOVE_FROM, drifter))
    projecting.run_pass()
    (stored / MOVE_TO).parent.mkdir(parents=True, exist_ok=True)
    (stored / MOVE_TO).write_bytes(PERSON)
    commit(writer, moves=(write.PageMove(MOVE_FROM, MOVE_TO),))

    report = projecting.run_pass()

    assert (report.held, report.removed) == (1, 0)
    assert (stored / MOVE_TO).read_bytes() == PERSON
    assert (stored / MOVE_FROM).read_bytes() == drifter
    assert rows(stored, "SELECT path, kind FROM store_held") == [(MOVE_TO, "edit")]
    assert len(rows(stored, "SELECT path FROM store_manifest WHERE path = ?", MOVE_FROM)) == 1


def test_a_refused_rename_leaves_no_new_folder_behind(stored, writer, projecting, monkeypatch):
    """A projector that makes the new folder of a move before it has checked the old file, so that
    a refused rename leaves an empty folder in a person's vault."""
    commit(writer, **put(MOVE_FROM, b"# Drifter\n"))
    projecting.run_pass()
    moved_to = f"{NOTES}/Fresh/Drifter.md"
    commit(writer, moves=(write.PageMove(MOVE_FROM, moved_to),))
    observe = projector.observe

    def a_person_replaces_the_file_after_it_was_judged(filesystem, rel, **options):
        seen = observe(filesystem, rel, **options)
        if rel == MOVE_FROM and seen is not None:
            (stored / MOVE_FROM).unlink()
            (stored / MOVE_FROM).write_bytes(PERSON)
        return seen

    monkeypatch.setattr(projector, "observe", a_person_replaces_the_file_after_it_was_judged)

    report = projecting.run_pass()

    assert report.deferred == {moved_to: "the file changed before it was renamed"}
    assert not (stored / NOTES / "Fresh").exists()


@posix_only_spy
def test_a_projection_flushes_every_folder_it_changes_before_the_manifest_records_it(
    stored, writer, projecting, monkeypatch
):
    """A projector that records a file in the manifest while the folder entry of the file, of an
    unlink, or of a folder that the projection made is still only in the page cache."""
    deep = f"{NOTES}/Fresh/Deeper/Page.md"
    flushed = []
    from exomem import _held_fs_posix

    flush_directory = _held_fs_posix.PosixHeldFilesystem.flush_directory

    def spy(self, directory):
        folder = os.readlink(f"/proc/self/fd/{directory.descriptor}")
        recorded = rows(stored, "SELECT 1 FROM store_manifest WHERE path = ?", deep)
        flushed.append((folder, bool(recorded)))
        return flush_directory(self, directory)

    monkeypatch.setattr(_held_fs_posix.PosixHeldFilesystem, "flush_directory", spy)
    commit(writer, **put(deep, b"# Page\n"))

    projecting.run_pass()

    root = stored.resolve()
    assert {folder for folder, _ in flushed} >= {
        str(root / NOTES / "Fresh" / "Deeper"),
        str(root / NOTES / "Fresh"),
        str(root / NOTES),
    }
    assert not any(recorded for _, recorded in flushed), "the manifest came before a flush"

    flushed.clear()
    commit(writer, deletes=(write.PageDelete(deep),))
    projecting.run_pass()
    assert [folder for folder, _ in flushed] == [str(root / NOTES / "Fresh" / "Deeper")]


@posix_only_spy
def test_a_retry_that_finds_the_file_in_place_flushes_its_folder_and_every_folder_above(
    stored, writer, projecting, monkeypatch
):
    """A projector whose retry trusts an earlier attempt's flush of the folders that attempt
    made, so that a kill between the install and the flush leaves a folder entry in the cache."""
    deep = f"{NOTES}/Fresh/Deeper/Page.md"
    body = b"# Page\n"
    commit(writer, **put(deep, body))
    (stored / deep).parent.mkdir(parents=True)
    (stored / deep).write_bytes(body)  # the first attempt installed it and died before a flush
    flushed = []
    from exomem import _held_fs_posix

    flush_directory = _held_fs_posix.PosixHeldFilesystem.flush_directory

    def spy(self, directory):
        flushed.append(
            (
                os.readlink(f"/proc/self/fd/{directory.descriptor}"),
                bool(rows(stored, "SELECT 1 FROM store_manifest WHERE path = ?", deep)),
            )
        )
        return flush_directory(self, directory)

    monkeypatch.setattr(_held_fs_posix.PosixHeldFilesystem, "flush_directory", spy)

    assert projecting.run_pass().projected == 1

    root = stored.resolve()
    assert {folder for folder, _ in flushed} >= {
        str(root / NOTES / "Fresh" / "Deeper"),
        str(root / NOTES / "Fresh"),
        str(root / NOTES),
        str(root / "Knowledge Base"),
        str(root),
    }
    assert not any(recorded for _, recorded in flushed), "the manifest came before a flush"


RELEASE_WITH_STORE_SCHEMA = "v0.167.0"
#: Run by the interpreter that imports the released code: it opens the store, which runs that
#: release's schema steps, then installs what its migration installs on an empty vault: the
#: owners' tables, their record in ``store_mode_objects`` and the store-mode row. Its route
#: itself needs the three index sidecars and a lease topology that a bare vault lacks.
CREATE_A_STORE_AS_RELEASED = """
import sys
from pathlib import Path

import exomem
from exomem.collection_store import connection
from exomem.vault_store import migrate

released_source, root = Path(sys.argv[1]), Path(sys.argv[2])
assert Path(exomem.__file__).resolve().is_relative_to(released_source), exomem.__file__
with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
    with handle.transaction() as tx:
        lexical, graph, refs = migrate.create_owner_schema(tx)
        tx.executemany(
            "INSERT INTO store_mode_objects(name, type) VALUES (?, ?)",
            list({**lexical, **graph, **refs}.items()),
        )
        tx.execute(
            "INSERT INTO store_mode(id, mode, revision, changed_at, release) "
            "VALUES (1, 'store', 1, '2026-11-08 00:00:00', ?)",
            (exomem.__version__,),
        )
"""


def test_a_store_that_the_released_code_created_takes_a_commit_a_pass_and_a_status(
    tmp_path, monkeypatch
):
    """A store schema step that a released version already ran is never rewritten, so a column
    that only this version's migration adds fails every commit and projector pass on such a
    store with "no such column"."""
    repository = Path(__file__).resolve().parents[1]
    tag = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--quiet",
            RELEASE_WITH_STORE_SCHEMA,
        ],
        capture_output=True,
        check=False,
    )
    if tag.returncode != 0:
        if os.environ.get("CI"):
            pytest.fail(f"CI must fetch the tag {RELEASE_WITH_STORE_SCHEMA}: use fetch-depth 0")
        pytest.skip(f"the tag {RELEASE_WITH_STORE_SCHEMA} is not in this clone")
    released_source = tmp_path / "released"
    released_source.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", RELEASE_WITH_STORE_SCHEMA, "src"],
        capture_output=True,
        check=True,
    ).stdout
    (tmp_path / "released.tar").write_bytes(archive)
    with tarfile.open(tmp_path / "released.tar") as tar:
        tar.extractall(released_source, filter="data")
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    source = (released_source / "src").resolve()
    subprocess.run(
        [sys.executable, "-c", CREATE_A_STORE_AS_RELEASED, str(source), str(root)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source)},
        check=True,
    )

    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        writing = CollectionWriter(root, handle)
        commit(writing, **put(NEW, b"# Gamma\n"))
        report = projector.Projector(root, run=lambda work: work(writing)).run_pass()
        commit(writing, **put(NEW, b"# Gamma\n\nA second version.\n"))

    assert (report.projected, report.deferred) == (1, {})
    assert (root / NEW).read_bytes() == b"# Gamma\n"
    monkeypatch.setattr(mode.state_migration, "vault_store_recorded", lambda _root: True)
    figures = mode.status(root)
    assert (figures["queued_projections"], "unreadable" in figures) == (1, False)


# -- the kill trial -------------------------------------------------------------------------------

FOLDERS = 20
PAGES_PER_FOLDER = 26


def bulk(folder, number):
    return f"{NOTES}/Bulk{folder:02d}/Page{number:03d}.md"


def body_of(rel, version=1):
    return f"# {rel}\n\nVersion {version} of a generated page.\n".encode()


def _project_and_die(environment, root, where, countdown):
    """One projector start in a fresh interpreter that the operating system kills partway through.

    ``publication`` kills it with a page's temporary written and its rename not yet done.
    ``manifest`` kills it after a page's file is installed and before the manifest row that
    records it.
    """
    os.environ.clear()
    os.environ.update(environment)
    seen = {"count": 0}

    def die():
        kill_self()

    if where == "publication":
        publish = held_fs.publish_bytes

        def dying_publish(*arguments, **keywords):
            seen["count"] += 1
            if seen["count"] == countdown:
                prepare = keywords.get("prepare")

                def hook(staged):
                    if prepare is not None:
                        prepare(staged)
                    die()

                keywords["prepare"] = hook
            return publish(*arguments, **keywords)

        held_fs.publish_bytes = dying_publish
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        if where == "manifest":

            def trace(statement):
                if statement.startswith("INSERT INTO store_manifest"):
                    seen["count"] += 1
                    if seen["count"] == countdown:
                        die()

            handle.connection.set_trace_callback(trace)
        starting = projector.Projector(root, run=lambda work: work(CollectionWriter(root, handle)))
        starting.sweep_temporaries()
        starting.run_pass()
    os._exit(0)


def die_in_a_child(root, where, countdown):
    child = multiprocessing.get_context("spawn").Process(
        target=_project_and_die, args=(dict(os.environ), root, where, countdown)
    )
    started = time.monotonic()
    child.start()
    child.join(300)
    assert child.exitcode == KILLED, "the child must be killed where the trial says"
    return time.monotonic() - started


def unrecorded(root):
    """The pages whose file holds the store's bytes while the manifest does not record them."""
    found = []
    with read.snapshot(root) as view:
        manifested = {
            path for (path,) in view.connection.execute("SELECT path FROM store_manifest")
        }
        for meta in view.pages(NOTES):
            file = root / meta.path
            if meta.path not in manifested and file.is_file():
                if hashlib.sha256(file.read_bytes()).hexdigest() == meta.sha256:
                    found.append(meta.path)
    return found


def test_a_killed_projector_drains_on_restart_to_identical_files_and_no_held_rows(stored):
    """A restart that reads a torn projection as a person's edit, leaves a temporary behind,
    leaves a page unprojected, or overwrites or removes a file without the store's bytes behind
    it."""
    pages = [
        bulk(folder, number) for folder in range(FOLDERS) for number in range(PAGES_PER_FOLDER)
    ]
    assert len(pages) >= 500
    timings = {"pages": len(pages)}
    path = connection.store_path(stored)
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        commit(
            CollectionWriter(stored, handle),
            puts=(
                *(write.PagePut(rel, body_of(rel)) for rel in pages),
                write.PagePut(ALPHA, EDITED),
            ),
            deletes=(write.PageDelete(BETA),),
        )

    timings["first_run_until_killed_in_a_publication_seconds"] = die_in_a_child(
        stored, "publication", 137
    )
    assert temporaries(stored), "the first kill must land inside a publication"
    timings["second_run_until_killed_before_a_manifest_row_seconds"] = die_in_a_child(
        stored, "manifest", 60
    )
    assert temporaries(stored) == [], "the start after the first kill must sweep its leftover"
    (replaced,) = unrecorded(stored)
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        # A newer revision of the page whose projection the kill left unrecorded.
        commit(CollectionWriter(stored, handle), **put(replaced, body_of(replaced, version=2)))
    timings["third_run_until_killed_before_a_manifest_row_seconds"] = die_in_a_child(
        stored, "manifest", 30
    )
    (deleted,) = unrecorded(stored)
    assert deleted != replaced
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        # A delete of the page whose projection the second kill left unrecorded.
        commit(CollectionWriter(stored, handle), deletes=(write.PageDelete(deleted),))

    with connection.open_writer(path, lease_check=lambda: True) as handle:
        restarted = projector.Projector(
            stored, run=lambda work: work(CollectionWriter(stored, handle))
        )
        began = time.monotonic()
        swept = restarted.sweep_temporaries()
        timings["restart_sweep_seconds"] = time.monotonic() - began
        began = time.monotonic()
        report = restarted.run_pass()
        timings["restart_drain_seconds"] = time.monotonic() - began
    timings["restart_swept_temporaries"] = swept
    timings["restart_projected"], timings["restart_removed"] = report.projected, report.removed

    assert temporaries(stored) == []
    assert report.held == 0 and report.deferred == {}
    assert rows(stored, "SELECT COUNT(*) FROM store_projection_queue") == [(0,)]
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    with read.snapshot(stored) as view:
        for meta in view.pages(NOTES):
            page = view.page(meta.path)
            assert (stored / meta.path).read_bytes() == page.body
            assert rows(stored, "SELECT sha256 FROM store_manifest WHERE path = ?", meta.path) == [
                (meta.sha256,)
            ]
    assert (stored / replaced).read_bytes() == body_of(replaced, version=2)
    assert not (stored / deleted).exists() and not (stored / BETA).exists()
    assert (stored / ALPHA).read_bytes() == EDITED
    destination = os.environ.get("EXOMEM_PROJECTOR_TIMINGS")
    if destination:
        with open(destination, "w") as record:
            json.dump(timings, record, indent=2, sort_keys=True)
