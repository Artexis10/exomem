"""Recovering the capture date of photos that arrived without it, end to end on a disposable vault.

The owner previews a recovery, writes it with the preview's plan_id, and a date search then finds
each photo with the source that dated it. Every image is built in the test.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from test_capture_backfill import _backfill, _file
from test_capture_metadata_index import _reset

from exomem import capture_dates, capture_match, lexstore, maintenance_jobs
from exomem import find as find_module
from exomem.cli_ops import OpError
from exomem.vault import parse_frontmatter

pytestmark = [
    pytest.mark.frame_codecs,
    pytest.mark.skipif(not lexstore.fts5_available(), reason="SQLite build lacks FTS5"),
]


def _scene(seed: int, *, patch: bool = False):
    """A picture with enough structure to fingerprint: smooth random blobs, optionally with a small edit."""
    import numpy as np
    from PIL import Image

    cells = np.random.default_rng(seed).integers(0, 256, (9, 12, 3), dtype=np.uint8)
    scene = Image.fromarray(cells).resize((640, 480), Image.Resampling.BICUBIC)
    if patch:
        scene.paste((255, 255, 255), (300, 200, 360, 260))
    return scene


def _page(seed: int, *, jitter: int = 0):
    """A photographed printed page: rows of word-sized dark bars on paper. Two different pages look alike to a coarse hash."""
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    image = Image.new("L", (640, 480), 235)
    draw = ImageDraw.Draw(image)
    y = 40 + rnd.randint(-jitter, jitter)
    while y < 440:
        x = 50 + rnd.randint(-jitter, jitter)
        while x < 550:
            width = rnd.randint(18, 70)
            draw.rectangle((x, y, min(x + width, 590), y + 12), fill=45)
            x += width + 10
        y += 24
    return image.convert("RGB")


def _jpeg(image, *, taken: str | None = None, size: tuple[int, int] | None = None, quality: int = 90) -> bytes:
    from PIL import Image

    exif = Image.Exif()
    if taken is not None:
        exif.get_ifd(0x8769)[0x9003] = taken
    if size is not None:
        image = image.resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif, quality=quality)
    return out.getvalue()


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _fields(vault: Path, page: str) -> dict:
    return parse_frontmatter((vault / page).read_text(encoding="utf-8"))[0]


def _snapshot(folder: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(folder).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in sorted(folder.rglob("*")) if path.is_file()
    }


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """A folder of originals that the owner keeps outside the vault: one phone photo, in a subfolder."""
    folder = tmp_path / "phone-archive"
    (folder / "2020").mkdir(parents=True)
    (folder / "2020" / "IMG_0001.jpg").write_bytes(_jpeg(_scene(3), taken="2020:02:03 04:05:06"))
    return folder


@pytest.fixture
def library(vault: Path) -> dict[str, tuple[str, str]]:
    """The vault's photos by name, as (original path, sidecar path), filed without any capture block."""
    photos = {
        "original": _jpeg(_scene(1), taken="2021:05:06 07:08:09"),
        "copy-of-original": _jpeg(_scene(1), size=(320, 240), quality=70),
        "burst-a": _jpeg(_scene(2), taken="2022:01:01 10:00:00"),
        "burst-b": _jpeg(_scene(2, patch=True), taken="2022:01:01 10:00:07"),
        "burst-copy": _jpeg(_scene(2), size=(320, 240), quality=70),
        "unrelated": _jpeg(_scene(9), size=(320, 240), quality=70),
        "copy-of-archived": _jpeg(_scene(3), size=(320, 240), quality=70),
    }
    filed = {name: _file(vault, name, data) for name, data in photos.items()}
    filed["Screenshot from 2026-07-04 09-15-30"] = _file(
        vault, "Screenshot from 2026-07-04 09-15-30", _png(_scene(5)), extension="png"
    )
    return filed


def _recover(vault: Path, archive: Path) -> tuple[dict, dict]:
    kwargs = {"recover": True, "originals_root": str(archive)}
    preview = _backfill(vault, **kwargs)
    return preview, _backfill(vault, dry_run=False, plan_id=preview["plan_id"], **kwargs)


def test_recovery_dates_each_photo_from_the_one_source_that_explains_it(vault, library, archive):
    """Catches a recovery that dates a copy from an ambiguous or unrelated original, or leaves a recoverable one undated."""
    originals_before = _snapshot(archive)

    preview, written = _recover(vault, archive)

    assert preview["dated_by_source"] == {"camera-exif": 3, "file-name": 1, "matched-original": 2}
    assert preview["undated"] == 2
    # The screenshot is dated by its name, and no original explains it.
    assert preview["unmatched"] == {
        "another original with a different capture time is nearly as close": 1,
        "no original within the match distance": 2,
    }
    assert {item["path"] for item in preview["unmatched_listed"]} == {
        library["burst-copy"][1], library["unrelated"][1], library["Screenshot from 2026-07-04 09-15-30"][1],
    }
    assert written["updated"] == preview["would_update"] and written["skipped_changed"] == 0

    original_id = _fields(vault, library["original"][1])["exomem_id"]
    copy = _fields(vault, library["copy-of-original"][1])
    assert (copy["capture_date"], copy["capture_date_source"], copy["capture_date_matched_from"]) == (
        "2021-05-06", "matched-original", original_id,
    )
    archived = _fields(vault, library["copy-of-archived"][1])
    assert (archived["capture_date"], archived["capture_date_source"], archived["capture_date_matched_from"]) == (
        "2020-02-03", "matched-original", "2020/IMG_0001.jpg",
    )
    shot = _fields(vault, library["Screenshot from 2026-07-04 09-15-30"][1])
    assert (shot["capture_time"], shot["capture_date_source"]) == ("2026-07-04T09:15:30", "file-name")
    for name in ("original", "burst-a", "burst-b"):
        assert _fields(vault, library[name][1])["capture_date_source"] == "camera-exif"
    for name in ("burst-copy", "unrelated"):
        assert "capture_date" not in _fields(vault, library[name][1])
    assert _snapshot(archive) == originals_before  # nothing under originals_root is written, moved or touched


def test_a_plain_backfill_after_recovery_changes_nothing_and_never_wipes_a_recovered_date(vault, library, archive):
    """Catches a re-read that strips a matched date because the copy's own bytes state none."""
    _recover(vault, archive)

    again = _backfill(vault)

    assert again["would_update"] == 0
    assert again["dated_by_source"] == {"camera-exif": 3, "file-name": 1, "matched-original": 2}


def test_a_rerun_of_recovery_has_nothing_left_to_change(vault, library, archive):
    _recover(vault, archive)

    again = _backfill(vault, recover=True, originals_root=str(archive))

    assert again["would_update"] == 0


def test_a_search_by_capture_date_finds_the_fallback_dated_photo_labelled_and_a_source_filter_excludes_it(
    vault, library, archive
):
    _recover(vault, archive)
    _reset(vault)
    by_sidecar = {sidecar: name for name, (_original, sidecar) in library.items()}

    def found(filters: dict) -> dict[str, str | None]:
        hits = find_module.find(
            vault, query="harbour", scope="kb-only", mode="keyword", graph=False, result_level="page",
            filters=filters, limit=50,
        )
        return {by_sidecar[hit.path]: hit.as_dict().get("capture_date_source") for hit in hits}

    any_day = {"page.capture_date": {"$gte": "1900-01-01"}}
    everything = found(any_day)

    assert everything["unrelated"] == everything["burst-copy"] == capture_dates.source_for("captured-field")
    assert everything["copy-of-original"] == "matched-original"
    assert everything["original"] == "camera-exif"
    evidence = {"$and": [any_day, {"page.capture_date_source": {"$in": ["camera-exif", "matched-original", "file-name"]}}]}
    assert set(found(evidence)) == set(everything) - {"unrelated", "burst-copy"}
    assert set(found({"page.capture_date": {"$between": ["2021-05-06", "2021-05-06"]}})) == {"original", "copy-of-original"}


def test_a_source_filter_alone_lists_only_photos_dated_by_that_source(vault, library, archive):
    _recover(vault, archive)
    _reset(vault)
    hits = find_module.find(
        vault, query="harbour", scope="kb-only", mode="keyword", graph=False, result_level="page",
        filters={"page.capture_date_source": {"$in": ["matched-original"]}}, limit=50,
    )

    assert {hit.path for hit in hits} == {library["copy-of-original"][1], library["copy-of-archived"][1]}


def test_originals_root_is_refused_on_evermatter_cloud(vault, archive, monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")

    with pytest.raises(OpError) as refused:
        _backfill(vault, originals_root=str(archive))

    assert refused.value.code == "ORIGINALS_ROOT_UNAVAILABLE"


@pytest.mark.parametrize("where", ["missing", "relative", "inside-the-vault"])
def test_originals_root_must_be_an_existing_absolute_folder_outside_the_vault(vault, tmp_path, where):
    named = {
        "missing": str(tmp_path / "nowhere"),
        "relative": "phone-archive",
        "inside-the-vault": str(vault / "Knowledge Base"),
    }[where]

    with pytest.raises(OpError) as refused:
        _backfill(vault, originals_root=named)

    assert refused.value.code == "INVALID_ORIGINALS_ROOT"


def test_a_plan_made_for_one_archive_is_refused_for_another(vault, library, archive, tmp_path):
    other = tmp_path / "other-archive"
    other.mkdir()
    preview = _backfill(vault, recover=True, originals_root=str(archive))

    with pytest.raises(OpError) as stale:
        _backfill(vault, dry_run=False, plan_id=preview["plan_id"], recover=True, originals_root=str(other))

    assert stale.value.code == "STALE_MAINTENANCE_PLAN"


def test_a_look_alike_page_is_not_given_the_date_of_a_different_picture(vault, tmp_path):
    """Catches a recovery that dates a photo of one page from the original photo of another page.

    The two pages pass the coarse hash (the copy's reason is the finer check, not "no original within the
    match distance"), so only the finer-scale comparison keeps the copy undated.
    """
    folder = tmp_path / "phone-archive"
    folder.mkdir()
    (folder / "IMG_0100.jpg").write_bytes(_jpeg(_page(33), taken="2021:03:04 05:06:07"))
    _, other_page = _file(vault, "another-page", _jpeg(_page(29, jitter=12), size=(320, 240), quality=70))

    preview, _written = _recover(vault, folder)

    assert preview["dated_by_source"] == {}
    assert preview["unmatched_listed"] == [{"path": other_page, "reason": capture_match.NOT_CONFIRMED}]
    assert "capture_date" not in _fields(vault, other_page)


def test_the_preview_lists_each_match_with_its_source_date_and_origin_before_anything_is_written(
    vault, library, archive, monkeypatch
):
    """Catches a preview that hides which original gave which date, so the owner cannot check a match."""
    planned: list[tuple[str, str | None]] = []
    monkeypatch.setattr(maintenance_jobs, "record_planned", lambda items, **_kw: planned.extend(items))
    original_id = _fields(vault, library["original"][1])["exomem_id"]

    preview = _backfill(vault, recover=True, originals_root=str(archive))

    matches = {item["path"]: item for item in preview["matched_listed"]}
    assert matches == {
        library["copy-of-original"][1]: {
            "path": library["copy-of-original"][1], "time": "2021-05-06T07:08:09", "from": original_id,
        },
        library["copy-of-archived"][1]: {
            "path": library["copy-of-archived"][1], "time": "2020-02-03T04:05:06", "from": "2020/IMG_0001.jpg",
        },
    }
    rows = dict(planned)
    for page, facts in (
        ("copy-of-original", ("matched-original", "2021-05-06T07:08:09", original_id)),
        ("copy-of-archived", ("matched-original", "2020-02-03T04:05:06", "2020/IMG_0001.jpg")),
        ("Screenshot from 2026-07-04 09-15-30", ("file-name", "2026-07-04T09:15:30")),
    ):
        assert all(fact in rows[library[page][1]] for fact in facts), rows[library[page][1]]
    assert "capture_date" not in _fields(vault, library["copy-of-original"][1])  # a preview writes nothing
