"""A Google Photos Takeout is filed as photos that carry the caption of their sidecars, and no JSON Source, and no sidecar time.

OpenSpec carry-takeout-sidecar-captions, groups 1 and 2.2. A sidecar's time dates no page: it is read into the sidecar's row only. The vault is a temporary one with a real stage, and the
cell's filer drives `adoption_studio file` through the dispatcher, as for any import. The folders, names and sidecars are
invented, in the name forms that community tools report for Takeout. Each test names the failure it catches.
"""

# ruff: noqa: F811 -- the fixtures of the first batch's tests are imported and used by name
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path

import pytest
from test_adoption_series import _window
from test_app_export_import import (  # noqa: F401 -- `_private_journal_folder` is an autouse fixture that the tests need
    _private_journal_folder,
    expansion_rows,
    file_to_done,
    source_pages,
    vault,
)
from test_cloud_import_windows import Rig, rig  # noqa: F401 -- `rig` is a fixture that the window test needs
from test_notes_mail_exports import JPEG

from exomem import (
    adoption_consume,
    client_artifacts,
    export_expansion,
    export_sidecars,
    import_stage,
    lexstore,
)
from exomem import find as find_module
from exomem.vault import parse_frontmatter

ALBUM = "Takeout/Google Photos/Lake trip"
UPLOADED = dt.datetime(2025, 1, 9, 8, 30, tzinfo=dt.UTC)
APPLIED, EMPTY = "IMPORT_SIDECAR_APPLIED", "IMPORT_SIDECAR_EMPTY"
ALREADY, NOT_FILED = "IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED", "IMPORT_SIDECAR_PHOTO_NOT_FILED"
UNPAIRED, UNREADABLE = "IMPORT_SIDECAR_UNPAIRED", "IMPORT_SIDECAR_UNREADABLE"


def taken_at(number: int) -> dt.datetime:
    return dt.datetime(2022, 7, 4, 12, 0, tzinfo=dt.UTC) + dt.timedelta(days=number, minutes=number)


def stamp(moment: dt.datetime) -> dict:
    return {"timestamp": str(int(moment.timestamp())), "formatted": moment.strftime("%b %d, %Y, %I:%M:%S %p UTC")}


def sidecar(title: str, *, caption: str = "", taken: dt.datetime | None = None, created: dt.datetime = UPLOADED) -> bytes:
    """One sidecar as Takeout writes it: a title, an owner's description, the two times, a position and a link.

    `taken` defaults to a time that is not the upload time, which is what a sidecar usually states.
    """
    document = {
        "title": title, "description": caption, "imageViews": "3", "creationTime": stamp(created),
        "photoTakenTime": stamp(taken or taken_at(0)),
        "geoData": {"latitude": 61.5, "longitude": 8.3, "altitude": 0.0, "latitudeSpan": 0.0, "longitudeSpan": 0.0},
        "url": "https://photos.example.invalid/photo/abc123",
        "googlePhotosOrigin": {"mobileUpload": {"deviceType": "ANDROID_PHONE"}},
    }
    return json.dumps(document, indent=2).encode()


def photo(name: str) -> bytes:
    """A JPEG whose bytes are its own: a trailer that holds the name keeps two photos from being duplicates."""
    return JPEG + name.encode()


def stage_takeout(vault: Path, members: dict[str, bytes]) -> str:
    """A stage as the Cloud import Job leaves it, whose series file asks the cell to file it."""
    paths = sorted(members)
    import_id = _window(vault, members, series=paths)
    (vault / "_Imports" / import_id / import_stage.SERIES_FILE).write_text(json.dumps(
        {"label": "Google Photos", "paths": paths, "filing": "cell", "origin": {"browser_family": "cli"}}))
    return import_id


def filed_files(vault: Path, import_id: str) -> dict[str, tuple[dict, str]]:
    """Each Source page of the vault, by the path in the stage of the file it was filed from."""
    prefix = f"_Imports/{import_id}/"
    return {page[0]["imported_from"].removeprefix(prefix): page for page in source_pages(vault).values()}


def caption_of(pages: dict[str, tuple[dict, str]], path: str) -> str | None:
    """The text of the page's description section, or None when the page has none."""
    text = pages[path][1]
    if "\n## Description\n" not in text:
        return None
    return text.split("\n## Description\n", 1)[1].split("\n## ", 1)[0].strip()


def one_photo_albums(vault: Path, members: dict[str, bytes]) -> tuple[str, dict[str, tuple[dict, str]], dict]:
    import_id = stage_takeout(vault, members)
    view = file_to_done(vault, import_id, calls=200)
    return import_id, filed_files(vault, import_id), view


def test_an_album_files_each_photo_with_its_sidecars_caption_and_no_json_source(vault: Path) -> None:
    """Catches a Takeout that files its sidecars as 50 JSON Sources, or files its photos without the caption the owner typed."""
    members: dict[str, bytes] = {}
    for number in range(50):
        name = f"IMG_{number:04d}.jpg"
        members[f"{ALBUM}/{name}"] = photo(name)
        caption = f"day {number} at the lake" if number % 2 == 0 and number < 48 else ""  # a caption on every other photo
        members[f"{ALBUM}/{name}.supplemental-metadata.json"] = sidecar(name, caption=caption, taken=taken_at(number))
    import_id = stage_takeout(vault, members)

    view = file_to_done(vault, import_id, calls=200)

    pages = filed_files(vault, import_id)
    assert len(pages) == 50 and not any(path.endswith(".json") for path in pages)  # no JSON Source
    assert not list((vault / "Knowledge Base" / "Sources").rglob("*.json"))
    for number in range(50):
        expected = f"day {number} at the lake" if number % 2 == 0 and number < 48 else None
        assert caption_of(pages, f"{ALBUM}/IMG_{number:04d}.jpg") == expected
    assert view["state"] == "done"
    assert view["left_out"]["by_reason"] == {APPLIED: 24, EMPTY: 26}
    assert view["left_out"]["sidecars"] == {"captioned": 24}
    assert view["progress"] == {"total": 100, "filed": 50, "skipped_duplicates": 0, "failed": 0, "left_out": 50, "remaining": 0}


def test_a_photo_with_no_exif_and_a_sidecar_with_a_photo_taken_time_files_with_no_capture_time_from_the_sidecar(vault: Path) -> None:
    """Catches a sidecar time written to a page: nothing yet says where Google's `photoTakenTime` comes from for a file that states none."""
    _import_id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_1.jpg": photo("IMG_1.jpg"),
        f"{ALBUM}/IMG_1.jpg.json": sidecar("IMG_1.jpg", caption="Dusk", taken=taken_at(3)),
    })

    assert not any(key.startswith("capture_") for key in pages[f"{ALBUM}/IMG_1.jpg"][0])
    assert caption_of(pages, f"{ALBUM}/IMG_1.jpg") == "Dusk"
    assert view["left_out"]["by_reason"] == {APPLIED: 1}


def test_a_sidecar_whose_name_suffix_is_cut_pairs_with_its_photo(vault: Path) -> None:
    """Catches a pairing that expects the full suffix word: Takeout cuts it, and every such photo would lose its caption."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_100.jpg": photo("IMG_100.jpg"),
        f"{ALBUM}/IMG_100.jpg.supplemental-me.json": sidecar("IMG_100.jpg", caption="caption 100"),
        f"{ALBUM}/IMG_200.jpg": photo("IMG_200.jpg"),
        f"{ALBUM}/IMG_200.jpg.json": sidecar("IMG_200.jpg", caption="caption 200"),
    })

    assert caption_of(pages, f"{ALBUM}/IMG_100.jpg") == "caption 100"
    assert caption_of(pages, f"{ALBUM}/IMG_200.jpg") == "caption 200"
    assert view["left_out"]["by_reason"] == {APPLIED: 2}


def test_a_name_cut_by_takeout_pairs_through_the_title_on_both_sides_of_the_cut(vault: Path) -> None:
    """Catches a pairing that needs the whole name: a long photo and its sidecar are cut at different lengths."""
    full = "birthday_party_with_all_the_cousins_and_the_whole_family_2019.jpg"
    # Takeout keeps 47 characters of a photo's stem and 46 of its sidecar's name, so each is a prefix of the title.
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/birthday_party_with_all_the_cousins_and_the_who.jpg": photo(full),
        f"{ALBUM}/birthday_party_with_all_the_cousins_and_the_wh.json": sidecar(full, caption="the cousins"),
    })

    assert caption_of(pages, f"{ALBUM}/birthday_party_with_all_the_cousins_and_the_who.jpg") == "the cousins"
    assert view["left_out"]["by_reason"] == {APPLIED: 1}


def test_one_cut_sidecar_name_that_fits_two_photos_is_settled_by_the_title(vault: Path) -> None:
    """Catches a cut name that pairs with whichever long name comes first: only the title says which photo it is."""
    cut = "birthday_party_with_all_the_cousins_and_the_wh"
    whole = f"{cut}ole_family_2019.jpg"
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/{cut}o.jpg": photo("whole"),
        f"{ALBUM}/{cut}a.jpg": photo("whale"),
        f"{ALBUM}/{cut}.json": sidecar(whole, caption="the whole family"),
    })

    assert caption_of(pages, f"{ALBUM}/{cut}o.jpg") == "the whole family"
    assert caption_of(pages, f"{ALBUM}/{cut}a.jpg") is None
    assert view["left_out"]["by_reason"] == {APPLIED: 1}


def test_a_title_with_a_replaced_character_pairs_with_the_file_that_takeout_named(vault: Path) -> None:
    """Catches a title compared as typed: Takeout writes `&` and `?` as `_` in the file name, so those photos lose their caption."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/Q_A night.jpg": photo("Q_A night.jpg"),
        f"{ALBUM}/Q_A night.jpg.supplemental-metadata.json": sidecar("Q&A night.jpg", caption="quiz night"),
        f"{ALBUM}/Why_.jpg": photo("Why_.jpg"),
        f"{ALBUM}/Why_.jpg.supplemental-metadata.json": sidecar("Why?.jpg", caption="why not"),
    })

    assert caption_of(pages, f"{ALBUM}/Q_A night.jpg") == "quiz night"
    assert caption_of(pages, f"{ALBUM}/Why_.jpg") == "why not"
    assert view["left_out"]["by_reason"] == {APPLIED: 2}


def test_duplicates_with_the_copy_number_in_either_place_each_take_their_own_sidecars_caption(vault: Path) -> None:
    """Catches a copy that takes the first sidecar's caption: Takeout puts the number before `.json` or in the photo's name."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG.jpg": photo("IMG.jpg"),
        f"{ALBUM}/IMG(1).jpg": photo("IMG(1).jpg"),
        f"{ALBUM}/IMG(2).jpg": photo("IMG(2).jpg"),
        f"{ALBUM}/IMG.jpg.json": sidecar("IMG.jpg", caption="first copy"),
        f"{ALBUM}/IMG.jpg(1).json": sidecar("IMG.jpg", caption="second copy"),
        f"{ALBUM}/IMG(2).jpg.supplemental-metadata.json": sidecar("IMG.jpg", caption="third copy"),
    })

    assert caption_of(pages, f"{ALBUM}/IMG.jpg") == "first copy"
    assert caption_of(pages, f"{ALBUM}/IMG(1).jpg") == "second copy"
    assert caption_of(pages, f"{ALBUM}/IMG(2).jpg") == "third copy"
    assert view["left_out"]["by_reason"] == {APPLIED: 3}


def test_a_numbered_copy_of_a_cut_suffix_pairs_with_the_numbered_photo(vault: Path) -> None:
    """Catches a cut suffix that loses its copy number: `.suppl(1).json` belongs to the photo `IMG(1).jpg`."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_7.jpg": photo("IMG_7.jpg"),
        f"{ALBUM}/IMG_7(1).jpg": photo("IMG_7(1).jpg"),
        f"{ALBUM}/IMG_7.jpg.suppl.json": sidecar("IMG_7.jpg", caption="original"),
        f"{ALBUM}/IMG_7.jpg.suppl(1).json": sidecar("IMG_7.jpg", caption="copy"),
    })

    assert caption_of(pages, f"{ALBUM}/IMG_7.jpg") == "original"
    assert caption_of(pages, f"{ALBUM}/IMG_7(1).jpg") == "copy"
    assert view["left_out"]["by_reason"] == {APPLIED: 2}


def test_a_photo_whose_own_name_ends_in_a_number_pairs_with_its_sidecar(vault: Path) -> None:
    """Catches a name read as a copy number: the owner called the file `Trip(1).jpg`, and its title says so."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/Trip(1).jpg": photo("Trip(1).jpg"),
        f"{ALBUM}/Trip(1).jpg.supplemental-metadata.json": sidecar("Trip(1).jpg", caption="the trip"),
    })

    assert caption_of(pages, f"{ALBUM}/Trip(1).jpg") == "the trip"
    assert view["left_out"]["by_reason"] == {APPLIED: 1}


def test_a_jpeg_title_pairs_with_a_jpg_file_and_a_video_never_takes_a_photos_sidecar(vault: Path) -> None:
    """Catches an extension compared as a string, which drops `.jpeg` against `.jpg`, and a media type that is ignored."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_9.jpg": photo("IMG_9.jpg"),
        f"{ALBUM}/IMG_9.jpg.supplemental-metadata.json": sidecar("IMG_9.jpeg", caption="a still"),
        f"{ALBUM}/CLIP_1.mp4": b"\x00\x00\x00\x18ftypmp42" + b"clip one",
        f"{ALBUM}/CLIP_1.jpg.supplemental-metadata.json": sidecar("CLIP_1.jpg", caption="not a still"),
    })

    assert caption_of(pages, f"{ALBUM}/IMG_9.jpg") == "a still"
    assert caption_of(pages, f"{ALBUM}/CLIP_1.mp4") is None
    assert view["left_out"]["by_reason"] == {APPLIED: 1, UNPAIRED: 1}


def test_a_heic_and_a_jpg_of_one_shot_each_pair_with_their_own_sidecar(vault: Path) -> None:
    """Catches a pairing by image-or-video only: a phone's HEIC and its JPG copy share a stem, so both sidecars went ambiguous."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_60.heic": b"\x00\x00\x00\x18ftypheic" + b"shot 60",
        f"{ALBUM}/IMG_60.jpg": photo("IMG_60.jpg"),
        f"{ALBUM}/IMG_60.heic.supplemental-metadata.json": sidecar("IMG_60.heic", caption="the heic"),
        f"{ALBUM}/IMG_60.jpg.supplemental-metadata.json": sidecar("IMG_60.jpg", caption="the jpg"),
    })

    assert caption_of(pages, f"{ALBUM}/IMG_60.heic") == "the heic"
    assert caption_of(pages, f"{ALBUM}/IMG_60.jpg") == "the jpg"
    assert view["left_out"]["by_reason"] == {APPLIED: 2}


def test_a_raw_file_and_a_jpg_of_one_shot_each_pair_with_their_own_sidecar(vault: Path) -> None:
    """Catches the same ambiguity for a camera's raw file beside its JPG."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/DSC_61.dng": b"II*\x00" + b"raw 61",
        f"{ALBUM}/DSC_61.jpg": photo("DSC_61.jpg"),
        f"{ALBUM}/DSC_61.dng.json": sidecar("DSC_61.dng", caption="the raw file"),
        f"{ALBUM}/DSC_61.jpg.json": sidecar("DSC_61.jpg", caption="the jpg"),
    })

    assert caption_of(pages, f"{ALBUM}/DSC_61.dng") == "the raw file"
    assert caption_of(pages, f"{ALBUM}/DSC_61.jpg") == "the jpg"
    assert view["left_out"]["by_reason"] == {APPLIED: 2}


def test_a_jpg_sidecar_never_pairs_with_a_png_of_the_same_stem(vault: Path) -> None:
    """Catches a title whose extension names another format giving its caption: the PNG would carry a JPG's caption."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/Scan_62.png": b"\x89PNG\r\n\x1a\n" + b"scan 62",
        f"{ALBUM}/Scan_62.jpg.json": sidecar("Scan_62.jpg", caption="a jpg"),
    })

    assert caption_of(pages, f"{ALBUM}/Scan_62.png") is None
    assert view["left_out"]["by_reason"] == {UNPAIRED: 1}


def test_two_sidecars_that_fit_one_photo_leave_both_unpaired_and_the_photo_without_a_caption(vault: Path) -> None:
    """Catches a pairing that picks one: the wrong sidecar would caption a photo and nothing would show it."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_30.jpg": photo("IMG_30.jpg"),
        f"{ALBUM}/IMG_30.jpg.json": sidecar("IMG_30.jpg", caption="one"),
        f"{ALBUM}/IMG_30.jpg.supplemental-metadata.json": sidecar("IMG_30.jpg", caption="two"),
    })

    assert caption_of(pages, f"{ALBUM}/IMG_30.jpg") is None
    assert view["left_out"]["by_reason"] == {UNPAIRED: 2}
    assert {item["path"] for item in view["left_out"]["items"]} == {
        f"{ALBUM}/IMG_30.jpg.json", f"{ALBUM}/IMG_30.jpg.supplemental-metadata.json"}


def test_a_sidecar_without_its_photo_files_nothing_and_is_listed_by_path(vault: Path) -> None:
    """Catches a sidecar of a photo in another archive part that is dropped without a trace or filed as JSON."""
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_40.jpg": photo("IMG_40.jpg"),
        f"{ALBUM}/IMG_40.jpg.json": sidecar("IMG_40.jpg", caption="here"),
        f"{ALBUM}/IMG_41.jpg.json": sidecar("IMG_41.jpg", caption="elsewhere"),
    })

    assert list(pages) == [f"{ALBUM}/IMG_40.jpg"]
    assert view["state"] == "done"
    assert view["left_out"]["by_reason"] == {APPLIED: 1, UNPAIRED: 1}
    assert {"path": f"{ALBUM}/IMG_41.jpg.json", "code": UNPAIRED} in view["left_out"]["items"]


def test_a_photo_without_a_sidecar_files_without_a_caption_and_a_folder_keeps_its_own_pairs(vault: Path) -> None:
    """Catches a name matched across folders: the same file name in two albums must take each album's own caption."""
    _id, pages, view = one_photo_albums(vault, {
        "Takeout/Google Photos/One/IMG_5.jpg": photo("one/IMG_5.jpg"),
        "Takeout/Google Photos/One/IMG_5.jpg.json": sidecar("IMG_5.jpg", caption="album one"),
        "Takeout/Google Photos/Two/IMG_5.jpg": photo("two/IMG_5.jpg"),
        "Takeout/Google Photos/Two/IMG_5.jpg.json": sidecar("IMG_5.jpg", caption="album two"),
        "Takeout/Google Photos/Two/IMG_6.jpg": photo("two/IMG_6.jpg"),
    })

    assert caption_of(pages, "Takeout/Google Photos/One/IMG_5.jpg") == "album one"
    assert caption_of(pages, "Takeout/Google Photos/Two/IMG_5.jpg") == "album two"
    assert caption_of(pages, "Takeout/Google Photos/Two/IMG_6.jpg") is None
    assert view["left_out"]["by_reason"] == {APPLIED: 2}


def test_an_album_metadata_file_and_a_sidecar_over_a_mebibyte_file_as_ordinary_files(vault: Path) -> None:
    """Catches an album's `metadata.json` read as a sidecar, and a huge JSON file read whole into the pass."""
    huge = json.dumps({
        "title": "IMG_50.jpg", "photoTakenTime": stamp(taken_at(40)), "description": "x" * (1024 * 1024 + 1)}).encode()
    album = json.dumps({"title": "Lake trip", "description": "", "access": "", "date": stamp(taken_at(0))}).encode()
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/metadata.json": album,
        f"{ALBUM}/IMG_50.jpg": photo("IMG_50.jpg"),
        f"{ALBUM}/IMG_50.jpg.json": huge,
    })

    assert set(pages) == {f"{ALBUM}/metadata.json", f"{ALBUM}/IMG_50.jpg", f"{ALBUM}/IMG_50.jpg.json"}
    assert caption_of(pages, f"{ALBUM}/IMG_50.jpg") is None
    assert view["left_out"]["by_reason"] == {}


def test_the_same_takeout_imported_again_files_nothing_and_counts_every_sidecar_as_its_photo_already_imported(vault: Path) -> None:
    """Catches a re-import that files the JSON files it skipped before, or one that counts an unchanged photo as captioned again."""
    members = {}
    for number in range(6):
        name = f"IMG_{number}.jpg"
        members[f"{ALBUM}/{name}"] = photo(name)
        members[f"{ALBUM}/{name}.json"] = sidecar(name, caption=f"photo {number}")
    first = stage_takeout(vault, members)
    file_to_done(vault, first, calls=200)
    before = source_pages(vault)

    view = file_to_done(vault, stage_takeout(vault, members), calls=200)

    assert source_pages(vault) == before and len(before) == 6
    assert view["progress"]["skipped_duplicates"] == 6 and view["progress"]["filed"] == 0
    assert view["left_out"]["by_reason"] == {ALREADY: 6}
    assert view["left_out"]["sidecars"] == {"captioned": 0}


def test_the_reply_to_a_second_import_says_the_sidecars_captions_were_not_applied_because_the_vault_held_the_photos(vault: Path) -> None:
    """Catches a reply that reads as if the new sidecars captioned the photos, when the vault already held them and kept its own pages."""
    from exomem import adoption_run

    members = {f"{ALBUM}/IMG_80.jpg": photo("IMG_80.jpg"), f"{ALBUM}/IMG_80.jpg.json": sidecar("IMG_80.jpg", caption="Kept")}
    file_to_done(vault, stage_takeout(vault, members), calls=200)
    stage = f"_Imports/{stage_takeout(vault, members)}"

    reply = adoption_run.file(vault, path=stage)
    for _ in range(200):
        if reply["filing"] != "working":
            break
        reply = adoption_run.file(vault, path=stage, continuation=reply["continuation"])

    residual = reply["filed"]["residual"]
    assert residual["unfiled"]["files"] == 0  # the photo is in the vault, so nothing needs the person
    assert "caption was not applied, because the vault already held its photo" in residual["summary"]


def test_an_album_copy_of_a_photo_already_filed_counts_its_sidecar_as_already_imported_and_not_for_review(vault: Path) -> None:
    """Catches a copy in a second album whose sidecar is reported as a photo that did not file, so the owner reviews a photo that is in the vault."""
    shared = JPEG + b"in two albums"
    import_id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_70.jpg": shared,
        f"{ALBUM}/IMG_70.jpg.json": sidecar("IMG_70.jpg", caption="first album"),
        "Takeout/Google Photos/Photos from 2022/IMG_70.jpg": shared,
        "Takeout/Google Photos/Photos from 2022/IMG_70.jpg.json": sidecar("IMG_70.jpg", caption="second album"),
    })

    assert list(pages) == [f"{ALBUM}/IMG_70.jpg"]
    assert view["progress"]["filed"] == 1 and view["progress"]["skipped_duplicates"] == 1
    assert view["left_out"]["by_reason"] == {APPLIED: 1, ALREADY: 1}
    assert all(adoption_consume.is_filed(code) for code in view["left_out"]["by_reason"])  # none of it is "needs review"


def test_a_sidecar_whose_photo_did_not_file_is_counted_as_such_and_left_for_review(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a photo that failed to file reported as in the vault: its caption would be lost without a trace."""
    monkeypatch.setenv(client_artifacts.ADOPTION_CEILING_ENV, str(len(JPEG) + 5))
    _id, pages, view = one_photo_albums(vault, {
        f"{ALBUM}/small.jpg": JPEG + b"s",
        f"{ALBUM}/small.jpg.json": sidecar("small.jpg", caption="small"),
        f"{ALBUM}/large.jpg": JPEG + b"l" * 100,  # over the ceiling: adoption does not copy it
        f"{ALBUM}/large.jpg.json": sidecar("large.jpg", caption="large"),
    })

    assert f"{ALBUM}/large.jpg" not in pages
    assert view["left_out"]["by_reason"] == {APPLIED: 1, NOT_FILED: 1, "over_ceiling": 1}
    assert not adoption_consume.is_filed(NOT_FILED)


def test_no_photo_files_before_every_sidecar_is_read_when_the_pass_outlasts_a_call(vault: Path, monkeypatch) -> None:
    """Catches photos that file while the sidecars are still being read, and so file without their caption for good."""
    expand = export_expansion.expand
    filed_while_reading: list[int] = []
    calls: list[int] = []

    def one_sidecar_a_call(root, import_id, *, keep_going, rewalk=False):
        """The call's budget is spent after its first file, so every call yields."""
        calls.append(1)
        if not any("sidecars_end" in row for row in expansion_rows(root, import_id)):
            filed_while_reading.append(len(source_pages(root)))
        expand(root, import_id, keep_going=lambda: False, rewalk=rewalk)

    monkeypatch.setattr(export_expansion, "expand", one_sidecar_a_call)
    members = {}
    for number in range(12):
        name = f"IMG_{number:02d}.jpg"
        members[f"{ALBUM}/{name}"] = photo(name)
        members[f"{ALBUM}/{name}.json"] = sidecar(name, caption=f"photo {number}")
    import_id = stage_takeout(vault, members)

    view = file_to_done(vault, import_id, calls=600)

    pages = filed_files(vault, import_id)
    assert len(calls) >= 12 and set(filed_while_reading) == {0}  # a call per sidecar, and no Source until the last was read
    for number in range(12):
        assert caption_of(pages, f"{ALBUM}/IMG_{number:02d}.jpg") == f"photo {number}"
    assert view["left_out"]["by_reason"] == {APPLIED: 12}


def test_an_ordinary_import_with_json_files_opens_each_of_them_once_however_often_it_is_filed_again(vault: Path, monkeypatch) -> None:
    """Catches a stage with no sidecar that is read again at every call: each call would reopen every JSON file, up to a mebibyte each."""
    opened: list[Path] = []
    examine = export_sidecars._examine
    monkeypatch.setattr(export_sidecars, "_examine", lambda path, known: opened.append(path) or examine(path, known))
    import_id = stage_takeout(vault, {f"project/part{number}.json": json.dumps({"name": f"part {number}"}).encode() for number in range(3)})

    for _call in range(3):
        export_expansion.expand(vault, import_id, keep_going=lambda: True)

    assert len(opened) == 3


def test_a_sidecar_that_cannot_be_read_is_listed_alone_while_the_pass_reads_on_and_no_json_files(vault: Path) -> None:
    """Catches a read fault that ends the pass early: every sidecar after it would file as a JSON Source, position data in plain text."""
    members = {}
    for number in range(4):
        name = f"IMG_{number}.jpg"
        members[f"{ALBUM}/{name}"] = photo(name)
        members[f"{ALBUM}/{name}.json"] = sidecar(name, caption=f"Shot {number}")
    import_id = stage_takeout(vault, members)
    unreadable = vault / "_Imports" / import_id / ALBUM / "IMG_1.jpg.json"
    unreadable.unlink()
    os.symlink(vault / "_Imports" / import_id / ALBUM / "IMG_3.jpg.json", unreadable)  # a link, which the pass never follows

    view = file_to_done(vault, import_id, calls=200)

    pages = filed_files(vault, import_id)
    assert {path for path in pages if path.endswith(".jpg")} == {f"{ALBUM}/IMG_{number}.jpg" for number in range(4)}
    assert not {path for path in pages if path.endswith(".json")}  # no sidecar files as a Source, read or not
    assert [caption_of(pages, f"{ALBUM}/IMG_{number}.jpg") for number in range(4)] == ["Shot 0", None, "Shot 2", "Shot 3"]  # read on after the fault
    assert view["state"] == "done"
    assert view["left_out"]["by_reason"] == {APPLIED: 3, UNREADABLE: 1, "IMPORT_EXPORT_PARTIAL": 1}
    assert [row["unreadable"] for row in expansion_rows(vault, import_id) if "unreadable" in row] == [f"{ALBUM}/IMG_1.jpg.json"]
    assert expansion_rows(vault, import_id)[-1]["sidecars_end"] == "partial"


def test_a_caption_files_in_the_description_section_with_a_card_number_removed_and_search_finds_it(vault: Path) -> None:
    """Catches a caption that files whole, card number included, or one that no search can reach."""
    from test_capture_metadata_index import _reset

    import_id, pages, _view = one_photo_albums(vault, {
        f"{ALBUM}/IMG_60.jpg": photo("IMG_60.jpg"),
        f"{ALBUM}/IMG_60.jpg.json": sidecar("IMG_60.jpg", caption="Regatta dinner, paid with card 4111 1111 1111 1111"),
    })

    text = pages[f"{ALBUM}/IMG_60.jpg"][1]
    # The description section alone: the rest of a page holds random ids, digests and times in which "4111" can occur by chance.
    section = text.split("\n## Description\n", 1)[1].split("\n## ", 1)[0]
    assert section.strip() == "Regatta dinner, paid with card [card number removed, ends 1111]"
    # The stage's state folder keeps the sidecar's row until the stage is discarded: no card number may sit in it either.
    captions = [row["caption"] for row in expansion_rows(vault, import_id) if "sidecar" in row]
    assert captions == ["Regatta dinner, paid with card [card number removed, ends 1111]"]
    if lexstore.fts5_available():
        _reset(vault)
        found = {query: [hit.path for hit in find_module.find(vault, query=query, scope="kb-only", mode="keyword", graph=False, result_level="page", limit=10)]
                 for query in ("regatta", "4111")}
        assert found == {"regatta": list(source_pages(vault)), "4111": []}


def test_the_file_reply_names_each_sidecar_by_what_became_of_its_photo_and_none_as_not_selected(vault: Path) -> None:
    """Catches a done reply that says the owner left a sidecar out when filing read it, and asks for a review of one that did its work."""
    from exomem import adoption_run

    import_id = stage_takeout(vault, {
        f"{ALBUM}/IMG_50.jpg": photo("IMG_50.jpg"),
        f"{ALBUM}/IMG_50.jpg.json": sidecar("IMG_50.jpg", caption="Dusk"),
        f"{ALBUM}/IMG_51.jpg": photo("IMG_51.jpg"),
        f"{ALBUM}/IMG_51.jpg.json": sidecar("IMG_51.jpg"),  # no caption: it gives its photo nothing
        f"{ALBUM}/IMG_52.jpg.json": sidecar("IMG_52.jpg", caption="Elsewhere"),
    })
    stage = f"_Imports/{import_id}"
    reply = adoption_run.file(vault, path=stage)
    for _ in range(200):
        if reply["filing"] != "working":
            break
        reply = adoption_run.file(vault, path=stage, continuation=reply["continuation"])

    residual = reply["filed"]["residual"]
    assert reply["filing"] == "done"
    assert {reason: entry["files"] for reason, entry in residual["by_reason"].items()} == {APPLIED: 1, EMPTY: 1, UNPAIRED: 1}
    assert residual["unfiled"]["files"] == 1  # only the sidecar that fits no photo needs the person's look


def _jpeg_file(*, taken: str | None, seed: int) -> bytes:
    """A small real JPEG that states `taken` (an EXIF time) or none, whose pixels differ by `seed`."""
    import io

    from PIL import Image

    exif = Image.Exif()
    if taken is not None:
        exif.get_ifd(0x8769)[0x9003] = taken
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (seed, 90, 160)).save(out, "JPEG", exif=exif)
    return out.getvalue()


def _read_again_by_the_media_worker(vault: Path, import_id: str, monkeypatch) -> dict[str, str]:
    """Run the media worker over every filed photo, as it runs after a filing; return each page's text by the photo's path in the stage."""
    from exomem import extract, media_worker
    from exomem.ocr_models import OcrReading

    monkeypatch.setattr(extract, "_ocr_png", lambda _png: OcrReading("", ()))
    monkeypatch.setattr(extract, "_has_audio_stream", lambda _path: False)
    texts = {}
    for page, (frontmatter, _text) in source_pages(vault).items():
        media_worker.MediaWorker(vault, execution_mode="inline")._process(media_worker._Job(
            binary_path=vault / frontmatter["evidence_file"], sidecar_path=vault / page, media_type="image"))
        texts[frontmatter["imported_from"].removeprefix(f"_Imports/{import_id}/")] = (vault / page).read_text(encoding="utf-8")
    return texts


@pytest.mark.frame_codecs
def test_the_media_worker_keeps_the_caption_and_dates_a_photo_from_its_picture_alone_whatever_its_sidecar_states(vault: Path, monkeypatch) -> None:
    """Catches a worker that reads a sidecar's time into a picture that states none, or drops the caption filing put on the page."""
    members = {
        f"{ALBUM}/received.jpg": _jpeg_file(taken=None, seed=1),
        f"{ALBUM}/camera.jpg": _jpeg_file(taken="2021:03:02 10:00:00", seed=2),
    }
    for name in list(members):
        members[f"{name}.json"] = sidecar(name.rpartition("/")[2], taken=taken_at(70), caption="Sunday regatta")
    import_id, pages, _view = one_photo_albums(vault, members)
    assert not any(key.startswith("capture_") for path in pages for key in pages[path][0])  # filing dates no photo

    texts = _read_again_by_the_media_worker(vault, import_id, monkeypatch)

    assert all("\n## Description\n\nSunday regatta\n" in text for text in texts.values())  # the caption stays where filing put it
    received, camera = (parse_frontmatter(texts[f"{ALBUM}/{name}.jpg"])[0] for name in ("received", "camera"))
    assert not any(key.startswith("capture_") for key in received)  # nothing dates the picture that states no time
    assert (camera["capture_date"], camera["capture_date_source"]) == ("2021-03-02", "camera-exif")


@pytest.mark.frame_codecs
def test_a_filed_photo_with_a_dated_name_and_no_sidecar_takes_its_date_from_the_name(vault: Path, monkeypatch) -> None:
    """Catches a worker that reads the stored name, so a Takeout photo with no sidecar never gets the date its name states."""
    members = {f"{ALBUM}/20200314_101112.jpg": _jpeg_file(taken=None, seed=4)}
    import_id, pages, _view = one_photo_albums(vault, members)
    assert "capture_time" not in pages[f"{ALBUM}/20200314_101112.jpg"][0]

    texts = _read_again_by_the_media_worker(vault, import_id, monkeypatch)

    named = parse_frontmatter(texts[f"{ALBUM}/20200314_101112.jpg"])[0]
    assert (named["capture_time"], named["capture_date_source"]) == ("2020-03-14T10:11:12", "file-name")


@pytest.mark.frame_codecs
def test_the_owner_backfill_dates_a_filed_photo_from_the_name_its_owner_gave_it(vault: Path) -> None:
    """Catches a backfill that reads the filer's stored name, so a filed photo whose name states its time stays undated."""
    from exomem import capture_backfill
    from exomem.governance.principal import library_scope

    members = {f"{ALBUM}/20200314_101112.jpg": _jpeg_file(taken=None, seed=5)}
    _import_id, pages, _view = one_photo_albums(vault, members)
    assert "capture_time" not in pages[f"{ALBUM}/20200314_101112.jpg"][0]

    with library_scope():
        plan = capture_backfill.backfill(vault)
        capture_backfill.backfill(vault, dry_run=False, plan_id=plan["plan_id"])

    named = next(fields for fields, _text in source_pages(vault).values() if fields.get("original_filename"))
    assert (named["capture_time"], named["capture_date_source"]) == ("2020-03-14T10:11:12", "file-name")


@pytest.mark.skipif(not (shutil.which("age") and shutil.which("age-keygen")), reason="real age executables required")
def test_a_takeout_folder_that_spans_two_windows_files_every_photo_with_its_caption(vault: Path, rig: Rig) -> None:
    """Catches a window cut that sends a folder's photos in one window and their sidecars in the last: every caption would be lost."""
    album = rig.source / ALBUM
    album.mkdir(parents=True)
    for number in range(8):
        name = f"IMG_{number:02d}.jpg"
        (album / name).write_bytes(photo(name).ljust(800, b"\0"))  # an artifact that the synthetic cell offloads, as the real cell does a photo
        (album / f"{name}.supplemental-metadata.json").write_bytes(sidecar(name, caption=f"day {number} at the lake"))
    text = sum(path.stat().st_size for path in album.glob("*.json"))
    # The cell must hold all the text at once (2 x its bytes) and has room for about four photos with their sidecars besides.
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 2 * text + 4 * 800 + 4000

    rig.run()

    windows = [rig.cloud.sent(import_id) for import_id in rig.cloud.completed]
    assert len(windows) > 1
    for window in windows:
        file_to_done(vault, stage_takeout(vault, {path: (rig.source / path).read_bytes() for path in window}), calls=200)
    pages = {page[0]["imported_from"].split("/", 2)[2]: page for page in source_pages(vault).values()}  # `_Imports/<window>/<path>`
    assert len(pages) == 8
    for number in range(8):
        assert caption_of(pages, f"{ALBUM}/IMG_{number:02d}.jpg") == f"day {number} at the lake"
