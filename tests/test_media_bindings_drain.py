"""A retained store releases custody only after every logical binding has local bytes."""

from __future__ import annotations

import hashlib
import io
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_media_store import _retained, selected_store  # noqa: F401

from exomem import (
    artifact_lifecycle,
    held_fs,
    media_bindings,
    media_drain_record,
    media_jobs,
    media_store,
    original_pool,
    preserve,
)
from exomem.governance.principal import library_scope


def test_two_shared_filings_drain_as_one_object_and_hydrate_through_an_own_reference(
    vault, selected_store, monkeypatch,  # noqa: F811
):
    """A pool key must never become a caller path, and two bindings must not double the drain count."""
    content = b"one protected and one ordinary filing share these exact bytes"
    with library_scope():
        first = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                        stream=io.BytesIO(content))
        second = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="second.txt",
                                         stream=io.BytesIO(content), raw_protection=True)
    for item in (first, second):
        assert media_store.offload(vault, vault / item.path) is not None
    companions = {item.sidecar_path: (vault / item.sidecar_path).read_bytes() for item in (first, second)}
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as prepared:
        with media_store._guard(vault, "test_pool_publication"):
            prepared.publish()
            for item in (first, second):
                (vault / item.path).unlink()
    assert media_bindings.rebuild(vault)
    with media_store.open_original(vault, first.path):
        pass
    assert media_store.evict(vault, vault / first.path)
    _retained(selected_store, vault)

    assert media_store.drain_census(vault) == (1, len(content))
    assert media_store.drain_record(vault).unit == "physical_objects"
    assert media_store.drain_step(vault, claimable=lambda: None) == 1
    assert media_store.drain_census(vault) == (0, 0)

    assert media_store.drain_record(vault).latched
    assert original_pool.object_path(vault, digest).read_bytes() == content
    for item in (first, second):
        assert not (vault / item.path).exists()
        with media_store.open_original(vault, item.path) as source:
            assert source.stream.read() == content
        assert (vault / item.sidecar_path).read_bytes() == companions[item.sidecar_path]


def test_a_missing_unbacked_original_is_unknown_instead_of_a_complete_zero(vault, selected_store):  # noqa: F811
    """A reference-only walk would omit this retained identity and incorrectly release the store key."""
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="datasets", filename="unbacked.csv",
                                       stream=io.BytesIO(b"sample,value\n1,2\n"), raw_protection=True)
    companion = (vault / item.sidecar_path).read_bytes()
    (vault / item.path).unlink()
    _retained(selected_store, vault)

    with pytest.raises(media_store.MediaStoreError, match="MEDIA_CENSUS_UNKNOWN"):
        media_store.drain_census(vault)

    assert media_store.drain_record(vault).blocked_by == (item.path,)
    assert not media_drain_record.record_path(vault).exists()
    assert (vault / item.sidecar_path).read_bytes() == companion


def test_an_extant_blank_companion_cannot_erase_an_acquired_binding_witness(vault, selected_store):  # noqa: F811
    """Rebuilding the projection must retain evidence of a filing whose companion lost its identity."""
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="retained.txt",
                                       stream=io.BytesIO(b"retained bytes"))
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    (vault / item.path).unlink()
    (vault / item.sidecar_path).write_bytes(b"")
    _retained(selected_store, vault)

    with pytest.raises(media_store.MediaStoreError, match="MEDIA_CENSUS_UNKNOWN"):
        media_store.drain_census(vault)

    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.checkpoint(conn).complete
        assert conn.execute("SELECT binary_rel FROM media_bindings").fetchone()[0] == item.path
    assert not media_drain_record.record_path(vault).exists()


def test_a_frontier_change_during_physical_probes_cannot_publish_zero(vault, selected_store, monkeypatch):  # noqa: F811
    """A completed scan cannot bless bindings that changed before its atomic ledger publication."""
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="changing.txt",
                                       stream=io.BytesIO(b"locally present bytes"))
    _retained(selected_store, vault)
    probe = media_store._on_volume

    def change_after_probe(root, relative):
        present = probe(root, relative)
        if relative == item.path:
            media_bindings.withdraw(root)
        return present

    monkeypatch.setattr(media_store, "_on_volume", change_after_probe)
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_CENSUS_UNKNOWN"):
        media_store.drain_census(vault)
    assert not media_drain_record.record_path(vault).exists()


def test_a_legacy_reference_only_zero_does_not_survive_ledger_upgrade(vault):
    """An older census never proved that unbacked or shared bindings had local bytes."""
    ledger = media_jobs.MediaJobStore(vault)
    media_drain_record.write(vault, 0, 0, datetime.now(UTC))
    with sqlite3.connect(ledger.path) as conn:
        conn.execute("ALTER TABLE media_drain DROP COLUMN proof_version")
        conn.execute("INSERT INTO media_drain(singleton, evicted, bytes, latched, attempted_at) "
                     "VALUES(1, 0, 0, 1, '2026-01-01T00:00:00+00:00')")
    record = media_store.drain_record(vault)
    assert not record.latched and record.attempt_failed and record.evicted is None

    media_jobs.MediaJobStore(vault)

    assert media_store.drain_record(vault) is None
    assert not media_drain_record.record_path(vault).exists()
