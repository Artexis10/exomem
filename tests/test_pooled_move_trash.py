"""Move, Trash and recovery of filings whose bytes are one shared, digest-addressed object."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest
import test_media_store

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
# A configured media store, for the cases that evict an original.
selected_store = test_media_store.selected_store


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


LOOSE = "Knowledge Base/Notes/Media/loose.txt"


def _evicted_in_trash(vault: Path, monkeypatch: pytest.MonkeyPatch) -> delete_file.DeleteFileResult:
    """An offloaded original that went to Trash and was evicted there before the archive enabled the pool."""
    from test_vault_store_media import offloaded

    offloaded(vault, LOOSE)
    trashed = delete_file.delete_file(vault, path=LOOSE, confirm=True)
    with media_store.open_original(vault, vault / trashed.trash_path):
        pass  # a read records the cache binding that an eviction at the Trash path needs
    assert media_store.evict(vault, vault / trashed.trash_path)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    return trashed


def test_an_original_evicted_in_trash_before_pooling_recovers_by_its_trash_path_as_an_ordinary_file(
        vault, selected_store, monkeypatch):
    """Catches a recovery that takes the entry's own reference companion for a trashed filing's page and refuses
    the entry the caller picked, or that fetches the entry into the pool and leaves no file at its Trash path to
    move back."""
    from test_vault_store_media import ORIGINAL_BYTES

    trashed = _evicted_in_trash(vault, monkeypatch)

    planned = recover_from_trash.recover_from_trash(vault, trash_path=trashed.trash_path, validate_only=True)
    recovered = recover_from_trash.recover_from_trash(vault, trash_path=trashed.trash_path)

    assert planned.restored_path == recovered.restored_path == LOOSE
    assert (vault / LOOSE).read_bytes() == ORIGINAL_BYTES
    assert originals.original_state(vault, LOOSE).local_path is None
    assert list_trash.list_trash(vault).as_dict() == {"entries": [], "count": 0, "orphan_sidecars": [], "orphan_files": []}
    assert not original_pool.object_path(vault, hashlib.sha256(ORIGINAL_BYTES).hexdigest()).exists()


def test_a_companion_retired_to_trash_is_no_filing_of_the_bytes_another_filing_pools(
        vault, selected_store, monkeypatch):
    """Catches an archive that reads the companion left at a trashed original's path as a live filing once another
    filing pools identical bytes: the trashed path resolves again and serves that filing's object."""
    from test_vault_store_media import ORIGINAL_BYTES

    trashed = _evicted_in_trash(vault, monkeypatch)
    other, _page = _filing(vault, "other.bin", ORIGINAL_BYTES)
    shared = _pool(vault, monkeypatch, other, ORIGINAL_BYTES)

    assert originals.original_state(vault, LOOSE).state == "missing"
    assert originals.digest_addressed(vault, LOOSE) is None
    recovered = recover_from_trash.recover_from_trash(vault, trash_path=trashed.trash_path)

    assert recovered.restored_path == LOOSE and (vault / LOOSE).read_bytes() == ORIGINAL_BYTES
    assert shared.read_bytes() == ORIGINAL_BYTES
    assert media_store.read_original_bytes(vault, other).data == ORIGINAL_BYTES


def _unconfirmed_retirement(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A companion at `LOOSE` whose Trash entry was partly emptied after another filing pooled the same bytes."""
    from test_vault_store_media import ORIGINAL_BYTES

    trashed = _evicted_in_trash(vault, monkeypatch)
    other, _page = _filing(vault, "other.bin", ORIGINAL_BYTES)
    _pool(vault, monkeypatch, other, ORIGINAL_BYTES)
    (vault / f"{trashed.trash_path}.md").unlink()  # A partly emptied Trash keeps the entry's placement metadata.


def test_a_companion_whose_trash_retirement_is_unconfirmed_refuses_reads(vault, selected_store, monkeypatch):
    """Catches a damaged Trash entry that lets the companion at a trashed original's path read as a live filing of
    the bytes another filing pools, or as a path with nothing at it."""
    _unconfirmed_retirement(vault, monkeypatch)

    with pytest.raises(media_store.MediaStoreError) as refused, media_store.open_original(vault, vault / LOOSE):
        pass
    assert refused.value.code == "MEDIA_ORIGINAL_UNVERIFIED"
    with pytest.raises(originals.RetirementUnconfirmed):
        originals.digest_addressed(vault, LOOSE)


def test_a_delete_of_an_original_whose_trash_retirement_is_unconfirmed_names_the_entry_to_fix(
        vault, selected_store, monkeypatch):
    """Catches a delete that answers with a raw internal error, which tells the owner nothing to change."""
    _unconfirmed_retirement(vault, monkeypatch)

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(vault, path=LOOSE, confirm=True)

    assert refused.value.code == "ORIGINAL_RETIREMENT_UNCONFIRMED"
    assert f"{LOOSE}.md" in refused.value.reason and "empty or restore that Trash entry" in refused.value.reason


def test_a_move_of_an_original_whose_trash_retirement_is_unconfirmed_names_the_entry_to_fix(
        vault, selected_store, monkeypatch):
    """Catches a move that answers with a raw internal error, which tells the owner nothing to change."""
    _unconfirmed_retirement(vault, monkeypatch)

    with pytest.raises(move_file.MoveFileError) as refused:
        move_file.move_file(vault, old_path=LOOSE, new_path=f"{FOLDER}/moved.txt")

    assert refused.value.code == "ORIGINAL_RETIREMENT_UNCONFIRMED"
    assert (vault / f"{LOOSE}.md").exists()


def test_a_folder_delete_refuses_a_member_whose_trash_retirement_is_unconfirmed(vault, selected_store, monkeypatch):
    """Catches the binding walk leaving out a member whose original is unknown, so the folder goes to Trash as if
    that member had no original to account for."""
    _unconfirmed_retirement(vault, monkeypatch)
    folder = LOOSE.rsplit("/", 1)[0]

    with pytest.raises(delete_directory.DeleteDirectoryError) as refused:
        delete_directory.delete_directory(vault, path=folder, confirm=True, recursive=True)

    assert refused.value.code == "ORIGINAL_RETIREMENT_UNCONFIRMED"
    assert (vault / f"{LOOSE}.md").exists()


def test_a_delete_beside_an_original_whose_trash_retirement_is_unconfirmed_goes_ahead(
        vault, selected_store, monkeypatch):
    """Catches one damaged Trash entry refusing every delete, move and download of the other files in its folder."""
    _unconfirmed_retirement(vault, monkeypatch)
    sibling, page = _filing(vault, "sibling.bin", folder=LOOSE.rsplit("/", 1)[0])

    trashed = delete_file.delete_file(vault, path=sibling, confirm=True)

    assert not (vault / sibling).exists() and (vault / trashed.trash_path).read_bytes() == DATA
    assert (vault / page).exists() and (vault / f"{LOOSE}.md").exists()


def test_a_folder_whose_page_names_a_path_now_holding_an_unconfirmed_retirement_recovers(
        vault, selected_store, monkeypatch):
    """Catches a recovery that a trashed page's stale binding refuses with a raw error: the page names its old path,
    where a later original's damaged Trash entry leaves a companion of its own, which binds nothing in the folder."""
    from test_vault_store_media import offloaded

    folder = LOOSE.rsplit("/", 1)[0]
    offloaded(vault, LOOSE)
    trashed = delete_directory.delete_directory(vault, path=folder, confirm=True, recursive=True)
    _unconfirmed_retirement(vault, monkeypatch)
    restored = "Knowledge Base/Notes/Restored"

    recovered = recover_from_trash.recover_from_trash(vault, trash_path=trashed.trash_path, restore_path=restored)

    assert recovered.restored_path == restored
    assert (vault / restored / "loose.txt.md").exists() and (vault / f"{LOOSE}.md").exists()


def test_a_download_of_a_page_whose_original_retirement_is_unconfirmed_names_what_to_fix(
        vault, selected_store, monkeypatch):
    """Catches the download route answering a raw server error, which tells the owner nothing to change."""
    from test_upload_endpoint import _client

    from exomem import upload_tokens

    _unconfirmed_retirement(vault, monkeypatch)
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")

    response = client.get("/download", params={"path": f"{LOOSE}.md"},
                          headers={"Authorization": f"Bearer {upload_tokens.mint_bound('sekret', audience='owner')}"})

    assert response.status_code == 409
    assert response.json()["code"] == "ORIGINAL_RETIREMENT_UNCONFIRMED"
    assert "empty or restore that Trash entry" in response.json()["reason"]


def test_a_background_sweep_skips_an_original_whose_trash_retirement_is_unconfirmed_and_goes_on(
        vault, selected_store, monkeypatch, caplog):
    """Catches one damaged Trash entry stopping every background offload and media scan of the archive."""
    import logging

    from exomem import media_processing

    _unconfirmed_retirement(vault, monkeypatch)

    with caplog.at_level(logging.WARNING, logger="exomem.media_store"):
        media_processing.reconcile_all_media(vault)
        swept = [item.path.relative_to(vault.resolve()).as_posix()
                 for item in media_processing.unplaced_originals(vault)]

    assert swept == [f"{FOLDER}/other.bin"]
    assert f"{LOOSE}.md" in caplog.text and "unknown" in caplog.text


@pytest.mark.parametrize("surface", ["export", "stream", "operator"])
def test_an_export_that_meets_an_unconfirmed_retirement_fails_and_names_the_member(
        vault, selected_store, monkeypatch, tmp_path, surface):
    """Catches an export that dies with a raw error carrying a private path, or that reads as complete without the
    original whose state is unknown."""
    import io

    from test_vault_portability import _context

    from exomem import cloud_operator_portability, vault_portability

    _unconfirmed_retirement(vault, monkeypatch)
    artifacts, sink = tmp_path / "artifacts", io.BytesIO()

    with pytest.raises(vault_portability.PortabilityError) as refused:
        if surface == "export":
            vault_portability.export_quiesced_vault(vault, artifacts, context=_context())
        elif surface == "stream":
            vault_portability.stream_quiesced_vault(vault, sink, context=_context())
        else:
            cloud_operator_portability.archive_members(vault)

    assert refused.value.code == "ORIGINAL_RETIREMENT_UNCONFIRMED" and refused.value.member == f"{LOOSE}.md"
    assert LOOSE not in str(refused.value) and refused.value.__cause__ is None
    assert not artifacts.exists() and sink.getvalue() == b""


def test_an_archive_without_shared_storage_still_moves_its_ordinary_bytes(vault):
    """Catches the shared-storage path leaking into an unconverted archive, whose bytes must move with the page."""
    binary, page = _filing(vault)
    target = f"{FOLDER}/Archive/ledger.bin"

    move_file.move_file(vault, old_path=page, new_path=f"{target}.md")

    assert (vault / target).read_bytes() == DATA and not (vault / binary).exists()
    assert _bound(vault, f"{target}.md") == target


def _identical_pooled_evidence(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Two Evidence pages of the same bytes, whose originals are one shared object, as their page paths."""
    from duplicate_fixtures import evidence_filing

    canonical = evidence_filing(vault, "Case", "letters", "notice.png", DATA)
    copy = evidence_filing(vault, "Case", "exhibits", "exhibit-a.png", DATA)
    _pool(vault, monkeypatch, canonical.removesuffix(".md"))
    (vault / copy.removesuffix(".md")).unlink()
    return canonical, copy


def test_a_fold_of_filings_that_share_one_stored_copy_says_so_instead_of_calling_an_original_damaged(vault, monkeypatch):
    """Catches the fold telling the owner an intact shared original no longer hashes to its page."""
    from exomem import curation

    canonical, copy = _identical_pooled_evidence(vault, monkeypatch)
    pinned = {path: {"path": path, "expected_hash": vault_module.content_hash((vault / path).read_text(encoding="utf-8"))}
              for path in (canonical, copy)}
    step = {"step_id": "dedup", "kind": "fold-duplicate",
            "args": {"canonical": pinned[canonical], "copy": pinned[copy], "why": "The same bytes are filed twice."}}

    with library_scope(), pytest.raises(curation.CurationError) as refused:
        curation.propose(vault, {"version": 1, "title": "Fold", "steps": [step]})

    assert refused.value.code == "CURATION_STEP_INVALID"
    assert "already share one stored copy" in refused.value.reason and "keep each filing" in refused.value.reason


def test_a_fold_whose_copy_has_an_unconfirmed_trash_retirement_names_the_entry_to_fix(vault, monkeypatch):
    """Catches the fold answering with a raw internal error when a member's Trash witness no longer confirms it."""
    from exomem import curation, trash_metadata

    canonical, copy = _identical_pooled_evidence(vault, monkeypatch)

    def unconfirmed(_vault, companion, *_args):
        raise originals.RetirementUnconfirmed(companion)

    # The damaged witness itself: building it under Evidence takes a fold, an eviction and a partly emptied Trash.
    monkeypatch.setattr(trash_metadata, "retired_companion", unconfirmed)
    pinned = {path: {"path": path, "expected_hash": vault_module.content_hash((vault / path).read_text(encoding="utf-8"))}
              for path in (canonical, copy)}
    step = {"step_id": "dedup", "kind": "fold-duplicate",
            "args": {"canonical": pinned[canonical], "copy": pinned[copy], "why": "The same bytes are filed twice."}}

    with library_scope(), pytest.raises(curation.CurationError) as refused:
        curation.propose(vault, {"version": 1, "title": "Fold", "steps": [step]})

    assert refused.value.code == "CURATION_STEP_INVALID"
    assert "empty or restore that Trash entry" in refused.value.reason


def test_the_audit_reports_filings_that_share_one_stored_copy_with_no_fold_and_nothing_to_free(vault, monkeypatch):
    """Catches pooled duplicates counted as a stale index, which hides them and points the owner at the index."""
    from exomem import commands

    canonical, copy = _identical_pooled_evidence(vault, monkeypatch)

    with library_scope():
        result = commands.op_audit(vault, categories=["duplicate_content"])

    [finding] = result["findings"]
    assert sorted(finding["paths"]) == sorted([canonical, copy])
    assert finding["meta"]["steps"] == [] and "already share one stored copy" in finding["meta"]["no_fold"]
    assert finding["meta"]["bytes_freed"] == 0
    assert result["metadata"]["duplicates"]["stale_index_groups"] == 0
