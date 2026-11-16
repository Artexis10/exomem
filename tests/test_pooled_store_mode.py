"""A pooled filing is no outside delete for the store-mode importer.

A store-mode vault keeps a binary record for each original that an outside change could remove. A filing whose bytes
moved into the shared pool keeps its record but has no file at its logical path. This suite lives apart from
`test_pooled_export_fixity` because the store-mode suite's `vault` fixture replaces the plain one.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from test_pooled_export_fixity import _pool
from test_pooled_move_trash import _rows
from test_shared_originals import _pooled_capture
from test_vault_store_importer import StoreThread
from test_vault_store_media import (  # noqa: F401 - fixtures
    ORIGINAL_BYTES,
    coordinator_url,
    enter_store_mode,
    media_state,
    offloaded,
    on_store_thread,
    original_thread,
    selected_store,
    serve_through,
    stored,
    stored_original,
    vault,
)
from test_vault_store_projector import NOTES

from exomem import (
    artifact_lifecycle,
    delete_file,
    list_trash,
    media_bindings,
    media_store,
    original_conversion,
    original_pool,
    originals,
    recover_from_trash,
)
from exomem import vault as vault_module
from exomem.governance.principal import library_scope
from exomem.vault_store import importer, projector, read
from exomem.vault_store import media as store_media
from exomem.vault_store import targets as store_targets

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only")


def _bound(root, page: str) -> str | None:
    """The original that a page binds, as the store holds the page."""
    fields, _body, _block = vault_module.parse_frontmatter(read.owned_bytes(root, page).decode("utf-8"))
    return originals.bound_path(fields)


def test_a_pooled_filing_is_no_outside_delete_and_keeps_its_binary_record(
    vault, stored_original, original_thread, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """An importer that reads the missing logical file as an outside delete drops the binary record,
    after which the store holds the companion for an original that nothing names."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    before = media_state(vault, rel)

    _pool(vault, monkeypatch, vault / rel)
    swept = settling.sweep()
    named = settling.import_paths([rel])

    assert not (vault / rel).exists()
    assert swept.removed == [] and named.removed == []
    assert media_state(vault, rel) == before


def test_a_pooled_filing_is_no_outside_delete_when_no_media_store_is_configured(
    vault, stored_original, original_thread, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """Without a media store the importer has no eviction to explain a missing file, so only the pool can."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    before = media_state(vault, rel)

    _pool(vault, monkeypatch, vault / rel)
    monkeypatch.delenv(originals.CONFIG_ENV)
    assert originals.binding_for(vault) is None
    swept = settling.sweep()
    named = settling.import_paths([rel])

    assert store_media.managed(vault, rel) == media_store.SUPPRESS
    assert swept.removed == [] and named.removed == []
    assert media_state(vault, rel)["binary"] == before["binary"]


@pytest.fixture
def pooled_note(vault, selected_store, monkeypatch):  # noqa: F811 - the store-mode suite's fixtures
    """An offloaded original outside the append-only trees, whose bytes then moved into the pool of a store-mode
    vault, with a thread that owns the store's writer."""
    rel = f"{NOTES}/Media/loose.txt"
    offloaded(vault, rel)
    enter_store_mode(vault)
    shared = original_pool.object_path(vault, _pool(vault, monkeypatch, vault / rel))
    thread = StoreThread(vault)
    serve_through(monkeypatch, thread)
    importer.Importer(vault, run=thread.run).sweep()
    yield rel, shared, thread
    thread.close()


@pytest.mark.parametrize("named", ["page", "binary"])
def test_a_trashed_pooled_filing_in_store_mode_lists_as_one_original_and_recovers_to_its_path(
    vault, pooled_note, named  # noqa: F811 - the store-mode suite's fixtures
):
    """A store-mode Trash that refuses a pooled filing, or that moves bytes the archive keeps only in the pool,
    leaves the owner no way to trash it; a recovery that cannot find its page strands it in Trash."""
    rel, shared, thread = pooled_note
    page = originals.sidecar_path(Path(rel)).as_posix()
    assert media_bindings.rebuild(vault)

    trashed = on_store_thread(thread, lambda: delete_file.delete_file(
        vault, path=page if named == "page" else rel, confirm=True))

    assert not store_targets.holds(vault, page) and not (vault / trashed.trash_path).exists()
    listed = list_trash.list_trash(vault)
    assert [(entry.trash_path, entry.original_path, entry.kind) for entry in listed.entries] == [
        (trashed.trash_path, rel, "file")]
    assert listed.orphan_files == [] and listed.orphan_sidecars == []
    assert media_bindings.follow_store(vault)
    complete, rows = _rows(vault)
    assert complete and rows == {(f"{trashed.trash_path}.md", trashed.trash_path)}

    recovered = on_store_thread(thread, lambda: recover_from_trash.recover_from_trash(
        vault, trash_path=trashed.trash_path))

    assert recovered.restored_path == rel and _bound(vault, page) == rel
    assert not store_targets.holds(vault, f"{trashed.trash_path}.md")
    projector.Projector(vault, run=thread.run).run_pass()
    assert list_trash.list_trash(vault).as_dict() == {
        "entries": [], "count": 0, "orphan_sidecars": [], "orphan_files": []}
    assert shared.read_bytes() == ORIGINAL_BYTES
    assert media_store.read_original_bytes(vault, rel).data == ORIGINAL_BYTES
    assert media_bindings.follow_store(vault)
    complete, rows = _rows(vault)
    assert complete and rows == {(page, rel)}


def test_an_original_evicted_in_trash_before_pooling_recovers_by_its_trash_path_in_store_mode(
    vault, selected_store, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """A recovery that takes the entry's own reference companion for a trashed filing's page refuses the entry the
    caller picked; one that fetches the entry into the pool finds no file at its Trash path and fails."""
    rel = f"{NOTES}/Media/loose.txt"
    offloaded(vault, rel)
    trashed = delete_file.delete_file(vault, path=rel, confirm=True)
    with media_store.open_original(vault, vault / trashed.trash_path):
        pass  # a read records the cache binding that an eviction at the Trash path needs
    assert media_store.evict(vault, vault / trashed.trash_path)
    enter_store_mode(vault)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    thread = StoreThread(vault)
    serve_through(monkeypatch, thread)
    try:
        planned = on_store_thread(thread, lambda: recover_from_trash.recover_from_trash(
            vault, trash_path=trashed.trash_path, validate_only=True))
        recovered = on_store_thread(thread, lambda: recover_from_trash.recover_from_trash(
            vault, trash_path=trashed.trash_path))
        projector.Projector(vault, run=thread.run).run_pass()
        listed = list_trash.list_trash(vault).as_dict()
    finally:
        thread.close()

    assert planned.restored_path == recovered.restored_path == rel
    assert (vault / rel).read_bytes() == ORIGINAL_BYTES
    assert originals.original_state(vault, rel).local_path is None
    assert listed == {"entries": [], "count": 0, "orphan_sidecars": [], "orphan_files": []}
    assert not original_pool.object_path(vault, hashlib.sha256(ORIGINAL_BYTES).hexdigest()).exists()


def test_a_store_mode_conversion_drops_the_binary_record_of_the_file_it_retires(
    vault, stored_original, original_thread, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """A conversion that keeps the record of a file it retired leaves a shape no pooled capture has: a store rollback
    reads that original as deleted. One that rewrites the page breaks the grants bound to its bytes."""
    rel = stored_original
    page = originals.original_state(vault, rel).sidecar_path.relative_to(vault).as_posix()
    page_before = read.owned_bytes(vault, page)
    data = (vault / rel).read_bytes()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))

    def convert():
        with library_scope():
            return original_conversion.convert(vault, dry_run=False)

    report = on_store_thread(original_thread, convert)

    assert report["converted"] == 1
    assert media_state(vault, rel)["binary"] == []
    assert not (vault / rel).exists()
    assert originals.pooled(vault, rel).local_path.read_bytes() == data
    assert read.owned_bytes(vault, page) == page_before


def test_a_refused_record_retirement_is_reported_and_a_rerun_completes_it(
    vault, stored_original, original_thread, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """Catches a conversion that reports a filing converted while its store record survives a refused commit."""
    from exomem.vault_store import batch as store_batch
    from exomem.vault_store.write import StoreConflict

    rel = stored_original
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    commit = store_batch.commit_batch

    def refuse(*_args, **_kwargs):
        raise StoreConflict(rel, "another writer changed the record")

    def convert():
        with library_scope():
            return original_conversion.convert(vault, dry_run=False)

    monkeypatch.setattr(store_batch, "commit_batch", refuse)
    refused = on_store_thread(original_thread, convert)
    monkeypatch.setattr(store_batch, "commit_batch", commit)
    rerun = on_store_thread(original_thread, convert)

    assert (refused["converted"], refused["skipped_changed"]) == (0, [rel])
    assert rerun["converted"] == 1
    assert media_state(vault, rel)["binary"] == []


def test_a_fresh_pooled_capture_in_store_mode_records_no_binary_and_is_no_outside_delete(
    vault, tmp_path, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """Catches a store binary record made for a pooled filing, which the importer then reads as an outside delete
    of a file that was never there (it drops the record and the page's binding); or a capture that writes the
    bytes into the store or at the logical path, beside the one pool object that should hold them."""
    data = b"Speaker A: one recording, filed twice in a store-mode vault."
    digest = hashlib.sha256(data).hexdigest()
    enter_store_mode(vault)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    thread = StoreThread(vault)
    serve_through(monkeypatch, thread)
    try:
        first = on_store_thread(thread, lambda: _pooled_capture(vault, tmp_path, monkeypatch, "briefing-a.txt", data))
        second = on_store_thread(thread, lambda: _pooled_capture(vault, tmp_path, monkeypatch, "briefing-b.txt", data))
        settling = importer.Importer(vault, run=thread.run)
        swept = settling.sweep()
        named = settling.import_paths([first.artifact_path, second.artifact_path])
    finally:
        thread.close()

    [shared] = (original_pool.pool_root(vault) / "sha256").rglob("*.blob")
    assert shared == original_pool.object_path(vault, digest)
    assert first.artifact_path != second.artifact_path
    for added in (first, second):
        assert store_targets.holds(vault, added.path)
        assert not (vault / added.artifact_path).exists()
        state = media_state(vault, added.artifact_path)
        assert state["binary"] == [] and len(state["sidecar"]) == 1
        assert media_store.read_original_bytes(vault, added.artifact_path).data == data
    assert swept.removed == [] and named.removed == []
