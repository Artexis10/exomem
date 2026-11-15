"""Real store recovery versions and all logical readers decide one physical eviction."""

from __future__ import annotations

import hashlib
import io

import pytest
from test_media_store import selected_store  # noqa: F401

from exomem import (
    artifact_lifecycle,
    held_fs,
    media_bindings,
    media_store,
    original_pool,
    originals,
    preserve,
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
    with media_store.open_original(vault, second.path) as source:
        assert not media_store.evict(vault, vault / first.path)
        assert source.stream.read() == content
    if all_recoverable:
        assert media_store.evict(vault, vault / first.path)
        assert not original_pool.object_path(vault, digest).exists()
        assert all(originals.original_state(vault, item.path).state == "evicted" for item in (first, second))
    else:
        assert not media_store.evict(vault, vault / first.path)
        assert original_pool.object_path(vault, digest).read_bytes() == content
    assert sidecars == {rel: (vault / rel).read_bytes() for rel in sidecars}
