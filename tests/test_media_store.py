"""Stored originals retain their evidence identity across cache operations."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from exomem import originals


@pytest.fixture
def selected_store(vault: Path, tmp_path: Path, monkeypatch):
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        pytest.skip("the real age integration requires age and age-keygen")
    identity = tmp_path / "identity"
    generated = subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True)
    recipient = subprocess.run(["age-keygen", "-y", str(identity)], capture_output=True, text=True, check=True).stdout.strip()
    assert generated.returncode == 0
    name_key = tmp_path / "name-key"
    name_key.write_bytes(b"k" * 32)
    values = {"store": "test-store", "adapter": "filesystem-test", "root": str(tmp_path / "objects"),
              "key_version": "1", "recipient": recipient, "identity_path": str(identity),
              "name_key_path": str(name_key), "enabled": True, "eviction_enabled": True,
              "max_temporary_bytes": 10_000_000, "max_hydrations": 2,
              "minimum_idle_seconds": 0, "eviction_target": 0.7, "eviction_margin": 0.1}
    config = tmp_path / "media.json"
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    return values, config


def _preserved(vault: Path, *, category="samples") -> Path:
    from exomem import media_jobs, preserve

    captured = preserve.preserve(vault, scope="Test", category=category, filename="sample.txt",
                                 content="The durable transcript contains a searchable observation.")
    path = vault / captured.path
    preserve.update_sidecar_extraction(vault, vault / captured.sidecar_path,
                                      text="A searchable observation.", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    return path


def _offload(vault: Path, path: Path):
    from exomem import media_jobs, media_store

    ledger = media_jobs.MediaJobStore(vault)
    queued = ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE)
    if queued is not None:
        claimed = ledger.claim_next()
        assert claimed.binary_path == path
    reference = media_store.offload(vault, path)
    if queued is not None:
        ledger.complete_offload(claimed)
        ledger.complete(claimed)
    return reference


def test_real_age_offload_and_verified_hydration_preserve_the_sidecar(vault: Path, selected_store):
    from exomem import media_jobs, media_processing, media_store
    from exomem.governance import companions

    path = _preserved(vault)
    source = path.read_bytes()
    reference = _offload(vault, path)
    stored = Path(selected_store[0]["root"]) / reference.object / reference.version
    assert stored.read_bytes().startswith(b"age-encryption.org/v1\n")
    assert reference.object != hashlib.sha256(source).hexdigest()
    sidecar = path.with_name(path.name + ".md")
    published = sidecar.read_bytes()
    assert media_store.evict(vault, path)
    assert originals.original_state(vault, path).state == "evicted"
    assert companions.classify(vault, path.relative_to(vault).as_posix()).stored_originals
    assert media_processing.reconcile_media(vault, path).state == "completed"
    with media_store.open_original(vault, path) as original:
        assert original.stream.read() == source
        assert not media_store.evict(vault, path)
    assert media_processing.reconcile_media(vault, path).state == "completed"
    assert sidecar.read_bytes() == published
    assert not media_jobs.MediaJobStore(vault).has_binary(path)


def test_offload_reuses_only_a_live_reference(vault: Path, selected_store):

    first = _preserved(vault, category="first")
    initial = _offload(vault, first)
    duplicate = _preserved(vault, category="second")
    reused = _offload(vault, duplicate)
    assert reused == initial
    first.unlink()
    first.with_name(first.name + ".md").unlink()
    duplicate.unlink()
    duplicate.with_name(duplicate.name + ".md").unlink()
    returned = _preserved(vault, category="third")
    latest = _offload(vault, returned)
    assert latest.object == initial.object
    assert latest.version != initial.version


def test_corrupt_ciphertext_never_places_an_original(vault: Path, selected_store):
    from exomem import media_store

    path = _preserved(vault)
    reference = _offload(vault, path)
    assert media_store.evict(vault, path)
    stored = Path(selected_store[0]["root"]) / reference.object / reference.version
    stored.write_bytes(b"corrupt")
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_ORIGINAL_UNVERIFIED"):
        with media_store.open_original(vault, path):
            pytest.fail("corruption must not yield an original")
    assert not path.exists()


def test_a_vault_taken_off_its_store_reads_an_evicted_original_as_unavailable(vault: Path, selected_store):
    """cellctl renders a deselected cell's store with its identity only; a read must not crash or delete."""
    from exomem import media_store

    values, config = selected_store
    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    config.write_text(json.dumps({"vaults": {str(vault): {
        "store": values["store"], "adapter": values["adapter"], "enabled": False}}}))
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        with media_store.open_original(vault, path):
            pytest.fail("a store without hydration limits must not hydrate")
    assert originals.original_state(vault, path).state == "evicted"


def test_offload_demand_survives_processing_completion_and_discard(vault: Path):
    from exomem.media_jobs import MediaJob, MediaJobStore

    binary = vault / "Knowledge Base" / "sample.bin"
    store = MediaJobStore(vault)
    store.enqueue(MediaJob(binary, binary.with_name(binary.name + ".md"), "binary", do_ocr=True))
    claimed = store.claim_next()
    store.enqueue(MediaJob(binary, claimed.sidecar_path, "binary", do_ocr=False, do_offload=True))
    store.complete(claimed)
    offload_job = store.claim_next()
    assert offload_job.do_offload and not offload_job.do_ocr
    store.enqueue(MediaJob(binary, claimed.sidecar_path, "binary", do_ocr=False, do_offload=True))
    assert not store.complete_offload(offload_job)
    store.complete(offload_job)
    assert store.get_by_binary(binary, media_type="binary").do_offload
    store.discard(store.get_by_binary(binary, media_type="binary"))
    assert store.get_by_binary(binary, media_type="binary").do_offload


def test_completed_local_transcript_retires_work_from_a_previous_release_ledger(vault: Path):
    from exomem import media_jobs, media_processing

    path = _preserved(vault)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN offload_generation")
        connection.execute("ALTER TABLE jobs DROP COLUMN do_offload")
    assert media_processing.reconcile_media(vault, path, explicit=False).state == "completed"
    assert not store.has_binary(path)


@pytest.mark.parametrize("state", ["blocked", "failed"])
def test_offload_runs_without_changing_an_extraction_verdict(vault: Path, selected_store, monkeypatch, state):
    import base64

    from exomem import media_jobs, media_processing, media_store, media_worker, preserve

    with monkeypatch.context() as unselected:
        unselected.delenv("EXOMEM_MEDIA_STORE_CONFIG")
        captured = preserve.preserve(vault, scope="Test", category="waiting", filename="sample.mp4",
                                     content_base64=base64.b64encode(b"recording bytes").decode())
        path = vault / captured.path
        media_processing.reconcile_media(vault, path, explicit=False)
        ledger = media_jobs.MediaJobStore(vault)
        processing = ledger.claim_next()
        assert ledger.mark(processing, state, "speech engine unavailable", blocked_reason=media_jobs.ENGINE_UNAVAILABLE)
    verdict = ledger.get(processing.id)
    assert media_store.enqueue_offload(vault, path) == 1
    assert media_store.enqueue_offload(vault, path) == 0
    assert ledger.get_by_binary(path) == verdict
    before = (vault / captured.sidecar_path).read_bytes()
    assert media_processing.mark_processing_unavailable(vault, reason="runtime unavailable", next_action="restore the runtime") == 0
    assert (vault / captured.sidecar_path).read_bytes() == before
    offload_job = ledger.claim_next()
    assert offload_job.do_offload and not offload_job.do_ocr
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    assert worker._process(offload_job).state == "complete"
    ledger.complete(offload_job)
    assert originals.original_state(vault, path).state == "cached"
    assert ledger.get(processing.id) == verdict
    assert not media_store.evict(vault, path)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_unconfirmed_delete_refuses_before_store_access(vault: Path, selected_store, kind):
    from exomem import delete_directory, delete_file, media_store

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    root = Path(selected_store[0]["root"])
    root.rename(root.with_name("offline-objects"))
    operation = delete_file.delete_file if kind == "file" else delete_directory.delete_directory
    error = delete_file.DeleteFileError if kind == "file" else delete_directory.DeleteDirectoryError
    target = path if kind == "file" else path.parent
    with pytest.raises(error) as refused:
        operation(vault, path=target.relative_to(vault).as_posix(), confirm=False)
    assert refused.value.code == "UNCONFIRMED"
    assert not path.exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_append_only_delete_refuses_before_store_access(vault: Path, selected_store, kind):
    from exomem import delete_directory, delete_file, media_store

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    root = Path(selected_store[0]["root"])
    root.rename(root.with_name("offline-objects"))
    operation = delete_file.delete_file if kind == "file" else delete_directory.delete_directory
    error = delete_file.DeleteFileError if kind == "file" else delete_directory.DeleteDirectoryError
    target = path if kind == "file" else path.parent
    with pytest.raises(error) as refused:
        operation(vault, path=target.relative_to(vault).as_posix(), confirm=True)
    assert refused.value.code == "APPEND_ONLY"
    assert not path.exists()


def test_delete_preview_and_path_or_access_refusals_do_not_materialize(vault: Path, selected_store, monkeypatch):
    from exomem import (
        delete_directory,
        delete_file,
        media_jobs,
        media_processing,
        media_store,
        preserve,
    )
    from exomem.governance import catalog_publication, egress

    path = vault / "Knowledge Base" / "Notes" / "Media" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stored original")
    media_processing.reconcile_media(vault, path)
    preserve.update_sidecar_extraction(vault, path.with_name(path.name + ".md"), text="transcript", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    _offload(vault, path)
    assert media_store.evict(vault, path)
    root = Path(selected_store[0]["root"])
    root.rename(root.with_name("offline-objects"))
    relative = path.relative_to(vault).as_posix()
    with pytest.raises(delete_directory.DeleteDirectoryError) as not_empty:
        delete_directory.delete_directory(vault, path=path.parent.relative_to(vault).as_posix(), confirm=True)
    assert not_empty.value.code == "NOT_EMPTY"
    with pytest.raises(delete_file.DeleteFileError) as invalid:
        delete_file.delete_file(vault, path="../outside.txt", confirm=True)
    assert invalid.value.code == "INVALID_PATH"
    with pytest.raises(delete_file.DeleteFileError) as stale:
        delete_file.delete_file(vault, path=relative, confirm=True, expected_hash="0" * 64)
    assert stale.value.code == "STALE_DELETE"
    preview = delete_file.delete_file(vault, path=relative, confirm=True, validate_only=True)
    assert preview.content_hash == hashlib.sha256(b"stored original").hexdigest()

    def refuse_catalog(*args, **kwargs):
        raise catalog_publication.CatalogPublicationError("catalog unavailable")

    with monkeypatch.context() as refused_catalog:
        refused_catalog.setattr(catalog_publication, "prepare_catalog_membership_batch", refuse_catalog)
        with pytest.raises(delete_file.DeleteFileError) as file_catalog:
            delete_file.delete_file(vault, path=relative, confirm=True)
        assert file_catalog.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
        with pytest.raises(delete_directory.DeleteDirectoryError) as directory_catalog:
            delete_directory.delete_directory(vault, path=path.parent.relative_to(vault).as_posix(), confirm=True, recursive=True)
        assert directory_catalog.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    monkeypatch.setattr(egress, "restricted_release_filter", lambda *_args, **_kwargs: lambda _path: False)
    with pytest.raises(delete_file.DeleteFileError) as withheld:
        delete_file.delete_file(vault, path=relative, confirm=True)
    assert withheld.value.code == "NOT_FOUND"
    assert not path.exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_authorized_delete_holds_original_pins_through_the_transition(vault: Path, selected_store, monkeypatch, kind):
    from exomem import (
        delete_directory,
        delete_file,
        graph_sync,
        media_jobs,
        media_processing,
        media_store,
        preserve,
    )

    path = vault / "Knowledge Base" / "Notes" / "Media" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stored original")
    media_processing.reconcile_media(vault, path)
    preserve.update_sidecar_extraction(vault, path.with_name(path.name + ".md"), text="transcript", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    _offload(vault, path)
    assert media_store.evict(vault, path)
    begin = graph_sync.begin_deletion_transition

    def contend(*args, **kwargs):
        assert path.read_bytes() == b"stored original"
        assert not media_store.evict(vault, path)
        return begin(*args, **kwargs)

    monkeypatch.setattr(graph_sync, "begin_deletion_transition", contend)
    if kind == "file":
        trashed = delete_file.delete_file(vault, path=path.relative_to(vault).as_posix(), confirm=True)
        copied = vault / trashed.trash_path
    else:
        trashed = delete_directory.delete_directory(vault, path=path.parent.relative_to(vault).as_posix(), confirm=True, recursive=True)
        copied = vault / trashed.trash_path / path.name
    assert copied.read_bytes() == b"stored original"
    assert originals.original_state(vault, path).state == "missing"


@pytest.mark.parametrize("failure", ["outage", "disabled"])
def test_store_failure_keeps_offload_demand_retryable_through_the_worker(vault: Path, selected_store, monkeypatch, failure):
    """A failed attempt once stranded its demand: the row ended failed and nothing re-queued it."""
    import os

    from exomem import media_jobs, media_store, media_worker

    values, config = selected_store
    if failure == "disabled":
        config.write_text(json.dumps({"vaults": {str(vault): {**values, "enabled": False}}}))
    path = _preserved(vault)
    ledger = media_jobs.MediaJobStore(vault)
    with monkeypatch.context() as outage:
        if failure == "outage":
            def unavailable(self, name, stream, *, size, sha256):
                with sqlite3.connect(ledger.path) as captured:
                    assert captured.execute("SELECT count(*) FROM media_protections").fetchone()[0] == 1
                raise OSError("store unavailable")

            outage.setattr(media_store.FilesystemStore, "upload", unavailable)
        # A second discovery retries an outage but leaves a disabled store's demand parked.
        for _attempt in range(2):
            media_store.enqueue_offloads(vault)
            assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    failed = ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE)
    assert failed.do_offload and "MEDIA_STORE_UNAVAILABLE" in failed.last_error
    assert originals.original_state(vault, path).state == "local"
    with sqlite3.connect(ledger.path) as captured:
        assert captured.execute("SELECT count(*) FROM media_protections").fetchone()[0] == int(failure == "outage")
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    assert media_store.enqueue_offloads(vault) == 1
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    assert originals.original_state(vault, path).state == "cached"
    assert ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is None
    with sqlite3.connect(ledger.path) as captured:
        assert captured.execute("SELECT count(*) FROM media_protections").fetchone()[0] == 0


@pytest.mark.parametrize("new_bound", [None, 100_000])
def test_a_size_refusal_parks_until_the_configured_bound_allows_it(vault: Path, selected_store, new_bound):
    """A size refusal must park repeated discovery and recover when its bound changes."""
    import os

    from exomem import media_jobs, media_store, media_worker

    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "max_object_bytes": 64}}}))
    path = _preserved(vault)
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    assert originals.original_state(vault, path).state == "local"
    assert media_jobs.MediaJobStore(vault).get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is None
    assert media_store.enqueue_offloads(vault) == 0
    [refusal] = media_jobs.tool_status(vault)["offload_refusals"]
    assert refusal["path"] == path.relative_to(vault).as_posix()
    assert refusal["code"] == "MEDIA_OBJECT_TOO_LARGE" and refusal["ciphertext_size"] > 64
    assert not list((Path(values["root"])).rglob("*"))
    updated = values if new_bound is None else {**values, "max_object_bytes": new_bound}
    config.write_text(json.dumps({"vaults": {str(vault): updated}}))
    assert media_jobs.tool_status(vault)["offload_refusals"] == []
    assert media_store.enqueue_offloads(vault) == 1
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    assert originals.original_state(vault, path).state == "cached"


def test_unverified_offload_stays_parked_until_explicit_retry(vault: Path, selected_store):
    """Discovery must not repeatedly hash corrupt bytes or prevent a repaired original's explicit retry."""
    import os

    from exomem import media_jobs, media_processing, media_store, media_worker

    path = _preserved(vault)
    expected = path.read_bytes()
    sidecar = path.with_name(path.name + ".md")
    from exomem.vault import parse_frontmatter

    canonical, transcript, _ = parse_frontmatter(sidecar.read_text(), strict=True)
    path.write_bytes(b"corrupt")
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    assert media_store.enqueue_offloads(vault) == 0
    [refusal] = media_jobs.tool_status(vault)["offload_refusals"]
    assert refusal["code"] == "MEDIA_ORIGINAL_UNVERIFIED"
    assert refusal["ciphertext_size"] is None
    path.write_bytes(expected)
    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "max_object_bytes": 100_000}}}))
    assert media_store.enqueue_offloads(vault) == 0
    retried = media_processing.retry_media(vault, path)
    assert retried.state == "completed" and retried.requeued == 1
    repaired, restored_transcript, _ = parse_frontmatter(sidecar.read_text(), strict=True)
    assert restored_transcript == transcript
    assert repaired["extracted_by"] == canonical["extracted_by"]
    assert repaired["binary_sha256"] == canonical["binary_sha256"]
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    assert originals.original_state(vault, path).state == "cached"


def test_a_new_sidecar_digest_releases_only_its_old_verification_refusal(vault: Path, selected_store):
    """A replacement original must not inherit a terminal refusal for the previous input."""
    from exomem import media_processing, media_store

    path = _preserved(vault)
    previous_digest = originals.original_state(vault, path).binary_sha256
    path.write_bytes(b"corrupt")
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, path)
    assert refused.value.code == "MEDIA_ORIGINAL_UNVERIFIED"
    path.write_bytes(b"A replacement original with different bytes and a new binding.")
    media_processing.reconcile_media(vault, path)
    assert originals.original_state(vault, path).binary_sha256 != previous_digest
    assert media_store.offload(vault, path) is not None
    assert originals.original_state(vault, path).state == "cached"


def test_hydration_reports_local_disk_exhaustion_as_local(vault: Path, selected_store, monkeypatch):
    """A local staging write must not become a misleading remote-store outage."""
    import errno
    from contextlib import contextmanager

    from exomem import media_store

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    temporary_file = media_store.tempfile.TemporaryFile

    class FullDisk:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, _chunk):
            raise OSError(errno.ENOSPC, "staging disk is full")

    @contextmanager
    def full_staging(*args, **kwargs):
        with temporary_file(*args, **kwargs) as stream:
            yield FullDisk(stream)

    monkeypatch.setattr(media_store.tempfile, "TemporaryFile", full_staging)
    with pytest.raises(OSError) as error:
        with media_store.open_original(vault, path):
            pytest.fail("a full staging disk cannot hydrate an original")
    assert error.value.errno == errno.ENOSPC
    assert originals.original_state(vault, path).state == "evicted"


def test_page_move_refuses_the_catalog_before_hydrating(vault: Path, selected_store, monkeypatch):
    """An unavailable object store must not mask a move's catalog refusal or change its files."""
    from exomem import media_jobs, media_processing, media_store, move_file, preserve
    from exomem.governance import catalog_publication

    path = vault / "Knowledge Base" / "Notes" / "Media" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stored original")
    media_processing.reconcile_media(vault, path)
    sidecar = path.with_name(path.name + ".md")
    preserve.update_sidecar_extraction(vault, sidecar, text="transcript", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    _offload(vault, path)
    assert media_store.evict(vault, path)
    canonical = sidecar.read_bytes()
    root = Path(selected_store[0]["root"])
    root.rename(root.with_name("offline-objects"))

    def refuse_catalog(*args, **kwargs):
        raise catalog_publication.CatalogPublicationError("catalog unavailable")

    monkeypatch.setattr(catalog_publication, "prepare_catalog_membership_batch", refuse_catalog)
    with pytest.raises(move_file.MoveFileError) as error:
        move_file.move_file(vault, old_path=sidecar.relative_to(vault).as_posix(),
                            new_path="Knowledge Base/Notes/Media/renamed.txt.md")
    assert error.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert sidecar.read_bytes() == canonical and not path.exists()
    assert not (path.parent / "renamed.txt.md").exists()


def test_legacy_refusal_migration_preserves_measured_sizes_and_protections(vault: Path):
    """Upgrading a retained ledger must not lose a refusal or an upload protection."""
    from exomem import media_jobs

    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        # This is the shipped legacy schema, before unmeasured ciphertext became nullable.
        connection.execute("DROP TABLE media_offload_refusals")
        connection.execute("CREATE TABLE media_offload_refusals ("
                           "binary_rel TEXT PRIMARY KEY, sha256 TEXT NOT NULL, code TEXT NOT NULL, "
                           "ciphertext_size INTEGER NOT NULL, created_at REAL NOT NULL)")
        connection.execute("INSERT INTO media_offload_refusals VALUES (?, ?, ?, ?, ?)",
                           ("Knowledge Base/Evidence/sample.bin", "a" * 64, "MEDIA_OBJECT_TOO_LARGE", 2048, 1.0))
        connection.execute("INSERT INTO media_protections VALUES (?, ?, ?, ?)",
                           ("protected", "Knowledge Base/Evidence/sample.bin", "b" * 64, 1.0))
    media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT ciphertext_size FROM media_offload_refusals").fetchall() == [(2048,)]
        connection.execute("INSERT INTO media_offload_refusals VALUES (?, ?, ?, ?, ?)",
                           ("Knowledge Base/Evidence/unverified.bin", "c" * 64, "MEDIA_ORIGINAL_UNVERIFIED", None, 2.0))
        assert connection.execute("SELECT token FROM media_protections").fetchall() == [("protected",)]


def test_idle_pins_do_not_spend_hydration_capacity_or_block_unrelated_eviction(vault: Path, selected_store):
    """Only live hydration reservations spend capacity, and only a target's pins prevent eviction."""
    import os

    from exomem import media_jobs, media_store

    path = _preserved(vault)
    expected = path.read_bytes()
    _offload(vault, path)
    assert media_store.evict(vault, path)
    finished = subprocess.Popen(["true"])
    dead_pid = finished.pid
    finished.wait()
    ledger = media_jobs.MediaJobStore(vault)
    relative = path.relative_to(vault).as_posix()
    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "max_hydrations": 1}}}))
    with sqlite3.connect(ledger.path) as connection:
        connection.executemany("INSERT INTO media_access VALUES (?, ?, ?, 0, 0)",
                               [(f"idle-{i}", "Knowledge Base/Evidence/other.bin", os.getpid()) for i in range(100)])
        connection.execute("INSERT INTO media_access VALUES ('dead', ?, ?, 1024, 1)", (relative, dead_pid))
        connection.execute("INSERT INTO media_access VALUES ('live', ?, ?, 1024, 1)", (relative, os.getpid()))
    with pytest.raises(media_store.MediaStoreError) as busy:
        with media_store.open_original(vault, path):
            pytest.fail("a live reservation must retain its capacity")
    assert busy.value.code == "MEDIA_STORE_BUSY"
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM media_access WHERE token = 'live'")
    with media_store.open_original(vault, path) as original:
        assert original.stream.read() == expected
        assert not media_store.evict(vault, path)
    assert media_store.evict(vault, path)
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT count(*) FROM media_access").fetchone()[0] == 100


def test_stored_status_is_partial_and_exact_paths_survive_custody_loss(vault: Path, selected_store):
    """A bounded overview must not claim an empty inventory after custody loss hides its candidates."""
    from exomem import commands, media_jobs, media_store

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    relative = path.relative_to(vault).as_posix()
    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        connection.executemany("INSERT INTO media_cache VALUES (?, 'stale', ?, 'stale', 0)",
            [(f"Knowledge Base/zz-stale/{i:04}.bin", "a" * 64) for i in range(media_jobs.STATUS_JOB_LIMIT + 2)])
    result = media_jobs.tool_status(vault)
    assert len(result["stored_originals"]) == media_jobs.STATUS_JOB_LIMIT
    assert any(row["path"] == relative and row["state"] == "stored" for row in result["stored_originals"])
    coverage = result["media_inventory"]["stored_originals"]
    assert coverage == {"source": "media_cache", "limit": media_jobs.STATUS_JOB_LIMIT,
                        "coverage": "partial", "total": None, "state": "available"}
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DROP TABLE media_cache")
    overview = media_jobs.tool_status(vault)
    assert overview["stored_originals"] == []
    assert overview["media_inventory"]["stored_originals"]["state"] == "unavailable"
    exact = commands.op_process_media(vault, operation="status", path=relative)
    assert exact["stored_originals"] == [{"path": relative, "state": "stored", "original_state": "evicted",
                                          "processing_state": "completed"}]
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_schema WHERE name = 'media_cache'").fetchone() is None
    ledger.path.unlink()
    lost = media_jobs.tool_status(vault, path=relative)
    assert lost["stored_originals"] == exact["stored_originals"]
    assert lost["media_inventory"]["offload_refusals"]["state"] == "unavailable"
    assert not ledger.path.exists()


def test_status_path_stays_inside_the_knowledge_base(vault: Path):
    """The newly honored status selector must not read a path outside the vault."""
    from exomem import commands
    from exomem.cli_ops import OpError

    with pytest.raises(OpError) as error:
        commands.op_process_media(vault, operation="status", path="../outside.bin")
    assert error.value.code == "MEDIA_PATH_OUTSIDE_KB"


def test_withheld_candidates_do_not_change_status_coverage(vault: Path, selected_store):
    """A raw inventory count or truncation flag must not reveal withheld originals."""
    from test_governance_egress import _external, _reset_governance_caches, write_rule, write_scope

    from exomem import commands, media_jobs, media_store, writer_lease
    from exomem.governance.principal import request_scope
    from exomem.vault import parse_frontmatter, render_frontmatter_document, serialize_frontmatter

    visible = _preserved(vault, category="aaa-visible")
    _offload(vault, visible)
    assert media_store.evict(vault, visible)
    write_scope(vault, paths="Evidence/Test/zz-hidden/**")
    write_rule(vault, ceiling=0)
    _reset_governance_caches()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "process_media")
    with request_scope(_external()):
        before = writer_lease.invoke_command(command, vault, operation="status")
    sidecar = visible.with_name(visible.name + ".md")
    fields, body, _ = parse_frontmatter(sidecar.read_text(), strict=True)
    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        # Copies model evicted duplicates that pin the same confirmed version.
        for i in range(media_jobs.STATUS_JOB_LIMIT + 2):
            relative = f"Knowledge Base/Evidence/Test/zz-hidden/{i:04}.txt"
            target = vault / (relative + ".md")
            target.parent.mkdir(parents=True, exist_ok=True)
            copy = {**fields, "evidence_file": relative}
            target.write_text(render_frontmatter_document(serialize_frontmatter(copy), body))
            connection.execute("INSERT INTO media_cache VALUES (?, 'candidate', ?, 'evicted', 0)",
                               (relative, fields["binary_sha256"]))
    with request_scope(_external()):
        after = writer_lease.invoke_command(command, vault, operation="status")
    assert after["media_inventory"] == before["media_inventory"]
    assert after["stored_originals"] == before["stored_originals"]
    assert len(after["stored_originals"]) == 1


def test_uncertain_publication_keeps_protection_until_confirmed(vault: Path, selected_store, monkeypatch):
    from exomem import media_jobs, media_store, preserve

    path = _preserved(vault)
    commit = preserve.commit_media_sidecar_writes

    def interrupted(*args, **kwargs):
        commit(*args, **kwargs)
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(preserve, "commit_media_sidecar_writes", interrupted)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        media_store.offload(vault, path)
    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as captured:
        assert captured.execute("SELECT count(*) FROM media_protections").fetchone()[0] == 1
    assert originals.original_state(vault, path).state == "cached"
    monkeypatch.setattr(preserve, "commit_media_sidecar_writes", commit)
    media_store.offload(vault, path)
    with sqlite3.connect(ledger.path) as captured:
        assert captured.execute("SELECT count(*) FROM media_protections").fetchone()[0] == 0


def test_destroyed_version_cannot_authorize_eviction(vault: Path, selected_store):
    from exomem import media_store

    path = _preserved(vault)
    old = _offload(vault, path)
    (Path(selected_store[0]["root"]) / old.object / old.version).unlink()
    assert not media_store.evict(vault, path)
    assert path.exists() and originals.original_state(vault, path).state == "local"
    replacement = _offload(vault, path)
    assert replacement.version != old.version


def test_unknown_store_result_preserves_the_reference_and_local_bytes(vault: Path, selected_store, monkeypatch):
    from exomem import media_store

    path = _preserved(vault)
    reference = _offload(vault, path)

    def unavailable(self, name, version):
        raise TimeoutError("store request timed out")

    monkeypatch.setattr(media_store.FilesystemStore, "head", unavailable)
    assert not media_store.evict(vault, path)
    assert path.exists()
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        media_store.offload(vault, path)
    assert originals.original_state(vault, path).reference == reference


def test_changed_pending_bytes_drop_the_old_stored_version(vault: Path, selected_store):
    from exomem import media_jobs, media_processing, preserve

    captured = preserve.preserve(vault, scope="Test", category="pending", filename="sample.txt", content="initial bytes")
    path = vault / captured.path
    _offload(vault, path)
    path.write_bytes(b"replacement bytes while extraction is pending")
    media_processing.reconcile_media(vault, path, explicit=False)
    assert originals.original_state(vault, path).reference is None
    assert media_jobs.MediaJobStore(vault).get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE).do_offload


def test_watcher_can_read_an_existing_ledger_before_the_media_schema_upgrade(vault: Path, selected_store):
    from exomem import file_watcher, media_jobs

    path = _preserved(vault)
    _offload(vault, path)
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        connection.execute("DROP TABLE media_access")
        connection.execute("DROP TABLE media_cache")
    path.write_bytes(b"replacement bytes before the worker starts")
    watcher = file_watcher.FileWatcher(vault)
    watcher._record(path, deleted=False)
    watcher._flush()
    assert originals.original_state(vault, path).reference is None
    assert media_jobs.MediaJobStore(vault).get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE).do_offload


def test_placement_before_cache_row_crash_recovers_without_provenance_rewrite(vault: Path, selected_store, monkeypatch):
    from exomem import media_jobs, media_processing, media_store

    path = _preserved(vault)
    _offload(vault, path)
    sidecar = path.with_name(path.name + ".md")
    canonical = sidecar.read_bytes()
    assert media_store.evict(vault, path)
    record = media_store._record_cache

    def interrupted(*args, **kwargs):
        raise RuntimeError("cache bookkeeping interrupted")

    monkeypatch.setattr(media_store, "_record_cache", interrupted)
    with pytest.raises(RuntimeError, match="cache bookkeeping interrupted"):
        with media_store.open_original(vault, path):
            pytest.fail("the injected interruption must reach the caller")
    assert path.exists()
    monkeypatch.setattr(media_store, "_record_cache", record)
    assert media_processing.reconcile_media(vault, path, explicit=False).state == "completed"
    assert media_store.cache_matches(vault, path)
    assert sidecar.read_bytes() == canonical
    assert not media_jobs.MediaJobStore(vault).has_binary(path)


def test_status_and_explicit_retry_keep_stored_processing_state(vault: Path, selected_store):
    from exomem import commands, media_jobs, media_processing, media_store

    path = _preserved(vault)
    expected = path.read_bytes()
    _offload(vault, path)
    assert media_store.evict(vault, path)
    status = commands.op_process_media(vault, operation="status")
    assert status["stored_originals"] == [{"path": path.relative_to(vault).as_posix(),
        "state": "stored", "original_state": "evicted", "processing_state": "completed"}]
    assert media_processing.retry_media(vault, path).state == "completed"
    assert path.read_bytes() == expected
    assert not media_jobs.MediaJobStore(vault).has_binary(path)


def test_disabled_store_retains_identity_and_hydration(vault: Path, selected_store):
    from exomem import media_store

    path = _preserved(vault)
    expected = path.read_bytes()
    _offload(vault, path)
    assert media_store.evict(vault, path)
    values, config = selected_store
    values["enabled"] = False
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    assert originals.original_state(vault, path).state == "evicted"
    with media_store.open_original(vault, path) as source:
        assert source.stream.read() == expected
    assert not media_store.evict(vault, path)


def test_pending_processing_excludes_eviction(vault: Path, selected_store):
    from exomem import media_jobs, media_store

    path = _preserved(vault)
    _offload(vault, path)
    ledger = media_jobs.MediaJobStore(vault)
    ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))
    assert not media_store.evict(vault, path)
    assert path.exists()


def test_unsupported_bound_binary_offloads_without_extraction(vault: Path, selected_store):
    from exomem import media_jobs, media_store, media_worker, preserve

    captured = preserve.preserve(vault, scope="Test", category="opaque", filename="sample.bin", content="opaque bytes")
    path = vault / captured.path
    media_store.enqueue_offloads(vault)
    ledger = media_jobs.MediaJobStore(vault)
    job = ledger.claim_next()
    assert job.do_offload and not job.do_ocr
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    assert worker._process(job).state == "complete"
    ledger.complete(job)
    assert originals.original_state(vault, path).reference.version
    assert not ledger.has_binary(path)


def test_eviction_survives_real_reconciliation_and_deletion_fanout(vault: Path, selected_store, monkeypatch):
    import base64

    import numpy as np

    from exomem import embeddings, index_sync, media_processing, media_store, preserve, reconcile
    from exomem.governance import companions

    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    captured = preserve.preserve(vault, scope="Test", category="video", filename="sample.mp4",
                                 content_base64=base64.b64encode(b"original video bytes").decode(),
                                 text="The recording contains a searchable observation.")
    path = vault / captured.path
    media_processing.reconcile_media(vault, path)
    reference = _offload(vault, path)
    sidecar = vault / captured.sidecar_path
    canonical = sidecar.read_bytes()
    vector = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    vector[0] = 1
    embeddings.ClipIndex(vault).upsert_frames(captured.path, [(0.0, vector)], 1.0)
    frames = path.with_name(path.name + ".frames")
    frames.mkdir()
    frame = frames / "scene-001-t0ms.jpg"
    frame.write_bytes(b"derived frame")
    frame.with_name(frame.name + ".md").write_text("# Extracted frame\n")
    assert media_store.evict(vault, path)
    assert companions.classify(vault, captured.path).stored_originals[0].reference == reference
    index_sync.delete_after_remove(vault, [captured.path])
    report = reconcile.reconcile(vault)
    assert report.clip_orphans_removed == 0
    assert embeddings.ClipIndex(vault).has_frames(captured.path)
    assert embeddings.ClipIndex(vault).search(vector, 1)[0][0] == captured.path
    assert frame.exists() and frame.with_name(frame.name + ".md").exists()
    assert sidecar.read_bytes() == canonical
    with media_store.open_original(vault, path) as source:
        assert source.stream.read() == b"original video bytes"


def test_hydration_reservation_bounds_concurrency(vault: Path, selected_store, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from exomem import file_watcher, media_jobs, media_store

    first = _preserved(vault, category="first")
    _offload(vault, first)
    second = _preserved(vault, category="second")
    _offload(vault, second)
    assert media_store.evict(vault, first) and media_store.evict(vault, second)
    values, config = selected_store
    values["max_hydrations"] = 1
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    entered, release = threading.Event(), threading.Event()
    watcher = file_watcher.FileWatcher(vault)
    canonical = first.with_name(first.name + ".md").read_bytes()
    actual = media_store.FilesystemStore.open

    @contextmanager
    def paused(self, name, version):
        entered.set()
        assert release.wait(10)
        with actual(self, name, version) as stream:
            yield stream

    monkeypatch.setattr(media_store.FilesystemStore, "open", paused)

    def read_first():
        with media_store.open_original(vault, first) as source:
            return source.stream.read()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(read_first)
        try:
            assert entered.wait(10)
            watcher._record(first, deleted=False)
            watcher._flush()
            with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_BUSY"):
                with media_store.open_original(vault, second):
                    pytest.fail("a second hydration must wait for capacity")
            assert not second.exists()
        finally:
            release.set()
        assert pending.result(timeout=10) == first.read_bytes()
    watcher._flush()
    assert first.with_name(first.name + ".md").read_bytes() == canonical
    assert not media_jobs.MediaJobStore(vault).has_binary(first)
    first.write_bytes(b"an external replacement after hydration")
    watcher._record(first, deleted=False)
    watcher._flush()
    assert media_jobs.MediaJobStore(vault).has_binary(first)
    assert first.with_name(first.name + ".md").read_bytes() != canonical


def test_hydration_does_not_overwrite_an_external_create(vault: Path, selected_store, monkeypatch):
    from exomem import held_fs, media_store

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    publish = held_fs.publish_bytes

    def external_create(filesystem, parent, leaf, data, **kwargs):
        if leaf == path.name:
            path.write_bytes(b"external edit")
        return publish(filesystem, parent, leaf, data, **kwargs)

    monkeypatch.setattr(held_fs, "publish_bytes", external_create)
    with pytest.raises(held_fs.HeldFsError):
        with media_store.open_original(vault, path):
            pytest.fail("an external create must retain its bytes")
    assert path.read_bytes() == b"external edit"
    assert not media_store.cache_matches(vault, path)


def test_recovery_adoption_and_export_read_the_same_stored_bytes(vault: Path, selected_store):
    from exomem import adoption_plan, artifact_recovery, cloud_operator_portability, media_store
    from exomem.governance.principal import owner_principal

    path = _preserved(vault)
    expected = path.read_bytes()
    _offload(vault, path)
    canonical = path.with_name(path.name + ".md").read_bytes()
    assert media_store.evict(vault, path)
    with artifact_recovery.stage(vault, path.relative_to(vault).as_posix(), owner_principal(surface="mcp")) as (copied, descriptor):
        assert copied.private_path.read_bytes() == expected
        assert descriptor["sha256"] == hashlib.sha256(expected).hexdigest()
        assert not media_store.evict(vault, path)
    assert media_store.evict(vault, path)
    facts, text = adoption_plan.read_facts(vault, path.relative_to(vault).as_posix(), ceiling=1000, text_hint=True)
    assert facts["original_sha256"] == hashlib.sha256(expected).hexdigest()
    assert text == expected.decode()
    assert media_store.evict(vault, path)
    assert path.relative_to(vault) in cloud_operator_portability.archive_members(vault)
    assert path.read_bytes() == expected
    assert path.with_name(path.name + ".md").read_bytes() == canonical


def test_recovery_maps_an_unavailable_stored_original_to_its_public_refusal(vault: Path, selected_store):
    from exomem import artifact_recovery, media_store
    from exomem.governance.principal import owner_principal

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    root = Path(selected_store[0]["root"])
    root.rename(root.with_name("offline-objects"))
    with pytest.raises(artifact_recovery.RecoveryUnavailable):
        with artifact_recovery.stage(vault, path.relative_to(vault).as_posix(), owner_principal(surface="mcp")):
            pytest.fail("an unavailable store must not release an original")
    assert not path.exists()


def test_canonical_exports_include_stored_bytes_and_refuse_corruption(vault: Path, selected_store, tmp_path: Path):
    import io
    import zipfile

    from exomem import media_store, vault_portability

    path = _preserved(vault)
    expected = path.read_bytes()
    reference = _offload(vault, path)
    canonical = path.with_name(path.name + ".md").read_bytes()
    relative = path.relative_to(vault).as_posix()
    context = vault_portability.PortabilityContext(
        cell_id="cell-test", vault_id="vault-test", operation_id="export-test",
        created_at="2026-10-26T00:00:00Z", operator_authorized=True,
        lifecycle_state="quiesced", routing_stopped=True, active_mutations=0,
        background_writers_stopped=True, reads_allowed=True,
    )
    assert media_store.evict(vault, path)
    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=context)
    with zipfile.ZipFile(exported.archive_path) as archive:
        assert archive.read(relative) == expected
    record = next(item for item in exported.manifest["files"] if item["path"] == relative)
    assert record["sha256"] == hashlib.sha256(expected).hexdigest()
    assert media_store.evict(vault, path)
    stream = io.BytesIO()
    vault_portability.stream_quiesced_vault(vault, stream, context=context)
    with zipfile.ZipFile(stream) as archive:
        assert archive.read(relative) == expected
    assert path.with_name(path.name + ".md").read_bytes() == canonical
    assert media_store.evict(vault, path)
    (Path(selected_store[0]["root"]) / reference.object / reference.version).write_bytes(b"corrupt")
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_ORIGINAL_UNVERIFIED"):
        vault_portability.export_quiesced_vault(vault, tmp_path / "failed-export", context=context)
    assert not (tmp_path / "failed-export").exists()
    assert not path.exists()


def test_move_materializes_the_original_and_retains_its_version(vault: Path, selected_store):
    from exomem import media_processing, media_store, move_file
    from exomem.governance import companions

    path = _preserved(vault)
    reference = _offload(vault, path)
    assert media_store.evict(vault, path)
    destination = path.with_name("renamed.txt")
    move_file.move_file(vault, old_path=path.relative_to(vault).as_posix(), new_path=destination.relative_to(vault).as_posix())
    assert not path.exists()
    assert originals.original_state(vault, destination).reference == reference
    assert companions.classify(vault, destination.relative_to(vault).as_posix()).identities
    before = destination.with_name(destination.name + ".md").read_bytes()
    assert media_processing.reconcile_media(vault, destination, explicit=False).state == "completed"
    assert destination.with_name(destination.name + ".md").read_bytes() == before


def test_move_preview_and_withheld_refusal_never_hydrate(vault: Path, selected_store, monkeypatch):
    """Move once hydrated before its checks: an offline store broke NOT_FOUND, and a refusal fetched bytes."""
    from exomem import media_store, move_file
    from exomem.governance import egress

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    relative = path.relative_to(vault).as_posix()
    destination = path.with_name("renamed.txt").relative_to(vault).as_posix()
    root = Path(selected_store[0]["root"])
    root.rename(root.with_name("offline-objects"))
    preview = move_file.move_file(vault, old_path=relative, new_path=destination, validate_only=True)
    assert isinstance(preview, move_file.MoveFileValidation)
    for online in (False, True):
        if online:
            root.with_name("offline-objects").rename(root)
        with monkeypatch.context() as hidden:
            hidden.setattr(egress, "restricted_release_filter", lambda *_args, **_kwargs: lambda _path: False)
            with pytest.raises(move_file.MoveFileError) as withheld:
                move_file.move_file(vault, old_path=relative, new_path=destination)
        assert withheld.value.code == "NOT_FOUND"
    assert originals.original_state(vault, path).state == "evicted"


def test_authorized_path_download_serves_an_evicted_original(vault: Path, selected_store, monkeypatch):
    """The `?path=` download answered an evicted original with 404, as if it were deleted."""
    from starlette.testclient import TestClient

    from exomem import media_store, server, upload_tokens

    path = _preserved(vault)
    expected = path.read_bytes()
    _offload(vault, path)
    assert media_store.evict(vault, path)
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "test-secret")
    client = TestClient(server.build_server(require_auth=False).http_app())
    token = upload_tokens.mint_bound("test-secret", audience="owner")
    response = client.get("/download", params={"path": path.relative_to(vault).as_posix()},
                          headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and response.content == expected


def test_dataset_original_stays_local_so_query_data_reads_it(vault: Path, selected_store):
    """An evicted dataset made query_data answer NOT_FOUND; datasets stay on the volume (D1)."""
    from exomem import media_store, preserve, query_data

    captured = preserve.preserve(vault, scope="Test", category="data", filename="table.csv", content="a,b\n1,2\n")
    path = vault / captured.path
    assert media_store.enqueue_offloads(vault) == 0
    assert media_store.offload(vault, path) is None
    assert not media_store.evict(vault, path)
    assert originals.original_state(vault, path).state == "local"
    assert query_data.query_data(vault, path=captured.path).rows == [{"a": "1", "b": "2"}]


def test_offload_demand_does_not_hide_pending_extraction(vault: Path, selected_store):
    """The offload-only row counted as processing work, so discovery never re-queued a lost extraction."""
    from exomem import media_jobs, media_processing

    path = vault / "Knowledge Base" / "Notes" / "Media" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"pending original")
    media_processing.reconcile_media(vault, path)
    ledger = media_jobs.MediaJobStore(vault)
    ledger.discard(ledger.get_by_binary(path))
    assert ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE).do_offload
    media_processing.reconcile_all_media(vault)
    assert ledger.has_pending_ocr(path)


def test_explicit_trash_removes_the_stored_binding(vault: Path, selected_store):
    from exomem import delete_file, media_jobs, media_processing, media_store, preserve

    path = vault / "Knowledge Base" / "Notes" / "Media" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original bytes")
    media_processing.reconcile_media(vault, path)
    preserve.update_sidecar_extraction(vault, path.with_name(path.name + ".md"), text="extracted text", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    _offload(vault, path)
    assert media_store.evict(vault, path)
    trashed = delete_file.delete_file(vault, path=path.relative_to(vault).as_posix(), confirm=True)
    assert originals.original_state(vault, path).state == "missing"
    assert (vault / trashed.trash_path).read_bytes() == b"original bytes"


def test_original_states_use_only_the_bound_store(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "sample.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    reference = {
        "schema": 1, "store": "test-store", "key_version": "1",
        "object": "a" * 64, "version": "version-1",
        "ciphertext_size": 200, "ciphertext_sha256": "b" * 64,
    }
    fields = {"evidence_file": binary.relative_to(vault).as_posix(),
              "binary_sha256": digest, "binary_size": 8}
    from exomem.vault import render_frontmatter_document, serialize_frontmatter

    sidecar = binary.with_name(binary.name + ".md")
    sidecar.write_text(render_frontmatter_document(serialize_frontmatter(fields), "evidence"))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"vaults": {str(vault): {"store": "test-store"}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    assert originals.original_state(vault, binary).state == "local"
    fields["stored_object"] = reference
    sidecar.write_text(render_frontmatter_document(serialize_frontmatter(fields), "evidence"))
    assert originals.original_state(vault, binary).state == "cached"
    binary.unlink()
    assert originals.original_state(vault, binary).state == "evicted"
    assert originals.original_state(vault, binary, binding=originals.StoreBinding("other")).state == "missing"
    fields.pop("stored_object")
    sidecar.write_text(render_frontmatter_document(serialize_frontmatter(fields), "evidence"))
    assert originals.original_state(vault, binary).state == "missing"


def test_malformed_reference_cannot_prove_an_absent_original(tmp_path: Path):
    from exomem.vault import render_frontmatter_document, serialize_frontmatter

    binary = tmp_path / "Knowledge Base" / "sample.bin"
    binary.parent.mkdir()
    binary.with_name(binary.name + ".md").write_text(render_frontmatter_document(serialize_frontmatter({
        "evidence_file": "Knowledge Base/sample.bin", "binary_sha256": "a" * 64,
        "binary_size": 1, "stored_object": {"store": "test-store"},
    }), ""))
    assert originals.original_state(tmp_path, binary, binding=originals.StoreBinding("test-store")).state == "missing"


@pytest.mark.parametrize("filename, layout", [
    ("upload.txt", "native"), ("original.md", "opaque"),
    ("original.md", "notes"), ("original.md", "double"),
])
def test_captured_source_original_reconciles_and_recovers_without_metadata_rewrites(
    vault: Path, source_schema, selected_store, tmp_path: Path, filename, layout,
):
    from test_source_corrections import _legacy_markdown_pair

    from exomem import (
        add,
        artifact_recovery,
        media_jobs,
        media_processing,
        media_store,
        preserve,
        source_capture,
    )
    from exomem import vault as vault_module
    from exomem.governance.principal import owner_principal, request_scope

    raw_identity = "00000000-0000-4000-8000-000000000001"
    original = f"---\ntype: source\nexomem_id: {raw_identity}\n---\nOriginal bytes.\r\n".encode()
    staged = tmp_path / filename
    staged.write_bytes(original)
    mime = "text/plain" if filename.endswith(".txt") else "text/markdown; charset=utf-8"
    captured = add.add(vault, source_schema, title="Captured original", content="", source_type="session",
                       artifact=add.SourceArtifact(staged, filename, mime))
    path, page = vault / captured.artifact_path, vault / captured.path
    if layout in {"notes", "double"}:
        path, page = _legacy_markdown_pair(vault, path, page)
        if layout == "notes":
            canonical = preserve._sidecar_paths(path, protected=False, source=True)[0]
            page.rename(canonical)
            page = canonical
        else:
            unrelated = add.add(vault, source_schema, title="Captured original notes", content="Unrelated canonical Source.", source_type="session")
            assert vault / unrelated.path == preserve._sidecar_paths(path, protected=False, source=True)[0]
    elif layout == "opaque":
        assert path.name.endswith(".md.bin")
    metadata, _, _ = vault_module.parse_frontmatter(page.read_text())
    assert metadata["exomem_id"] != raw_identity
    assert metadata["original_filename"] == filename and metadata["original_content_type"] == mime
    classified = source_capture.canonical_pages(vault)
    assert page.relative_to(vault).as_posix() in classified.canonical
    assert path.relative_to(vault).as_posix() not in classified.canonical
    if layout == "native":
        assert path.name != filename
        media_processing.reconcile_media(vault, path, explicit=False)
        pending, _, _ = vault_module.parse_frontmatter(page.read_text())
        assert pending["exomem_id"] == metadata["exomem_id"]
        assert pending["original_filename"] == filename and pending["original_content_type"] == mime
        preserve.update_sidecar_extraction(vault, page, text="Completed original text.", engine="text")
        media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
        completed = page.read_bytes()
        assert media_processing.reconcile_media(vault, path, explicit=False).state == "completed"
        assert page.read_bytes() == completed
    else:
        assert media_processing.reconcile_media(vault, path, explicit=False) is None
    # A `.md` original is a vault note: it is never offloaded, so never evicted (D1).
    stored = layout in {"native", "opaque"}
    media_store.enqueue_offloads(vault)
    assert (_offload(vault, path) is not None) is stored
    published = page.read_bytes()
    assert originals.original_state(vault, path).sidecar_path == page
    assert media_store.evict(vault, path) is stored
    assert originals.original_state(vault, path).state == ("evicted" if stored else "local")
    assert media_processing.reconcile_all_media(vault) == 0
    assert page.read_bytes() == published
    who = owner_principal(surface="mcp")
    with request_scope(who), artifact_recovery.stage(vault, page.relative_to(vault).as_posix(), who) as (copied, descriptor):
        assert copied.private_path.read_bytes() == original
        assert descriptor["file_name"] == filename and descriptor["mime_type"] == mime
    assert media_processing.reconcile_all_media(vault) == 0
    assert page.read_bytes() == published
    assert not media_jobs.MediaJobStore(vault).has_pending_ocr(path)
