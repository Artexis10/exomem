"""Move, Trash and recovery of filings whose bytes are one shared, digest-addressed object."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest

from exomem import (
    artifact_lifecycle,
    delete_directory,
    delete_file,
    held_fs,
    list_trash,
    media_bindings,
    media_jobs,
    media_store,
    move_file,
    original_pool,
    originals,
    preserve,
    recover_from_trash,
)
from exomem import vault as vault_module
from exomem.governance.principal import library_scope

DATA = b"quarterly ledger, exported once and filed by its owner"
FOLDER = "Knowledge Base/Notes/Attachments"


def _filing(vault: Path, name: str = "ledger.bin", data: bytes = DATA, folder: str = FOLDER) -> tuple[str, str]:
    """An ordinary original with its page, outside the append-only trees, as its binary and page paths."""
    binary = vault / folder / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(data)
    with library_scope():
        page, _created = preserve.ensure_artifact_page(vault, binary)
    return binary.relative_to(vault).as_posix(), page.relative_to(vault).as_posix()


def _pool(vault: Path, monkeypatch: pytest.MonkeyPatch, binary: str, data: bytes = DATA) -> Path:
    """Keep the filing's bytes only as its shared object, as a converted archive holds them."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    if not original_pool.enabled(vault):
        original_pool.enable(vault)
    digest = hashlib.sha256(data).hexdigest()
    with original_pool.prepare(vault, digest, len(data), held_fs.Chunks(lambda: [data])) as staged:
        staged.publish()
    (vault / binary).unlink()
    return original_pool.object_path(vault, digest)


def _bound(vault: Path, page: str) -> str | None:
    fields, _body, _block = vault_module.parse_frontmatter((vault / page).read_text(encoding="utf-8"))
    return originals.bound_path(fields)


def _rows(vault: Path) -> tuple[bool, set[tuple[str, str]]]:
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        complete = media_bindings.checkpoint(conn).complete
        rows = {(row[0], row[1]) for row in conn.execute("SELECT companion_rel, binary_rel FROM media_bindings")}
    return complete, rows


@pytest.mark.parametrize("named", ["page", "binary"])
def test_moving_a_pooled_filing_repoints_its_page_and_leaves_its_shared_bytes(vault, monkeypatch, named):
    """Catches a move that renames bytes it does not hold, or that leaves the page naming the old path."""
    binary, page = _filing(vault)
    shared = _pool(vault, monkeypatch, binary)
    target = f"{FOLDER}/Archive/ledger.bin"

    move_file.move_file(vault, old_path=page if named == "page" else binary,
                        new_path=f"{target}.md" if named == "page" else target)

    assert not (vault / page).exists() and _bound(vault, f"{target}.md") == target
    assert not (vault / binary).exists() and not (vault / target).exists()
    assert shared.read_bytes() == DATA
    assert media_store.read_original_bytes(vault, target).data == DATA


def test_a_file_mode_move_leaves_the_binding_projection_complete_and_exact(vault, monkeypatch):
    """Catches the renamed page's old row surviving the move, which keeps eviction from trusting the census."""
    binary, page = _filing(vault)
    _pool(vault, monkeypatch, binary)
    assert media_bindings.rebuild(vault)
    target = f"{FOLDER}/Archive/ledger.bin"

    move_file.move_file(vault, old_path=page, new_path=f"{target}.md")

    complete, rows = _rows(vault)
    assert complete
    assert (f"{target}.md", target) in rows
    assert not {row for row in rows if page in row or binary in row}


def test_an_evicted_pooled_filing_moves_without_fetching_its_bytes(vault, tmp_path, monkeypatch):
    """Catches a move that downloads a stored version only to rename a file the archive never keeps."""
    binary, page = _filing(vault)
    config = tmp_path / "media.json"
    config.write_text(json.dumps({"vaults": {str(vault.resolve()): {"store": "test-store"}}}))
    monkeypatch.setenv(originals.CONFIG_ENV, str(config))
    reference = originals.StoredObject(1, "test-store", "1", "a" * 64, "version", 90, "b" * 64)
    fields, body, _block = vault_module.parse_frontmatter((vault / page).read_text(encoding="utf-8"))
    (vault / page).write_text(vault_module.render_frontmatter_document(
        vault_module.serialize_frontmatter({**fields, "stored_object": reference.fields()}), body,
    ), encoding="utf-8")
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    (vault / binary).unlink()
    assert originals.original_state(vault, binary).state == "evicted"

    def no_download(*args, **kwargs):
        raise AssertionError("a digest-addressed move fetched its stored version")

    monkeypatch.setattr(media_store, "_hydrate", no_download)
    target = f"{FOLDER}/Archive/ledger.bin"
    move_file.move_file(vault, old_path=binary, new_path=target)

    moved = originals.original_state(vault, target)
    assert moved.state == "evicted" and moved.reference == reference
    assert not (vault / target).exists()


def test_a_pooled_filing_holds_its_path_against_a_moved_file(vault, monkeypatch):
    """Catches a move that installs another file at a pooled filing's path, shadowing its shared bytes."""
    binary, _page = _filing(vault)
    _pool(vault, monkeypatch, binary)
    loose = vault / FOLDER / "loose.bin"
    loose.write_bytes(b"an unrelated file with no page")

    with pytest.raises(move_file.MoveFileError) as refused:
        move_file.move_file(vault, old_path=loose.relative_to(vault).as_posix(), new_path=binary)

    assert refused.value.code == "DEST_EXISTS"
    assert loose.exists() and not (vault / binary).exists()


@pytest.mark.parametrize("named", ["page", "binary"])
def test_a_trashed_pooled_filing_lists_as_one_original_and_recovers_to_its_path(vault, monkeypatch, named):
    """Catches a page trashed alone, which strands the shared bytes and leaves Trash that no census can read."""
    binary, page = _filing(vault)
    shared = _pool(vault, monkeypatch, binary)
    assert media_bindings.rebuild(vault)

    trashed = delete_file.delete_file(vault, path=page if named == "page" else binary, confirm=True)

    assert not (vault / page).exists() and not (vault / trashed.trash_path).exists()
    listed = list_trash.list_trash(vault)
    assert [(entry.trash_path, entry.original_path, entry.kind) for entry in listed.entries] == [
        (trashed.trash_path, binary, "file")]
    assert listed.orphan_files == [] and listed.orphan_sidecars == []
    complete, rows = _rows(vault)
    assert complete and rows == {(f"{trashed.trash_path}.md", trashed.trash_path)}

    recovered = recover_from_trash.recover_from_trash(vault, trash_path=trashed.trash_path)

    assert recovered.restored_path == binary and _bound(vault, page) == binary
    assert not (vault / f"{trashed.trash_path}.md").exists() and not (vault / trashed.trash_meta_path).exists()
    assert shared.read_bytes() == DATA and media_store.read_original_bytes(vault, binary).data == DATA
    complete, rows = _rows(vault)
    assert complete and rows == {(page, binary)}


def test_a_trashed_folder_keeps_its_pooled_filing_bound_through_recovery(vault, monkeypatch):
    """Catches a folder Trash that loses a pooled member's binding row or its placement."""
    binary, page = _filing(vault)
    shared = _pool(vault, monkeypatch, binary)
    assert media_bindings.rebuild(vault)

    trashed = delete_directory.delete_directory(vault, path=FOLDER, confirm=True, recursive=True)

    complete, rows = _rows(vault)
    assert complete and rows == {(f"{trashed.trash_path}/ledger.bin.md", f"{trashed.trash_path}/ledger.bin")}
    assert shared.read_bytes() == DATA

    recover_from_trash.recover_from_trash(vault, trash_path=trashed.trash_path)

    assert _bound(vault, page) == binary and media_store.read_original_bytes(vault, binary).data == DATA
    complete, rows = _rows(vault)
    assert complete and rows == {(page, binary)}


def test_an_archive_without_shared_storage_still_moves_its_ordinary_bytes(vault):
    """Catches the shared-storage path leaking into an unconverted archive, whose bytes must move with the page."""
    binary, page = _filing(vault)
    target = f"{FOLDER}/Archive/ledger.bin"

    move_file.move_file(vault, old_path=page, new_path=f"{target}.md")

    assert (vault / target).read_bytes() == DATA and not (vault / binary).exists()
    assert _bound(vault, f"{target}.md") == target
