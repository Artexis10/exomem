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
    values["allowance_bytes"] = 10_000_000
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
    # A failed or blocked job, an engine wait included, keeps no original (D3, M4).
    assert media_store.evict(vault, path)


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
    """An original above the object bound stays resident through discovery and offloads once the bound allows it."""
    import os

    from exomem import media_jobs, media_store, media_worker

    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "max_object_bytes": 64}}}))
    path = _preserved(vault)
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    assert originals.original_state(vault, path).state == "local"
    assert media_jobs.MediaJobStore(vault).get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is None
    assert media_store.enqueue_offloads(vault) == 0
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
        connection.executemany("INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read) "
                               "VALUES (?, 'stale', ?, 'stale', 0)",
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
    [exact] = commands.op_process_media(vault, operation="status", path=relative)["results"]
    assert exact["processing"] == {"state": "completed", "source": "sidecar", "freshness": "unknown"}
    assert exact["stored_originals"] == [{"path": relative, "sidecar_path": relative + ".md", "state": "stored", "original_state": "evicted",
                                          "processing_state": "completed"}]
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_schema WHERE name = 'media_cache'").fetchone() is None
    ledger.path.unlink()
    lost = media_jobs.tool_status(vault, path=relative)
    assert lost["stored_originals"] == exact["stored_originals"]
    assert lost["media_inventory"]["offload_refusals"]["state"] == "unavailable"
    assert not ledger.path.exists()


def test_exact_status_distinguishes_extraction_from_offload(vault: Path, selected_store):
    """Offload demand preserves a completed transcript; new extraction demand reports pending."""
    from exomem import commands, media_jobs

    path = _preserved(vault)
    relative = path.relative_to(vault).as_posix()
    ledger = media_jobs.MediaJobStore(vault)
    assert ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is not None
    [offload] = commands.op_process_media(vault, operation="status", path=relative)["results"]
    assert offload["processing"] == {"state": "completed", "source": "sidecar", "freshness": "unknown"}
    ledger.enqueue(media_jobs.MediaJob(binary_path=path, sidecar_path=path.with_name(path.name + ".md"),
                                     media_type="text", do_ocr=True, do_clip=False))
    [extraction] = commands.op_process_media(vault, operation="status", path=relative)["results"]
    assert extraction["processing"] == {"state": "pending", "source": "job"}


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
            connection.execute("INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read) "
                               "VALUES (?, 'candidate', ?, 'evicted', 0)",
                               (relative, fields["binary_sha256"]))
    with request_scope(_external()):
        after = writer_lease.invoke_command(command, vault, operation="status")
    assert after["media_inventory"] == before["media_inventory"]
    assert after["stored_originals"] == before["stored_originals"]
    assert len(after["stored_originals"]) == 1


@pytest.mark.parametrize("evicted", [False, True])
def test_exact_status_withholds_all_facts_for_a_restricted_original(vault: Path, selected_store, evicted):
    """A selector must not disclose private counts or index work beside a filtered job list."""
    from test_governance_egress import _external, _reset_governance_caches, write_rule, write_scope

    from exomem import commands, deferred_index, media_jobs, media_store, writer_lease
    from exomem.governance.principal import request_scope

    path = _preserved(vault, category="restricted")
    _offload(vault, path)
    if evicted:
        assert media_store.evict(vault, path)
    relative = path.relative_to(vault).as_posix()
    deferred_index.add_full(vault, [relative + ".md"])
    write_scope(vault, paths="Evidence/Test/restricted/**")
    write_rule(vault, ceiling=0)
    _reset_governance_caches()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "process_media")
    with request_scope(_external()):
        before = writer_lease.invoke_command(command, vault, operation="status", path=relative)
    assert before["results"] == []
    ledger = media_jobs.MediaJobStore(vault)
    other = _preserved(vault, category="unrelated")
    ledger.enqueue(media_jobs.MediaJob(binary_path=other, sidecar_path=other.with_name(other.name + ".md"),
                                     media_type="audio", do_ocr=True, do_clip=False))
    deferred_index.add_full(vault, [other.relative_to(vault).as_posix() + ".md"])
    with request_scope(_external()):
        after = writer_lease.invoke_command(command, vault, operation="status", path=relative)
    assert after == before


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
        "sidecar_path": path.relative_to(vault).as_posix() + ".md", "state": "stored", "original_state": "evicted", "processing_state": "completed"}]
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


def test_trash_keeps_a_stored_version_accounted_until_its_original_recovers(vault: Path, selected_store):
    from exomem import (
        delete_file,
        list_trash,
        media_allowance,
        media_jobs,
        media_processing,
        media_store,
        preserve,
        recover_from_trash,
        trash_metadata,
    )

    path = vault / "Knowledge Base" / "Notes" / "Media" / "sample.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original bytes")
    media_processing.reconcile_media(vault, path)
    preserve.update_sidecar_extraction(vault, path.with_name(path.name + ".md"), text="extracted text", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    reference = _offload(vault, path)
    assert media_store.evict(vault, path)
    assert media_allowance.reference_usage(vault).used_bytes == len(b"original bytes")
    trashed = delete_file.delete_file(vault, path=path.relative_to(vault).as_posix(), confirm=True)
    assert originals.original_state(vault, path).state == "missing"
    assert (vault / trashed.trash_path).read_bytes() == b"original bytes"
    assert media_allowance.reference_usage(vault).used_bytes == len(b"original bytes")
    listing = list_trash.list_trash(vault)
    assert [entry.trash_path for entry in listing.entries] == [trashed.trash_path]
    assert not listing.orphan_files and not listing.orphan_sidecars

    recover_from_trash.recover_from_trash(vault, trash_path=trashed.trash_path)

    assert path.read_bytes() == b"original bytes"
    assert originals.original_state(vault, path).state == "local"
    assert media_allowance.reference_usage(vault).used_bytes == 0
    assert not originals.sidecar_path(vault / trashed.trash_path).exists()
    assert not (vault / (trashed.trash_path + trash_metadata.METADATA_SUFFIX)).exists()
    assert (Path(selected_store[0]["root"]) / reference.object / reference.version).exists()


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


def test_reference_usage_counts_live_and_trash_versions_once(vault: Path, selected_store):
    """Archived and Trash companions count once, while a retained unreferenced object does not count."""
    from exomem import media_allowance, move_file, preserve
    from exomem.vault import parse_frontmatter

    path = _preserved(vault)
    expected = len(path.read_bytes())
    reference = _offload(vault, path)
    sidecar = path.with_name(path.name + ".md")
    fields, body, _ = parse_frontmatter(sidecar.read_text(), strict=True)
    archive_binary = "Knowledge Base/_archive/retained.txt"
    archive = vault / (archive_binary + ".md")
    archive.parent.mkdir(parents=True)
    archive.write_text(move_file._repoint_artifact(path.relative_to(vault).as_posix(), archive_binary)(sidecar.read_text()))
    trash_root = "Knowledge Base/_trash/2026-10-28/bundle"
    trash_sidecar = vault / trash_root / sidecar.name
    trash_sidecar.parent.mkdir(parents=True)
    trash_sidecar.write_bytes(sidecar.read_bytes())
    metadata = vault / (trash_root + ".meta.json")
    metadata.write_text(json.dumps({"original_path": path.parent.relative_to(vault).as_posix()}))

    captured = preserve.preserve(vault, scope="Test", category="retained", filename="other.txt",
                                content="A distinct original retained only in provider history.")
    other = vault / captured.path
    preserve.update_sidecar_extraction(vault, vault / captured.sidecar_path, text="Retained history.", engine="text")
    from exomem import media_jobs

    media_jobs.MediaJobStore(vault).retire_ocr(other, "text")
    unreferenced = _offload(vault, other)
    (vault / captured.sidecar_path).unlink()
    assert (Path(selected_store[0]["root"]) / unreferenced.object / unreferenced.version).exists()
    usage = media_allowance.reference_usage(vault)
    assert usage.used_bytes == expected
    assert len(usage.versions) == 1
    assert media_allowance.projected_bytes(usage, originals.BinaryBinding("another.txt", fields["binary_sha256"], expected), reference) == expected

    # Once only historical Trash placement remains, unreadable metadata makes its custody unknown.
    sidecar.unlink()
    archive.unlink()
    metadata.write_text("not JSON")
    assert media_allowance.reference_usage(vault).used_bytes is None
    metadata.write_text(json.dumps({"original_path": path.parent.relative_to(vault).as_posix()}))
    assert media_allowance.reference_usage(vault).used_bytes == expected
    trash_sidecar.unlink()
    assert media_allowance.reference_usage(vault).used_bytes == 0


def test_reference_usage_does_not_hide_a_malformed_potential_companion(vault: Path, selected_store):
    """An unfinished header cannot become a false zero; ordinary body examples do not affect custody."""
    from exomem import media_allowance

    path = _preserved(vault)
    expected = len(path.read_bytes())
    _offload(vault, path)
    note = vault / "Knowledge Base/Notes/incomplete.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\nevidence_file: unresolved\n")
    assert media_allowance.reference_usage(vault).used_bytes is None
    note.write_text("An ordinary example in the body:\n\n```yaml\nstored_object: invalid example\n```\n")
    assert media_allowance.reference_usage(vault).used_bytes == expected


def test_a_companion_candidate_with_an_unterminated_header_is_refused_not_skipped(vault: Path):
    """Custody readers refuse a header they cannot prove empty; ordinary strict readers treat it as prose."""
    from exomem.vault import FrontmatterError

    path = _preserved(vault)
    relative = path.relative_to(vault).as_posix()
    assert originals.bound_companion(vault, relative) is not None
    originals.sidecar_path(path).write_text("---\nevidence_file: unresolved\n")
    with pytest.raises(FrontmatterError):
        originals.bound_companion(vault, relative)


def test_reference_usage_rejects_conflicting_sizes_and_ignores_a_foreign_store(vault: Path, selected_store):
    """One version has one original size; a foreign store never spends this cell's allowance."""
    from exomem import media_allowance, move_file
    from exomem.vault import parse_frontmatter, render_frontmatter_document, serialize_frontmatter

    path = _preserved(vault)
    expected = len(path.read_bytes())
    _offload(vault, path)
    sidecar = path.with_name(path.name + ".md")
    fields, body, _ = parse_frontmatter(sidecar.read_text(), strict=True)
    other_rel = "Knowledge Base/Evidence/other.txt"
    other = vault / (other_rel + ".md")
    other.parent.mkdir(parents=True, exist_ok=True)
    moved, body, _ = parse_frontmatter(
        move_file._repoint_artifact(path.relative_to(vault).as_posix(), other_rel)(sidecar.read_text()), strict=True,
    )
    copy = {**moved, "binary_size": expected + 1}
    other.write_text(render_frontmatter_document(serialize_frontmatter(copy), body))
    assert media_allowance.reference_usage(vault).used_bytes is None
    copy["stored_object"] = {**fields["stored_object"], "store": "foreign-store"}
    other.write_text(render_frontmatter_document(serialize_frontmatter(copy), body))
    assert media_allowance.reference_usage(vault).used_bytes == expected


@pytest.mark.parametrize("recovery", ["free", "raise"])
def test_allowance_worker_retains_demand_until_current_quota_admits_it(vault: Path, selected_store, monkeypatch, recovery):
    """Quota must refuse before encryption, retain demand, and recover without changing the original's SHA."""
    import os

    from exomem import media_allowance, media_jobs, media_store, media_worker, preserve

    values, config = selected_store
    first = _preserved(vault, category="quota-first")
    size = len(first.read_bytes())
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "allowance_bytes": size}}}))
    _offload(vault, first)  # The exact allowance admits the first original.
    trash = vault / "Knowledge Base/_trash/retained.txt.md"
    trash.parent.mkdir()
    trash.write_bytes(first.with_name(first.name + ".md").read_bytes())
    trash.with_name("retained.txt.meta.json").write_text(json.dumps({"original_path": first.relative_to(vault).as_posix()}))
    assert media_allowance.reference_usage(vault).used_bytes == size
    captured = preserve.preserve(vault, scope="Test", category="quota-next", filename="next.bin", content="Another original.")
    path = vault / captured.path
    original = path.read_bytes()
    digest = originals.original_state(vault, path).binary_sha256
    ledger = media_jobs.MediaJobStore(vault)

    with monkeypatch.context() as refused:
        run = media_store.subprocess.run

        def no_encrypt(command, *args, **kwargs):
            if command[0] == "age":
                pytest.fail("quota refusal reached encryption")
            return run(command, *args, **kwargs)

        def no_upload(*args, **kwargs):
            pytest.fail("quota refusal reached the provider")

        refused.setattr(media_store.subprocess, "run", no_encrypt)
        refused.setattr(media_store.FilesystemStore, "upload", no_upload)
        assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
        assert media_store.enqueue_offloads(vault) == 0
        assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    pending = ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE)
    assert pending.do_offload and "MEDIA_ALLOWANCE_EXCEEDED" in pending.last_error
    assert path.read_bytes() == original
    [row] = media_jobs.tool_status(vault)["offload_refusals"]
    assert row["code"] == "MEDIA_ALLOWANCE_EXCEEDED" and row["ciphertext_size"] is None
    assert set(row) == {"path", "original_state", "code", "ciphertext_size"}
    if recovery == "free":
        first.with_name(first.name + ".md").unlink()
        trash.unlink()
    else:
        config.write_text(json.dumps({"vaults": {str(vault): {**values, "allowance_bytes": size + len(original)}}}))
    assert media_store.enqueue_offloads(vault) == 1
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    stored = originals.original_state(vault, path)
    assert stored.state == "cached" and stored.binary_sha256 == digest
    assert ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is None
    assert media_jobs.tool_status(vault)["offload_refusals"] == []


def test_allowance_refusal_reports_the_current_unknown_or_exceeded_code(vault: Path, selected_store):
    import os

    from exomem import media_jobs, media_store, media_worker

    values, config = selected_store
    path = _preserved(vault)
    without_allowance = {key: value for key, value in values.items() if key != "allowance_bytes"}
    config.write_text(json.dumps({"vaults": {str(vault): without_allowance}}))
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    [row] = media_jobs.tool_status(vault)["offload_refusals"]
    assert row["code"] == "MEDIA_ALLOWANCE_UNKNOWN"
    note = vault / "Knowledge Base/Notes/broken.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("---\nunterminated: custody\n")
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "allowance_bytes": 0}}}))
    assert media_jobs.tool_status(vault)["offload_refusals"][0]["code"] == "MEDIA_ALLOWANCE_UNKNOWN"
    note.write_text("Ordinary prose.\n")
    assert media_jobs.tool_status(vault)["offload_refusals"][0]["code"] == "MEDIA_ALLOWANCE_EXCEEDED"
    assert media_store.enqueue_offloads(vault) == 0
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, path)
    assert refused.value.code == "MEDIA_ALLOWANCE_EXCEEDED"
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    assert media_store.enqueue_offloads(vault) == 1
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    assert originals.original_state(vault, path).state == "cached"


def test_allowance_rechecks_a_new_upload_before_publishing_and_keeps_protection(vault: Path, selected_store, monkeypatch):
    from exomem import media_jobs, media_store

    values, config = selected_store
    path = _preserved(vault)
    original = path.read_bytes()
    upload = media_store.FilesystemStore.upload

    def lower_after_upload(*args, **kwargs):
        version = upload(*args, **kwargs)
        config.write_text(json.dumps({"vaults": {str(vault): {**values, "allowance_bytes": 0}}}))
        return version

    monkeypatch.setattr(media_store.FilesystemStore, "upload", lower_after_upload)
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, path)
    assert refused.value.code == "MEDIA_ALLOWANCE_EXCEEDED"
    assert path.read_bytes() == original
    assert originals.original_state(vault, path).reference is None
    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT count(*) FROM media_protections").fetchone()[0] == 1
        assert connection.execute("SELECT ciphertext_size FROM media_offload_refusals").fetchone()[0] is None


def test_stored_companion_stays_live_when_trash_placement_cannot_be_written(vault: Path, selected_store, monkeypatch):
    from exomem import (
        delete_file,
        media_allowance,
        media_jobs,
        media_processing,
        preserve,
        reserved_paths,
    )

    path = vault / "Knowledge Base/Notes/Media/clip.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original bytes")
    media_processing.reconcile_media(vault, path)
    preserve.update_sidecar_extraction(vault, originals.sidecar_path(path), text="searchable text", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    _offload(vault, path)
    companion = originals.sidecar_path(path)
    before = companion.read_bytes()
    publish = reserved_paths.publish_generic_bytes

    def fail_placement(root, name, data, **kwargs):
        if str(name).endswith(".meta.json"):
            raise OSError("placement write failed")
        return publish(root, name, data, **kwargs)

    monkeypatch.setattr(reserved_paths, "publish_generic_bytes", fail_placement)
    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(vault, path=companion.relative_to(vault).as_posix(), confirm=True)
    assert refused.value.code == "DELETE_FAILED"
    assert companion.read_bytes() == before
    assert media_allowance.reference_usage(vault).used_bytes == len(path.read_bytes())


def test_reference_restored_during_final_census_cannot_publish_over_quota(vault: Path, selected_store, monkeypatch):
    from exomem import media_allowance, media_jobs, media_store

    values, config = selected_store
    retained = _preserved(vault, category="retained-version")
    _offload(vault, retained)
    companion = originals.sidecar_path(retained)
    saved = companion.read_bytes()
    companion.unlink()
    candidate = _preserved(vault, category="new-version")
    expected = candidate.read_bytes()
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "allowance_bytes": len(expected)}}}))
    marker = candidate.parent / "z-marker.md"
    marker.write_text("---\ncensus_marker: true\n---\n")
    upload = media_store.FilesystemStore.upload
    uploaded, restored = [], []

    def record_upload(*args, **kwargs):
        version = upload(*args, **kwargs)
        uploaded.append(version)
        return version

    parse = media_allowance.parse_frontmatter

    def restore_reference(text, **kwargs):
        parsed = parse(text, **kwargs)
        if uploaded and parsed[0].get("census_marker") and not restored:
            companion.write_bytes(saved)
            restored.append(True)
        return parsed

    monkeypatch.setattr(media_store.FilesystemStore, "upload", record_upload)
    monkeypatch.setattr(media_allowance, "parse_frontmatter", restore_reference)
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, candidate)
    assert restored and refused.value.code == "MEDIA_ALLOWANCE_UNKNOWN"
    assert candidate.read_bytes() == expected
    assert originals.original_state(vault, candidate).reference is None
    assert media_allowance.reference_usage(vault).used_bytes == len(retained.read_bytes())
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        assert connection.execute("SELECT count(*) FROM media_protections").fetchone()[0] == 1


def test_archive_reuse_rechecks_the_last_reference_after_confirmation(vault: Path, selected_store, monkeypatch):
    from dataclasses import replace

    from exomem import media_allowance, media_jobs, media_store, move_file
    from exomem.vault import parse_frontmatter, render_frontmatter_document, serialize_frontmatter

    values, config = selected_store
    first = _preserved(vault, category="archive-reuse")
    reference = _offload(vault, first)
    backend = media_store.store_for(vault)
    with backend.open(reference.object, reference.version) as stream:
        retained = replace(reference, version=backend.upload(reference.object, stream, size=reference.ciphertext_size, sha256=reference.ciphertext_sha256))
    sidecar = first.with_name(first.name + ".md")
    archive_path = "Knowledge Base/_archive/retained.txt"
    archive = vault / (archive_path + ".md")
    archive.parent.mkdir()
    archive.write_text(move_file._repoint_artifact(first.relative_to(vault).as_posix(), archive_path)(sidecar.read_text()))
    trash = vault / "Knowledge Base/_trash/retained.txt.md"
    trash.parent.mkdir()
    trash.write_bytes(sidecar.read_bytes())
    trash.with_name("retained.txt.meta.json").write_text(json.dumps({"original_path": first.relative_to(vault).as_posix()}))
    sidecar.unlink()
    usage = media_allowance.reference_usage(vault)
    assert usage.used_bytes == len(first.read_bytes())
    assert list(usage.live_paths.values()) == [archive_path]
    path = _preserved(vault, category="archive-duplicate")
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "allowance_bytes": len(path.read_bytes())}}}))
    ledger = media_jobs.MediaJobStore(vault)
    head = media_store.FilesystemStore.head
    changed = []

    def change_during_protected_confirmation(*args, **kwargs):
        result = head(*args, **kwargs)
        with sqlite3.connect(ledger.path) as connection:
            protected = connection.execute("SELECT 1 FROM media_protections WHERE binary_rel = ?", (path.relative_to(vault).as_posix(),)).fetchone()
        if protected and not changed:
            fields, body, _marker = parse_frontmatter(archive.read_text(), strict=True)
            fields["stored_object"] = retained.fields()
            archive.write_text(render_frontmatter_document(serialize_frontmatter(fields), body))
            trash.unlink()
            changed.append(True)
        return result

    monkeypatch.setattr(media_store.FilesystemStore, "head", change_during_protected_confirmation)
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, path)
    assert changed and refused.value.code == "MEDIA_ALLOWANCE_EXCEEDED"
    assert path.exists() and originals.original_state(vault, path).reference is None
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT count(*) FROM media_protections").fetchone()[0] == 1


def test_a_resident_original_gets_no_offload_job(vault: Path, selected_store):
    """Catches filing that queues an offload the store must refuse after encrypting the whole file."""
    from exomem import media_jobs

    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "max_temporary_bytes": 1000}}}))

    path = _preserved(vault)

    assert media_jobs.MediaJobStore(vault).get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is None
    assert originals.original_state(vault, path).state == "local"


def test_offload_refuses_a_resident_original_before_it_encrypts(vault: Path, selected_store, monkeypatch):
    """Catches offload writing a ciphertext temporary file for an original that can never leave the volume."""
    import tempfile

    from exomem import media_store

    path = _preserved(vault)
    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "max_temporary_bytes": 1000}}}))
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda *a, **k: pytest.fail("a resident original needs no temporary file"))
    monkeypatch.setattr(media_store.subprocess, "run", lambda *a, **k: pytest.fail("a resident original is never encrypted"))

    assert _offload(vault, path) is None

    refusals, _coverage = media_store.offload_refusals(vault)
    assert [(row["path"], row["code"]) for row in refusals] == [(path.relative_to(vault).as_posix(), "MEDIA_ORIGINAL_RESIDENT")]
    assert originals.original_state(vault, path).state == "local"


def test_eviction_skips_an_original_whose_plaintext_exceeds_the_hydration_capacity(vault: Path, selected_store):
    """Catches eviction of an original that no hydration could bring back after the capacity shrank."""
    from exomem import media_store

    path = _preserved(vault)
    _offload(vault, path)
    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "max_temporary_bytes": path.stat().st_size - 1}}}))

    assert not media_store.evict(vault, path)
    assert path.exists()


def _stored(vault: Path, category: str, size: int) -> Path:
    """A filed original of `size` bytes whose stored copy is confirmed and cached, with no job left."""
    from exomem import media_jobs, preserve

    captured = preserve.preserve(vault, scope="Test", category=category, filename="sample.txt", content="x" * size)
    path = vault / captured.path
    preserve.update_sidecar_extraction(vault, vault / captured.sidecar_path, text="An observation.", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    _offload(vault, path)
    assert originals.original_state(vault, path).state == "cached"
    return path


def _volume(total: int, base: int, *paths: Path):
    """Disk usage in which only `paths` that are still on the volume add to `base`."""
    from types import SimpleNamespace

    def usage(_path):
        used = base + sum(path.stat().st_size for path in paths if path.exists())
        return SimpleNamespace(total=total, used=used, free=total - used)

    return usage


def _configure(selected_store, vault: Path, **changes):
    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {**values, **changes}}}))


def test_a_never_read_original_leaves_under_pressure_and_an_owner_read_one_stays(vault: Path, selected_store, monkeypatch):
    """Catches idle time that counts processing as a read, or that ignores an owner's recent read."""
    from exomem import media_store

    unread = _stored(vault, "unread", 300)
    played = _stored(vault, "played", 600)
    _configure(selected_store, vault, minimum_idle_seconds=3600)
    with media_store.open_original(vault, played, read=True):
        pass
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 7_000, unread, played))

    media_store.evict_to_target(vault)

    assert originals.original_state(vault, unread).state == "evicted"
    assert originals.original_state(vault, played).state == "cached"


def test_eviction_takes_unread_originals_first_and_among_them_those_with_no_pending_job(vault: Path, selected_store, monkeypatch):
    """Catches eviction in plain size order, which takes an owner's old video before an unread recording."""
    from exomem import media_jobs, media_store

    played = _stored(vault, "played", 900)
    queued = _stored(vault, "queued", 600)
    idle = _stored(vault, "idle", 300)
    with media_store.open_original(vault, played, read=True):
        pass
    media_jobs.MediaJobStore(vault).enqueue(media_jobs.MediaJob(queued, queued.with_name(queued.name + ".md"), "text"))
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 7_500, played, queued, idle))
    order: list[Path] = []
    actual = media_store.evict
    monkeypatch.setattr(media_store, "evict", lambda vault, path: order.append(path) or actual(vault, path))

    media_store.evict_to_target(vault)

    assert order == [idle, queued, played]


def test_a_pending_job_that_no_worker_claimed_does_not_keep_its_original(vault: Path, selected_store):
    """Catches the old rule under which every queued transcription kept its recording on the volume."""
    from exomem import media_jobs, media_store

    path = _stored(vault, "recording", 300)
    media_jobs.MediaJobStore(vault).enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))

    assert media_store.evict(vault, path)


def test_a_claimed_job_keeps_its_original_across_a_timeout_slice_and_a_memory_stop(vault: Path, selected_store):
    """Catches a long recording that is evicted and hydrated again for each slice."""
    from exomem import media_brakes, media_jobs, media_store

    path = _stored(vault, "recording", 300)
    ledger = media_jobs.MediaJobStore(vault)
    ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))

    sliced = ledger.claim_next()
    assert ledger.record_timeout_stop(sliced, stop_limit=5, context="slice") == media_jobs.PENDING
    assert not media_store.evict(vault, path)
    stopped = ledger.claim_next()
    assert ledger.record_memory_stop(stopped, kind=media_brakes.HARD_LIMIT, stop_limit=5, context="stop") == media_jobs.PENDING
    assert not media_store.evict(vault, path)
    ledger.complete(ledger.claim_next())
    assert media_store.evict(vault, path)


@pytest.mark.parametrize("state, reason", [("failed", None), ("blocked", "engine_unavailable")])
def test_failed_and_blocked_jobs_do_not_keep_their_originals(vault: Path, selected_store, state, reason):
    """Catches a pin left by a job that ended without completing, an engine wait included."""
    from exomem import media_jobs, media_store

    path = _stored(vault, "recording", 300)
    ledger = media_jobs.MediaJobStore(vault)
    ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))
    claimed = ledger.claim_next()

    assert ledger.mark(claimed, state, "stopped", blocked_reason=reason)
    assert media_store.evict(vault, path)


def test_a_demand_evicts_below_the_everyday_stop_point_until_the_budget_covers_it(vault: Path, selected_store, monkeypatch):
    """Catches eviction that waits for the everyday target while an import series stalls."""
    from exomem import media_store

    large, middle, small = _stored(vault, "large", 3_000), _stored(vault, "middle", 2_000), _stored(vault, "small", 1_000)
    monkeypatch.setattr(shutil, "disk_usage", _volume(100_000, 20_000, large, middle, small))
    assert media_store.evict_to_target(vault) == 0

    assert media_store.record_demand(vault, "series-a", 4_000, budget=0, reachable_budget=6_000)
    assert media_store.evict_to_target(vault) == 2

    assert not large.exists() and not middle.exists() and small.exists()


def test_a_lapsed_demand_evicts_nothing(vault: Path, selected_store, monkeypatch):
    """Catches a demand that keeps the owner's cache cold after its sender stopped renewing it."""
    import time

    from exomem import media_store

    path = _stored(vault, "recording", 3_000)
    monkeypatch.setattr(shutil, "disk_usage", _volume(100_000, 20_000, path))
    assert media_store.record_demand(vault, "series-a", 2_000, budget=0, reachable_budget=6_000)
    renewed = time.time()
    monkeypatch.setattr(media_store.time, "time", lambda: renewed + media_store.DEMAND_LEASE_SECONDS + 1)

    assert media_store.evict_to_target(vault) == 0
    assert path.exists()


def test_a_demand_that_eviction_cannot_meet_and_one_above_the_reachable_budget_evict_nothing(vault: Path, selected_store, monkeypatch):
    """Catches eviction that empties the cache for a demand it cannot meet, or trusts any status caller."""
    from exomem import media_store

    first, second = _stored(vault, "first", 2_000), _stored(vault, "second", 1_000)
    monkeypatch.setattr(shutil, "disk_usage", _volume(100_000, 20_000, first, second))

    assert media_store.record_demand(vault, "short", 4_000, budget=500, reachable_budget=6_000)
    assert media_store.evict_to_target(vault) == 0
    assert not media_store.record_demand(vault, "short", 40_000, budget=0, reachable_budget=9_000)
    assert media_store.evict_to_target(vault) == 0
    assert first.exists() and second.exists()


def test_two_series_keep_separate_demands_and_eviction_serves_the_larger(vault: Path, selected_store, monkeypatch):
    """Catches one demand per cell, where a later small demand would replace a waiting larger one."""
    from exomem import media_jobs, media_store

    large, middle, small = _stored(vault, "large", 2_000), _stored(vault, "middle", 1_000), _stored(vault, "small", 500)
    monkeypatch.setattr(shutil, "disk_usage", _volume(100_000, 20_000, large, middle, small))

    assert media_store.record_demand(vault, "series-b", 2_500, budget=0, reachable_budget=6_000)
    assert media_store.record_demand(vault, "series-a", 1_500, budget=0, reachable_budget=6_000)
    assert media_store.evict_to_target(vault) == 2

    assert not large.exists() and not middle.exists() and small.exists()
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        assert {row[0] for row in connection.execute("SELECT series FROM media_demands")} == {"series-a", "series-b"}


def test_eviction_reads_a_bounded_candidate_set_and_no_sidecar(vault: Path, selected_store, monkeypatch):
    """Catches eviction that walks every sidecar, or every cached original, on each check."""
    from exomem import media_jobs, media_store

    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        connection.executemany(
            "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size) VALUES (?, '{}', ?, '', 0, 100)",
            [(f"Knowledge Base/Evidence/{index}.bin", "0" * 64) for index in range(500)])
    monkeypatch.setattr(media_store, "bound_originals", lambda *a, **k: pytest.fail("eviction reads no sidecar"))
    considered: list[Path] = []

    def refuse(_vault, path):
        considered.append(path)
        return False

    monkeypatch.setattr(media_store, "evict", refuse)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000))

    assert media_store.evict_to_target(vault) == 0
    assert 0 < len(considered) <= 64


def test_eviction_follows_an_offload_without_a_restart_and_offload_records_no_read(vault: Path, selected_store, monkeypatch):
    """Catches eviction that waits for the start-up scan, or an offload that stamps the original as read."""
    import os

    from exomem import media_worker

    _configure(selected_store, vault, minimum_idle_seconds=3600)
    path = _preserved(vault)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 7_500, path))

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0

    assert originals.original_state(vault, path).state == "evicted"


def test_an_offload_is_claimed_before_queued_transcription(vault: Path, selected_store):
    """Catches a claim in plain queue order, where a long transcription queue delays the room an offload frees."""
    from exomem import media_jobs

    ledger = media_jobs.MediaJobStore(vault)
    queued = vault / "queued.mp3"
    ledger.enqueue(media_jobs.MediaJob(queued, queued.with_name("queued.mp3.md"), "audio"))
    filed = _preserved(vault)

    claimed = ledger.claim_next()

    assert claimed.do_offload and claimed.binary_path == filed


def test_a_fresh_job_whose_original_is_on_the_volume_runs_before_an_older_evicted_one(vault: Path, selected_store):
    """Catches a claim that hydrates the oldest job first while a capture waits with its bytes on the volume."""
    from exomem import media_jobs, media_store

    evicted = _stored(vault, "evicted", 300)
    assert media_store.evict(vault, evicted)
    local = _stored(vault, "local", 300)
    ledger = media_jobs.MediaJobStore(vault)
    for path in (evicted, local):
        ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))

    assert ledger.claim_next().binary_path == local
    assert ledger.claim_next().binary_path == evicted


def test_jobs_whose_originals_stayed_on_the_volume_do_not_fill_the_hydration_cap(vault: Path, selected_store):
    """Catches a cap that counts every claimed job, so long local recordings stall every hydration."""
    from exomem import media_jobs, media_store

    first, second = _stored(vault, "first", 300), _stored(vault, "second", 300)
    evicted = _stored(vault, "evicted", 300)
    assert media_store.evict(vault, evicted)
    ledger = media_jobs.MediaJobStore(vault)
    for path in (first, second, evicted):
        ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))

    claimed = [ledger.claim_next() for _ in range(3)]

    assert [job.binary_path for job in claimed] == [first, second, evicted]
    with sqlite3.connect(ledger.path) as connection:
        recorded = dict(connection.execute("SELECT binary_rel, hydrated FROM jobs"))
    assert recorded == {path.relative_to(vault).as_posix(): int(path == evicted) for path in (first, second, evicted)}


def test_at_the_cap_the_worker_claims_no_job_that_needs_a_hydration_and_still_claims_a_local_one(vault: Path, selected_store):
    """Catches hydrated jobs that pin more than the volume holds, or a cap that also stalls local captures."""
    from exomem import media_jobs, media_store

    hydrated = [_stored(vault, f"hydrated-{index}", 300) for index in range(3)]
    for path in hydrated:
        assert media_store.evict(vault, path)
    ledger = media_jobs.MediaJobStore(vault)
    for path in hydrated[:2]:
        ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))
    running = [ledger.claim_next(), ledger.claim_next()]
    local = _stored(vault, "local", 300)
    for path in (hydrated[2], local):
        ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))

    assert ledger.claim_next().binary_path == local
    assert ledger.claim_next() is None
    ledger.complete(running[0])
    assert ledger.claim_next().binary_path == hydrated[2]


@pytest.mark.parametrize("cause", ["transport", "capacity"])
def test_a_hydration_that_cannot_run_now_returns_the_job_with_no_attempt_spent_and_its_progress_kept(
        vault: Path, selected_store, cause):
    """Catches a hydration outage that fails the job, which no scan queues again, or that drops its transcript progress."""
    import os
    import time

    from exomem import media_jobs, media_store, media_worker

    path = _stored(vault, "recording", 300)
    assert media_store.evict(vault, path)
    ledger = media_jobs.MediaJobStore(vault)
    job_id = ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("INSERT INTO speech_progress(job_id, stop_offset) VALUES (?, 1234)", (job_id,))
        if cause == "capacity":
            connection.execute("INSERT INTO media_access(token, binary_rel, pid, temporary_bytes, hydrating) "
                               "VALUES ('other', 'elsewhere', ?, 10000000, 1)", (os.getpid(),))
    if cause == "transport":
        root = Path(selected_store[0]["root"])
        root.rename(root.with_name("offline-objects"))

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0

    returned = ledger.get(job_id)
    assert returned.state == media_jobs.PENDING and returned.attempts == 0 and returned.last_error is None
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT stop_offset FROM speech_progress WHERE job_id = ?", (job_id,)).fetchone() == (1234,)
        retry_at = connection.execute("SELECT retry_at FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    assert retry_at > time.time() + 50
    assert originals.original_state(vault, path).state == "evicted"


def test_a_returned_job_waits_a_retry_time_that_doubles_from_one_minute_to_fifteen(vault: Path, monkeypatch):
    """Catches a fixed retry time, which polls a down store every minute for hours, or an unbounded one."""
    import time

    from exomem import media_jobs

    ledger = media_jobs.MediaJobStore(vault)
    job_id = ledger.enqueue(media_jobs.MediaJob(vault / "recording.mp3", vault / "recording.mp3.md", "audio"))
    now = time.time()
    waits = []
    for _ in range(6):
        monkeypatch.setattr(media_jobs.time, "time", lambda moment=now: moment)
        assert ledger.return_for_retry(ledger.claim_next())
        assert ledger.claim_next() is None
        with sqlite3.connect(ledger.path) as connection:
            retry_at = connection.execute("SELECT retry_at FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        waits.append(round(retry_at - now))
        now = retry_at

    assert waits == [60, 120, 240, 480, 900, 900]


def test_an_original_larger_than_the_whole_hydration_capacity_blocks_its_job(vault: Path, selected_store):
    """Catches a capacity check that reports a file that can never fit as busy, which retries it forever."""
    import os

    from exomem import media_jobs, media_store, media_worker

    path = _stored(vault, "recording", 3_000)
    assert media_store.evict(vault, path)
    _configure(selected_store, vault, max_temporary_bytes=2_000)
    ledger = media_jobs.MediaJobStore(vault)
    job_id = ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0

    blocked = ledger.get(job_id)
    assert blocked.state == media_jobs.BLOCKED and blocked.last_error.startswith("MEDIA_HYDRATION_TOO_LARGE")
    assert ledger.claim_next() is None


def test_a_corrupt_stored_object_fails_its_job_as_unverified(vault: Path, selected_store):
    """Catches a verification mismatch treated as an outage, which hydrates a corrupt object again and again."""
    import os

    from exomem import media_jobs, media_store, media_worker

    path = _stored(vault, "recording", 300)
    assert media_store.evict(vault, path)
    for stored in Path(selected_store[0]["root"]).rglob("*"):
        if stored.is_file():
            stored.write_bytes(b"x" * stored.stat().st_size)
    ledger = media_jobs.MediaJobStore(vault)
    job_id = ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0

    failed = ledger.get(job_id)
    assert failed.state == media_jobs.FAILED and "MEDIA_ORIGINAL_UNVERIFIED" in failed.last_error
    assert not path.exists()


def test_the_path_download_refuses_an_original_larger_than_the_hydration_capacity_with_no_retry_hint(
        vault: Path, selected_store, monkeypatch):
    """Catches the download answering 503 ORIGINAL_UNAVAILABLE, which tells the owner to retry a file that never fits."""
    from starlette.testclient import TestClient

    from exomem import media_store, server, upload_tokens

    path = _stored(vault, "recording", 3_000)
    assert media_store.evict(vault, path)
    _configure(selected_store, vault, max_temporary_bytes=2_000)
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "test-secret")
    client = TestClient(server.build_server(require_auth=False).http_app())
    token = upload_tokens.mint_bound("test-secret", audience="owner")

    response = client.get("/download", params={"path": path.relative_to(vault).as_posix()},
                          headers={"Authorization": f"Bearer {token}"})

    assert response.json()["code"] == "MEDIA_HYDRATION_TOO_LARGE"
    assert response.status_code < 500 and "retry-after" not in response.headers
