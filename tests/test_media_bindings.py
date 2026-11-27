"""Physical lifetime facts must never confer access or lose an acquired pin."""

from __future__ import annotations

import hashlib
import io
from contextlib import closing
from dataclasses import replace

import pytest

from exomem import (
    artifact_lifecycle,
    held_fs,
    media_bindings,
    media_jobs,
    media_store,
    original_pool,
    originals,
    preserve,
)
from exomem.governance.principal import library_scope


def test_equal_ordinary_files_stay_distinct_until_their_paths_are_retired(vault, monkeypatch):
    """Digest equality must not turn two unconverted files into one physical allocation."""
    content = b"independent original bytes"
    with library_scope():
        first = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                        stream=io.BytesIO(content))
        second = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="second.txt",
                                         stream=io.BytesIO(content), raw_protection=True)
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.checkpoint(conn).complete
        assert {row[0] for row in conn.execute("SELECT object_key FROM media_bindings")} == {first.path, second.path}
    companions = {item.sidecar_path: (vault / item.sidecar_path).read_bytes() for item in (first, second)}
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as prepared:
        prepared.publish()
    for item in (first, second):
        (vault / item.path).unlink()
    assert media_bindings.rebuild(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*), COUNT(DISTINCT object_key) FROM media_bindings").fetchone()[:] == (2, 1)
    assert companions == {rel: (vault / rel).read_bytes() for rel in companions}


def test_interrupted_binding_update_stays_incomplete_and_stale_completion_cannot_restore_it(vault):
    """An old receipt must not bless a newer, unfinished canonical publication."""
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    before = media_bindings.withdraw(vault)
    assert before is not None and before.complete
    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.checkpoint(conn).complete
    newer = media_bindings.withdraw(vault)
    assert not media_bindings.apply_delta(vault, (), (), expected=before)
    assert media_bindings.apply_delta(vault, (), (), expected=newer)
    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.checkpoint(conn).complete
    assert media_bindings.rebuild(vault)
    before = media_bindings.withdraw(vault)
    assert media_bindings.apply_delta(vault, (), (), expected=before)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.checkpoint(conn).complete


def test_damaged_companion_withdraws_inventory_instead_of_erasing_its_filing(vault):
    """A damaged identity must not turn an original's retention requirement into an empty inventory."""
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="sample.txt",
                                       stream=io.BytesIO(b"a retained original"))
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    (vault / item.sidecar_path).write_text(f"---\nevidence_file: {item.path}\n---\n", encoding="utf-8")
    assert not media_bindings.rebuild(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.checkpoint(conn).complete
        assert conn.execute("SELECT 1 FROM media_bindings WHERE binary_rel = ?", (item.path,)).fetchone() is not None


@pytest.mark.parametrize("interrupted", [False, True])
def test_file_publication_withdraws_before_install_and_only_a_complete_rollback_restores_it(vault, monkeypatch, interrupted):
    """A partial file batch must not leave pre-write eviction facts marked complete."""
    from exomem import vault as vault_module

    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="sample.txt",
                                       stream=io.BytesIO(b"a retained original"))
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    prior = (vault / item.sidecar_path).read_bytes()
    observed = []

    class Interrupted(BaseException):
        pass

    def after_publication(path):
        if path == vault / item.sidecar_path:
            with closing(ledger._connect(readonly=True)) as conn:
                observed.append(media_bindings.checkpoint(conn).complete)
            raise Interrupted() if interrupted else OSError("publication interrupted")

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", after_publication)
    with library_scope(), pytest.raises(Interrupted if interrupted else Exception):
        preserve.update_sidecar_extraction(vault, vault / item.sidecar_path, text="The new extracted text.", engine="text")
    assert observed == [False]
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.checkpoint(conn).complete is (not interrupted)
    if not interrupted:
        assert (vault / item.sidecar_path).read_bytes() == prior
    else:
        assert (vault / item.sidecar_path).read_bytes() != prior
        assert media_bindings.rebuild(vault)


def test_successful_file_publication_refreshes_only_the_changed_binding(vault, monkeypatch):
    """A normal companion edit must not need a whole-archive census before eviction can resume."""
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="sample.txt",
                                       stream=io.BytesIO(b"a retained original"))
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    with library_scope():
        preserve.update_sidecar_extraction(vault, vault / item.sidecar_path, text="The new extracted text.", engine="text")
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.checkpoint(conn).complete
        digest = conn.execute("SELECT companion_hash FROM media_bindings WHERE binary_rel = ?", (item.path,)).fetchone()[0]
        assert digest == hashlib.sha256((vault / item.sidecar_path).read_bytes()).hexdigest()


def test_external_edit_withdraws_before_debounce_without_blocking_reads_or_new_filings(vault, monkeypatch):
    """Unknown inventory should retain local bytes while the ordinary archive remains usable."""
    from exomem import file_watcher, freshness

    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="sample.txt",
                                       stream=io.BytesIO(b"a retained original"))
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    watcher = file_watcher.FileWatcher(vault, debounce_seconds=3600)
    with watcher._lock:
        watcher._record_external_locked(vault / item.sidecar_path, deleted=False)
    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.checkpoint(conn).complete
    try:
        with media_store.open_original(vault, item.path) as source:
            assert source.stream.read() == b"a retained original"
        with library_scope():
            created = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="another.txt",
                                              stream=io.BytesIO(b"new independent bytes"))
        # A pooled archive files new bytes once in its pool; the filing reads them through its logical path.
        with media_store.open_original(vault, created.path) as source:
            assert source.stream.read() == b"new independent bytes"
    finally:
        freshness.clear_external_pending(vault, through=watcher._pending_external_epoch)


def test_claimed_job_and_read_pin_keep_the_object_they_acquired_after_a_binding_changes(vault):
    """A refile must not move a running or returned job's pin onto its replacement bytes."""
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="sample.txt",
                                       stream=io.BytesIO(b"a pending extraction"))
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        row = dict(conn.execute("SELECT * FROM media_bindings WHERE binary_rel = ?", (item.path,)).fetchone())
    binding = media_bindings.Binding(**row)
    ledger.enqueue(media_jobs.MediaJob(vault / item.path, vault / item.sidecar_path, "text", do_ocr=True))
    job = ledger.claim_next()
    assert job is not None
    with closing(ledger._connect(readonly=True)) as conn:
        assert conn.execute("SELECT object_key FROM jobs WHERE id = ?", (job.id,)).fetchone()[0] is None
    with media_store._pin(vault, vault / item.path) as (_ledger, token), media_store.open_original(vault, item.path) as source:
        media_store._bind_pin(ledger, token, originals.original_state(vault, item.path), source, job=job)
        before = media_bindings.withdraw(vault)
        replacement = replace(binding, object_key="Knowledge Base/Evidence/replacement.bin")
        assert media_bindings.apply_delta(vault, (item.sidecar_path,), (replacement,), expected=before)
        with closing(ledger._connect(readonly=True)) as conn:
            assert conn.execute("SELECT object_key FROM media_access WHERE binary_rel = ?", (item.path,)).fetchone()[0] == item.path
            pinned = conn.execute("SELECT object_key, pinned FROM jobs WHERE id = ?", (job.id,)).fetchone()
            assert tuple(pinned) == (item.path, 1)
        # A claimed job returned to pending retains its existing physical pin.
        with closing(ledger._connect()) as conn:
            with conn:
                conn.execute("UPDATE jobs SET state = 'pending' WHERE id = ?", (job.id,))
        reclaimed = ledger.claim_next()
        assert reclaimed is not None and reclaimed.id == job.id
        with closing(ledger._connect(readonly=True)) as conn:
            assert conn.execute("SELECT object_key FROM jobs WHERE id = ?", (job.id,)).fetchone()[0] == item.path
        assert source.stream.read() == b"a pending extraction"


def test_object_lifetime_aggregates_residency_recovery_owner_age_and_existing_pins(vault):
    """One unbacked or resident filing must retain bytes shared with an evictable sibling."""
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    key = "Knowledge Base/Evidence/shared.bin"
    reference = '{"schema":1,"store":"archive","key_version":"key","object":"' + "a" * 64 + '","version":"1",' \
                '"ciphertext_size":30,"ciphertext_sha256":"' + "b" * 64 + '"}'
    first = media_bindings.Binding("Knowledge Base/Evidence/one.bin", "Knowledge Base/Evidence/one.md",
                                   "c" * 64, key, "d" * 64, 10, False, reference)
    second = replace(first, binary_rel="Knowledge Base/Evidence/two.bin",
                     companion_rel="Knowledge Base/Evidence/two.md", resident_only=True, reference=None)
    before = media_bindings.withdraw(vault)
    assert media_bindings.apply_delta(vault, (), (first, second), expected=before)
    with closing(ledger._connect()) as conn:
        with conn:
            for binding, read in ((first, 2.0), (second, 8.0)):
                conn.execute("INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, owner_read) "
                             "VALUES (?, ?, ?, '', 0, ?)", (binding.binary_rel, reference, binding.sha256, read))
            media_bindings.coalesce_cache(conn, (key,))
        assert conn.execute("SELECT COUNT(*) FROM media_cache WHERE binary_rel = ?", (key,)).fetchone()[0] == 1
        facts = media_bindings.object_facts(conn, key)
        assert facts is not None and facts.bindings == 2
        assert facts.resident_only and not facts.recoverable and facts.owner_read == 8.0 and not facts.pinned
        assert len(facts.references) == 1
    # Merely queued work does not pin the object.
    ledger.enqueue(media_jobs.MediaJob(vault / first.binary_rel, vault / first.companion_rel, "text", do_ocr=True))
    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.object_facts(conn, key).pinned
    assert ledger.claim_next() is not None
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.object_facts(conn, key).pinned
    media_bindings.withdraw(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.object_facts(conn, key) is None


def test_a_file_write_while_the_ledger_is_being_created_files_without_bindings(vault, monkeypatch):
    """The ledger file appears before its creator commits the schema; a write in that window must still file."""
    import sqlite3

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    path = media_jobs.job_store_path(vault.resolve())
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    rel = "Notes/sample.md"

    assert media_bindings.begin_file_change(vault, {rel: b"# Sample\n"}, [rel]) is None
    assert media_bindings.withdraw(vault) is None
