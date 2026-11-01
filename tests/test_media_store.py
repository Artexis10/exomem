"""Stored originals retain their evidence identity across cache operations."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem import held_fs, originals, reserved_paths


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
    assert [entry.name for entry in path.parent.iterdir() if entry.name.startswith(".exomem-held-publish-")] == []


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
    """A full volume must not become a misleading remote-store outage, and leaves no temporary file."""
    import errno
    import os

    from exomem import media_store

    path = _preserved(vault)
    _offload(vault, path)
    assert media_store.evict(vault, path)
    fdopen = os.fdopen

    class FullDisk:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stream.close()

        def write(self, _chunk):
            raise OSError(errno.ENOSPC, "the volume is full")

    def full_volume(descriptor, mode="r", *args, **kwargs):
        stream = fdopen(descriptor, mode, *args, **kwargs)
        return FullDisk(stream) if "w" in mode else stream

    monkeypatch.setattr(media_store.os, "fdopen", full_volume)
    with pytest.raises(OSError) as error:
        with media_store.open_original(vault, path):
            pytest.fail("a full volume cannot hydrate an original")
    assert error.value.errno == errno.ENOSPC
    assert originals.original_state(vault, path).state == "evicted"
    assert [entry.name for entry in path.parent.iterdir() if entry.name.startswith(".exomem-held-publish-")] == []


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
        connection.executemany("INSERT INTO media_access(token, binary_rel, pid) VALUES (?, ?, ?)",
                               [(f"idle-{i}", "Knowledge Base/Evidence/other.bin", os.getpid()) for i in range(100)])
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, temporary_bytes, hydrating) "
                           "VALUES ('dead', ?, ?, 1024, 1)", (relative, dead_pid))
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, temporary_bytes, hydrating) "
                           "VALUES ('live', ?, ?, 1024, 1)", (relative, os.getpid()))
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
    # The identity alone, which a drained cell keeps, has no policy to evict by. A turned-off store whose policy
    # asks for eviction evicts a hydrated copy again (D12, the retained store).
    config.write_text(json.dumps({"vaults": {str(vault): {k: values[k] for k in ("store", "adapter")} | {"enabled": False}}}))
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
    install = held_fs.install_temporary

    def external_create(filesystem, parent, temporary, leaf, **kwargs):
        if leaf == path.name:
            path.write_bytes(b"external edit")
        return install(filesystem, parent, temporary, leaf, **kwargs)

    monkeypatch.setattr(held_fs, "install_temporary", external_create)
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


def test_an_original_cached_before_the_size_column_still_leaves_under_pressure(vault: Path, selected_store, monkeypatch):
    """Catches an upgrade that keeps every original cached before it on the volume for good."""
    from exomem import media_jobs, media_store

    path = _stored(vault, "cached-before-the-upgrade", 300)
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        connection.execute("UPDATE media_cache SET size = NULL")
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000, path))

    assert media_store.evict_to_target(vault) == 1
    assert originals.original_state(vault, path).state == "evicted"


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


def test_an_original_whose_cache_identity_drifted_keeps_its_row_and_owner_read(vault: Path, selected_store, monkeypatch):
    """Catches a pass that drops the cache row of an original still on its path, which loses the owner's last read."""
    from exomem import media_jobs, media_store

    played = _stored(vault, "played", 600)
    with media_store.open_original(vault, played, read=True):
        pass
    played.chmod(0o640)
    assert not media_store.cache_matches(vault, played)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 7_000, played))

    media_store.evict_to_target(vault)

    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        rows = connection.execute("SELECT owner_read FROM media_cache WHERE binary_rel = ?",
                                  (played.relative_to(vault).as_posix(),)).fetchall()
    assert len(rows) == 1 and rows[0][0] is not None
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
    # Each eviction confirms its stored copy once, so the store's reads give the order.
    order: list[Path] = []
    paths = {originals.original_state(vault, path).reference.object: path for path in (played, queued, idle)}
    head = media_store.FilesystemStore.head
    monkeypatch.setattr(media_store.FilesystemStore, "head",
                        lambda store, name, version: order.append(paths[name]) or head(store, name, version))

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


def _rows_of_moved_originals(vault: Path, *sizes: int) -> None:
    """Cache rows that a move or a deletion left behind: the path no longer holds an original."""
    from exomem import media_jobs

    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        connection.executemany(
            "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size) VALUES (?, '{}', ?, '', 0, ?)",
            [(f"Knowledge Base/Evidence/moved-{index}.bin", "0" * 64, size) for index, size in enumerate(sizes)])


def test_rows_of_moved_originals_neither_stop_eviction_nor_stay(vault: Path, selected_store, monkeypatch):
    """Catches a pass that ends at candidates it cannot evict, so rows that moves left keep a real original
    cached for good, or eviction that walks every sidecar."""
    from exomem import media_jobs, media_store

    real = _stored(vault, "real", 50)
    _rows_of_moved_originals(vault, *[100] * 200)
    monkeypatch.setattr(media_store, "bound_originals", lambda *a, **k: pytest.fail("eviction walks no sidecars"))
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000, real))

    assert media_store.evict_to_target(vault) == 1

    assert originals.original_state(vault, real).state == "evicted"
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_cache WHERE binary_rel LIKE '%moved-%'").fetchone()[0] == 0


def test_eviction_reads_past_a_candidate_it_keeps(vault: Path, selected_store, monkeypatch):
    """Catches a pass that ends at a page of originals it must keep, here one whose stored copy fails verification."""
    from exomem import media_store

    damaged, sound = _stored(vault, "damaged", 1_000), _stored(vault, "sound", 500)
    reference = originals.original_state(vault, damaged).reference
    (Path(selected_store[0]["root"]) / reference.object / reference.version).write_bytes(b"corrupt")
    monkeypatch.setattr(media_store, "_EVICTION_BATCH", 1)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000, damaged, sound))

    assert media_store.evict_to_target(vault) == 1

    assert damaged.exists() and not sound.exists()


def test_a_store_outage_ends_the_eviction_pass_at_its_first_head(vault: Path, selected_store, monkeypatch):
    """Catches a pass that asks an unreachable store once per candidate, which holds its caller for one timeout each."""
    from exomem import media_store

    stored = [_stored(vault, f"recording-{index}", 100 + index) for index in range(3)]
    heads = []

    def unreachable(self, name, version):
        heads.append(name)
        raise OSError("the store is unreachable")

    monkeypatch.setattr(media_store.FilesystemStore, "head", unreachable)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000, *stored))

    assert media_store.evict_to_target(vault) == 0

    assert len(heads) == 1
    assert all(path.exists() for path in stored)


def test_a_demand_that_only_rows_of_moved_originals_could_meet_evicts_nothing(vault: Path, selected_store, monkeypatch):
    """Catches room counted from rows that moves left, which evicts real originals for a demand eviction cannot meet."""
    from exomem import media_store

    large, small = _stored(vault, "large", 1_000), _stored(vault, "small", 500)
    _rows_of_moved_originals(vault, 3_000)
    monkeypatch.setattr(shutil, "disk_usage", _volume(100_000, 20_000, large, small))

    assert media_store.record_demand(vault, "series-a", 4_000, budget=0, reachable_budget=6_000)
    assert media_store.evict_to_target(vault) == 0

    assert large.exists() and small.exists()


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


def test_a_speech_job_that_waits_for_a_busy_gpu_keeps_its_original_and_gives_up_its_hydration_slot(
        vault: Path, selected_store, monkeypatch):
    """Catches a GPU wait that keeps the hydration slot, so every image whose original is in the store waits for
    the GPU too, or that drops the pin, so on a full volume each GPU recheck downloads the recording again. Also
    catches a claim that skips speech work only among local originals, so a child that knows the GPU is busy
    still downloads the next recording from the store for nothing."""
    import os

    from exomem import (
        asr_runtime,
        extract,
        media_jobs,
        media_processing,
        media_store,
        media_worker,
        preserve,
    )

    _configure(selected_store, vault, max_hydrations=1)
    # Only a transcribed original goes to the store, so the jobs that hydrate one are requests to process it again.
    speech, later = (preserve.preserve_bytes(vault, scope="Test", category="audio", filename=name, data=name.encode(),
                                             text="[0:00] earlier words") for name in ("meeting.m4a", "call.m4a"))
    image = preserve.preserve_bytes(vault, scope="Test", category="photos", filename="receipt.png",
                                    data=b"\x89PNG fake", text="RECEIPT 41")
    for captured in (speech, later, image):
        _offload(vault, vault / captured.path)
        media_processing.reprocess_media(vault, captured.path)
        # A queued job that no worker claimed yet does not keep its original on the volume.
        assert media_store.evict(vault, vault / captured.path)
    stored = {captured.path: originals.original_state(vault, vault / captured.path).reference.object
              for captured in (speech, later, image)}
    downloads = []
    store_open = media_store.FilesystemStore.open

    def counted_open(self, name, version):
        downloads.append(name)
        return store_open(self, name, version)

    def extract_text(path, **_kwargs):
        if extract.media_type_for(Path(path)) == "image":
            return extract.ExtractResult(text="RECEIPT 42", media_type="image", engine="tesseract")
        raise asr_runtime.ASRGpuBusy("the GPU has 512 MiB free, 2048 MiB needed")

    monkeypatch.setattr(media_store.FilesystemStore, "open", counted_open)
    monkeypatch.setattr(extract, "extract_text", extract_text)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    # The volume stays above the eviction target, so each timer pass evicts every original that no job pins.
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 8_000))

    for _recheck in range(3):  # the supervisor starts a child for each GPU recheck, and it has not seen the busy GPU
        assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == media_worker._GPU_BUSY_EXIT_CODE
        media_worker.MediaWorker(vault, execution_mode="process")._drain_parent_results()
        media_store.evict_to_target(vault)

    assert "RECEIPT 42" in (vault / image.sidecar_path).read_text(encoding="utf-8")
    assert [downloads.count(stored[captured.path]) for captured in (speech, later)] == [1, 0]
    assert originals.original_state(vault, vault / speech.path).state == "cached"
    waiting = media_jobs.MediaJobStore(vault).get_by_binary(vault / speech.path)
    assert (waiting.state, waiting.attempts) == (media_jobs.PENDING, 0)


@pytest.mark.parametrize(("cause", "code"), [("transport", "MEDIA_STORE_UNAVAILABLE"), ("capacity", "MEDIA_STORE_BUSY")])
def test_a_hydration_that_cannot_run_now_returns_the_job_with_no_attempt_spent_and_its_progress_kept(
        vault: Path, selected_store, cause, code):
    """Catches a hydration outage that fails the job, which no scan queues again, that drops its transcript progress,
    or that status shows as plain pending with no reason."""
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
    assert returned.state == media_jobs.PENDING and returned.attempts == 0
    [shown] = [job for job in media_jobs.status(vault)["jobs"] if job["id"] == job_id]
    assert shown["state"] == media_jobs.PENDING and shown["error"].startswith(code)
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


def test_a_persons_retry_starts_the_return_wait_again_at_one_minute(vault: Path, monkeypatch):
    """Catches a retry that keeps the earlier returns, so a job that a person retried waits 15 minutes at its next return."""
    import time

    from exomem import media_jobs

    ledger = media_jobs.MediaJobStore(vault)
    job_id = ledger.enqueue(media_jobs.MediaJob(vault / "recording.mp3", vault / "recording.mp3.md", "audio"))
    now = time.time()
    monkeypatch.setattr(media_jobs.time, "time", lambda: now)
    for _ in range(4):
        assert ledger.return_for_retry(ledger.claim_next())
        now += media_jobs.RETRY_LIMIT_SECONDS
    assert ledger.mark(ledger.claim_next(), media_jobs.FAILED, "MEDIA_ORIGINAL_UNVERIFIED: the stored copy differs")
    assert ledger.retry(include_failed=True) == 1

    assert ledger.return_for_retry(ledger.claim_next())

    with sqlite3.connect(ledger.path) as connection:
        retry_at = connection.execute("SELECT retry_at FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    assert round(retry_at - now) == media_jobs.RETRY_FIRST_SECONDS


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


@pytest.mark.parametrize("cloud", [False, True])
def test_a_job_blocked_on_a_too_large_original_names_the_hydration_capacity(vault: Path, selected_store, monkeypatch,
                                                                             cloud):
    """Catches the engine-unavailable guidance on a block that only a larger hydration capacity clears."""
    import os

    from exomem import media_engines, media_jobs, media_store, media_worker

    path = _stored(vault, "recording", 3_000)
    assert media_store.evict(vault, path)
    _configure(selected_store, vault, max_temporary_bytes=2_000)
    job_id = media_jobs.MediaJobStore(vault).enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0
    if cloud:
        monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")

    [job] = [job for job in media_jobs.status(vault)["jobs"] if job["id"] == job_id]

    assert job["state"] == media_jobs.BLOCKED and job["retryable"]
    assert job["next_action"] != media_engines.unavailable_next_action()
    assert "hydration capacity" in job["next_action"]


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


_STALLED_HYDRATION = """
import sys
import time
from pathlib import Path

from exomem import media_store

vault, path, signal = (Path(argument) for argument in sys.argv[1:4])
stream = media_store._stored_chunks


def stalled(backend, reference):
    # Everything but the last byte, so age writes its first chunks and then waits.
    data = b"".join(stream(backend, reference))
    for start in range(0, len(data) - 1, 65536):
        yield data[start:min(start + 65536, len(data) - 1)]
    signal.touch()
    time.sleep(120)
    yield data[-1:]


media_store._stored_chunks = stalled
with media_store.open_original(vault, path):
    pass
"""


@pytest.mark.parametrize("restart", ["runtime start", "next reservation"])
def test_a_hydration_killed_mid_write_leaves_nothing_at_the_path_and_its_row_and_file_clear_after(
        vault: Path, selected_store, tmp_path: Path, monkeypatch, restart):
    """Catches a stop that leaves a partial original at its path, or a temporary file and reservation that stay for good."""
    import signal
    import sys
    import time

    from exomem import media_jobs, media_store, server_runtime

    path = _stored(vault, "recording", 300_000)
    expected = path.read_bytes()
    assert media_store.evict(vault, path)
    stalled = tmp_path / "stalled"
    child = subprocess.Popen([sys.executable, "-c", _STALLED_HYDRATION, str(vault), str(path), str(stalled)])

    def temporaries():
        return [entry for entry in path.parent.iterdir() if entry.name.startswith(".exomem-held-publish-")]

    try:
        deadline = time.monotonic() + 30
        while not (stalled.exists() and temporaries() and temporaries()[0].stat().st_size > 0):
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        assert not path.exists()
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()
    assert not path.exists()
    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT pid, temporary_name, temporary_bytes FROM media_access").fetchall() == [
            (child.pid, temporaries()[0].name, len(expected))]

    if restart == "runtime start":
        monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
        server_runtime._start_media_worker(vault)
    else:
        with media_store.open_original(vault, path) as original:
            assert original.stream.read() == expected

    assert temporaries() == []
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT count(*) FROM media_access").fetchone()[0] == 0


def test_a_reused_process_id_does_not_keep_a_dead_reservation(vault: Path, selected_store):
    """Catches a liveness check by process ID alone: a restarted container gives the same ID to a new process."""
    import os

    from exomem import media_jobs, media_store

    if media_jobs.process_start(os.getpid()) is None:
        pytest.skip("this platform exposes no process start time")
    path = _stored(vault, "recording", 3_000)
    ledger = media_jobs.MediaJobStore(vault)
    relative = path.relative_to(vault).as_posix()
    residue = path.with_name(".exomem-held-publish-" + "a" * 32)
    residue.write_bytes(b"part of an earlier hydration")
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, pid_start, temporary_bytes, hydrating, "
                           "temporary_name) VALUES ('earlier', ?, ?, '1', 3000, 1, ?)",
                           (relative, os.getpid(), residue.name))
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, pid_start) VALUES ('current', ?, ?, ?)",
                           (relative, os.getpid(), media_jobs.process_start(os.getpid())))

    assert media_store.clear_dead_reservations(vault) == 1

    assert not residue.exists()
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT token FROM media_access").fetchall() == [("current",)]


@pytest.mark.parametrize("unset", [("max_temporary_bytes", "max_hydrations"), ("max_hydrations",)])
def test_a_policy_without_hydration_limits_offloads_and_evicts_nothing(vault: Path, selected_store, monkeypatch, unset):
    """Catches a policy without hydration limits, the chart default, that fails every offload,
    or a half-set one that evicts an original no hydration could bring back."""
    from exomem import media_store

    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): {k: v for k, v in values.items() if k not in unset}}}))
    path = _preserved(vault)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000, path))

    assert _offload(vault, path) is not None
    assert originals.original_state(vault, path).state == "cached"
    assert media_store.evict_to_target(vault) == 0
    assert path.exists()


def test_a_zero_hydration_limit_turns_the_capacity_off_and_local_work_still_runs(vault: Path, selected_store):
    """Catches a non-positive hydration limit that raises into every claim, which stops all local media work."""
    from exomem import media_jobs

    _configure(selected_store, vault, max_hydrations=0)
    ledger = media_jobs.MediaJobStore(vault)
    job_id = ledger.enqueue(media_jobs.MediaJob(vault / "recording.mp3", vault / "recording.mp3.md", "audio"))

    assert ledger.next_pending().id == job_id
    assert ledger.claim_next().id == job_id
    assert "max_hydrations" in media_jobs.status(vault)["hydration_policy_problem"]


def test_an_original_as_large_as_the_whole_hydration_capacity_hydrates(vault: Path, selected_store):
    """Catches a reservation that also counts the ciphertext, so a file that fits the capacity never hydrates."""
    from exomem import media_store

    path = _stored(vault, "recording", 3_000)
    expected = path.read_bytes()
    assert media_store.evict(vault, path)
    _configure(selected_store, vault, max_temporary_bytes=3_000)

    with media_store.open_original(vault, path) as original:
        assert original.stream.read() == expected


def test_an_offload_waits_with_no_attempt_spent_while_a_hydration_holds_the_capacity(vault: Path, selected_store):
    """Catches an offload that writes its ciphertext beside a hydration that holds the capacity, or fails while it waits."""
    import os
    import time

    from exomem import media_jobs, media_worker

    path = _preserved(vault)
    ledger = media_jobs.MediaJobStore(vault)
    assert ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE).state == media_jobs.PENDING
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, temporary_bytes, hydrating) "
                           "VALUES ('hydration', 'elsewhere', ?, ?, 1)",
                           (os.getpid(), selected_store[0]["max_temporary_bytes"] - 10))

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0

    waiting = ledger.get_by_binary(path, media_type=media_jobs.OFFLOAD_MEDIA_TYPE)
    assert waiting.state == media_jobs.PENDING and waiting.attempts == 0
    assert originals.original_state(vault, path).state == "local"
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM media_access WHERE token = 'hydration'")
        connection.execute("UPDATE jobs SET retry_at = ? WHERE id = ?", (time.time() - 1, waiting.id))

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0

    assert originals.original_state(vault, path).state == "cached"


def test_a_busy_capacity_keeps_the_offload_of_a_row_whose_other_stages_ran(vault: Path, selected_store):
    """Catches a busy capacity that fails a row from before offload had rows of its own, after its other stages ran."""
    import os

    from exomem import media_jobs, media_worker

    path = _preserved(vault)
    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM jobs")
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, temporary_bytes, hydrating) "
                           "VALUES ('hydration', 'elsewhere', ?, ?, 1)",
                           (os.getpid(), selected_store[0]["max_temporary_bytes"] - 10))
    job_id = ledger.enqueue(media_jobs.MediaJob(path, originals.original_state(vault, path).sidecar_path, "text",
                                                do_ocr=False, do_reembed=True, do_offload=True))

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.05) == 0

    waiting = ledger.get(job_id)
    assert waiting.state == media_jobs.PENDING and waiting.do_offload and not waiting.do_reembed
    assert originals.original_state(vault, path).state == "local"


def test_a_reservation_of_a_reused_process_id_does_not_defer_the_watcher(vault: Path, selected_store):
    """Catches a liveness check by process ID alone, which defers a path's events after its holder died."""
    import os

    from exomem import media_jobs, media_store

    if media_jobs.process_start(os.getpid()) is None:
        pytest.skip("this platform exposes no process start time")
    path = _stored(vault, "recording", 300)
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, pid_start) VALUES ('earlier', ?, ?, '1')",
                           (path.relative_to(vault).as_posix(), os.getpid()))

    assert media_store.managed_event(vault, path) == "suppress"


def test_a_failed_hydration_removes_its_temporary_after_its_folder_moved(vault: Path, selected_store, monkeypatch):
    """Catches a temporary removed by its path, which leaves a partial original in a folder that moved meanwhile."""
    from exomem import held_fs, media_store

    path = _stored(vault, "recording", 3_000)
    assert media_store.evict(vault, path)
    moved = path.parent.with_name(path.parent.name + "-moved")

    def interrupted(backend, reference, identity, sink):
        sink.write(b"part of the original")
        sink.flush()
        path.parent.rename(moved)
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the download stopped")

    monkeypatch.setattr(media_store, "_decrypt_into", interrupted)
    with pytest.raises(media_store.MediaStoreError, match="the download stopped"):
        with media_store.open_original(vault, path):
            pytest.fail("an interrupted hydration yields no original")

    assert [entry.name for entry in moved.iterdir() if held_fs.is_publish_temporary(entry.name)] == []


def test_an_offload_takes_no_hydration_slot(vault: Path, selected_store, monkeypatch):
    """Catches an offload that takes a hydration slot, so an owner's playback waits for every upload."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from exomem import media_store

    evicted = _stored(vault, "playback", 3_000)
    expected = evicted.read_bytes()
    assert media_store.evict(vault, evicted)
    local = _preserved(vault, category="upload")
    _configure(selected_store, vault, max_hydrations=1)
    uploading, release = threading.Event(), threading.Event()
    upload = media_store.FilesystemStore.upload

    def paused(self, name, stream, *, size, sha256):
        uploading.set()
        assert release.wait(10)
        return upload(self, name, stream, size=size, sha256=sha256)

    monkeypatch.setattr(media_store.FilesystemStore, "upload", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(media_store.offload, vault, local)
        try:
            assert uploading.wait(10)
            with media_store.open_original(vault, evicted) as original:
                assert original.stream.read() == expected
        finally:
            release.set()
        assert pending.result(timeout=10) is not None


# The status of an import sent as windows reads this ledger; the budget terms are in tests/test_adoption_windows.py.

# A volume of 1 GB at an 80% growth threshold, a 1 MB reserve, a 10 MB hydration capacity for two hydrations,
# and a largest offloadable file of 5 MB (0.5% of the volume): 21 MB of fixed terms.
_WINDOW_VOLUME = 1_000_000_000
_FIXED_TERMS = 1_000_000 + 10_000_000 + 2 * 5_000_000


def _window_policy(selected_store, monkeypatch) -> None:
    vault = Path(next(iter(json.loads(selected_store[1].read_text())["vaults"])))
    _configure(selected_store, vault, growth_threshold=0.8, volume_fraction=0.005, minimum_idle_seconds=3600)
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", "1000000")


def _window_status(vault: Path, **arguments) -> dict:
    from exomem import commands

    return commands.op_adoption_studio(vault, action="status", **arguments)["window_budget"]


def test_the_status_counts_a_copy_the_worker_hydrated_in_the_job_pinned_bytes(vault: Path, selected_store, monkeypatch):
    """Catches job-pinned bytes that miss the hydrated copy of an evicted original, which a claimed job returns when it ends."""
    from exomem import media_jobs, media_store

    _window_policy(selected_store, monkeypatch)
    path = _stored(vault, "recording", 300)
    size = path.stat().st_size
    assert media_store.evict(vault, path)
    ledger = media_jobs.MediaJobStore(vault)
    ledger.enqueue(media_jobs.MediaJob(path, path.with_name(path.name + ".md"), "text"))
    assert ledger.claim_next().binary_path == path
    assert _window_status(vault)["job_pinned"] == {"bytes": 0}  # Claimed, and nothing hydrated yet.

    with media_store.open_original(vault, path):  # The worker's hydration places the copy.
        pass

    assert originals.original_state(vault, path).state == "cached"
    assert _window_status(vault)["job_pinned"] == {"bytes": size}


@pytest.mark.parametrize("used, reachable, effective", [(776_500_000, 2_000_000, 2_500_000), (770_000_000, 8_500_000, 5_000_000)])
def test_the_effective_file_limit_is_the_reachable_budget_plus_the_job_pinned_bytes_up_to_the_largest_offloadable_file(
        vault: Path, selected_store, monkeypatch, used, reachable, effective):
    """Catches a limit that leaves out the room a claimed job returns, or that promises a file above the largest offloadable one."""
    from exomem import media_jobs

    _window_policy(selected_store, monkeypatch)
    pinned = _stored(vault, "pinned", 500_000)
    ledger = media_jobs.MediaJobStore(vault)
    ledger.enqueue(media_jobs.MediaJob(pinned, pinned.with_name(pinned.name + ".md"), "text"))
    ledger.claim_next()
    # The 500 000 bytes of the pinned original count in use: 0.8 * 1 000 000 000 - (used + 500 000) - 21 000 000 = reachable.
    monkeypatch.setattr(shutil, "disk_usage", _volume(_WINDOW_VOLUME, used, pinned))

    figures = _window_status(vault)

    assert figures["reachable_budget"] == {"bytes": reachable}
    assert figures["job_pinned"] == {"bytes": 500_000}
    assert figures["effective_file_limit"] == {"bytes": effective}


def test_with_eviction_off_the_effective_file_limit_adds_no_pinned_bytes(vault: Path, selected_store, monkeypatch):
    """Catches a limit that promises the bytes of a claimed job when eviction is off: nothing turns them into room."""
    from exomem import media_jobs

    _window_policy(selected_store, monkeypatch)
    _configure(selected_store, vault, growth_threshold=0.8, volume_fraction=0.005, minimum_idle_seconds=3600,
               eviction_enabled=False)
    pinned = _stored(vault, "pinned", 500_000)
    ledger = media_jobs.MediaJobStore(vault)
    ledger.enqueue(media_jobs.MediaJob(pinned, pinned.with_name(pinned.name + ".md"), "text"))
    ledger.claim_next()
    monkeypatch.setattr(shutil, "disk_usage", _volume(_WINDOW_VOLUME, 776_500_000, pinned))

    figures = _window_status(vault)

    assert figures["reachable_budget"] == {"bytes": 2_000_000}
    assert figures["job_pinned"] == {"bytes": 500_000}
    assert figures["effective_file_limit"] == {"bytes": 2_000_000}


def test_the_reachable_budget_adds_what_eviction_filing_and_a_pending_offload_free_but_not_a_recent_owner_read(
        vault: Path, selected_store, monkeypatch):
    """Catches a reachable budget that counts an original the owner just read, or leaves out a term that frees room."""
    from test_adoption_windows import _window

    from exomem import media_jobs, media_store

    _window_policy(selected_store, monkeypatch)
    unread, played = _stored(vault, "unread", 3_000), _stored(vault, "played", 1_000)
    with media_store.open_original(vault, played, read=True):
        pass
    local = _preserved(vault, category="pending")  # Filing queues its offload, which no worker has run.
    assert media_jobs.MediaJobStore(vault).get_by_binary(local, media_type=media_jobs.OFFLOAD_MEDIA_TYPE).do_offload
    _window(vault, {"report.pdf": 4_000})  # A landed window that holds one unfiled artifact of 4 000 bytes.
    held = [unread, played, local]
    in_use = sum(path.stat().st_size for path in held)
    # The window budget is 1 000 bytes: 0.8 * 1 000 000 000 - in use - 21 000 000 of fixed terms.
    monkeypatch.setattr(shutil, "disk_usage", _volume(_WINDOW_VOLUME, 779_000_000 - 1_000 - in_use, *held))

    figures = _window_status(vault)

    assert figures["window_budget"] == {"bytes": 1_000}
    # Plus the unread original (3 000), the local original once offloaded and evicted, and the unfiled artifact (4 000).
    assert figures["reachable_budget"] == {"bytes": 1_000 + 3_000 + local.stat().st_size + 4_000}


def test_a_status_call_records_and_renews_a_demand_for_each_series_and_ignores_one_above_the_reachable_budget(
        vault: Path, selected_store, monkeypatch):
    """Catches a demand that replaces another series' lease, or one the cell cannot meet that still asks for eviction."""
    from exomem import media_jobs

    _window_policy(selected_store, monkeypatch)
    held = [_stored(vault, "large", 3_000), _stored(vault, "middle", 2_000), _stored(vault, "small", 1_000)]
    base = 779_000_000 - 1_000 - 6_000  # A window budget of 1 000 bytes, and 6 000 bytes that eviction can free.
    monkeypatch.setattr(shutil, "disk_usage", _volume(_WINDOW_VOLUME, base, *held))
    ledger = media_jobs.MediaJobStore(vault)

    def leases() -> dict[str, tuple[int, int, float]]:
        with sqlite3.connect(ledger.path) as connection:
            return {series: (demand, offset, renewed) for series, demand, offset, renewed in connection.execute(
                "SELECT series, demand_bytes, budget_offset, renewed_at FROM media_demands")}

    first = _window_status(vault, series_id="series-a", next_window_bytes=4_000)
    assert first["window_budget"] == {"bytes": 1_000} and first["reachable_budget"] == {"bytes": 7_000}
    assert first["demand"]["recorded"] is True
    _window_status(vault, series_id="series-b", next_window_bytes=6_500)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("UPDATE media_demands SET renewed_at = 1")
    _window_status(vault, series_id="series-a", next_window_bytes=5_000)
    _window_status(vault, series_id="series-b", next_window_bytes=6_000)

    renewed = leases()
    assert {series: demand for series, (demand, _offset, _renewed) in renewed.items()} == {"series-a": 5_000, "series-b": 6_000}
    assert all(lease[2] > 1 for lease in renewed.values())
    assert renewed["series-a"][1] == 1_000 + base + 6_000  # The cell stored its own budget as an offset from its own use.

    ignored = _window_status(vault, series_id="series-c", next_window_bytes=7_001)

    assert ignored["demand"]["recorded"] is False
    assert set(leases()) == {"series-a", "series-b"}


def _recorded_by_a_batch(vault: Path) -> int:
    """Offload one original through a batch, which records its owner census; returns the bytes that the census counts."""
    from exomem import media_store

    path = _filed(vault, 1, category="recorded")[0]
    media_store.offload(vault, path, batch=media_store.OffloadBatch(vault))
    return path.stat().st_size


def test_the_status_reports_the_remaining_allowance_from_the_census_that_offload_recorded(
        vault: Path, selected_store, monkeypatch):
    """Catches an allowance figure that reports the setting alone, or a status that walks every note to measure it."""
    used = _recorded_by_a_batch(vault)
    censuses = _count_censuses(monkeypatch)

    allowance = _window_status(vault)["media_allowance"]

    assert censuses == []
    assert {key: allowance[key] for key in ("allowance_bytes", "used_bytes", "remaining_bytes")} == {
        "allowance_bytes": 10_000_000, "used_bytes": used, "remaining_bytes": 10_000_000 - used}
    assert datetime.fromisoformat(allowance["observed_at"]).tzinfo is not None


def test_a_plain_status_with_no_recorded_census_reports_the_use_as_unknown_and_takes_none(
        vault: Path, selected_store, monkeypatch):
    """Catches a status that reads 0 bytes in use before any offload ran, or that walks the vault to find out."""
    censuses = _count_censuses(monkeypatch)

    allowance = _window_status(vault)["media_allowance"]

    assert censuses == []
    assert allowance["allowance_bytes"] == 10_000_000 and "used_bytes" not in allowance and allowance["unknown"]


def test_a_status_that_carries_a_demand_takes_one_census_when_none_is_recorded(vault: Path, selected_store, monkeypatch):
    """Catches a series that starts with no figure for the room it needs, because only offload ever recorded one."""
    path = _stored(vault, "recording", 300)  # A stored original; its offload was not a batch, so no census is recorded.
    censuses = _count_censuses(monkeypatch)

    allowance = _window_status(vault, series_id="series-a", next_window_bytes=1)["media_allowance"]

    assert len(censuses) == 1
    assert allowance["used_bytes"] == path.stat().st_size and allowance["observed_at"]


def _filed(vault: Path, count: int, *, category: str = "batch") -> list[Path]:
    """`count` filed originals of 40 distinct bytes each, so that none reuses another's stored version."""
    from exomem import preserve

    return [vault / preserve.preserve(vault, scope="Test", category=category, filename=f"member-{number}.txt",
                                      content=f"{number:040d}").path for number in range(count)]


def _count_censuses(monkeypatch) -> list[int]:
    from exomem import media_allowance

    reads: list[int] = []
    read = media_allowance.reference_usage

    def counted(*args, **kwargs):
        reads.append(1)
        return read(*args, **kwargs)

    monkeypatch.setattr(media_allowance, "reference_usage", counted)
    return reads


def test_a_batch_of_offloads_reads_the_owner_census_once(vault: Path, selected_store, monkeypatch):
    """Catches each offload reading every owner reference up to three times, which makes a long series quadratic."""
    from exomem import media_store

    members = _filed(vault, 12)
    reads = _count_censuses(monkeypatch)
    batch = media_store.OffloadBatch(vault)

    for path in members:
        media_store.offload(vault, path, batch=batch)

    assert all(originals.original_state(vault, path).reference is not None for path in members)
    assert len(reads) == 1


def test_other_writes_land_between_two_members_of_a_batch(vault: Path, selected_store, monkeypatch):
    """Catches a batch that keeps the mutation boundary from its first member to its last."""
    import threading

    from exomem import media_store, mutation_lock

    first, second = _filed(vault, 2)
    reads = _count_censuses(monkeypatch)
    batch = media_store.OffloadBatch(vault)
    media_store.offload(vault, first, batch=batch)

    assert not mutation_lock.current_thread_holds_boundary()
    landed: list[Path] = []
    writer = threading.Thread(target=lambda: landed.extend(_filed(vault, 1, category="written-between-members")),
                              daemon=True)
    writer.start()
    writer.join(timeout=15)
    media_store.offload(vault, second, batch=batch)

    assert not writer.is_alive() and landed and landed[0].exists()
    assert originals.original_state(vault, second).reference is not None
    assert len(reads) == 1


def test_a_batch_charges_each_version_it_publishes_to_its_census(vault: Path, selected_store, monkeypatch):
    """Catches a batch census that forgets its own earlier members, so the batch fills the allowance and passes it."""
    from exomem import media_store

    first, second, third = _filed(vault, 3)
    _configure(selected_store, vault, allowance_bytes=2 * first.stat().st_size)
    reads = _count_censuses(monkeypatch)
    batch = media_store.OffloadBatch(vault)

    media_store.offload(vault, first, batch=batch)
    media_store.offload(vault, second, batch=batch)
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, third, batch=batch)

    assert refused.value.code == "MEDIA_ALLOWANCE_EXCEEDED"
    assert originals.original_state(vault, third).reference is None
    assert len(reads) == 1


def test_a_batch_member_whose_reference_changed_before_publication_is_refused(vault: Path, selected_store, monkeypatch):
    """Catches a shared census standing in for the publication-time check that the original is unchanged."""
    from exomem import media_store

    first, second = _filed(vault, 2)
    batch = media_store.OffloadBatch(vault)
    media_store.offload(vault, first, batch=batch)
    upload = media_store.FilesystemStore.upload
    elsewhere: list[object] = []

    def bind_elsewhere(*args, **kwargs):
        version = upload(*args, **kwargs)
        if not elsewhere:
            elsewhere.append(None)  # the competing offload uploads too
            elsewhere[0] = media_store.offload(vault, second)
        return version

    monkeypatch.setattr(media_store.FilesystemStore, "upload", bind_elsewhere)
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, second, batch=batch)

    assert refused.value.code == "MEDIA_ORIGINAL_CHANGED"
    assert originals.original_state(vault, second).reference == elsewhere[0]


def test_a_census_that_could_not_count_replaces_an_older_known_figure_in_the_status(vault: Path, selected_store, monkeypatch):
    """Catches a status that keeps showing the last known use after the references could no longer be counted."""
    from exomem import media_allowance, media_store

    _recorded_by_a_batch(vault)
    unreadable = media_allowance.ReferenceUsage(None, "2026-11-01T08:00:00+00:00")
    monkeypatch.setattr(media_allowance, "reference_usage", lambda *args, **kwargs: unreadable)
    next_member = _filed(vault, 2, category="next")[1]

    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, next_member, batch=media_store.OffloadBatch(vault))
    allowance = _window_status(vault)["media_allowance"]

    assert refused.value.code == "MEDIA_ALLOWANCE_UNKNOWN"
    assert "used_bytes" not in allowance and allowance["unknown"] and allowance["observed_at"] == unreadable.observed_at


def test_a_status_with_a_demand_counts_afresh_once_the_recorded_census_is_older_than_the_demand_lease(
        vault: Path, selected_store, monkeypatch):
    """Catches a series that sizes its window by a figure from before the lease, which later offloads may have changed."""
    from exomem import media_jobs, media_store

    used = _recorded_by_a_batch(vault)
    old = (datetime.now(UTC) - timedelta(seconds=media_store.DEMAND_LEASE_SECONDS + 60)).isoformat()
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        connection.execute("UPDATE media_owner_census SET observed_at = ?", (old,))
    censuses = _count_censuses(monkeypatch)

    plain = _window_status(vault)["media_allowance"]
    assert censuses == [] and plain["observed_at"] == old and plain["used_bytes"] == used

    fresh = _window_status(vault, series_id="series-a", next_window_bytes=1)["media_allowance"]
    again = _window_status(vault, series_id="series-a", next_window_bytes=1)["media_allowance"]

    assert len(censuses) == 1 and fresh["observed_at"] > old and again["observed_at"] == fresh["observed_at"]
    assert media_store.recorded_census(vault).observed_at == fresh["observed_at"]


def test_members_that_the_allowance_refuses_share_the_batch_census(vault: Path, selected_store, monkeypatch):
    """Catches a batch that reads the owner census again after each refused member, which makes a refused series quadratic."""
    from exomem import media_store

    first, *refused = _filed(vault, 4)
    _configure(selected_store, vault, allowance_bytes=first.stat().st_size)
    reads = _count_censuses(monkeypatch)
    batch = media_store.OffloadBatch(vault)
    media_store.offload(vault, first, batch=batch)

    for path in refused:
        with pytest.raises(media_store.MediaStoreError) as error:
            media_store.offload(vault, path, batch=batch)
        assert error.value.code == "MEDIA_ALLOWANCE_EXCEEDED"

    assert len(reads) == 1


@pytest.mark.parametrize("error", [held_fs.HeldFsError("PATH_NOT_FOUND", "the ledger"), reserved_paths.ReservedPathLeafError("RESERVED")])
def test_a_ledger_that_refuses_the_census_record_does_not_stop_the_batch(vault: Path, selected_store, error):
    """Catches an offload that stops because its status record was refused, which costs the status only a fresher figure."""
    from exomem import media_store

    class RefusingLedger:
        def _connect(self, **_options):
            raise error

    usage = media_store.OffloadBatch(vault).usage(RefusingLedger())

    assert usage.versions is not None


def _retained(selected_store, vault: Path, **policy) -> None:
    """The policy cellctl renders for a cell whose edge is turned off: offload off, the rest of the policy kept."""
    _configure(selected_store, vault, **{"enabled": False, "growth_threshold": 0.8, **policy})


def _evicted_pair(vault: Path) -> tuple[Path, Path, int]:
    """Two stored originals, both evicted, and their bytes."""
    from exomem import media_store

    first, second = _stored(vault, "first", 300), _stored(vault, "second", 400)
    evicted = first.stat().st_size + second.stat().st_size
    assert media_store.evict(vault, first) and media_store.evict(vault, second)
    return first, second, evicted


def test_a_retained_store_that_cannot_hold_its_evicted_originals_evicts_a_hydrated_copy_again(vault: Path, selected_store, monkeypatch):
    """Catches a turned-off cell that hydrates an owner read and never evicts the copy, so the volume fills."""
    from exomem import commands, media_store

    first, second, evicted = _evicted_pair(vault)
    _retained(selected_store, vault)
    newcomer = _preserved(vault, category="newcomer")
    # 450 bytes in use; the growth threshold of 800 leaves room for 350 of the evicted bytes.
    monkeypatch.setattr(shutil, "disk_usage", _volume(1_000, 450, first, second))

    with media_store.open_original(vault, first, read=True):
        pass
    assert originals.original_state(vault, first).state == "cached"
    assert media_store.evict_to_target(vault) == 1

    assert originals.original_state(vault, first).state == "evicted"
    assert originals.original_state(vault, second).state == "evicted"
    with pytest.raises(media_store.MediaStoreError, match="offload is disabled"):
        media_store.offload(vault, newcomer)
    assert originals.original_state(vault, newcomer).state == "local"
    status = commands.op_adoption_studio(vault, action="status")
    assert status["retained_store"]["shortfall"] == {"bytes": evicted - 350}


def test_a_retained_store_whose_evicted_originals_fit_keeps_a_hydrated_copy(vault: Path, selected_store, monkeypatch):
    """Catches eviction that undoes the drain: a hydrated copy leaves again although everything evicted fits."""
    from exomem import commands, media_store

    first, second, _evicted = _evicted_pair(vault)
    _retained(selected_store, vault, growth_threshold=0.99, eviction_target=0.5)
    monkeypatch.setattr(shutil, "disk_usage", _volume(1_000, 250, first, second))

    with media_store.open_original(vault, first, read=True):
        pass
    # The copy puts the volume above its eviction target, which evicts it while the policy was served.
    assert shutil.disk_usage(vault).used > 500

    assert media_store.evict_to_target(vault) == 0
    assert originals.original_state(vault, first).state == "cached"
    assert commands.op_adoption_studio(vault, action="status")["retained_store"]["shortfall"] == {"bytes": 0}


def test_a_retained_store_with_an_unreadable_ledger_reports_its_shortfall_as_unknown(vault: Path, selected_store, monkeypatch):
    """Catches a shortfall of 0 for a ledger that the cell could not read."""
    from exomem import commands, media_jobs

    first, second, _evicted = _evicted_pair(vault)
    _retained(selected_store, vault)
    monkeypatch.setattr(shutil, "disk_usage", _volume(1_000, 450, first, second))
    media_jobs.MediaJobStore(vault).path.write_bytes(b"not a database" * 100)

    shortfall = commands.op_adoption_studio(vault, action="status")["retained_store"]["shortfall"]

    assert set(shortfall) == {"unknown"}


def _census(vault: Path, capsys) -> tuple[int, str, str]:
    """Run `exomem cell-media-census` as the backup Job does: it reads the drain record and nothing else."""
    from exomem import cell_media_census

    code = cell_media_census.main(["--vault", str(vault)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _ledger_rows(vault: Path) -> dict[str, tuple[int, int | None]]:
    """Each cache row of the ledger as `(evicted, size)`, by the original's path."""
    from exomem import media_jobs

    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        return {relative: (evicted, size) for relative, evicted, size in
                connection.execute("SELECT binary_rel, evicted, size FROM media_cache")}


def test_the_census_counts_three_evicted_originals_and_their_bytes(vault: Path, selected_store, capsys):
    """Catches a census that counts a hydrated copy as evicted, or reports a size other than the original's."""
    from exomem import media_drain_record, media_store

    paths = [_stored(vault, f"original-{size}", size) for size in (200, 300, 450)]
    cached = _stored(vault, "still-on-the-volume", 100)
    sizes = [path.stat().st_size for path in paths]
    for path in paths:
        assert media_store.evict(vault, path)
    _retained(selected_store, vault)

    assert media_store.drain_census(vault) == (3, sum(sizes))

    code, out, _err = _census(vault, capsys)
    assert code == 0 and out == media_drain_record.read(vault) + "\n"
    assert out.startswith(f"3 {sum(sizes)} ")
    rows = _ledger_rows(vault)
    assert {rows[path.relative_to(vault).as_posix()] for path in paths} == {(1, size) for size in sizes}
    assert rows[cached.relative_to(vault).as_posix()][0] == 0
    assert not media_store.drain_record(vault).latched


def test_the_census_counts_an_original_whose_eviction_stopped_before_its_ledger_commit(vault: Path, selected_store):
    """Catches a census read from the ledger, which says 0 for an original whose bytes left before its row was marked."""
    from exomem import media_store

    path = _stored(vault, "interrupted", 300)
    size = path.stat().st_size
    path.unlink()  # Eviction unlinks the bytes, then marks the row; a stop in between leaves evicted = 0.
    _retained(selected_store, vault)
    assert _ledger_rows(vault)[path.relative_to(vault).as_posix()] == (0, size)

    assert media_store.drain_census(vault) == (1, size)

    assert _ledger_rows(vault)[path.relative_to(vault).as_posix()] == (1, size)


def test_a_drained_retained_store_never_evicts_again(vault: Path, selected_store, monkeypatch):
    """Catches an eviction after the zero that cellctl acts on, which would put an original out of the cell's reach."""
    from exomem import media_jobs, media_store

    cached, other = _stored(vault, "cached", 300), _stored(vault, "other", 200)
    _retained(selected_store, vault)
    assert media_store.drain_census(vault) == (0, 0)
    assert media_store.drain_record(vault).latched
    # A row evicted with no size leaves the shortfall unknown, which evicts as a served cell does.
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        connection.execute("UPDATE media_cache SET evicted = 1, size = NULL WHERE binary_rel = ?",
                           (other.relative_to(vault).as_posix(),))
    monkeypatch.setattr(shutil, "disk_usage", _volume(1_000, 600, cached, other))
    assert shutil.disk_usage(vault).used > 700  # Above the eviction target.

    assert media_store.evict_to_target(vault) == 0

    assert originals.original_state(vault, cached).state == "cached"


def test_a_census_above_zero_clears_the_latch(vault: Path, selected_store):
    """Catches a latch that outlives a restore: originals evicted again, and eviction off for good."""
    from exomem import media_store

    path = _stored(vault, "restored", 300)
    size = path.stat().st_size
    _retained(selected_store, vault)
    assert media_store.drain_census(vault) == (0, 0)
    assert media_store.drain_record(vault).latched
    path.unlink()  # A restore brings back the companion without its bytes.

    assert media_store.drain_census(vault) == (1, size)

    assert not media_store.drain_record(vault).latched


def test_an_unknown_census_leaves_the_latch_and_the_record_as_they_were(vault: Path, selected_store):
    """Catches a census that skips the row it cannot read, or rewrites the record, so a cell that holds media drains."""
    from exomem import media_drain_record, media_store

    path = _stored(vault, "cached", 300)
    _retained(selected_store, vault)
    assert media_store.drain_census(vault) == (0, 0)
    line = media_drain_record.read(vault)
    # A companion cut off inside its front matter: the walk cannot say what it binds.
    path.with_name(path.name + ".md").write_bytes(b"---\nkind: companion\n")

    with pytest.raises(media_store.MediaStoreError) as unknown:
        media_store.drain_census(vault)

    assert unknown.value.code == "MEDIA_CENSUS_UNKNOWN" and str(vault) not in str(unknown.value)
    assert media_store.drain_record(vault).latched
    assert media_drain_record.read(vault) == line


def test_a_served_policy_clears_the_record_and_the_latch(vault: Path, selected_store):
    """Catches a record that survives the store being turned on, which a later turn-off would read as drained."""
    from exomem import media_drain_record, media_store

    _stored(vault, "cached", 300)
    _retained(selected_store, vault)
    assert media_store.drain_census(vault) == (0, 0)
    _configure(selected_store, vault)

    media_store.drain_tick(vault)

    assert media_store.drain_record(vault) is None
    assert not media_drain_record.record_path(vault).exists()


def test_a_retained_store_whose_originals_fit_hydrates_them_back_and_records_zero(vault: Path, selected_store, monkeypatch, capsys):
    """Catches a turned-off store that keeps its key and its evicted originals although they fit on the volume."""
    from exomem import media_store

    paths = [_stored(vault, f"original-{size}", size) for size in (200, 300, 450)]
    for path in paths:
        assert media_store.evict(vault, path)
    _retained(selected_store, vault)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 1_000, *paths))

    media_store.drain_tick(vault)

    assert [originals.original_state(vault, path).state for path in paths] == ["cached"] * 3
    code, out, _err = _census(vault, capsys)
    assert code == 0 and out.startswith("0 0 ")
    assert media_store.drain_record(vault).latched


def test_the_media_workers_eviction_tick_drains_a_retained_store(vault: Path, selected_store, monkeypatch):
    """Catches a drain that nothing schedules: the cell keeps its evicted originals and its key."""
    from exomem import media_store, media_worker

    path = _stored(vault, "evicted", 300)
    assert media_store.evict(vault, path)
    _retained(selected_store, vault)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 1_000, path))

    media_worker._evict(vault)

    assert originals.original_state(vault, path).state == "cached"
    assert media_store.drain_record(vault).latched


def test_a_drain_inside_the_margin_band_hydrates_nothing_and_evicts_nothing(vault: Path, selected_store, monkeypatch):
    """Catches a drain that hydrates what eviction then evicts again, so the volume churns."""
    from exomem import media_store

    first, second, kept = _stored(vault, "first", 250), _stored(vault, "second", 300), _stored(vault, "kept", 200)
    assert media_store.evict(vault, first) and media_store.evict(vault, second)
    _retained(selected_store, vault, eviction_target=0.15)
    # 200 in use leaves room for 600 below the growth threshold; the 550 evicted do not leave the margin of 100.
    monkeypatch.setattr(shutil, "disk_usage", _volume(1_000, 0, first, second, kept))
    assert shutil.disk_usage(vault).used > 150  # Above the eviction target, so eviction would act on a shortfall.

    assert media_store.drain_step(vault) == 0
    assert media_store.evict_to_target(vault) == 0

    assert [originals.original_state(vault, path).state for path in (first, second, kept)] == ["evicted", "evicted", "cached"]


def test_the_drain_stops_when_an_owners_write_takes_the_room_below_the_growth_threshold(vault: Path, selected_store, monkeypatch):
    """Catches a drain that hydrates past the growth threshold when the volume fills while it runs."""
    from types import SimpleNamespace

    from exomem import media_store

    first, second = _stored(vault, "first", 300), _stored(vault, "second", 400)
    assert media_store.evict(vault, first) and media_store.evict(vault, second)
    _retained(selected_store, vault)

    def usage(_path):
        # The owner writes 400 bytes as the first original returns.
        used = sum(path.stat().st_size for path in (first, second) if path.exists()) + (400 if first.exists() else 0)
        return SimpleNamespace(total=1_000, used=used, free=1_000 - used)

    monkeypatch.setattr(shutil, "disk_usage", usage)

    assert media_store.drain_step(vault) == 1

    assert originals.original_state(vault, first).state == "cached"
    assert originals.original_state(vault, second).state == "evicted"


def test_the_drain_leaves_a_busy_hydration_slot_to_the_owner(vault: Path, selected_store, monkeypatch):
    """Catches a drain that waits on, steals or fails over the hydration slot that an owner's read holds."""
    import os

    from exomem import media_jobs, media_store

    path = _stored(vault, "evicted", 300)
    assert media_store.evict(vault, path)
    _retained(selected_store, vault, max_hydrations=1)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 1_000, path))
    ledger = media_jobs.MediaJobStore(vault)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("INSERT INTO media_access(token, binary_rel, pid, temporary_bytes, hydrating) "
                           "VALUES ('owner', 'elsewhere', ?, 1024, 1)", (os.getpid(),))

    assert media_store.drain_step(vault) == 0

    assert originals.original_state(vault, path).state == "evicted"
    with sqlite3.connect(ledger.path) as connection:
        assert [token for (token,) in connection.execute("SELECT token FROM media_access")] == ["owner"]
        connection.execute("DELETE FROM media_access WHERE token = 'owner'")
    assert media_store.drain_step(vault) == 1


def test_a_census_that_just_counted_waits_out_the_hour(vault: Path, selected_store, monkeypatch):
    """Catches a walk of the whole Knowledge Base on every tick of the media worker."""
    from exomem import media_jobs, media_store

    path = _stored(vault, "interrupted", 300)
    path.unlink()
    _retained(selected_store, vault, eviction_enabled=False)
    calls: list[Path] = []
    real = media_store.drain_census
    monkeypatch.setattr(media_store, "drain_census", lambda root: calls.append(root) or real(root))

    media_store.drain_tick(vault)
    media_store.drain_tick(vault)
    assert len(calls) == 1

    long_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    with sqlite3.connect(media_jobs.MediaJobStore(vault).path) as connection:
        connection.execute("UPDATE media_drain SET attempted_at = ?", (long_ago,))
    media_store.drain_tick(vault)
    assert len(calls) == 2


def test_the_retained_status_reports_the_census_the_latch_and_the_originals_too_large_to_hydrate(vault: Path, selected_store, monkeypatch):
    """Catches a status that hides the originals that keep the key, or shows a drain that no census has confirmed."""
    from exomem import commands, media_store

    first, second, _evicted = _evicted_pair(vault)
    _retained(selected_store, vault, max_temporary_bytes=350)
    monkeypatch.setattr(shutil, "disk_usage", _volume(1_000, 100, first, second))
    before = commands.op_adoption_studio(vault, action="status")["retained_store"]

    media_store.drain_census(vault)
    after = commands.op_adoption_studio(vault, action="status")["retained_store"]

    assert set(before["census"]) == {"unknown"} and before["drained"] is False
    assert (after["census"]["evicted"], after["census"]["bytes"]) == (2, 700)
    assert after["drained"] is False
    assert after["above_hydration_capacity"] == {"count": 1}
