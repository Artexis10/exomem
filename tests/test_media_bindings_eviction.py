"""Real store recovery versions and all logical readers decide one physical eviction."""

from __future__ import annotations

import hashlib
import io
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest
from test_media_store import selected_store  # noqa: F401

from exomem import (
    artifact_lifecycle,
    held_fs,
    media_bindings,
    media_capacity,
    media_jobs,
    media_store,
    original_pool,
    originals,
    preserve,
    reserved_paths,
)
from exomem import (
    vault as vault_module,
)
from exomem.governance.principal import library_scope


@pytest.mark.parametrize("all_recoverable", [False, True])
def test_a_shared_object_evicts_only_after_every_filing_has_recovery_and_every_read_releases(
    vault, selected_store, monkeypatch, all_recoverable,  # noqa: F811
):
    """One filing's confirmed version must not authorize deletion beneath an unbacked or pinned sibling."""
    content = b"one original with two independent archive filings"
    with library_scope():
        first = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                        stream=io.BytesIO(content))
        second = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="second.txt",
                                         stream=io.BytesIO(content), raw_protection=True)
    assert media_store.offload(vault, vault / first.path) is not None
    if all_recoverable:
        assert media_store.offload(vault, vault / second.path) is not None
    sidecars = {item.sidecar_path: (vault / item.sidecar_path).read_bytes() for item in (first, second)}
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(content).hexdigest()
    # This fixture exercises already-converted bindings, not the maintenance conversion job.
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as prepared:
        with media_store._guard(vault, "test_shared_original_publication"):
            prepared.publish()
            for item in (first, second):
                (vault / item.path).unlink()
    assert media_bindings.rebuild(vault)
    for item in (first, second):
        with media_store.open_original(vault, item.path) as source:
            assert source.stream.read() == content
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        rows = conn.execute("SELECT binary_rel, size FROM media_cache").fetchall()
        assert [tuple(row) for row in rows] == [(original_pool.object_path(vault, digest).relative_to(vault).as_posix(), len(content))]
    if all_recoverable:
        assert all(media_store.cache_matches(vault, vault / item.path) for item in (first, second))
    bounds = media_capacity.bounds_for(originals.configuration(vault), vault)
    assert bounds is not None
    ledger = media_jobs.MediaJobStore(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        members, parameters = media_store._logical_cache_members(vault, conn)
        references = {row["binary_rel"]: row["reference"] for row in conn.execute(members, parameters)}
    assert set(references) == {first.path, second.path}
    assert references[first.path] is not None
    assert (references[second.path] is not None) is all_recoverable
    figures = media_store.occupancy(vault, bounds, eviction_idle_seconds=0)
    assert figures.evictable_bytes == (len(content) if all_recoverable else 0)
    candidates = media_store._candidates(ledger, {"hydration": bounds.hydration_bytes, "idle_before": float("inf")}, 0)
    assert candidates == ([(min(first.path, second.path), len(content))] if all_recoverable else [])
    job = None
    if all_recoverable:
        ledger.enqueue(media_jobs.MediaJob(vault / second.path, vault / second.sidecar_path, "text"))
        assert media_store.occupancy(vault, bounds, eviction_idle_seconds=0).evictable_bytes == len(content)
        job = ledger.claim_next()
        assert job is not None
        figures = media_store.occupancy(vault, bounds, eviction_idle_seconds=0)
        assert figures.evictable_bytes == 0 and figures.pinned_bytes == len(content)
        with media_store.open_original(vault, second.path, job=job):
            pass
        assert ledger.defer(job)
        assert media_store.occupancy(vault, bounds, eviction_idle_seconds=0).pinned_bytes == len(content)
        job = ledger.claim_next()
        assert job is not None
        ledger.complete(job)
    with media_store.open_original(vault, second.path) as source:
        assert media_store.occupancy(vault, bounds, eviction_idle_seconds=0).evictable_bytes == 0
        assert not media_store.evict(vault, vault / first.path)
        assert source.stream.read() == content
    if all_recoverable:
        assert media_store.evict(vault, vault / first.path)
        assert not original_pool.object_path(vault, digest).exists()
        assert all(originals.original_state(vault, item.path).state == "evicted" for item in (first, second))
        rows, coverage = media_store.processing_status(vault)
        assert coverage["coverage"] == "partial" and coverage["state"] == "available"
        assert {row["path"] for row in rows} == {first.path, second.path}
        downloads = []
        actual_open = media_store.FilesystemStore.open

        @contextmanager
        def download_while_a_sibling_requests(self, name, version):
            downloads.append((name, version))
            with closing(ledger._connect(readonly=True)) as conn:
                reservation = conn.execute("SELECT * FROM media_access WHERE hydrating = 1").fetchone()
                assert reservation["hydration_key"] == original_pool.object_path(vault, digest).relative_to(vault).as_posix()
                assert reservation["object_key"] is None  # A destination does not prove acquired bytes.
                assert reservation["temporary_identity"] is not None
                assert reservation["temporary_bytes"] == len(content)
            # Two slots are available; the shared destination itself prevents the second download.
            with pytest.raises(media_store.MediaStoreError, match="STORE_BUSY"):
                with media_store.open_original(vault, second.path):
                    pytest.fail("another download already owns these shared bytes")
            with actual_open(self, name, version) as stream:
                yield stream

        monkeypatch.setattr(media_store.FilesystemStore, "open", download_while_a_sibling_requests)
        for filing in (first, second):
            with media_store.open_original(vault, filing.path) as source:
                assert source.stream.read() == content
                source.require_current()
            assert not (vault / filing.path).exists()
        assert len(downloads) == 1
        assert original_pool.object_path(vault, digest).read_bytes() == content
        with closing(ledger._connect(readonly=True)) as conn:
            assert conn.execute("SELECT count(*) FROM media_access").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM media_cache WHERE evicted = 0").fetchone()[0] == 1
    else:
        assert not media_store.evict(vault, vault / first.path)
        assert original_pool.object_path(vault, digest).read_bytes() == content
    assert sidecars == {rel: (vault / rel).read_bytes() for rel in sidecars}


def test_a_new_filing_retains_the_shared_object_during_verification_and_until_commit_releases(
    vault, selected_store, monkeypatch,  # noqa: F811
):
    """Eviction must not remove a verified winner while a new filing hashes it or awaits its commit."""
    content = b"a recoverable original reused by a new filing"
    with library_scope():
        filing = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                         stream=io.BytesIO(content))
    assert media_store.offload(vault, vault / filing.path) is not None
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(content).hexdigest()
    target = original_pool.object_path(vault, digest)
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as prepared:
        prepared.publish()
        (vault / filing.path).unlink()
    assert media_bindings.rebuild(vault)
    sidecar = (vault / filing.sidecar_path).read_bytes()
    with media_store.open_original(vault, filing.path) as source:
        assert source.stream.read() == content
    actual_open = reserved_paths._open_owner_binary
    attempted = []

    class ReadWhileEvicting:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size=-1):
            if not attempted:
                attempted.append(True)
                assert not media_store.evict(vault, vault / filing.path)
            return self.stream.read(size)

        def seek(self, offset):
            return self.stream.seek(offset)

    @contextmanager
    def open_during_eviction(root, path, descriptor):
        with actual_open(root, path, descriptor) as source:
            yield replace(source, stream=ReadWhileEvicting(source.stream)) if path == target else source

    monkeypatch.setattr(reserved_paths, "_open_owner_binary", open_during_eviction)
    with pytest.raises(RuntimeError, match="filing commit interrupted"):
        with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda content=content: [content])) as prepared:
            assert attempted == [True]
            assert prepared.confirm_existing()  # Replacing winner handles must retain the physical pin.
            prepared.publish()
            assert not media_store.evict(vault, vault / filing.path)
            prepared.require_current()
            raise RuntimeError("filing commit interrupted")
    assert (vault / filing.sidecar_path).read_bytes() == sidecar
    assert media_store.evict(vault, vault / filing.path)
    assert not target.exists()


@pytest.mark.parametrize("claimed_job", [False, True])
def test_rebinding_after_read_admission_pins_and_ages_the_bytes_actually_acquired(
    vault, selected_store, monkeypatch, claimed_job,  # noqa: F811
):
    """A stale admission key must not leave replacement bytes evictable or age another physical object."""
    first_bytes, second_bytes = b"the first original", b"the replacement original with its own filing"
    filings = []
    with library_scope():
        for name, content in (("first.txt", first_bytes), ("second.txt", second_bytes)):
            filings.append(preserve.preserve_stream(vault, scope="Archive", category="documents", filename=name,
                                                    stream=io.BytesIO(content)))
    for filing in filings:
        assert media_store.offload(vault, vault / filing.path) is not None
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    keys = []
    for filing, content in zip(filings, (first_bytes, second_bytes), strict=True):
        digest = hashlib.sha256(content).hexdigest()
        with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda content=content: [content])) as prepared:
            prepared.publish()
            (vault / filing.path).unlink()
        keys.append(original_pool.object_path(vault, digest).relative_to(vault).as_posix())
    assert media_bindings.rebuild(vault)
    for filing in filings:
        with media_store.open_original(vault, filing.path) as source:
            source.require_current()
    ledger = media_jobs.MediaJobStore(vault)
    job = None
    if claimed_job:
        ledger.enqueue(media_jobs.MediaJob(vault / filings[0].path, vault / filings[0].sidecar_path, "text"))
        job = ledger.claim_next()
        assert job is not None
        with closing(ledger._connect(readonly=True)) as conn:
            assert conn.execute("SELECT object_key FROM jobs WHERE id = ?", (job.id,)).fetchone()[0] is None
    with closing(ledger._connect()) as conn, conn:
        conn.executemany("UPDATE media_cache SET owner_read = ? WHERE binary_rel = ?", ((1.0, keys[0]), (2.0, keys[1])))
    first, second = filings
    first_fields, first_body, _marker = vault_module.parse_frontmatter((vault / first.sidecar_path).read_text())
    second_fields, _body, _marker = vault_module.parse_frontmatter((vault / second.sidecar_path).read_text())
    replacement_binding = originals.binary_binding(second_fields)
    assert replacement_binding is not None
    first_fields["stored_object"] = second_fields["stored_object"]
    descriptor = first_fields["governance_companion"]
    descriptor["artifact_sha256"] = replacement_binding.sha256
    descriptor["artifact_size"] = replacement_binding.size
    replacement = vault_module.render_frontmatter_document(vault_module.serialize_frontmatter(first_fields), first_body)
    actual_pin = media_store._pin
    rebound = []

    @contextmanager
    def rebind_after_admission(root, path, **kwargs):
        with actual_pin(root, path, **kwargs) as pin:
            if path == vault / first.path and not rebound:
                rebound.append(True)
                with library_scope():
                    vault_module.batch_atomic_write(
                        [vault_module.PlannedWrite(vault / first.sidecar_path, replacement)], vault_root=vault)
            yield pin

    monkeypatch.setattr(media_store, "_pin", rebind_after_admission)
    with media_store.open_original(vault, first.path, read=True, job=job) as source:
        assert media_bindings.rebuild(vault)
        assert not media_store.evict(vault, vault / second.path)
        assert source.stream.read() == second_bytes
        source.require_current()
    with closing(ledger._connect(readonly=True)) as conn:
        ages = dict(conn.execute("SELECT binary_rel, owner_read FROM media_cache"))
    assert ages[keys[0]] == 1.0 and ages[keys[1]] > 2.0
    if job is not None:
        assert not media_store.evict(vault, vault / second.path)
        assert ledger.defer(job)
        reclaimed = ledger.claim_next()
        assert reclaimed is not None and reclaimed.claim_revision != job.claim_revision
        assert not ledger.bind_original(job, keys[0])  # The former worker cannot change the new claim.
        assert not ledger.bind_original(reclaimed, keys[0])  # A slice cannot move an acquired pin.
        with media_store.open_original(vault, first.path, job=reclaimed) as source:
            assert source.stream.read() == second_bytes
        ledger.complete(reclaimed)
    assert media_store.evict(vault, vault / second.path)


def test_a_drain_latch_keeps_a_shared_object_resident_through_every_filing(
    vault, selected_store, monkeypatch,  # noqa: F811
):
    """Catches a shared object's eviction that ignores the drain latch, so a drained cell's zero stops proving local bytes.

    Both filings could be recovered from the store, so only the latch refuses the eviction. A served policy releases it.
    """
    from test_media_store import _configure, _retained

    content = b"one original with two independent archive filings"
    with library_scope():
        first = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                        stream=io.BytesIO(content))
        second = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="second.txt",
                                         stream=io.BytesIO(content), raw_protection=True)
    for item in (first, second):
        assert media_store.offload(vault, vault / item.path) is not None
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as prepared:
        with media_store._guard(vault, "test_shared_original_publication"):
            prepared.publish()
            for item in (first, second):
                (vault / item.path).unlink()
    for item in (first, second):
        with media_store.open_original(vault, item.path) as source:
            assert source.stream.read() == content
    _retained(selected_store, vault)
    assert media_bindings.rebuild(vault)  # A policy change makes the binding facts stale until they are rebuilt.

    assert media_store.drain_census(vault) == (0, 0)
    assert media_store.drain_record(vault).latched
    for item in (first, second):
        assert not media_store.evict(vault, vault / item.path)
    assert original_pool.object_path(vault, digest).read_bytes() == content

    _configure(selected_store, vault)
    media_store.drain_tick(vault)
    assert media_store.drain_record(vault) is None
    assert media_bindings.rebuild(vault)
    assert media_store.evict(vault, vault / first.path)
    assert not original_pool.object_path(vault, digest).exists()
