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

from exomem import extract, file_watcher, media_jobs, media_processing, media_store, media_worker
from exomem.collection_store import runtime
from exomem.governance.principal import library_scope
from exomem.init import init_vault
from exomem.vault_store import batch, importer, read

# The generated vault and the store thread are the importer suite's own fixtures.
coordinator_url = importer_suite.coordinator_url
vault = importer_suite.vault
stored = importer_suite.stored
store_thread = importer_suite.store_thread
selected_store = media_store_suite.selected_store

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

PICTURE = f"{NOTES}/plain.png"
PICTURE_BYTES = b"\x89PNG plain"
CHANGED_BYTES = b"\x89PNG a person replaced the picture"
MOVED = f"{NOTES}/moved.png"


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
    assert swept.media_recovered >= 1


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
def original_thread(vault, stored_original):
    thread = StoreThread(vault)
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
    # The service's one door to the store thread; a bare test has no running service to supply it.
    door = types.SimpleNamespace(
        call=lambda work, **_options: store_thread.run(lambda _writer: work())
    )
    monkeypatch.setattr(runtime, "_server", lambda _root: door)
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
