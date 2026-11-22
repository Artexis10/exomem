"""Preview recovery through the filing write, durable ledger and process worker."""

from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_vault_store_media as store_suite
from test_client_artifacts import _staged
from test_image_view import _photo
from test_media_store import selected_store as selected_store
from test_vault_store_projector import NOTES

from exomem import (
    artifact_recovery,
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
from exomem.vault import PlannedWrite, parse_frontmatter, render_frontmatter_document, serialize_frontmatter
from exomem.vault_store import batch, importer, read
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


@pytest.mark.parametrize("change", ["unchanged", "binding", "access"])
def test_buffered_store_preview_rechecks_live_release_after_original_close(stored, store_thread, monkeypatch, change):
    from test_access_read_paths import _write_cfg

    relative = f"{NOTES}/buffered-preview-fixture.jpg"
    original = _photo(900, 600)
    (stored / relative).write_bytes(original)
    importer.Importer(stored, run=store_thread.run).import_paths([relative])
    ledger = media_jobs.MediaJobStore(stored)
    claim = ledger.claim_next()
    assert claim is not None and claim.media_type == media_jobs.PREVIEW_MEDIA_TYPE
    media_worker.MediaWorker(stored, execution_mode="process")._process(claim)
    companion = claim.sidecar_path.relative_to(stored).as_posix()
    before = read.owned_bytes(stored, companion)
    assert before is not None and claim.preview_sha256 is not None
    expected_jpeg = media_previews.read(stored, relative, claim.preview_sha256)
    assert expected_jpeg is not None
    hold_original = media_store.hold_original

    @contextmanager
    def change_on_close(*args, **kwargs):
        with hold_original(*args, **kwargs) as held:
            yield held
        if change == "binding":
            rebound = before.decode().replace(claim.preview_sha256, hashlib.sha256(b"new original").hexdigest())
            store_thread.run(lambda writer: batch.commit_batch(
                writer, [PlannedWrite(claim.sidecar_path, rebound)], vault_root=stored,
            ))
            assert read.owned_bytes(stored, companion) == before
        elif change == "access":
            _write_cfg(stored, 'excluded:\n  - "Notes"\n')

    monkeypatch.setattr(media_store, "hold_original", change_on_close)
    if change == "unchanged":
        digest_fd = reserved_paths._digest_fd
        original_inode = (stored / relative).stat().st_ino
        original_reads = 0

        def count_original_reads(fd, **kwargs):
            nonlocal original_reads
            if os.fstat(fd).st_ino == original_inode:
                original_reads += 1
            return digest_fd(fd, **kwargs)

        monkeypatch.setattr(reserved_paths, "_digest_fd", count_original_reads)
        with library_scope(), read.request_snapshot(stored):
            descriptor, jpeg = media_previews.show(stored, relative)
        assert descriptor["sha256"] == claim.preview_sha256 and jpeg == expected_jpeg
        assert original_reads == 1
        return

    with library_scope(), read.request_snapshot(stored), pytest.raises(artifact_recovery.RecoveryUnavailable):
        media_previews.show(stored, relative)

    if change == "binding":
        assert read.owned_bytes(stored, companion) != before


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


@pytest.mark.parametrize("published", ["none", "earlier", "current"])
def test_restart_acknowledges_only_its_own_ready_record(vault, monkeypatch, published):
    original = _photo(910, 630)
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-interrupted-final-attempt.jpg",
            stream=io.BytesIO(original),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    for attempt in range(media_jobs.MAX_PREVIEW_ATTEMPTS - 1):
        previous = ledger.claim_next()
        assert previous is not None
        if published == "earlier" and attempt == 0:
            assert media_previews.generate(vault, previous, ledger, deadline=None)
        assert ledger.recover_interrupted() == 1

    claim = ledger.claim_next()
    assert claim is not None and claim.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
    if published == "current":
        assert media_previews.generate(vault, claim, ledger, deadline=None)
    else:
        class WorkerStopped(BaseException):
            pass

        open_original = media_store.open_original

        @contextmanager
        def interrupted_on_close(*args, **kwargs):
            with open_original(*args, **kwargs) as source:
                yield source
                raise WorkerStopped

        monkeypatch.setattr(media_store, "open_original", interrupted_on_close)
        with pytest.raises(WorkerStopped):
            media_previews.generate(vault, claim, ledger, deadline=None)
    # Recover before the worker acknowledged the outcome, with or without a ready record.
    assert ledger.recover_interrupted() == 1
    acknowledgement = ledger.claim_next()
    assert acknowledgement is not None and acknowledgement.id == claim.id
    assert acknowledgement.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
    assert acknowledgement.preview_receipt_id == claim.preview_receipt_id
    assert acknowledgement.claim_revision > claim.claim_revision
    media_worker.MediaWorker(vault, execution_mode="process")._process(acknowledgement)
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED
    assert (settled.last_error is None) == (published == "current")
    assert (media_previews.read(vault, saved.path, hashlib.sha256(original).hexdigest()) is not None) == (
        published != "none"
    )


def test_preview_publication_requires_the_resident_binding_after_source_close(vault, monkeypatch):
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-resident-rebound-fixture.jpg",
            stream=io.BytesIO(_photo(915, 633)),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claim = ledger.claim_next()
    assert claim is not None
    prior_jpeg = _photo(300, 200)
    prior_digest = hashlib.sha256(prior_jpeg).hexdigest()
    record = media_previews._record_path(private_state.store(vault, media_previews.STORE), saved.path)
    record.parent.mkdir()
    private_state.write_record(
        record.parent, record.stem,
        {"version": 1, "path": saved.path, "sha256": prior_digest, "jpeg": base64.b64encode(prior_jpeg).decode("ascii")},
        temp_suffix=".tmp",
    )
    open_original = media_store.open_original
    replacement_digest = hashlib.sha256(b"new original").hexdigest()

    @contextmanager
    def rebind_on_close(*args, **kwargs):
        with open_original(*args, **kwargs) as source:
            yield source
            fields, body, marker = parse_frontmatter(claim.sidecar_path.read_text(encoding="utf-8"))
            assert marker is not None
            fields["binary_sha256"] = replacement_digest
            fields["governance_companion"]["artifact_sha256"] = replacement_digest
            rebound = render_frontmatter_document(serialize_frontmatter(fields), body)
            claim.sidecar_path.write_text(rebound, encoding="utf-8")

    monkeypatch.setattr(media_store, "open_original", rebind_on_close)
    media_worker.MediaWorker(vault, execution_mode="process")._process(claim)

    assert originals.original_state(vault, saved.path).binary_sha256 == replacement_digest
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is not None
    assert media_previews.read(vault, saved.path, prior_digest) == prior_jpeg
    assert media_previews.read(vault, saved.path, claim.preview_sha256) is None


@pytest.mark.parametrize("replacement_during", [
    "lookup", "existing_record", "lookup_fault", "staging", "companion", "malformed_scalar",
    "busy_pin_cleanup", "busy_pin_boundary", "pin_cleanup", "acquisition_malformed",
])
def test_pooled_preview_publication_refuses_lost_custody(vault, tmp_path, monkeypatch, replacement_during):
    from test_shared_originals import _pooled_capture

    original = _photo(913, 633)
    captured = _pooled_capture(vault, tmp_path, monkeypatch, "pooled-preview-publication-fixture.jpg", original)
    relative = captured.artifact_path
    binary = vault / relative
    assert not binary.exists()
    media_processing.reconcile_media(vault, relative)
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claim = ledger.claim_next()
    assert claim is not None and claim.media_type == media_jobs.PREVIEW_MEDIA_TYPE
    existing = None
    if replacement_during in {"existing_record", "lookup_fault", "acquisition_malformed"}:
        assert media_previews.generate(vault, claim, ledger, deadline=None)
        [record] = media_previews.store_path(vault).glob("*/*.json")
        existing = record.read_bytes(), record.stat().st_ino
        assert ledger.recover_interrupted() == 1
        prior = claim
        claim = ledger.claim_next()
        assert claim is not None and claim.id == prior.id
        if replacement_during == "lookup_fault":
            while claim.attempts < media_jobs.MAX_PREVIEW_ATTEMPTS:
                assert ledger.recover_interrupted() == 1
                claim = ledger.claim_next()
                assert claim is not None and claim.id == prior.id

    replacement = _photo(634, 914)
    if replacement_during == "pin_cleanup":
        pin = media_store._pin

        @contextmanager
        def replace_during_pin_cleanup(*args, **kwargs):
            with pin(*args, **kwargs) as pinned:
                yield pinned
                binary.write_bytes(replacement)

        monkeypatch.setattr(media_store, "_pin", replace_during_pin_cleanup)
    elif replacement_during == "acquisition_malformed":
        open_original = media_store.open_original

        def malform_before_open(*args, **kwargs):
            claim.sidecar_path.write_text("---\ninvalid: !!bool unexpected\n---\n")
            return open_original(*args, **kwargs)

        monkeypatch.setattr(media_store, "open_original", malform_before_open)
    elif replacement_during in {"busy_pin_cleanup", "busy_pin_boundary"}:
        while claim.attempts < media_jobs.MAX_PREVIEW_ATTEMPTS:
            assert ledger.recover_interrupted() == 1
            claim = ledger.claim_next()
            assert claim is not None
        local_original = media_store._open_local_original
        connect = media_jobs.MediaJobStore._connect
        closing_pin = False

        @contextmanager
        def replace_before_pin_cleanup(*args, **kwargs):
            nonlocal closing_pin
            with local_original(*args, **kwargs) as source:
                yield source
                binary.write_bytes(replacement)
                closing_pin = True

        def busy_pin_cleanup(*args, **kwargs):
            nonlocal closing_pin
            if closing_pin:
                closing_pin = False
                if replacement_during == "busy_pin_boundary":
                    raise OpError("MUTATION_BUSY", "The mutation boundary is busy.", "Retry the operation.",
                                  details={"status": "retryable", "committed": False})
                # The retained pin encounters a real writer, with no wait so the test can release it.
                with closing(sqlite3.connect(ledger.path, timeout=0)) as holder, holder:
                    holder.execute("BEGIN IMMEDIATE")
                    with closing(sqlite3.connect(ledger.path, timeout=0)) as contender, contender:
                        contender.execute("BEGIN IMMEDIATE")
            return connect(*args, **kwargs)

        monkeypatch.setattr(media_store, "_open_local_original", replace_before_pin_cleanup)
        monkeypatch.setattr(media_jobs.MediaJobStore, "_connect", busy_pin_cleanup)
    elif replacement_during in {"staging", "companion", "malformed_scalar"}:
        stage_record = private_state.stage_record

        @contextmanager
        def replace_after_staging(*args, **kwargs):
            with stage_record(*args, **kwargs) as provisional:
                if replacement_during == "companion":
                    claim.sidecar_path.write_bytes(b"\xff")
                elif replacement_during == "malformed_scalar":
                    claim.sidecar_path.write_text("---\ninvalid: !!bool unexpected\n---\n")
                else:
                    binary.write_bytes(replacement)
                yield provisional

        monkeypatch.setattr(private_state, "stage_record", replace_after_staging)
    else:
        read_for_publication = media_previews._read_for_publication

        def replace_during_lookup(*args):
            result = read_for_publication(*args)
            binary.write_bytes(replacement)
            if replacement_during == "lookup_fault":
                raise OpError("PREVIEW_RECORD_UNAVAILABLE", "Temporary record read failure.", "Retry the worker.",
                              details={"status": "retryable"})
            return result

        monkeypatch.setattr(media_previews, "_read_for_publication", replace_during_lookup)

    media_worker.MediaWorker(vault, execution_mode="process")._process(claim)

    if replacement_during == "companion":
        assert claim.sidecar_path.read_bytes() == b"\xff"
    elif replacement_during in {"malformed_scalar", "acquisition_malformed"}:
        assert claim.sidecar_path.read_text() == "---\ninvalid: !!bool unexpected\n---\n"
    else:
        assert binary.read_bytes() == replacement
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED
    assert settled.last_error == "preview original unavailable"
    if existing is None:
        assert not list(media_previews.store_path(vault).glob("*/*.json"))
    else:
        assert (record.read_bytes(), record.stat().st_ino) == existing


@pytest.mark.parametrize("malformed", [b"\xff", b"---\nfields: [unterminated\n---\n"])
def test_resident_preview_refuses_malformed_companion_at_acquisition(vault, selected_store, monkeypatch, malformed):
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-malformed-resident.jpg",
            stream=io.BytesIO(_photo(915, 633)),
        )
    ledger = media_jobs.MediaJobStore(vault)
    offload = ledger.claim_next()
    assert offload is not None and offload.media_type == media_jobs.OFFLOAD_MEDIA_TYPE
    assert ledger.complete_offload(offload)
    ledger.complete(offload)
    claim = ledger.claim_next()
    assert claim is not None and claim.media_type == media_jobs.PREVIEW_MEDIA_TYPE
    assert (vault / saved.path).is_file()
    open_original = media_store.open_original

    def malform_before_open(*args, **kwargs):
        claim.sidecar_path.write_bytes(malformed)
        return open_original(*args, **kwargs)

    monkeypatch.setattr(media_store, "open_original", malform_before_open)
    media_worker.MediaWorker(vault, execution_mode="process")._process(claim)

    assert claim.sidecar_path.read_bytes() == malformed
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED
    assert settled.last_error == "preview original unavailable"
    assert originals.original_state(vault, saved.path).present


@pytest.mark.parametrize("settlement_error", ["busy", "readonly", "persistent"])
def test_process_worker_settles_a_definitive_preview_without_accepting_old_record(
    vault, tmp_path, monkeypatch, caplog, settlement_error,
):
    from test_shared_originals import _pooled_capture

    original = _photo(915, 633)
    captured = _pooled_capture(vault, tmp_path, monkeypatch, "preview-busy-settlement.jpg", original)
    relative = captured.artifact_path
    binary = vault / relative
    assert not binary.exists()
    media_processing.reconcile_media(vault, relative)
    ledger = media_jobs.MediaJobStore(vault)
    claim = ledger.claim_next()
    assert claim is not None and claim.preview_sha256 is not None
    first_receipt = claim.preview_receipt_id
    assert media_previews.generate(vault, claim, ledger, deadline=None)
    previous_jpeg = media_previews.read(vault, relative, claim.preview_sha256)
    assert previous_jpeg is not None
    while claim.attempts < media_jobs.MAX_PREVIEW_ATTEMPTS - 1:
        assert ledger.recover_interrupted() == 1
        claim = ledger.claim_next()
        assert claim is not None

    local_original = media_store._open_local_original
    connect = media_jobs.MediaJobStore._connect
    finish_preview = media_jobs.MediaJobStore.finish_preview
    refused_writes = []

    with closing(sqlite3.connect(ledger.path, timeout=0)) as holder, monkeypatch.context() as fault:
        @contextmanager
        def replace_then_hold_ledger(*args, **kwargs):
            with local_original(*args, **kwargs) as source:
                yield source
                binary.write_bytes(_photo(633, 915))
                if settlement_error == "busy":
                    holder.execute("BEGIN IMMEDIATE")

        def connect_until_holder_clears(*args, **kwargs):
            if holder.in_transaction:
                with closing(sqlite3.connect(ledger.path, timeout=0)) as contender:
                    try:
                        contender.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as error:
                        refused_writes.append(error.sqlite_errorcode)
                        # Hold through both the pin deletion and the first exact-claim settlement.
                        if len(refused_writes) == 2:
                            holder.rollback()
                        raise
            return connect(*args, **kwargs)

        def readonly_first_settlement(self, job, **kwargs):
            if refused_writes and settlement_error == "readonly":
                return finish_preview(self, job, **kwargs)

            def readonly_connection(store, *args, **options):
                connection = connect(store, *args, **options)
                connection.execute("PRAGMA query_only=ON")
                return connection

            if settlement_error == "persistent":
                if len(refused_writes) == 3:
                    pytest.fail("the child must leave the ledger after three failed settlement writes")
                if not refused_writes:
                    # This also prevents the child from clearing its worker marker on exit.
                    fault.setattr(media_jobs.MediaJobStore, "_connect", readonly_connection)
                try:
                    return finish_preview(self, job, **kwargs)
                except sqlite3.OperationalError as error:
                    refused_writes.append(error.sqlite_errorcode)
                    raise

            with monkeypatch.context() as failed_write:
                failed_write.setattr(self, "_connect", lambda: readonly_connection(self))
                try:
                    return finish_preview(self, job, **kwargs)
                except sqlite3.OperationalError as error:
                    refused_writes.append(error.sqlite_errorcode)
                    raise

        def unexpected_decode(*args, **kwargs):
            pytest.fail("settling a definitive refusal must not run the codec again")

        fault.setattr(media_store, "_open_local_original", replace_then_hold_ledger)
        fault.setattr(media_previews, "_decode", unexpected_decode)
        if settlement_error == "busy":
            fault.setattr(media_jobs.MediaJobStore, "_connect", connect_until_holder_clears)
            assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01) == 0
            assert refused_writes == [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY]
        else:
            fault.setattr(media_jobs.MediaJobStore, "finish_preview", readonly_first_settlement)
            code = media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01)
            if settlement_error == "persistent":
                assert code == media_worker._TRANSIENT_EXIT_CODE
                assert refused_writes == [sqlite3.SQLITE_READONLY] * 3
            else:
                assert code == 0 and refused_writes == [sqlite3.SQLITE_READONLY]

    if settlement_error == "persistent":
        assert ledger.worker_pid() == os.getpid()
        assert ledger.recover_interrupted() == 1
        ledger.clear_worker(os.getpid())
        acknowledgement = ledger.claim_next()
        assert acknowledgement is not None and acknowledgement.id == claim.id
        assert acknowledgement.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
        assert acknowledgement.preview_receipt_id and acknowledgement.preview_receipt_id != first_receipt
        monkeypatch.setattr(media_previews, "_decode", unexpected_decode)
        monkeypatch.setattr(media_store, "open_original", unexpected_decode)
        media_worker.MediaWorker(vault, execution_mode="process")._process(acknowledgement)

    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED
    assert settled.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
    expected = "preview retry limit reached" if settlement_error == "persistent" else "preview original unavailable"
    assert settled.last_error == expected
    assert media_previews.read(vault, relative, claim.preview_sha256) == previous_jpeg
    if settlement_error != "busy":
        assert sum("SQLITE_READONLY" in record.getMessage() for record in caplog.records) == 1


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
        assert acknowledgement.preview_receipt_id == claim.preview_receipt_id
        worker._process(acknowledgement)
        assert failures == [fault]
        waiting = ledger.get(claim.id)
        assert waiting is not None and waiting.state == media_jobs.PENDING
        assert waiting.attempts == media_jobs.MAX_PREVIEW_ATTEMPTS
        assert ledger.claim_next() is None

    now += media_jobs.RETRY_FIRST_SECONDS
    retry = ledger.claim_next()
    assert retry is not None and retry.id == claim.id
    assert retry.preview_receipt_id == claim.preview_receipt_id
    assert retry.claim_revision > acknowledgement.claim_revision
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


def test_storage_failure_after_install_retries_the_same_receipt_without_reopening_original(vault, monkeypatch):
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-post-install-sync.jpg",
            stream=io.BytesIO(_photo(910, 630)),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claim = ledger.claim_next()
    assert claim is not None and claim.preview_sha256 and claim.preview_receipt_id
    now = time.time()
    monkeypatch.setattr(media_jobs, "time", SimpleNamespace(time=lambda: now))
    worker = media_worker.MediaWorker(vault, execution_mode="process")

    def unavailable(*args, **kwargs):
        raise OSError("preview directory flush is unavailable")

    with monkeypatch.context() as blocked:
        blocked.setattr(private_state, "sync_directory", unavailable)
        worker._process(claim)

    assert media_previews.read(vault, saved.path, claim.preview_sha256) is not None
    waiting = ledger.get(claim.id)
    assert waiting is not None and waiting.state == media_jobs.PENDING and waiting.preview_finish_error
    assert waiting.preview_receipt_id == claim.preview_receipt_id

    def unexpected_open(*args, **kwargs):
        pytest.fail("post-install acknowledgement must not open the original or run the codec")

    monkeypatch.setattr(media_store, "open_original", unexpected_open)
    monkeypatch.setattr(media_previews, "_decode", unexpected_open)
    now += media_jobs.RETRY_FIRST_SECONDS
    acknowledgement = ledger.claim_next()
    assert acknowledgement is not None and acknowledgement.preview_receipt_id == claim.preview_receipt_id
    assert acknowledgement.attempts == claim.attempts
    worker._process(acknowledgement)
    settled = ledger.get(claim.id)
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error is None


def test_reused_preview_reserves_space_for_the_receipt(vault):
    with library_scope():
        saved = preserve.preserve_stream(
            vault, scope="Test", category="Images", filename="preview-legacy-at-envelope-limit.jpg",
            stream=io.BytesIO(_photo(920, 640)),
        )
    ledger = media_jobs.MediaJobStore(vault, create=False)
    claim = ledger.claim_next()
    assert claim is not None and claim.preview_sha256
    record = {"version": 1, "path": saved.path, "sha256": claim.preview_sha256, "jpeg": ""}
    max_legacy_bytes = ((media_previews.MAX_RECORD_BYTES - len(json.dumps(record))) // 4) * 3
    # The size gate reads the JPEG markers and envelope before any codec runs.
    legacy_jpeg = b"\xff\xd8" + bytes(max_legacy_bytes - 4) + b"\xff\xd9"
    record["jpeg"] = base64.b64encode(legacy_jpeg).decode("ascii")
    path = media_previews._record_path(private_state.store(vault, media_previews.STORE), saved.path)
    path.parent.mkdir()
    private_state.write_record(path.parent, path.stem, record, temp_suffix=".tmp")
    assert media_previews.read(vault, saved.path, claim.preview_sha256) == legacy_jpeg

    assert media_previews.generate(vault, claim, ledger, deadline=None)

    jpeg = media_previews.read(vault, saved.path, claim.preview_sha256)
    assert jpeg is not None and jpeg != legacy_jpeg
    assert path.stat().st_size <= media_previews.MAX_RECORD_BYTES
    assert media_previews.confirm_install(vault, claim)


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
    jpeg = media_previews.read(vault, saved.path, first.preview_sha256)
    assert jpeg is not None
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
    with monkeypatch.context() as blocked:
        blocked.setattr(private_state, "stage_record", unnecessary_replacement)
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

    assert (record.read_bytes(), record.stat().st_ino) == installed
    retry = ledger.claim_next()
    assert retry is not None and retry.id == claim.id
    worker._process(retry)
    settled = ledger.get(claim.id)
    expected_error = None if fault == "reuse" else "preview retry limit reached"
    assert settled is not None and settled.state == media_jobs.COMPLETED and settled.last_error == expected_error
    expected_attempts = claim.attempts if fault == "reuse" else media_jobs.MAX_PREVIEW_ATTEMPTS
    assert settled.attempts == expected_attempts
    assert media_previews.read(vault, saved.path, first.preview_sha256) == jpeg
    if fault == "reuse":
        assert retry.preview_receipt_id != first.preview_receipt_id
        assert media_previews.confirm_install(vault, retry)
    else:
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
