"""Preview recovery through the filing write, durable ledger and process worker."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_vault_store_media as store_suite
from test_client_artifacts import _staged
from test_image_view import _photo
from test_vault_store_projector import NOTES

from exomem import (
    client_artifacts,
    file_watcher,
    media_engines,
    media_jobs,
    media_previews,
    media_processing,
    media_store,
    media_worker,
    originals,
    preserve,
    private_state,
    reserved_paths,
    schema,
    server_runtime,
    state_paths,
)
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope
from exomem.vault import parse_frontmatter
from exomem.vault_store import importer, read
from exomem.vault_store import media as store_media
from exomem.writer_lease import get_manager

coordinator_url = store_suite.coordinator_url
vault = store_suite.vault
stored = store_suite.stored
store_thread = store_suite.store_thread
pytestmark = [
    pytest.mark.frame_codecs,
    pytest.mark.skipif(os.name != "posix", reason="the codec child requires POSIX resource limits"),
]


@pytest.fixture(autouse=True)
def _preview_settings(monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setenv("EXOMEM_MEDIA_WORKER_MODE", "process")
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "previews")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")


def test_store_worker_publishes_and_rebuilds_a_missing_preview(stored, store_thread):
    relative = f"{NOTES}/preview-worker-store-fixture.jpg"
    original = _photo(900, 600)
    (stored / relative).write_bytes(original)
    settling = importer.Importer(stored, run=store_thread.run)
    assert settling.import_paths([relative]).media_derived == [relative]
    sidecar = f"{relative}.md"
    captured_page = read.owned_bytes(stored, sidecar)
    assert captured_page is not None
    ledger = media_jobs.MediaJobStore(stored)
    pending = ledger.get_by_binary(stored / relative, media_type=media_jobs.PREVIEW_MEDIA_TYPE)
    assert pending is not None and pending.state == media_jobs.PENDING

    def run_worker():
        child = subprocess.run(
            [sys.executable, "-m", "exomem.media_worker_child", "--vault", str(stored),
             "--parent-pid", str(os.getpid()), "--idle-seconds", "0.1"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        assert child.returncode == 0, child.stderr

    run_worker()
    settled = ledger.get(pending.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is None
    digest = hashlib.sha256(original).hexdigest()
    assert media_previews.read(stored, relative, digest) is not None
    assert read.owned_bytes(stored, sidecar) == captured_page

    # Restore can keep the successful ledger receipt without the rebuildable preview payload.
    preview_state = state_paths.vault_state_dir(stored) / media_previews.STORE
    record, = preview_state.glob("*/*.json")
    record.unlink()
    assert media_previews.read(stored, relative, digest) is None
    media_processing.reconcile_all_media(stored)
    rebuilt = ledger.get(pending.id)
    assert rebuilt is not None and rebuilt.state == media_jobs.PENDING and rebuilt.preview_sha256 == digest
    run_worker()
    assert media_previews.read(stored, relative, digest) is not None
    assert read.owned_bytes(stored, sidecar) == captured_page


def test_reconciliation_recovers_intent_after_filing_survives_a_held_ledger(vault, monkeypatch):
    with monkeypatch.context() as disabled, library_scope():
        disabled.setenv("EXOMEM_MEDIA_ENGINES", "")
        old = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-historical-recovery-fixture.jpg",
            stream=io.BytesIO(_photo(800, 500)),
        )

    def unavailable(*_args):
        raise sqlite3.OperationalError("database is locked")

    original = _photo(901, 601)
    with monkeypatch.context() as blocked, library_scope():
        blocked.setattr(media_jobs.MediaJobStore, "_initialize", unavailable)
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-recover-demand-fixture.jpg",
            stream=io.BytesIO(original),
        )
    assert (vault / saved.path).read_bytes() == original
    assert any("Preview admission deferred" in text for text in saved.warnings)
    assert not media_jobs.job_store_path(vault).exists()

    media_processing.reconcile_all_media(vault)
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claimed = ledger.claim_next()
    assert claimed is not None and claimed.binary_path == vault / saved.path
    assert claimed.preview_sha256 == hashlib.sha256(original).hexdigest()
    assert ledger.get_by_binary(vault / old.path, media_type=media_jobs.PREVIEW_MEDIA_TYPE) is None
    assert ledger.get_by_binary(vault / saved.path) is None


def test_busy_publication_settles_at_the_attempt_cap_without_re_admission(vault, monkeypatch):
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-terminal-budget-fixture.jpg",
            stream=io.BytesIO(_photo(900, 600)),
        )
    sidecar = vault / saved.sidecar_path
    captured_page = sidecar.read_text(encoding="utf-8")
    ledger = media_jobs.MediaJobStore(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    manager = get_manager()
    original_guard = manager.consistency_guard
    refused = []

    @contextmanager
    def guard(*args, **kwargs):
        if kwargs.get("operation") == "media_preview_publish":
            refused.append(1)
            raise OpError("MUTATION_BUSY", "boundary busy", "Retry preview.")
        with original_guard(*args, **kwargs) as held:
            yield held

    now = time.time()
    monkeypatch.setattr(media_jobs, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(manager, "consistency_guard", guard)
    for attempt in range(1, media_jobs.MAX_PREVIEW_ATTEMPTS + 1):
        job = ledger.claim_next()
        assert job is not None and job.attempts == attempt
        worker._process(job)
        assert ledger.claim_next() is None
        now += media_jobs.RETRY_FIRST_SECONDS * 2 ** (attempt - 1)
    assert len(refused) == 3
    current = ledger.get_by_binary(vault / saved.path, media_type=media_jobs.PREVIEW_MEDIA_TYPE)
    assert current is not None and current.state == media_jobs.COMPLETED and current.last_error
    assert sidecar.read_text(encoding="utf-8") == captured_page
    media_processing.reconcile_all_media(vault)
    assert ledger.claim_next() is None
    captured_fields, captured_body, _ = parse_frontmatter(captured_page)
    current_fields, current_body, _ = parse_frontmatter(sidecar.read_text(encoding="utf-8"))
    assert current_body == captured_body
    assert originals.binary_binding(current_fields) == originals.binary_binding(captured_fields)
    assert media_previews.requested_digest(current_fields, saved.path) == current.preview_sha256


def test_global_media_disable_stamps_no_preview_intent(vault, monkeypatch):
    monkeypatch.setenv(media_engines.RUNTIME_DISABLE_ENV, "1")
    assert media_engines.status()[media_engines.PREVIEWS] == media_engines.UNAVAILABLE
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-runtime-off-fixture.jpg",
            stream=io.BytesIO(_photo(900, 600)),
        )
    assert (vault / saved.path).is_file()
    ledger = media_jobs.MediaJobStore(vault)
    assert ledger.next_pending() is None
    monkeypatch.delenv(media_engines.RUNTIME_DISABLE_ENV)
    media_processing.reconcile_all_media(vault)
    assert ledger.next_pending() is None


def test_source_capture_commits_preview_demand_before_queueing(vault, tmp_path, monkeypatch):
    original = _photo(920, 640)
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda file, _budget, **_kwargs: _staged(
        tmp_path, file["file_id"], "preview-source-capture-fixture.jpg", original,
    ))
    # An interrupted post-commit callback leaves discovery responsible for admission.
    with monkeypatch.context() as missed, library_scope():
        missed.setattr(client_artifacts, "_schedule_media", lambda *_args, **_kwargs: None)
        outcome = client_artifacts.capture_source_artifacts(
            vault,
            source_schema=schema.load_source_schema(vault),
            title="Studio reference photo",
            source_type="research-report",
            files=[{"download_url": "https://files.example/reference.jpg", "file_id": "preview-source"}],
        )["files"][0]
    assert outcome["outcome"] == "stored"
    relative = outcome["stored_path"]
    assert (vault / relative).read_bytes() == original
    digest = hashlib.sha256(original).hexdigest()
    page = (vault / outcome["page"]).read_text(encoding="utf-8")
    assert media_previews.requested_digest(parse_frontmatter(page)[0], relative) == digest

    media_processing.reconcile_all_media(vault)
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claimed = ledger.claim_next()
    assert claimed is not None and claimed.binary_path == vault / relative and claimed.preview_sha256 == digest


@pytest.mark.parametrize(("sweep", "prose"), [("startup", False), ("periodic", False), ("periodic", True)])
def test_discovery_queues_a_first_companion_after_the_watcher_event_was_missed(vault, monkeypatch, sweep, prose):
    watcher = file_watcher.FileWatcher(vault)
    if sweep == "periodic":
        watcher._reconcile_once(seed=True)
    original = _photo(950, 650)
    binary = vault / NOTES / "preview-first-companion-fixture.jpg"
    binary.write_bytes(original)
    if prose:
        binary.with_suffix(".jpg.md").write_text("A caption from the original filing.\n", encoding="utf-8")
    assert originals.bound_companion(vault, binary.relative_to(vault).as_posix()) is None

    if sweep == "startup":
        # Keep the worker from consuming the claim so the test can read the durable queue.
        worker = SimpleNamespace(start=lambda: None, requeue_runtime_blocks=lambda: None, scan_pending=lambda: None)
        monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _vault: worker)
        assert server_runtime._start_media_worker(vault) is worker
    else:
        watcher._reconcile_once(seed=False)
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claimed = ledger.claim_next()
    assert claimed is not None and claimed.binary_path == binary
    digest = hashlib.sha256(original).hexdigest()
    assert claimed.preview_sha256 == digest
    fields, body, _ = parse_frontmatter(claimed.sidecar_path.read_text(encoding="utf-8"))
    assert media_previews.requested_digest(fields, binary.relative_to(vault).as_posix()) == digest
    if prose:
        assert "A caption from the original filing." in body


def test_restart_acknowledges_a_ready_record_on_the_final_attempt(vault):
    original = _photo(910, 630)
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-interrupted-final-attempt.jpg",
            stream=io.BytesIO(original),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    for _ in range(media_jobs.MAX_PREVIEW_ATTEMPTS - 1):
        previous = ledger.claim_next()
        assert previous is not None
        assert ledger.recover_interrupted() == 1

    claim = ledger.claim_next()
    assert claim is not None and claim.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
    assert media_previews.generate(vault, claim, ledger, deadline=None)
    # Stop after the private record is installed, before the worker's ledger acknowledgement.
    assert ledger.recover_interrupted() == 1
    acknowledgement = ledger.claim_next()
    assert acknowledgement is not None and acknowledgement.id == claim.id
    assert acknowledgement.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
    media_worker.MediaWorker(vault, execution_mode="process")._process(acknowledgement)
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is None
    assert media_previews.read(vault, saved.path, hashlib.sha256(original).hexdigest()) is not None


def test_retryable_hydration_contention_keeps_preview_pending(vault, monkeypatch):
    original = _photo(900, 640)
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-hydration-retry-fixture.jpg",
            stream=io.BytesIO(original),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    claim = ledger.claim_next()
    assert claim is not None

    def hydration_busy(*_args, **_kwargs):
        raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "hydration capacity is in use")

    now = time.time()
    monkeypatch.setattr(media_jobs, "time", SimpleNamespace(time=lambda: now))
    with monkeypatch.context() as busy:
        busy.setattr(media_store, "open_original", hydration_busy)
        worker._process(claim)
    returned = ledger.get(claim.id)
    assert returned is not None and returned.state == media_jobs.PENDING and returned.attempts == 0
    assert ledger.claim_next() is None
    now += media_jobs.RETRY_FIRST_SECONDS
    retried = ledger.claim_next()
    assert retried is not None and retried.attempts == 1
    worker._process(retried)
    assert media_previews.read(vault, saved.path, hashlib.sha256(original).hexdigest()) is not None


@pytest.mark.parametrize("cleanup", ["sweep", "removal"])
def test_cleanup_batches_store_companions_and_preserves_uncertain_or_replaced_paths(stored, store_thread, monkeypatch, cleanup):
    samples = {
        f"{NOTES}/preview-healthy-cleanup-fixture.jpg": _photo(900, 600),
        f"{NOTES}/preview-directory-cleanup-fixture.jpg": _photo(901, 600),
        f"{NOTES}/preview-restored-cleanup-fixture.jpg": _photo(902, 600),
    }
    healthy, directory, restored = samples
    for relative, data in samples.items():
        (stored / relative).write_bytes(data)
    assert set(importer.Importer(stored, run=store_thread.run).import_paths(list(samples)).media_derived) == set(samples)
    ledger = media_jobs.MediaJobStore(stored, create=False)
    worker = media_worker.MediaWorker(stored, execution_mode="process")
    for _ in samples:
        claim = ledger.claim_next()
        assert claim is not None
        worker._process(claim)
    for relative, data in samples.items():
        assert media_previews.read(stored, relative, hashlib.sha256(data).hexdigest()) is not None

    (stored / directory).unlink()
    (stored / directory).mkdir()
    (stored / restored).unlink()

    manager = get_manager()
    original_guard = manager.consistency_guard
    original_bodies = read.StoreSnapshot.bodies
    original_owned_page = read.owned_page
    expected_companions = {f"{relative}.md" for relative in samples}
    batches = []

    def owned_page(root, relative):
        if relative in expected_companions:
            pytest.fail("cleanup must use its bounded companion batch")
        return original_owned_page(root, relative)

    def bodies(snapshot, paths):
        paths = tuple(paths)
        batches.append(set(paths))
        contents = original_bodies(snapshot, paths)
        if f"{healthy}.md" in contents:
            contents[f"{healthy}.md"] = b"\xff"  # An unreadable companion does not prove removal.
        return contents

    @contextmanager
    def guard(*args, **kwargs):
        if kwargs.get("operation") == "media_preview_retire":
            (stored / restored).write_bytes(samples[restored])
        with original_guard(*args, **kwargs) as held:
            yield held

    monkeypatch.setattr(read.StoreSnapshot, "bodies", bodies)
    monkeypatch.setattr(read, "owned_page", owned_page)
    monkeypatch.setattr(manager, "consistency_guard", guard)
    if cleanup == "sweep":
        media_previews.reconcile(stored)
    else:
        store_media.after_removal(stored, samples)
    expected = [expected_companions]
    if cleanup == "sweep":
        expected.append({f"{directory}.md", f"{restored}.md"})
    assert [batch & expected_companions for batch in batches] == expected
    assert media_previews.read(stored, directory, hashlib.sha256(samples[directory]).hexdigest()) is None
    for relative in (healthy, restored):
        assert media_previews.read(stored, relative, hashlib.sha256(samples[relative]).hexdigest()) is not None


@pytest.mark.parametrize("fault", ["read", "flush"])
def test_uncertain_final_acknowledgement_retries_without_new_codec_work(vault, monkeypatch, fault):
    original = _photo(911, 631)
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-ack-retry-fixture.jpg",
            stream=io.BytesIO(original),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    for _ in range(media_jobs.MAX_PREVIEW_ATTEMPTS - 1):
        assert ledger.claim_next() is not None
        assert ledger.recover_interrupted() == 1
    claim = ledger.claim_next()
    assert claim is not None and claim.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
    assert media_previews.generate(vault, claim, ledger, deadline=None)
    now = time.time()
    monkeypatch.setattr(media_jobs, "time", SimpleNamespace(time=lambda: now))

    failures = []

    def unavailable(*_args, **_kwargs):
        failures.append(fault)
        raise OSError("temporary preview storage fault")

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("receipt recovery must not open, hydrate or decode the original")

    monkeypatch.setattr(media_store, "open_original", unexpected_open)
    with monkeypatch.context() as blocked:
        if fault == "read":
            blocked.setattr(media_previews, "_load", unavailable)
        else:
            blocked.setattr(private_state, "sync_directory", unavailable)
        assert ledger.recover_interrupted() == 1
        waiting = ledger.get(claim.id)
        assert waiting is not None and waiting.state == media_jobs.PENDING

        ordinary_id = ledger.enqueue(media_jobs.MediaJob(
            claim.binary_path, claim.sidecar_path, "image", do_ocr=False, do_clip=True,
        ))
        ordinary = ledger.claim_next()
        assert ordinary is not None and ordinary.id == ordinary_id
        ledger.complete(ordinary)
        assert not failures

        acknowledgement = ledger.claim_next()
        assert acknowledgement is not None and acknowledgement.id == claim.id
        worker._process(acknowledgement)
        assert failures == [fault]
        waiting = ledger.get(claim.id)
        assert waiting is not None and waiting.state == media_jobs.PENDING
        assert waiting.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
        assert ledger.claim_next() is None

    now += media_jobs.RETRY_FIRST_SECONDS
    retry = ledger.claim_next()
    assert retry is not None and retry.id == claim.id
    worker._process(retry)
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is None
    assert settled.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
    digest = hashlib.sha256(original).hexdigest()
    assert media_previews.read(vault, saved.path, digest) is not None

    record, = media_previews.store_path(vault).glob("*/*.json")
    record.unlink()
    media_processing.reconcile_all_media(vault)
    rebuild = ledger.get(claim.id)
    assert rebuild is not None and rebuild.state == media_jobs.PENDING and rebuild.preview_sha256 == digest


@pytest.mark.parametrize("fault", ["reuse", "install"])
def test_generation_preserves_an_installed_preview_during_a_read_fault(vault, monkeypatch, fault):
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-read-fault-fixture.jpg",
            stream=io.BytesIO(_photo(920, 640)),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    first = ledger.claim_next()
    assert first is not None
    assert media_previews.generate(vault, first, ledger, deadline=None)
    record, = media_previews.store_path(vault).glob("*/*.json")
    installed = record.read_bytes(), record.stat().st_ino
    assert ledger.recover_interrupted() == 1

    now = time.time()
    monkeypatch.setattr(media_jobs, "time", SimpleNamespace(time=lambda: now))
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    manager = get_manager()
    original_guard = manager.consistency_guard
    original_load = media_previews._load
    publishing = False
    unavailable = False

    @contextmanager
    def guard(*args, **kwargs):
        nonlocal publishing, unavailable
        with original_guard(*args, **kwargs) as held:
            operation = kwargs.get("operation")
            if operation == "media_preview_claim":
                unavailable = False
            publishing = operation == "media_preview_publish"
            try:
                yield held
            finally:
                publishing = False

    def load(path):
        nonlocal unavailable
        if path == record and (fault == "reuse" or publishing or unavailable):
            unavailable = True
            raise OSError(errno.EIO, "preview record temporarily unreadable")
        return original_load(path)

    def unnecessary_codec(*_args, **_kwargs):
        pytest.fail("an unreadable installed preview must not be decoded again")

    def unnecessary_replacement(*_args, **_kwargs):
        pytest.fail("an unreadable installed preview must not be replaced")

    def unnecessary_open(*_args, **_kwargs):
        pytest.fail("an unreadable installed preview must not open or hydrate the original")

    inspect_original = reserved_paths.inspect_generic_file

    def inspect_other(root, path, *args, **kwargs):
        if root == vault and path in (saved.path, vault / saved.path):
            pytest.fail("an unreadable installed preview must not open the resident original for inspection")
        return inspect_original(root, path, *args, **kwargs)

    monkeypatch.setattr(media_previews, "_decode", unnecessary_codec)
    monkeypatch.setattr(private_state, "write_record", unnecessary_replacement)
    with monkeypatch.context() as blocked:
        blocked.setattr(manager, "consistency_guard", guard)
        blocked.setattr(media_previews, "_load", load)
        if fault == "reuse":
            blocked.setattr(media_store, "open_original", unnecessary_open)
            blocked.setattr(reserved_paths, "inspect_generic_file", inspect_other)
        claim = ledger.claim_next()
        assert claim is not None and claim.id == first.id
        worker._process(claim)
        assert unavailable
        pending = ledger.get(claim.id)
        assert pending is not None and pending.state == media_jobs.PENDING
        assert ledger.claim_next() is None

        if fault == "reuse":
            assert pending.attempts == first.attempts
            now += media_jobs.RETRY_FIRST_SECONDS
        else:
            now += media_jobs.RETRY_FIRST_SECONDS * 2 ** (claim.attempts - 1)
            last = ledger.claim_next()
            assert last is not None and last.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
            worker._process(last)
            assert ledger.claim_next() is None

            monkeypatch.setattr(media_store, "open_original", unnecessary_open)
            monkeypatch.setattr(reserved_paths, "inspect_generic_file", inspect_other)
            now += media_jobs.RETRY_FIRST_SECONDS
            acknowledgement = ledger.claim_next()
            assert acknowledgement is not None and acknowledgement.attempts == last.attempts
            worker._process(acknowledgement)
            waiting = ledger.get(claim.id)
            assert waiting is not None and waiting.state == media_jobs.PENDING
            assert waiting.attempts == last.attempts and ledger.claim_next() is None
            now += 2 * media_jobs.RETRY_FIRST_SECONDS

    retry = ledger.claim_next()
    assert retry is not None and retry.id == claim.id
    worker._process(retry)
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is None
    expected_attempts = claim.attempts if fault == "reuse" else media_jobs.MAX_PREVIEW_ATTEMPTS
    assert settled.attempts == expected_attempts
    assert media_previews.read(vault, saved.path, first.preview_sha256) is not None
    assert (record.read_bytes(), record.stat().st_ino) == installed


@pytest.mark.parametrize("fault", ["probe", "recheck"])
def test_reconciliation_requires_confirmed_absence_before_rebuilding(vault, monkeypatch, fault):
    original = _photo(921, 640)
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-rebuild-stat-fixture.jpg",
            stream=io.BytesIO(original),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claim = ledger.claim_next()
    assert claim is not None
    media_worker.MediaWorker(vault, execution_mode="process")._process(claim)
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is None

    record, = media_previews.store_path(vault).glob("*/*.json")
    installed = record.read_bytes()
    displaced = record.with_suffix(".held")
    if fault == "recheck":
        record.rename(displaced)
    manager = get_manager()
    original_guard = manager.consistency_guard
    original_lstat = Path.lstat
    guarded = False
    refusals = []

    @contextmanager
    def guard(*args, **kwargs):
        nonlocal guarded
        with original_guard(*args, **kwargs) as held:
            guarded = kwargs.get("operation") == "media_preview_recover"
            if guarded and fault == "recheck":
                displaced.rename(record)
            try:
                yield held
            finally:
                guarded = False

    def lstat(path):
        if path == record and (fault == "probe" or guarded):
            refusals.append(fault)
            raise OSError(errno.EIO, "preview record temporarily unreachable")
        return original_lstat(path)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("preview rebuild admission must check existence without opening the record")

    digest = hashlib.sha256(original).hexdigest()
    with monkeypatch.context() as blocked:
        blocked.setattr(manager, "consistency_guard", guard)
        blocked.setattr(Path, "lstat", lstat)
        blocked.setattr(media_previews, "_load", unexpected_read)
        media_previews.recover(vault, ledger, [(vault / saved.path, vault / saved.sidecar_path, digest)])
        assert refusals
        assert ledger.get(claim.id) == settled
        assert ledger.claim_next() is None

    assert record.read_bytes() == installed
    assert media_previews.read(vault, saved.path, digest) is not None


def test_reconciliation_preserves_a_receipt_published_while_acquiring_the_guard(vault, monkeypatch):
    original = _photo(922, 640)
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-concurrent-receipt-fixture.jpg",
            stream=io.BytesIO(original), media_pending=True,
        )
    binary, sidecar = vault / saved.path, vault / saved.sidecar_path
    digest = hashlib.sha256(original).hexdigest()
    ledger = media_jobs.MediaJobStore(vault)
    assert ledger.get_by_binary(binary, media_type=media_jobs.PREVIEW_MEDIA_TYPE) is None
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    manager = get_manager()
    original_guard = manager.consistency_guard
    published = []

    @contextmanager
    def guard(*args, **kwargs):
        if kwargs.get("operation") == "media_preview_recover":
            assert ledger.get_by_binary(binary, media_type=media_jobs.PREVIEW_MEDIA_TYPE) is None
            ledger.enqueue_preview(binary, sidecar, digest)
            claim = ledger.claim_next()
            assert claim is not None
            worker._process(claim)
            receipt = ledger.get(claim.id)
            assert receipt is not None and receipt.state == media_jobs.COMPLETED and receipt.last_error is None
            published.append(receipt)
            record, = media_previews.store_path(vault).glob("*/*.json")
            record.unlink()  # The private payload can disappear after its receipt is durable.
        with original_guard(*args, **kwargs) as held:
            yield held

    with monkeypatch.context() as concurrent:
        concurrent.setattr(manager, "consistency_guard", guard)
        media_processing.reconcile_all_media(vault)
    receipt, = published
    assert ledger.get(receipt.id) == receipt
    assert ledger.claim_next() is None

    media_processing.reconcile_all_media(vault)
    rebuilt = ledger.claim_next()
    assert rebuilt is not None and rebuilt.id == receipt.id and rebuilt.preview_sha256 == digest


def test_initial_preview_read_faults_leave_generation_eligible_after_storage_recovers(vault, monkeypatch):
    original = _photo(923, 640)
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-initial-read-retry-fixture.jpg",
            stream=io.BytesIO(original),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    now = time.time()
    monkeypatch.setattr(media_jobs, "time", SimpleNamespace(time=lambda: now))
    probes = []

    def unavailable(path):
        probes.append(path)
        raise OSError(errno.EIO, "preview storage temporarily unreadable")

    def unexpected_codec(*_args, **_kwargs):
        pytest.fail("an uncertain record check must defer before decoding")

    with monkeypatch.context() as blocked:
        blocked.setattr(media_previews, "_load", unavailable)
        blocked.setattr(media_previews, "_decode", unexpected_codec)
        for _ in range(media_jobs.MAX_PREVIEW_ATTEMPTS):
            claim = ledger.claim_next()
            assert claim is not None
            worker._process(claim)
            pending = ledger.get(claim.id)
            assert pending is not None and pending.state == media_jobs.PENDING
            assert ledger.claim_next() is None
            now += media_jobs.RETRY_LIMIT_SECONDS

    assert probes and all(not path.exists() for path in probes)
    retry = ledger.claim_next()
    assert retry is not None and retry.id == claim.id
    worker._process(retry)
    settled = ledger.get(retry.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is None
    assert settled.attempts == 1
    assert media_previews.read(vault, saved.path, hashlib.sha256(original).hexdigest()) is not None


def test_discovery_batches_new_preview_demands_within_its_limit(vault, monkeypatch):
    binaries = [vault / NOTES / f"preview-discovery-batch-{index}.jpg" for index in range(3)]
    for index, binary in enumerate(binaries):
        binary.write_bytes(_photo(900 + index, 630))
    lookups = []
    admissions = []
    guards = []

    ledger_type = media_jobs.MediaJobStore
    manager = get_manager()
    original_lookup = ledger_type.recoverable_preview_demands
    original_enqueue = ledger_type.enqueue_previews
    original_guard = manager.consistency_guard

    def lookup(store, candidates, **kwargs):
        lookups.append(tuple(binary for binary, _, _ in candidates))
        return original_lookup(store, candidates, **kwargs)

    def enqueue(store, candidates, **kwargs):
        admissions.append(tuple(binary for binary, _, _ in candidates))
        return original_enqueue(store, candidates, **kwargs)

    def guard(*args, **kwargs):
        operation = kwargs.get("operation")
        if operation in {"media_preview_admit", "media_preview_recover"}:
            guards.append(operation)
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(ledger_type, "recoverable_preview_demands", lookup)
    monkeypatch.setattr(ledger_type, "enqueue_previews", enqueue)
    monkeypatch.setattr(manager, "consistency_guard", guard)

    assert media_processing.reconcile_all_media(vault, limit=2) == 2
    selected = tuple(binaries[:2])
    assert lookups == [selected]
    assert admissions == [selected]
    assert guards == ["media_preview_recover"]

    ledger = ledger_type(vault, create=False)
    for binary in selected:
        claim = ledger.claim_next()
        assert claim is not None and claim.binary_path == binary
        assert claim.preview_sha256 == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert ledger.claim_next() is None
