"""Preview recovery through the filing write, durable ledger and process worker."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
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
    schema,
    server_runtime,
    state_paths,
)
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope
from exomem.vault import parse_frontmatter
from exomem.vault_store import importer, read
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


@pytest.mark.parametrize("sweep", ["startup", "periodic"])
def test_discovery_queues_a_first_companion_after_the_watcher_event_was_missed(vault, monkeypatch, sweep):
    watcher = file_watcher.FileWatcher(vault)
    if sweep == "periodic":
        watcher._reconcile_once(seed=True)
    original = _photo(950, 650)
    binary = vault / NOTES / "preview-first-companion-fixture.jpg"
    binary.write_bytes(original)
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
    fields = parse_frontmatter(claimed.sidecar_path.read_text(encoding="utf-8"))[0]
    assert media_previews.requested_digest(fields, binary.relative_to(vault).as_posix()) == digest


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


def test_cleanup_batches_store_companions_and_preserves_uncertain_or_replaced_paths(stored, store_thread, monkeypatch):
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
    media_previews.reconcile(stored)
    assert [batch & expected_companions for batch in batches] == [
        expected_companions,
        {f"{directory}.md", f"{restored}.md"},
    ]
    assert media_previews.read(stored, directory, hashlib.sha256(samples[directory]).hexdigest()) is None
    for relative in (healthy, restored):
        assert media_previews.read(stored, relative, hashlib.sha256(samples[relative]).hexdigest()) is not None
