"""The owner's backfill of capture metadata into the sidecars of photos filed before it was recorded.

Every case runs on a disposable vault with photos built in the test.
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest
import test_media_store
from test_capture_metadata import _dng, _extract_into_sidecar
from test_capture_metadata_index import EXTERNAL, _govern, _reset

from exomem import (
    artifact_lifecycle,
    capture_backfill,
    capture_metadata,
    commands,
    media_store,
    original_conversion,
    preserve,
    prior_extraction,
)
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope, request_scope

pytestmark = pytest.mark.frame_codecs

# A configured media store, for the case that offloads an original.
selected_store = test_media_store.selected_store

CAPTURE_LINE = re.compile(r"(?m)^(?:" + "|".join(capture_metadata.FIELDS) + r"):.*\n")
HELSINKI = {1: "N", 2: (60.0, 10.0, 30.0), 3: "E", 4: (24.0, 56.0, 15.0)}


def _jpeg(
    *, taken: str | None = None, gps: dict[int, object] | None = None, size=(64, 48),
    make: bytes | None = None, model: bytes | None = None,
) -> bytes:
    from PIL import Image

    exif = Image.Exif()
    if make is not None:
        exif[0x010F] = make
    if model is not None:
        exif[0x0110] = model
    if taken is not None:
        exif.get_ifd(0x8769)[0x9003] = taken
    for tag, value in (gps or {}).items():
        exif.get_ifd(0x8825)[tag] = value
    out = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(out, "JPEG", exif=exif)
    return out.getvalue()


def _file(
    vault: Path, name: str, data: bytes, *, text: str = "A harbour at dusk.", extension: str = "jpg",
    scope: str = "Photos",
) -> tuple[str, str]:
    """A photo filed and extracted before capture metadata existed: its page has text and no capture fields."""
    with library_scope():
        filed = preserve.preserve_bytes(
            vault, scope=scope, category="image", filename=f"{name}.{extension}", data=data,
            content_type="image/jpeg",
        )
        preserve.update_sidecar_extraction(vault, vault / filed.sidecar_path, text=text, engine="ocr")
    return filed.path, filed.sidecar_path


def _backfill(vault: Path, **arguments) -> dict:
    with library_scope():
        return capture_backfill.backfill(vault, **arguments)


def _run(vault: Path) -> dict:
    return _backfill(vault, dry_run=False, plan_id=_backfill(vault)["plan_id"])


def test_a_backfill_adds_the_capture_block_to_a_filed_photo_and_changes_nothing_else(vault):
    """Catches a backfill that rewrites the extracted text, a preserved note or another field of the page."""
    _phone, phone_page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    _broken, broken_page = _file(vault, "broken", b"\xff\xd8 not a jpeg")
    before = {page: (vault / page).read_text(encoding="utf-8") for page in (phone_page, broken_page)}

    preview = _backfill(vault)

    assert (preview["would_update"], preview["no_capture_metadata"]) == (1, 1)
    assert {page: (vault / page).read_text(encoding="utf-8") for page in before} == before  # a preview writes nothing

    report = _run(vault)

    after = (vault / phone_page).read_text(encoding="utf-8")
    assert (report["updated"], report["skipped_changed"]) == (1, 0)
    assert 'capture_time: "2024-06-01T14:03:22"' in after and "capture_lat: 60.175" in after
    assert CAPTURE_LINE.sub("", after) == before[phone_page]
    assert (vault / broken_page).read_text(encoding="utf-8") == before[broken_page]


def test_a_prior_extraction_page_is_left_as_it_is(vault):
    """Catches a backfill that treats the earlier extraction a reprocess kept as a second sidecar of the photo."""
    _original, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    sidecar = vault / page
    prior, ordinal = prior_extraction.next_page(sidecar)
    prior.write_text(
        prior_extraction.render(
            sidecar.read_text(encoding="utf-8"),
            artifact_name="phone.jpg",
            sidecar_link=prior_extraction.link_to(vault, sidecar),
            ordinal=ordinal,
            date_iso="2024-06-02",
            text="An earlier reading.",
        ),
        encoding="utf-8",
    )
    kept = prior.read_bytes()

    report = _run(vault)

    assert report["updated"] == 1
    assert prior.read_bytes() == kept


def test_a_second_backfill_has_nothing_to_do(vault):
    """Catches a backfill that rewrites pages that already hold what their original states."""
    _original, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    _run(vault)
    settled = (vault / page).read_bytes()

    again = _backfill(vault)
    _run(vault)

    assert (again["would_update"], again["already_current"]) == (0, 1)
    assert (vault / page).read_bytes() == settled


def test_a_page_with_unmarked_preserved_notes_is_skipped_and_counted(vault):
    """Catches a backfill that guesses where machine-owned text ends and overwrites a person's notes."""
    _original, page = _file(vault, "notes", _jpeg(taken="2024:06:01 14:03:22"))
    legacy = (vault / page).read_text(encoding="utf-8") + "\n## Preserved notes\n\nMy own note.\n"
    (vault / page).write_text(legacy, encoding="utf-8")
    _file(vault, "clean", _jpeg(taken="2024:06:02 09:00:00"))

    report = _run(vault)

    assert report["skipped"] == {f"{capture_backfill.AMBIGUOUS_BOUNDARY}: unmarked nonempty preserved notes have no machine-owned boundary": 1}
    assert report["updated"] == 1
    assert (vault / page).read_text(encoding="utf-8") == legacy


def test_an_original_that_is_not_on_the_volume_is_skipped_and_counted(vault):
    """Catches a backfill that fails, or records an empty block, for an original it cannot read."""
    original, page = _file(vault, "gone", _jpeg(taken="2024:06:01 14:03:22"))
    _file(vault, "here", _jpeg(taken="2024:06:02 09:00:00"))
    (vault / original).unlink()
    before = (vault / page).read_text(encoding="utf-8")

    report = _run(vault)

    assert report["skipped"] == {capture_backfill.BYTES_NOT_ON_VOLUME: 1}
    assert report["updated"] == 1
    assert (vault / page).read_text(encoding="utf-8") == before


def test_an_offloaded_original_is_skipped_and_never_fetched_back(vault, selected_store):
    """Catches a backfill that hydrates every offloaded original, which turns a metadata pass into a download."""
    from test_vault_store_media import offloaded

    original = offloaded(vault, "Knowledge Base/Notes/Media/offloaded.jpg")
    assert media_store.evict(vault, original)
    before = (vault / f"{original.relative_to(vault)}.md").read_text(encoding="utf-8")

    report = _run(vault)

    assert report["skipped"] == {capture_backfill.BYTES_NOT_ON_VOLUME: 1}
    assert not original.exists()
    assert media_store.original_state(vault, original).state == "evicted"
    assert (vault / f"{original.relative_to(vault)}.md").read_text(encoding="utf-8") == before


def test_a_photo_held_in_shared_storage_is_read_from_its_shared_object(vault, monkeypatch):
    """Catches a backfill that reads only ordinary files, so the photos of a converted archive keep no capture block."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original, page = _file(vault, "shared", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    with library_scope():
        original_conversion.convert(vault, dry_run=False)
    assert not (vault / original).exists()

    report = _run(vault)

    assert report["updated"] == 1
    assert "capture_lat: 60.175" in (vault / page).read_text(encoding="utf-8")


def test_only_the_owner_may_run_the_backfill(vault):
    """Catches a restricted caller who learns where hidden photos were taken, or has their pages rewritten."""
    _original, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    before = (vault / page).read_bytes()

    with pytest.raises(OpError) as refused:
        capture_backfill.backfill(vault, dry_run=False)

    assert refused.value.code == "MAINTENANCE_OWNER_REQUIRED"
    assert (vault / page).read_bytes() == before


def test_a_hostile_camera_name_keeps_every_sidecar_write_valid_and_a_restricted_read_positionless(
    vault, tmp_path, monkeypatch
):
    """Catches camera text that breaks the sidecar's YAML: the page then reads as no frontmatter at all,
    so a caller who may not see its position is handed the raw lines."""
    backslash = bytes([92])  # `\n` in a regex replacement template reads as a newline: a second write breaks the line
    photo = tmp_path / "hostile.jpg"
    photo.write_bytes(_jpeg(
        taken="2024:06:01 14:03:22", gps=HELSINKI, model=b'\x9b"q"',
        make=b"Acme\xe2\x84\xa2\xe2\x80\xa8" + backslash + b"nCorp\xf0\x9f\x93\xb7\xef\xbf\xbe",
    ))
    stated = {
        "camera_make": "Acme\u2122\u2028" + chr(92) + "nCorp\U0001f4f7\ufffe", "camera_model": '"q"',
        "capture_lat": 60.175, "capture_lon": 24.9375,
    }

    text = _extract_into_sidecar(vault, monkeypatch, photo, category="image", media_type="image")
    page = next(vault.rglob("hostile*.md")).relative_to(vault).as_posix()

    assert {k: v for k, v in vault_module.parse_frontmatter(text, custody=True)[0].items() if k in stated} == stated
    assert _backfill(vault)["already_current"] == 1  # a second write over the same lines must change nothing

    (vault / page).write_text(CAPTURE_LINE.sub("", text), encoding="utf-8")
    _run(vault)  # the backfill's own write

    written = (vault / page).read_text(encoding="utf-8")
    assert {k: v for k, v in vault_module.parse_frontmatter(written, custody=True)[0].items() if k in stated} == stated
    assert _backfill(vault)["already_current"] == 1

    _govern(vault, pages=6, originals=5)
    _reset(vault)
    with request_scope(EXTERNAL):
        shown = commands.op_get(vault, page, include_raw=True)
    assert shown["frontmatter"]["camera_make"] == stated["camera_make"]
    assert not [digits for digits in ("60.175", "24.9375") if digits in json.dumps(shown, default=str)]


def test_a_camera_raw_on_a_host_without_the_raw_decoder_is_skipped_not_counted_as_stating_nothing(vault, monkeypatch):
    """Catches a backfill that reads a missing decoder as a library of photos that state nothing."""
    import importlib.util

    _original, page = _file(vault, "shot", _dng({306: "2023:07:04 09:08:07"}), extension="dng")
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "rawpy" else real(name, *a))

    preview = _backfill(vault)

    assert (preview["would_update"], preview["no_capture_metadata"]) == (0, 0)
    assert preview["skipped"] == {capture_backfill.READER_UNAVAILABLE: 1}


def test_saves_while_the_census_reads_are_read_again_and_a_write_never_overwrites_a_moved_page(vault, monkeypatch):
    """Catches a backfill that fails, or plans a stale or doubled page, whenever the owner saves while it reads.

    A live vault takes saves during every daytime run. A page saved mid-read is read again after the walk: a photo
    is planned once from its new text, an ordinary note is not counted, a removed page is dropped, and only a page
    that changes again during that read is skipped. A page saved between the write's census and its commit keeps
    the save.
    """
    from contextlib import contextmanager

    from exomem import maintenance_jobs, reserved_paths
    from exomem.kbdir import managed_join, managed_root_rel

    _original, photo = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    replaced, removed, restless = (
        managed_join(f"zz-{name}-meanwhile.md") for name in ("replaced", "removed", "restless")
    )
    for page in (replaced, removed, restless):
        (vault / page).write_text("---\ntitle: An ordinary note\n---\nText.\n", encoding="utf-8")
    saved: list[str] = []

    def replace_atomically(page: str) -> None:
        staged = vault / f"{page}.staged"
        staged.write_text("---\ntitle: An ordinary note\n---\nRewritten.\n", encoding="utf-8")
        staged.replace(vault / page)

    def save_after_the_photo_is_read() -> None:
        """What the live service does while the census still holds the photo's page: five ordinary saves."""
        saved.append(photo)
        (vault / managed_join("Notes/added-meanwhile.md")).write_text("---\ntitle: New\n---\nNew.\n", encoding="utf-8")
        replace_atomically(replaced)
        replace_atomically(restless)
        with (vault / photo).open("a", encoding="utf-8") as edited:
            edited.write("\nEdited in place.\n")
        (vault / removed).unlink()

    real_walk, real_open = reserved_paths.open_generic_tree, reserved_paths.open_generic_binary

    @contextmanager
    def census_walk(vault_root, value, **options):
        with real_walk(vault_root, value, **options) as entries:
            def interleaved():
                for entry in entries:
                    yield entry
                    if value == managed_root_rel() and not saved and managed_join(entry.relative_path) == photo:
                        save_after_the_photo_is_read()
            yield interleaved()

    @contextmanager
    def second_read(vault_root, value, **options):
        with real_open(vault_root, value, **options) as source:
            if value == restless and saved and restless not in saved:
                saved.append(restless)
                with (vault / restless).open("a", encoding="utf-8") as edited:
                    edited.write("Saved again.\n")
            yield source

    monkeypatch.setattr(reserved_paths, "open_generic_tree", census_walk)
    monkeypatch.setattr(reserved_paths, "open_generic_binary", second_read)

    preview = _backfill(vault)

    assert saved == [photo, restless]
    assert preview["skipped"] == {capture_backfill.CHANGED_WHILE_READ: 1}
    assert preview["skipped_listed"] == [{"path": restless, "reason": capture_backfill.CHANGED_WHILE_READ}]
    assert preview["would_update"] == 1  # the photo, planned once from the text it has now

    real_commit = maintenance_jobs.commit_pages

    def commit_after_a_save(root, operation, entries, *args, **kwargs):
        with (vault / photo).open("a", encoding="utf-8") as edited:
            edited.write("\nThe owner's own line.\n")
        return real_commit(root, operation, entries, *args, **kwargs)

    monkeypatch.setattr(maintenance_jobs, "commit_pages", commit_after_a_save)

    report = _backfill(vault, dry_run=False, plan_id=preview["plan_id"])

    kept = (vault / photo).read_text(encoding="utf-8")
    assert (report["updated"], report["skipped_changed"]) == (0, 1)
    assert "Edited in place." in kept and "The owner's own line." in kept and "capture_time" not in kept


def test_a_photo_folder_linked_into_the_vault_stops_the_preview_instead_of_leaving_its_photos_out(vault, tmp_path):
    """Catches a preview that silently reads past a linked folder and reports a partial vault as the whole."""
    _original, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    folder = (vault / page).parent
    folder.rename(tmp_path / "elsewhere")
    folder.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(OpError) as refused:
        _backfill(vault)

    assert refused.value.code == "UNSAFE_PATH"
    assert "linked file or folder" in refused.value.message


@pytest.mark.parametrize("which", ["page", "photo"])
def test_a_hard_linked_photo_page_stops_the_preview_instead_of_leaving_it_out(vault, tmp_path, which):
    """Catches a preview that silently drops a file with a second name and reports a partial vault as the whole.

    The walk reads such a file once more after it ends, because a save gives a new page a second name for a moment;
    a page or a photo that keeps the second name still stops the preview.
    """
    _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    photo, page = _file(vault, "second", _jpeg(taken="2024:06:02 09:00:00"))
    os.link(vault / (page if which == "page" else photo), tmp_path / "second-copy")

    with pytest.raises(OpError) as refused:
        _backfill(vault)

    assert refused.value.code == "UNSAFE_PATH"
    assert "linked file or folder" in refused.value.message


@pytest.mark.skipif(os.name == "nt", reason="Windows publishes a new page by rename, so its page never has two names")
def test_a_page_published_while_the_preview_lists_its_folder_is_read_again_instead_of_stopping_it(vault, monkeypatch):
    """Catches a preview that stops when a new page is saved while it lists that page's folder.

    A page publication links the new file to the page's name before it drops the staged name, so for a moment the
    page has two names. The preview finishes and plans the photo.
    """
    from exomem import _held_fs_posix, held_fs

    _photo, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    folder = (vault / page).parent
    published, staged = folder / "published-meanwhile.md", folder / held_fs.publish_temporary_name()
    real_children = _held_fs_posix.PosixHeldFilesystem.children
    saved: list[str] = []

    def children_during_a_publication(self, parent, *, name_filter=None):
        if saved or Path(page).name not in os.listdir(parent.descriptor):
            return real_children(self, parent, name_filter=name_filter)
        saved.append(published.name)
        staged.write_text("---\ntitle: New\n---\nNew.\n", encoding="utf-8")
        os.link(staged, published)
        try:
            return real_children(self, parent, name_filter=name_filter)
        finally:
            staged.unlink()

    monkeypatch.setattr(_held_fs_posix.PosixHeldFilesystem, "children", children_during_a_publication)

    preview = _backfill(vault)

    assert saved == [published.name]
    assert (preview["would_update"], preview["skipped"]) == (1, {})


@pytest.mark.skipif(os.name == "nt", reason="the removal is injected between the POSIX listing's read of names and its stat")
def test_a_page_removed_while_the_preview_lists_its_folder_is_left_out_instead_of_stopping_it(vault, monkeypatch):
    """Catches a preview that stops when a page is removed after its folder's names were read but before its entry was."""
    from exomem import _held_fs_posix

    _photo, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    removed = (vault / page).parent / "removed-meanwhile.md"
    removed.write_text("---\ntitle: An ordinary note\n---\nText.\n", encoding="utf-8")
    real_stat = _held_fs_posix._stat

    def stat_after_a_removal(name, *args, **kwargs):
        if name == removed.name and removed.exists():
            removed.unlink()
        return real_stat(name, *args, **kwargs)

    monkeypatch.setattr(_held_fs_posix, "_stat", stat_after_a_removal)

    preview = _backfill(vault)

    assert not removed.exists()
    assert (preview["would_update"], preview["skipped"]) == (1, {})


@pytest.mark.skipif(os.name == "nt", reason="the move is injected into the POSIX folder open")
@pytest.mark.parametrize("scope", ["Photos", "Trip.md"])
def test_a_folder_moved_while_the_preview_reads_is_listed_as_a_skip_instead_of_dropped(vault, monkeypatch, scope):
    """Catches a preview that leaves out the photos of a folder renamed during the run and reports the rest as whole.

    A folder whose name looks like a page's is still a folder.
    """
    from exomem import _held_fs_posix

    pages = [_file(vault, name, _jpeg(taken="2024:06:01 14:03:22"), scope=scope)[1] for name in ("one", "two", "three")]
    folder = next(parent for parent in (vault / pages[0]).parents if parent.name == scope)
    real_parent = _held_fs_posix.PosixHeldFilesystem.parent
    moved: list[str] = []

    def parent_after_a_rename(self, relative, *args, **kwargs):
        if not moved and (vault / str(relative)) == folder:
            moved.append(str(relative))
            folder.rename(folder.with_name(f"{folder.name}-renamed"))
        return real_parent(self, relative, *args, **kwargs)

    monkeypatch.setattr(_held_fs_posix.PosixHeldFilesystem, "parent", parent_after_a_rename)

    preview = _backfill(vault)

    assert moved
    assert preview["skipped"] == {capture_backfill.FOLDER_MOVED: 1}
    assert preview["skipped_listed"] == [{"path": folder.relative_to(vault).as_posix(), "reason": capture_backfill.FOLDER_MOVED}]
    assert preview["would_update"] == 0
