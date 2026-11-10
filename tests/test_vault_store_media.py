"""Store mode's media writer: an outside media binary gets its derived rows from the store thread,
and the media store's own eviction and restore are no outside change.

OpenSpec make-vault-store-canonical tasks 6.4 and 6.5. The vault is the migration suite's generated
vault, moved into store mode by ``store-mode migrate``. The importer, the store writer and the media
job ledger are real, and the thread that owns the writer binds it as the service's store thread
does. A fake extraction engine stands in where the media tests already use one. Each outcome is
read back from the store's rows, never from the importer's own report.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import types

import pytest
import test_media_store as media_store_suite
import test_vault_store_importer as importer_suite
from test_collection_store_migration import stop_writer
from test_media_store import _offload, _preserved
from test_vault_store_importer import StoreThread, rows, sha, wait_for
from test_vault_store_migration import build_indexes, migrate_apply
from test_vault_store_projector import NOTES

from exomem import (
    extract,
    file_watcher,
    media_jobs,
    media_processing,
    media_store,
    media_worker,
    reserved_paths,
)
from exomem.collection_store import runtime
from exomem.governance.principal import library_scope
from exomem.init import init_vault
from exomem.vault_store import batch, importer, read
from exomem.vault_store import media as store_media

# The generated vault is the importer suite's fixture; the media store's key is the sibling's.
coordinator_url = importer_suite.coordinator_url
vault = importer_suite.vault
stored = importer_suite.stored
selected_store = media_store_suite.selected_store

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

PICTURE = f"{NOTES}/plain.png"
PICTURE_BYTES = b"\x89PNG plain"
CHANGED_BYTES = b"\x89PNG a person replaced the picture"
MOVED = f"{NOTES}/moved.png"


def serve_through(monkeypatch, thread):
    """The service's one door to the store thread; a bare test has no running service for it."""
    door = types.SimpleNamespace(call=lambda work, **_options: thread.run(lambda _writer: work()))
    monkeypatch.setattr(runtime, "_server", lambda _root: door)


@pytest.fixture
def store_thread(stored, monkeypatch):
    thread = StoreThread(stored)
    serve_through(monkeypatch, thread)
    yield thread
    thread.close()


def sidecar_of(rel):
    return f"{rel}.md"


def stored_sidecar(root, rel):
    return read.owned_bytes(root, sidecar_of(rel))


VOLATILE = (b"exomem_id:", b"binary_mtime_ns:", b"binary_ctime_ns:")


def without_volatile(page):
    """A page without the fields that differ between two derivations of equal bytes: the id that
    each draws afresh and the stat times of the file that each reads."""
    return b"\n".join(line for line in page.split(b"\n") if not line.startswith(VOLATILE))


def commit_threads(monkeypatch):
    """The thread of every governed batch that the store commits, recorded as it commits."""
    seen = []
    commit = batch.commit_batch

    def recording(*arguments, **keywords):
        seen.append(threading.get_ident())
        return commit(*arguments, **keywords)

    monkeypatch.setattr(batch, "commit_batch", recording)
    return seen


@pytest.fixture
def file_mode_sidecar(vault, tmp_path):
    """The sidecar that file mode derives for ``PICTURE_BYTES``, from a second file-mode vault."""
    other = tmp_path / "file-mode-vault"
    init_vault(other)
    (other / PICTURE).parent.mkdir(parents=True, exist_ok=True)
    (other / PICTURE).write_bytes(PICTURE_BYTES)
    with library_scope():
        media_processing.reconcile_media(other, other / PICTURE, explicit=False)
    return (other / sidecar_of(PICTURE)).read_bytes()


def test_an_outside_new_or_changed_media_file_gets_its_rows_from_the_store_thread(
    file_mode_sidecar, stored, store_thread, monkeypatch
):
    """A store-mode importer that leaves an outside media file with no sidecar page and no job, that
    writes the page from the watcher's thread (which the store refuses), or derives other rows than
    file mode derives for the same bytes."""
    threads = commit_threads(monkeypatch)
    settling = importer.Importer(stored, run=store_thread.run)
    (stored / PICTURE).write_bytes(PICTURE_BYTES)

    report = settling.import_paths([PICTURE])

    assert without_volatile(stored_sidecar(stored, PICTURE)) == without_volatile(file_mode_sidecar)
    assert media_jobs.MediaJobStore(stored).get_by_binary(stored / PICTURE) is not None
    assert threads and set(threads) == {store_thread.ident}
    assert report.media_derived == [PICTURE] and report.media_failed == {}

    (stored / PICTURE).write_bytes(CHANGED_BYTES)
    report = settling.import_paths([PICTURE])

    assert sha(CHANGED_BYTES).encode() in stored_sidecar(stored, PICTURE)
    assert sha(PICTURE_BYTES).encode() not in stored_sidecar(stored, PICTURE)
    assert set(threads) == {store_thread.ident}
    assert report.imported == [PICTURE] and report.media_derived == [PICTURE]


def test_an_outside_rename_and_delete_settle_the_binary_record_and_keep_the_extracted_page(
    stored, store_thread
):
    """A rename that leaves the new path with no sidecar page, a delete that drops the page that
    holds the file's extracted text, or a record that still names a path with no file."""
    settling = importer.Importer(stored, run=store_thread.run)
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    settling.import_paths([PICTURE])

    (stored / PICTURE).rename(stored / MOVED)
    report = settling.import_paths([PICTURE, MOVED], {MOVED: PICTURE})

    assert rows(stored, "SELECT path FROM store_binaries WHERE path IN (?, ?)", PICTURE, MOVED) == [
        (MOVED,)
    ]
    assert sha(PICTURE_BYTES).encode() in stored_sidecar(stored, MOVED)
    assert report.moved == {MOVED: PICTURE} and report.media_derived == [MOVED]
    # The old page stays, as file mode keeps it: it may hold text that a person edited.
    assert stored_sidecar(stored, PICTURE)

    (stored / MOVED).unlink()
    report = settling.import_paths([MOVED])

    assert rows(stored, "SELECT path FROM store_binaries WHERE path = ?", MOVED) == []
    assert stored_sidecar(stored, MOVED)
    assert report.removed == [MOVED] and report.media_derived == []


def test_the_sweep_derives_the_rows_that_a_failed_derivation_left_missing(
    stored, store_thread, monkeypatch
):
    """A binary whose derivation failed once and that no event names again: its record is current
    and the sweep finds it unchanged, so without a recovery pass it never gets a sidecar page."""
    settling = importer.Importer(stored, run=store_thread.run)
    real = media_processing.reconcile_media
    fail = [True]

    def unreliable(*arguments, **keywords):
        if fail[0]:
            raise media_processing.MediaProcessingError("MEDIA_NOT_FOUND", "the engine was down")
        return real(*arguments, **keywords)

    monkeypatch.setattr(media_processing, "reconcile_media", unreliable)
    (stored / PICTURE).write_bytes(PICTURE_BYTES)

    failed = settling.import_paths([PICTURE])

    assert rows(stored, "SELECT path FROM store_binaries WHERE path = ?", PICTURE) != []
    assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", sidecar_of(PICTURE)) == []

    fail[0] = False
    swept = settling.sweep()

    assert stored_sidecar(stored, PICTURE)
    assert rows(stored, "SELECT path FROM store_binaries WHERE path = ?", PICTURE) != []
    assert failed.media_failed == {PICTURE: "MEDIA_NOT_FOUND"} and failed.media_derived == []
    assert swept.media_attempted >= 1


@pytest.fixture
def stored_original(vault, selected_store):
    """An offloaded original that file mode preserved, in a vault that then entered store mode."""
    path = _preserved(vault)
    _offload(vault, path)
    stop_writer()  # file mode's writes started this process's writer; the offline route refuses it
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    return path.relative_to(vault).as_posix()


@pytest.fixture
def original_thread(vault, stored_original, monkeypatch):
    thread = StoreThread(vault)
    serve_through(monkeypatch, thread)
    yield thread
    thread.close()


def media_state(root, rel):
    """Every row that a media binary has in the store, and the revision that the store stands at."""
    return {
        "binary": rows(root, "SELECT * FROM store_binaries WHERE path = ?", rel),
        "sidecar": rows(root, "SELECT * FROM store_pages WHERE path = ?", sidecar_of(rel)),
        "held": rows(root, "SELECT * FROM store_held"),
        "revision": read.current_revision(root),
    }


def test_evicting_and_restoring_an_original_is_no_outside_change(
    vault, stored_original, original_thread
):
    """An importer that reads an eviction as an outside delete (it drops the binary record and the
    store then holds the sidecar's text with no file) or a restore as an outside create."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    assert rows(vault, "SELECT path FROM store_binaries WHERE path = ?", rel) != []
    settling.sweep()
    before = media_state(vault, rel)

    assert media_store.evict(vault, vault / rel)
    assert not (vault / rel).exists()
    evicted = settling.sweep()
    named = settling.import_paths([rel])

    assert evicted.removed == [] and named.removed == []
    assert media_state(vault, rel) == before

    assert media_store.hydrate_evicted(vault, vault / rel)
    restored = settling.sweep()
    named = settling.import_paths([rel])

    assert restored.imported == [] and named.imported == []
    assert media_state(vault, rel) == before


def test_a_new_original_that_a_store_mode_vault_offloads_can_then_be_evicted_and_restored(
    stored, selected_store, store_thread
):
    """An offload that reads the sidecar back from a Markdown file that store mode has not
    projected: it commits the reference, then raises and leaves the original unfinished (cached,
    its protection kept, no proof that the cache matches), so the eviction has nothing to rest on.
    """
    settling = importer.Importer(stored, run=store_thread.run)
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    settling.import_paths([PICTURE])

    reference = _offload(stored, stored / PICTURE)

    assert reference is not None
    assert reference.object.encode() in stored_sidecar(stored, PICTURE)
    assert media_store.original_state(stored, PICTURE).state == "cached"
    assert media_store.cache_matches(stored, stored / PICTURE)
    with sqlite3.connect(media_jobs.job_store_path(stored)) as ledger:
        assert ledger.execute("SELECT count(*) FROM media_protections").fetchone() == (0,)
    settling.sweep()
    before = media_state(stored, PICTURE)

    assert media_store.evict(stored, stored / PICTURE)
    assert not (stored / PICTURE).exists()
    evicted = settling.sweep()

    assert evicted.removed == [] and media_state(stored, PICTURE) == before

    assert media_store.hydrate_evicted(stored, stored / PICTURE)
    restored = settling.sweep()

    assert restored.imported == [] and media_state(stored, PICTURE) == before
    assert before["held"] == []


def test_a_media_result_that_lands_while_the_store_is_busy_commits_on_the_store_thread(
    stored, store_thread, monkeypatch
):
    """A worker whose result commits from the worker's own thread (the store refuses it, and the
    extracted text never reaches the page) or that commits ahead of the write queued before it."""
    settling = importer.Importer(stored, run=store_thread.run)
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    settling.import_paths([PICTURE])
    jobs = media_jobs.MediaJobStore(stored)
    job = jobs.claim_next()
    assert job is not None and job.binary_path == stored / PICTURE
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="Words read from the picture.", media_type="image", engine="test"
        ),
    )
    threads = commit_threads(monkeypatch)
    worker = media_worker.MediaWorker(stored, execution_mode="process")
    worker._process(job)
    assert jobs.pending_result_count() == 1

    gate = threading.Event()
    busy = threading.Thread(target=store_thread.run, args=(lambda _writer: gate.wait(60),))
    busy.start()
    publishing = threading.Thread(target=worker._drain_parent_results)
    publishing.start()
    publishing.join(0.5)
    still_waiting = publishing.is_alive()
    gate.set()
    busy.join(60)
    publishing.join(60)

    assert still_waiting and not publishing.is_alive()
    assert jobs.pending_result_count() == 0
    assert b"Words read from the picture." in stored_sidecar(stored, PICTURE)
    assert threads and set(threads) == {store_thread.ident}


def test_a_running_watcher_derives_the_rows_of_a_media_file_that_a_person_adds(
    stored, store_thread, caplog
):
    """A watcher that calls media processing from its own thread, which the store refuses: the log
    shows a traceback for every binary a person adds and the file never gets its sidecar page."""
    caplog.set_level(logging.INFO, logger="exomem.file_watcher")
    watching = file_watcher.FileWatcher(
        stored, debounce_seconds=0.2, importer=importer.Importer(stored, run=store_thread.run)
    )
    assert watching.start()
    try:
        assert watching.wait_until_seeded(120)
        (stored / PICTURE).write_bytes(PICTURE_BYTES)
        sidecar_rows = "SELECT path FROM store_pages WHERE path = ?"
        wait_for(
            lambda: rows(stored, sidecar_rows, sidecar_of(PICTURE)), "the picture's sidecar page"
        )
    finally:
        watching.stop()

    assert rows(stored, "SELECT path FROM store_binaries WHERE path = ?", PICTURE) != []
    assert media_jobs.MediaJobStore(stored).get_by_binary(stored / PICTURE) is not None
    assert [record for record in caplog.records if record.exc_info or record.levelno >= 40] == []


def test_a_hard_linked_or_symlinked_binary_is_judged_as_it_is_without_a_media_store(
    vault, stored_original, original_thread
):
    """An importer that reads a binary that the media store cannot inspect as a file in flight: it
    records no hard link, never says why it refused a symlink, and asks the watcher to look again,
    for ever."""
    settling = importer.Importer(vault, run=original_thread.run)
    outside = vault / "outside.png"
    outside.write_bytes(PICTURE_BYTES)
    linked, symlinked = f"{NOTES}/linked.png", f"{NOTES}/symlinked.png"
    os.link(outside, vault / linked)
    os.symlink(outside, vault / symlinked)

    report = settling.import_paths([linked, symlinked])

    assert rows(vault, "SELECT path FROM store_binaries WHERE path = ?", linked) != []
    assert rows(vault, "SELECT path FROM store_binaries WHERE path = ?", symlinked) == []
    assert report.unjudged == {symlinked: "UNSAFE_PATH"}
    assert report.deferred == [] and report.managed == []


def test_a_media_ledger_that_cannot_be_read_keeps_the_record_of_a_missing_original(
    vault, stored_original, original_thread
):
    """An importer that reads an eviction as a delete when the ledger that would say so is corrupt,
    or that stops importing a changed original because of it."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    before = media_state(vault, rel)
    media_jobs.job_store_path(vault).write_bytes(b"this is not a database")
    original = (vault / rel).read_bytes()
    (vault / rel).unlink()

    report = settling.import_paths([rel])

    assert report.removed == [] and report.deferred == [] and rel in report.unjudged
    assert media_state(vault, rel) == before

    (vault / rel).write_bytes(original + b" and a person's edit")
    report = settling.import_paths([rel])

    assert report.imported == [rel] and report.unjudged == {}
    assert media_state(vault, rel)["binary"] != before["binary"]


def test_a_person_renaming_a_stored_original_moves_its_record_by_hash(
    vault, stored_original, original_thread
):
    """An importer that sets a stored original aside as evicted before it pairs the rename: the old
    record stays, the new path imports as a second binary, and nothing joins them."""
    rel = stored_original
    moved = f"{NOTES}/renamed-sample.txt"
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    (vault / rel).rename(vault / moved)

    report = settling.sweep()

    assert rows(vault, "SELECT path FROM store_binaries WHERE path IN (?, ?)", rel, moved) == [
        (moved,)
    ]
    assert report.moved == {moved: rel} and report.managed == []


def test_a_media_store_that_cannot_be_inspected_keeps_the_record_of_a_missing_original(
    vault, stored_original, original_thread, monkeypatch
):
    """An importer that reads a missing original as deleted when the media store fails to inspect
    it for a reason other than its ledger, and so drops the record that an eviction would keep."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    before = media_state(vault, rel)
    (vault / rel).unlink()

    def unreadable(_root, _path):
        raise OSError("input/output error")

    monkeypatch.setattr(media_store, "managed_event", unreadable)
    report = settling.import_paths([rel])

    assert report.removed == [] and rel in report.unjudged
    assert media_state(vault, rel) == before


def test_a_copy_that_took_an_evicted_originals_record_leaves_the_restored_original_its_own(
    vault, stored_original, original_thread
):
    """An importer that trusts the media store's cache for a file that has no record: a copy added
    during an eviction pairs as a rename and takes the record, and the restored original then reads
    as the media store's own bytes at every sweep and never gets a record back."""
    rel = stored_original
    copy = f"{NOTES}/copy-of-stored-original.txt"
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    original = (vault / rel).read_bytes()
    assert media_store.evict(vault, vault / rel)
    (vault / copy).write_bytes(original)

    assert settling.sweep().moved == {copy: rel}

    assert media_store.hydrate_evicted(vault, vault / rel)
    settling.sweep()
    settling.sweep()

    assert rows(
        vault, "SELECT path FROM store_binaries WHERE path IN (?, ?) ORDER BY path", rel, copy
    ) == sorted([(rel,), (copy,)])


def test_a_path_that_a_media_store_read_holds_open_is_left_for_the_watcher_to_look_at_again(
    vault, stored_original, original_thread
):
    """An importer that settles a stored original while a read of it is in flight, or that forgets
    the path so that the edit made meanwhile never imports."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    before = media_state(vault, rel)
    edited = (vault / rel).read_bytes() + b" and a person's edit"

    # The read itself fails when it closes, because the file changed under it.
    with pytest.raises(reserved_paths.ReservedPathLeafError), media_store.open_original(
        vault, vault / rel
    ):
        (vault / rel).write_bytes(edited)
        report = settling.import_paths([rel])

        assert report.deferred == [rel] and report.imported == []
        assert media_state(vault, rel) == before

    assert settling.import_paths([rel]).imported == [rel]


def test_a_commit_on_the_stores_own_thread_never_hops_to_its_own_queue(stored, monkeypatch):
    """A commit that runs on the store's server thread after it dropped its writer binding (a create
    that left the store pending does) would queue behind itself and wait for ever."""
    assert store_media.off_store_thread(stored)

    server = types.SimpleNamespace(thread=threading.current_thread())
    monkeypatch.setitem(runtime._SERVERS, stored.resolve(), server)

    assert not store_media.off_store_thread(stored)
