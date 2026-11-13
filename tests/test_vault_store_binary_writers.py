"""The lifecycle writers move binaries, folders and scene frames through the store in store mode.

OpenSpec make-vault-store-canonical task 2.5. The vault is the migration suite's generated vault,
moved into store mode by the real migrate route, with the writer bound as the store thread binds it.
``DIAGRAM`` is the binary that the migration recorded. The projector is off unless a test runs its
pass by hand, so a page that a commit stores has no file and every answer comes from the store.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_scene_frames import _FakeImg, _scene
from test_vault_store_bypass_writers import RAW_SOURCE, stored_part
from test_vault_store_importer import StoreThread
from test_vault_store_lifecycle_writers import project_by_hand, race_after_check
from test_vault_store_media import serve_through
from test_vault_store_migration import (  # noqa: F401
    DIAGRAM,
    DIAGRAM_BYTES,
    OPERATOR,
    OPERATOR_SECRET,
    vault,
)
from test_vault_store_write_path import planned, revision, store_rows, tree

from exomem import (
    curation,
    delete_directory,
    delete_file,
    extraction_parts,
    fold_leaf,
    move_file,
    preserve,
    reconcile,
    recover_from_trash,
    reserved_paths,
    scene_frames,
)
from exomem import init as init_module
from exomem import vault as vault_module
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import most_restrictive_principal, request_scope
from exomem.vault_store import batch, importer, ownership, read, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

# The fixtures of the suites that build this vault, re-exported by assignment: a test that takes one
# is then no redefinition of an import.
stored, writer, served = read_write.stored, read_write.writer, write_path.served

LINKER = "Knowledge Base/Notes/Figures.md"
LINKER_TEXT = "---\ntitle: Figures\ntype: note\n---\n# Figures\n\nThe chart is [[diagram.bin]].\n"


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def store_linker(root):
    """A stored page that links to the binary and has no file: only the store's rows name it."""
    vault_module.batch_atomic_write(
        [planned(root, LINKER, LINKER_TEXT, create_only=True)],
        vault_root=root,
        post_commit_fanout=False,
    )


def binary_record(root, rel):
    return store_rows(root, "SELECT sha256, size FROM store_binaries WHERE path = ?", rel)


def held(root):
    return store_rows(root, "SELECT path, kind FROM store_held")


def new_files(before, root):
    after = tree(root)
    return {path.relative_to(root).as_posix() for path in after if before.get(path) != after[path]}


def test_trash_of_a_recorded_binary_moves_its_file_and_its_record_together(served):
    """A binary trash that leaves the record at the old path, unlinks the bytes instead of moving
    them, or orphans a linking page that has no file yet."""
    store_linker(served)
    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(served, path=DIAGRAM, confirm=True)
    assert refused.value.code == "INBOUND_LINKS"
    before = tree(served)

    result = delete_file.delete_file(served, path=DIAGRAM, confirm=True, force_orphan=True)

    assert (served / result.trash_path).read_bytes() == DIAGRAM_BYTES
    assert not (served / DIAGRAM).exists()
    assert binary_record(served, result.trash_path) == [(sha(DIAGRAM_BYTES), len(DIAGRAM_BYTES))]
    assert binary_record(served, DIAGRAM) == []
    assert binary_record(served, result.trash_meta_path), "the sidecar is recorded in the commit"
    # The activity log is a page: it commits to the store and writes no file either.
    assert new_files(before, served) == {result.trash_path, result.trash_meta_path}
    assert result.inbound_link_count == 1


def test_a_trash_whose_commit_refuses_puts_the_binary_back(served, writer, monkeypatch):
    """A refused commit after the rename that leaves the file moved while the store still records
    it at its old path."""
    race_after_check(
        monkeypatch,
        writer,
        {"binaries": (write.BinaryRecord(DIAGRAM, sha(b"raced"), 5),)},
    )
    before = tree(served)
    start = revision(served)

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(served, path=DIAGRAM, confirm=True)

    assert refused.value.code == "STALE_DELETE"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert tree(served) == before, "the file is back and the sidecar is rolled back"


def test_a_fold_trash_of_a_binary_stops_when_its_verify_refuses(served):
    """A fold that moves a binary to the trash without proving again, right before the move, that
    a retained copy still holds the same bytes."""
    now = dt.datetime(2026, 11, 11, 3, 14, 37)
    preview = delete_file.delete_file(
        served, path=DIAGRAM, confirm=True, validate_only=True, now=now
    )

    def verify():
        raise fold_leaf.refuse("CURATION_BINDING_STALE", "the retained copy changed")

    before = tree(served)

    with pytest.raises(curation.CurationError) as refused:
        delete_file.delete_file(
            served,
            path=DIAGRAM,
            confirm=True,
            now=now,
            expected_hash=preview.content_hash,
            _fold=fold_leaf.Placement(preview.result.trash_path, verify),
        )

    assert refused.value.code == "CURATION_BINDING_STALE"
    assert tree(served) == before
    assert binary_record(served, DIAGRAM) == [(sha(DIAGRAM_BYTES), len(DIAGRAM_BYTES))]


@pytest.mark.parametrize("store", [False, True], ids=["file mode", "store mode"])
def test_a_previewed_binary_trash_refuses_bytes_that_changed_after_the_check(request, store):
    """A store-mode binary trash that binds the store record, which a rewrite on disk leaves as it
    was, and so moves to the trash bytes that the preview never hashed."""
    root = request.getfixturevalue("served" if store else "vault")
    now = dt.datetime(2026, 11, 11, 3, 14, 37)
    preview = delete_file.delete_file(root, path=DIAGRAM, confirm=True, validate_only=True, now=now)

    def rewrite():
        # The file changes after the check, right before the move.
        (root / DIAGRAM).write_bytes(b"rewritten")

    before = tree(root)

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(
            root,
            path=DIAGRAM,
            confirm=True,
            now=now,
            expected_hash=preview.content_hash,
            _fold=fold_leaf.Placement(preview.result.trash_path, rewrite),
        )

    assert refused.value.code == "STALE_DELETE"
    assert new_files(before, root) == {DIAGRAM}, "only the rewrite changed a file; nothing moved"
    assert (root / DIAGRAM).read_bytes() == b"rewritten"


JUDGED = "Knowledge Base/Notes/Judged.md"


def test_a_page_trash_refuses_a_page_that_changed_after_it_was_judged(
    served, writer, monkeypatch
):
    """A page trash that binds the page as a later read finds it, so a page that gained
    `superseded_by:` after its check goes to the trash anyway."""
    judged = b"---\ntitle: Judged\ntype: note\n---\n# Judged\n"
    superseded = judged.replace(b"type: note\n", b"type: note\nsuperseded_by: '[[Next]]'\n")
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "page", puts=(write.PagePut(JUDGED, judged),)),
    )
    describe = delete_file._trash_metadata_bytes

    def change_then_describe(*arguments, **options):
        # A competing commit between the check and the trash.
        write.commit(
            writer,
            write.StoreChange(
                write.Origin.GOVERNED, "race", puts=(write.PagePut(JUDGED, superseded),)
            ),
        )
        return describe(*arguments, **options)

    monkeypatch.setattr(delete_file, "_trash_metadata_bytes", change_then_describe)

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(served, path=JUDGED, confirm=True)

    assert refused.value.code == "STALE_DELETE"
    assert read.owned_bytes(served, JUDGED) == superseded


def test_a_trash_that_cannot_put_the_binary_back_says_so_and_one_sweep_records_it(
    served, writer
):
    """A refused commit whose rename back also fails, leaving a record that names a path with no
    file, or a held item, after the next sweep; or a refusal that does not say the file moved."""
    original_move = reserved_paths.move_generic_path

    def refuse(*arguments, **options):
        raise write.StoreConflict(DIAGRAM, "a competing writer changed it")

    def move_forward_only(*arguments, rollback=False, **options):
        if rollback:
            raise reserved_paths.ReservedPathLeafError("IO_REFUSED")
        return original_move(*arguments, rollback=rollback, **options)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(batch, "commit_batch", refuse)
        patch.setattr(reserved_paths, "move_generic_path", move_forward_only)
        with pytest.raises(delete_file.DeleteFileError) as refused:
            delete_file.delete_file(served, path=DIAGRAM, confirm=True)

    assert refused.value.code == "DELETE_FAILED"
    assert "the file moved, and the next sweep records it" in refused.value.reason
    moved_to = [
        path.relative_to(served).as_posix()
        for path in (served / "Knowledge Base/_trash").rglob("*diagram.bin")
    ]
    assert len(moved_to) == 1
    assert binary_record(served, DIAGRAM), "until the sweep, the store still names the old path"

    importer.Importer(served, run=lambda work: work(writer)).sweep()

    assert binary_record(served, DIAGRAM) == []
    assert binary_record(served, moved_to[0]) == [(sha(DIAGRAM_BYTES), len(DIAGRAM_BYTES))]
    assert held(served) == []


REPORT = "Knowledge Base/Notes/report.bin"
REPORT_BYTES = b"the quarterly tide report"
REPORT_PAGE = f"{REPORT}.md"
MOVED_REPORT = "Knowledge Base/Archive/report.bin"


def store_report(root, writer):
    """A paired artifact: its bytes on disk with a record, and a stored page that names them."""
    (root / REPORT).write_bytes(REPORT_BYTES)
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "artifact",
            binaries=(write.BinaryRecord(REPORT, sha(REPORT_BYTES), len(REPORT_BYTES)),),
            puts=(
                write.PagePut(
                    REPORT_PAGE,
                    f"---\ntitle: Report\ntype: note\nevidence_file: {REPORT}\n---\n# Report\n"
                    .encode(),
                ),
            ),
        ),
    )


def test_a_paired_artifact_moves_its_bytes_and_its_page_in_one_commit(served, writer):
    """A move that leaves the page naming bytes that are elsewhere, or a record at the old path."""
    store_report(served, writer)

    result = move_file.move_file(served, old_path=REPORT, new_path=MOVED_REPORT)

    assert result.new_path == f"{MOVED_REPORT}.md"
    assert (served / MOVED_REPORT).read_bytes() == REPORT_BYTES
    assert not (served / REPORT).exists()
    assert binary_record(served, MOVED_REPORT) == [(sha(REPORT_BYTES), len(REPORT_BYTES))]
    assert binary_record(served, REPORT) == []
    assert f"evidence_file: {MOVED_REPORT}".encode() in read.owned_bytes(served, result.new_path)
    assert store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", REPORT_PAGE) == []
    (moved_at,) = store_rows(
        served,
        "SELECT revision FROM store_audit WHERE path = ? AND operation = 'move'",
        result.new_path,
    )[0]
    trail = store_rows(
        served, "SELECT path, operation FROM store_audit WHERE revision = ?", moved_at
    )
    assert sorted(trail) == sorted(
        [
            (REPORT_PAGE, "move"),
            (result.new_path, "move"),
            (REPORT, "delete"),
            (MOVED_REPORT, "write"),
            # The move's log entry commits in the same transaction.
            ("Knowledge Base/log.md", "write"),
        ]
    )


FOLDER = "Knowledge Base/Project"
PLAN = f"{FOLDER}/Plan.md"
PLAN_TEXT = "---\ntitle: Plan\ntype: note\n---\n# Plan\n\nThe harbour works plan.\n"
CHART = f"{FOLDER}/chart.bin"
SKETCH = f"{FOLDER}/drafts/sketch.bin"


def build_folder(root, writer):
    """A folder of one stored page with no file, a recorded binary, a binary that nothing imported
    and a dot folder holding one part of the page's extraction."""
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "plan", puts=(write.PagePut(PLAN, PLAN_TEXT.encode()),)),
    )
    (root / FOLDER).mkdir(exist_ok=True)
    (root / CHART).write_bytes(b"chart")
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "chart", binaries=(write.BinaryRecord(CHART, sha(b"chart"), 5),)
        ),
    )
    (root / SKETCH).parent.mkdir(parents=True)
    (root / SKETCH).write_bytes(b"sketch")
    part = root / extraction_parts.part_path(Path(PLAN), 2)
    part.parent.mkdir(parents=True)
    part.write_text("the second part\n", encoding="utf-8")
    return extraction_parts.part_path(Path(PLAN), 2).as_posix()


def test_a_folder_trash_refuses_a_note_nothing_imported_and_a_page_added_after_its_read(
    served, writer, monkeypatch
):
    """A folder trash that leaves an unimported note behind, or trashes a page nobody read."""
    build_folder(served, writer)
    loose = served / FOLDER / "Loose.md"
    loose.write_text("# Loose\n", encoding="utf-8")
    before = tree(served)
    start = revision(served)

    with pytest.raises(Exception) as refused:
        delete_directory.delete_directory(served, path=FOLDER, confirm=True, recursive=True)
    assert getattr(refused.value, "code", None) == "VAULT_STORE_WRITER_UNROUTED"
    assert "Loose" not in str(refused.value)
    loose.unlink()
    del before[loose]
    race_after_check(
        monkeypatch, writer, {"puts": (write.PagePut(f"{FOLDER}/Late.md", b"# Late\n"),)}
    )

    with pytest.raises(delete_directory.DeleteDirectoryError) as raced:
        delete_directory.delete_directory(served, path=FOLDER, confirm=True, recursive=True)

    assert raced.value.code == "PATH_GUARD_CHANGED"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert tree(served) == before, "every rename is undone and the sidecar rolled back"


def test_a_folder_trash_moves_pages_in_the_store_and_files_on_disk(served, writer):
    """A folder trash that leaves an entry behind or a record at its old path."""
    part = build_folder(served, writer)

    result = delete_directory.delete_directory(served, path=FOLDER, confirm=True, recursive=True)

    trashed = result.trash_path
    assert read.owned_bytes(served, f"{trashed}/Plan.md") == PLAN_TEXT.encode()
    assert store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", PLAN) == []
    for rel, body in ((CHART, b"chart"), (SKETCH, b"sketch")):
        moved_to = f"{trashed}/{rel[len(FOLDER) + 1:]}"
        assert (served / moved_to).read_bytes() == body
        assert binary_record(served, moved_to) == [(sha(body), len(body))]
        assert binary_record(served, rel) == []
    assert (served / trashed / part[len(FOLDER) + 1:]).read_text(encoding="utf-8") == (
        "the second part\n"
    )
    assert not (served / FOLDER).exists(), "nothing of the folder is left on disk"
    assert result.file_count == 4

    recovered = recover_from_trash.recover_from_trash(served, trash_path=trashed)

    assert recovered.restored_path == FOLDER
    assert read.owned_bytes(served, PLAN) == PLAN_TEXT.encode()
    assert (served / SKETCH).read_bytes() == b"sketch"
    assert binary_record(served, CHART) == [(sha(b"chart"), 5)]
    assert (served / part).read_text(encoding="utf-8") == "the second part\n"
    assert not (served / trashed).exists()


def test_a_folder_trash_names_a_recorded_binary_whose_file_is_gone_and_moves_after_a_sweep(
    served, writer
):
    """A folder trash that refuses for a record whose file is gone with a reason that names
    neither the file nor what resolves it."""
    build_folder(served, writer)
    (served / CHART).unlink()
    before = tree(served)

    with pytest.raises(delete_directory.DeleteDirectoryError) as refused:
        delete_directory.delete_directory(served, path=FOLDER, confirm=True, recursive=True)

    assert refused.value.code == "DELETE_FAILED"
    assert CHART in refused.value.reason and "next sweep" in refused.value.reason
    assert tree(served) == before
    importer.Importer(served, run=lambda work: work(writer)).sweep()

    result = delete_directory.delete_directory(served, path=FOLDER, confirm=True, recursive=True)

    assert (served / result.trash_path / SKETCH[len(FOLDER) + 1:]).read_bytes() == b"sketch"
    assert binary_record(served, CHART) == []


def test_a_restricted_writers_folder_trash_names_no_file_that_nothing_imported(served, writer):
    """A folder trash, or its refusal, that tells a restricted caller the path of a file that no
    policy covers yet."""
    build_folder(served, writer)
    (served / "Knowledge Base/Notes/Mine.md").write_text(
        "---\ntitle: Mine\ntype: note\n---\n\nSee [[Plan]].\n", encoding="utf-8"
    )
    loose = served / FOLDER / "Loose.md"
    loose.write_text("# Loose\n", encoding="utf-8")

    with request_scope(most_restrictive_principal(surface="rest")):
        with pytest.raises(Exception) as refused:
            delete_directory.delete_directory(served, path=FOLDER, confirm=True, recursive=True)
        loose.unlink()
        result = delete_directory.delete_directory(
            served, path=FOLDER, confirm=True, recursive=True
        )

    assert "Loose" not in str(refused.value)
    assert result.inbound_link_count == 0
    assert "Mine" not in json.dumps(result.as_dict())
    assert "Mine" not in (served / result.trash_meta_path).read_text(encoding="utf-8")


def test_a_folder_recovery_refuses_a_page_added_below_its_destination_after_its_read(
    served, writer, monkeypatch
):
    """The prefix race: a recovery that lands a folder over a page that a commit put there."""
    build_folder(served, writer)
    trashed = delete_directory.delete_directory(
        served, path=FOLDER, confirm=True, recursive=True
    ).trash_path
    before = tree(served)
    start = revision(served)
    race_after_check(
        monkeypatch, writer, {"puts": (write.PagePut(f"{FOLDER}/Late.md", b"# Late\n"),)}
    )

    with pytest.raises(recover_from_trash.RecoverError) as refused:
        recover_from_trash.recover_from_trash(served, trash_path=trashed)

    assert refused.value.code == "DEST_EXISTS"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert tree(served) == before


def test_an_empty_folder_goes_to_the_trash_and_back(served):
    """A refusal of a folder the store holds no page in, or a sidecar left behind."""
    (served / FOLDER).mkdir()

    result = delete_directory.delete_directory(served, path=FOLDER, confirm=True)

    assert not (served / FOLDER).exists()
    assert (served / result.trash_path).is_dir()
    assert binary_record(served, result.trash_meta_path)

    recovered = recover_from_trash.recover_from_trash(served, trash_path=result.trash_path)

    assert recovered.restored_path == FOLDER
    assert (served / FOLDER).is_dir()
    assert not (served / result.trash_path).exists()
    assert binary_record(served, result.trash_meta_path) == []


@pytest.mark.parametrize("store", [False, True], ids=["file mode", "store mode"])
def test_a_binary_and_a_folder_trash_and_come_back_with_the_same_answers_in_both_modes(
    request, store
):
    """A store form whose codes or results drift from file mode's for the same calls, or that
    drops an empty folder of the trashed folder."""
    root = request.getfixturevalue("served" if store else "vault")
    (root / FOLDER / "empty" / "nested").mkdir(parents=True)
    (root / CHART).write_bytes(b"chart")
    (root / PLAN).write_text(PLAN_TEXT, encoding="utf-8")
    if store:
        importer.Importer(
            root, run=lambda work: work(request.getfixturevalue("writer"))
        ).import_paths([PLAN, CHART])

    with pytest.raises(delete_directory.DeleteDirectoryError) as refused:
        delete_directory.delete_directory(root, path=FOLDER, confirm=True)
    trashed = delete_file.delete_file(root, path=DIAGRAM, confirm=True)
    folder = delete_directory.delete_directory(root, path=FOLDER, confirm=True, recursive=True)
    kept_in_trash = (root / folder.trash_path / "empty" / "nested").is_dir()
    restored = recover_from_trash.recover_from_trash(root, trash_path=trashed.trash_path)
    back = recover_from_trash.recover_from_trash(root, trash_path=folder.trash_path)

    assert refused.value.code == "NOT_EMPTY"
    assert (trashed.inbound_link_count, folder.file_count) == (0, 2)
    assert (restored.restored_path, restored.kind) == (DIAGRAM, "file")
    assert (back.restored_path, back.kind) == (FOLDER, "directory")
    assert (root / DIAGRAM).read_bytes() == DIAGRAM_BYTES
    assert (root / CHART).read_bytes() == b"chart"
    assert kept_in_trash and (root / FOLDER / "empty" / "nested").is_dir()


VIDEO = "Knowledge Base/Media/harbour.mp4"
FRAMES = f"{VIDEO}.frames"


def frame_rows(root):
    after = f"{FRAMES}0"  # "0" sorts right after "/": the end of the folder's path range
    return (
        store_rows(root, "SELECT path FROM store_pages WHERE path > ? AND path < ?", FRAMES, after),
        store_rows(
            root, "SELECT path FROM store_binaries WHERE path > ? AND path < ?", FRAMES, after
        ),
    )


def store_frames(root, run):
    """Two frames as a store-mode vault holds them: each JPEG on disk with its record, and each
    sidecar a stored page with no file."""
    puts, records = [], []
    for index in range(2):
        jpg = f"{FRAMES}/{scene_frames.frame_filename(index, index * 5.0)}"
        body = b"\xff\xd8frame %d" % index
        (root / jpg).parent.mkdir(parents=True, exist_ok=True)
        (root / jpg).write_bytes(body)
        records.append(write.BinaryRecord(jpg, sha(body), len(body)))
        puts.append(write.PagePut(f"{jpg}.md", b"# Frame\n"))
    change = write.StoreChange(
        write.Origin.GOVERNED, "frames", puts=tuple(puts), binaries=tuple(records)
    )
    run(lambda writing: write.commit(writing, change))
    return [record.rel for record in records]


@pytest.fixture
def store_thread(stored, monkeypatch):
    thread = StoreThread(stored)
    serve_through(monkeypatch, thread)
    yield thread
    thread.close()


def test_clearing_stored_frames_off_the_store_thread_removes_pages_records_and_files(
    stored, store_thread
):
    """A clear that leaves the stored sidecar pages behind, or records of JPEGs that are gone."""
    assert scene_frames.clear_scene_frames(stored, stored / VIDEO) == 0, "no frames: nothing"
    jpgs = store_frames(stored, store_thread.run)

    removed = scene_frames.clear_scene_frames(stored, stored / VIDEO)

    assert removed == 4
    assert frame_rows(stored) == ([], [])
    assert not any((stored / jpg).exists() for jpg in jpgs)


def test_a_clear_killed_before_its_commit_heals_after_one_sweep_and_one_reconcile(
    served, writer
):
    """A crash between the unlinks and the commit that leaves orphan sidecar pages for good."""
    store_frames(served, lambda work: work(writer))

    def killed(*_arguments, **_options):
        raise RuntimeError("killed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(batch, "commit_batch", killed)
        with pytest.raises(RuntimeError):
            scene_frames.clear_scene_frames(served, served / VIDEO)
    importer.Importer(served, run=lambda work: work(writer)).sweep()
    assert frame_rows(served)[1] == [], "the sweep drops the records of the gone JPEGs"
    project_by_hand(served)

    report = reconcile.reconcile(served)

    assert frame_rows(served) == ([], [])
    assert report.frame_orphans_removed == 1
    assert report.frame_orphans_kept == []


def test_writing_frames_off_the_store_thread_commits_them_there(stored, store_thread):
    """The refusal that a media worker's frame write met in a store-mode vault. The worker holds
    no guard across the hop: the store thread takes the boundary for the frames' commit."""
    video = stored / VIDEO
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")

    pairs = scene_frames.write_scene_frames(stored, video, [(_scene(10.0), _FakeImg())])

    assert len(pairs) == 1
    jpg, sidecar = (path.relative_to(stored).as_posix() for path in pairs[0])
    assert binary_record(stored, jpg) == [(sha(b"\xff\xd8fakejpg"), len(b"\xff\xd8fakejpg"))]
    assert b"scene-frame" in read.owned_bytes(stored, sidecar)


def test_the_part_sweep_finds_a_stored_surplus_part_that_has_no_file(served):
    """A sweep that walks the disk and misses a stored part page the projector has not written."""
    vault_module.batch_atomic_write(
        [
            planned(
                served,
                RAW_SOURCE,
                "---\ntitle: Minutes\ntype: source\nextraction_parts: 2\n---\n# Minutes\n\n"
                f"{preserve._EXTRACTED_HEADING}\n\nFirst part.\n",
                create_only=True,
            )
        ],
        vault_root=served,
        post_commit_fanout=False,
    )
    part = stored_part(served, 3)
    assert not (served / part).exists()

    swept = extraction_parts.sweep(served, dry_run=False)

    assert (swept.removed, swept.unowned) == ([part], [])
    assert not read.page_exists(served, part)


def test_an_offline_overlay_and_schema_reclaim_commit_to_the_store_and_write_no_markdown(stored):
    """An overlay that writes a page's projection, or a reclaim that previews what it does not
    remove or unlinks a file that the store still holds."""
    source = next(iter(init_module.shipped_schema_sources()))
    relative = source.relative_to(init_module._SCAFFOLD).as_posix().split("/", 1)[1]
    legacy = f"Knowledge Base/_Schema/{relative}"
    deployed = init_module.shipped_schema_target(stored) / relative
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(source.read_bytes())
    # The legacy copy is a stored page whose file the projector already wrote.
    (stored / legacy).parent.mkdir(parents=True, exist_ok=True)
    (stored / legacy).write_bytes(source.read_bytes())
    with connection.open_writer(connection.store_path(stored), lease_check=lambda: True) as handle:
        write.commit(
            CollectionWriter(stored, handle),
            write.StoreChange(
                write.Origin.GOVERNED,
                "setup",
                puts=(
                    write.PagePut(legacy, source.read_bytes()),
                    write.PagePut("Knowledge Base/index.md", b"# My own index\n"),
                ),
            ),
        )
    before = tree(stored)

    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, OPERATOR_SECRET)
        overlay = init_module.init_vault(stored, force=True)
        preview = init_module.reclaim_legacy_shipped_schema(stored)
        applied = init_module.reclaim_legacy_shipped_schema(stored, apply=True)

    scaffold_index = (init_module._SCAFFOLD / "index.md").read_bytes()
    assert read.owned_bytes(stored, "Knowledge Base/index.md") == scaffold_index
    assert "Knowledge Base/index.md" in overlay["created"]
    assert relative in preview["removed"]
    assert applied["removed"] == preview["removed"]
    assert store_rows(stored, "SELECT 1 FROM store_pages WHERE path = ?", legacy) == []
    written_pages = {
        rel
        for rel in new_files(before, stored)
        if ownership.classify(rel) is ownership.Ownership.PAGE
    }
    assert written_pages == set(), "the projector, not the CLIs, writes the pages' files"
    assert (stored / legacy).read_bytes() == source.read_bytes(), "the projector removes it"


def test_an_offline_overlay_writes_nothing_before_admission_and_commits_a_missing_marker(stored):
    """An overlay that overwrites a file that a running service reads before the owner's route
    refuses it, or that writes the migration marker or the activation manifest through the
    service's path and refuses with a code the operator cannot act on."""
    deployed = init_module.shipped_schema_target(stored) / "SKILL.md"
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(b"# The deployed skill, as the service reads it\n")
    before = tree(stored)

    with pytest.raises(Exception) as refused:
        init_module.init_vault(stored, force=True)

    assert getattr(refused.value, "code", None) == "SCHEMA_FENCE_OPERATOR_UNAVAILABLE"
    assert tree(stored) == before
    marker = "Knowledge Base/_Schema/workflow-contract-migration.yaml"
    manifest = "Knowledge Base/_Schema/semantic-activation.yaml"
    # A vault that lacks both, as the projector leaves it once it removes their files.
    with connection.open_writer(connection.store_path(stored), lease_check=lambda: True) as handle:
        write.commit(
            CollectionWriter(stored, handle),
            write.StoreChange(
                write.Origin.GOVERNED,
                "setup",
                deletes=(write.PageDelete(marker), write.PageDelete(manifest)),
            ),
        )
    for rel in (marker, manifest):
        (stored / rel).unlink(missing_ok=True)

    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, OPERATOR_SECRET)
        overlay = init_module.init_vault(stored, force=True)

    assert read.owned_bytes(stored, marker) == b"schema_version: 1\nreview_required: true\n"
    assert b"schema_version" in read.owned_bytes(stored, manifest)
    assert manifest in overlay["created"]
