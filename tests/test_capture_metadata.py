"""What a photo or video states about its capture, read from real files built in the test."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from exomem import capture_metadata

pytestmark = pytest.mark.frame_codecs


def _jpeg(
    path: Path, *, original: str | None = None, offset: str | None = None, modified: str | None = None,
    gps: dict[int, object] | None = None, make: str | None = None, model: str | None = None,
    size: tuple[int, int] = (40, 30),
) -> Path:
    from PIL import Image

    exif = Image.Exif()
    if make is not None:
        exif[0x010F] = make
    if model is not None:
        exif[0x0110] = model
    if modified is not None:
        exif[0x0132] = modified
    taken = exif.get_ifd(0x8769)
    if original is not None:
        taken[0x9003] = original
    if offset is not None:
        taken[0x9011] = offset
    place = exif.get_ifd(0x8825)
    for tag, value in (gps or {}).items():
        place[tag] = value
    Image.new("RGB", size, (30, 90, 160)).save(path, "JPEG", exif=exif)
    return path


_HELSINKI = {1: "N", 2: (60.0, 10.0, 30.0), 3: "E", 4: (24.0, 56.0, 15.0)}


def test_a_phone_photo_states_its_local_time_with_offset_place_camera_and_size(tmp_path):
    photo = _jpeg(
        tmp_path / "phone.jpg", original="2024:06:01 14:03:22", offset="+03:00", gps=_HELSINKI,
        make="Acme", model="Phone 9", size=(64, 48),
    )

    facts = capture_metadata.read(photo, "image")

    assert facts.fields() == {
        "capture_time": "2024-06-01T14:03:22+03:00",
        "capture_date": "2024-06-01",
        "capture_lat": 60.175,
        "capture_lon": 24.9375,
        "camera_make": "Acme",
        "camera_model": "Phone 9",
        "pixel_width": 64,
        "pixel_height": 48,
    }


def test_a_photo_without_an_offset_records_the_local_time_as_the_camera_wrote_it(tmp_path):
    photo = _jpeg(tmp_path / "old.jpg", original="2012:12:25 23:59:58")

    assert capture_metadata.read(photo, "image").fields()["capture_time"] == "2012-12-25T23:59:58"


def test_a_scan_that_records_only_its_modification_time_has_no_capture_date(tmp_path):
    scan = _jpeg(tmp_path / "scan.jpg", modified="2025:03:09 10:00:00")

    facts = capture_metadata.read(scan, "image")

    assert "capture_time" not in facts.fields()
    assert "capture_date" not in facts.fields()


@pytest.mark.parametrize(
    "gps",
    [
        pytest.param({**_HELSINKI, 9: "V"}, id="receiver-had-no-fix"),
        pytest.param({1: "X", 2: (60.0, 0.0, 0.0), 3: "E", 4: (24.0, 0.0, 0.0)}, id="unknown-latitude-reference"),
        pytest.param({1: "N", 2: (60.0, 0.0, 0.0)}, id="longitude-missing"),
        pytest.param({1: "N", 2: (95.0, 0.0, 0.0), 3: "E", 4: (24.0, 0.0, 0.0)}, id="latitude-out-of-range"),
        pytest.param({1: "N", 2: (60.0, 75.0, 0.0), 3: "E", 4: (24.0, 0.0, 0.0)}, id="minutes-out-of-range"),
    ],
)
def test_a_gps_block_that_states_no_valid_position_gives_none(tmp_path, gps):
    photo = _jpeg(tmp_path / "gps.jpg", original="2024:06:01 14:03:22", gps=gps)

    fields = capture_metadata.read(photo, "image").fields()

    assert "capture_lat" not in fields
    assert "capture_lon" not in fields
    assert fields["capture_time"] == "2024-06-01T14:03:22"


def test_a_southern_and_western_position_has_negative_coordinates(tmp_path):
    photo = _jpeg(
        tmp_path / "sw.jpg", original="2024:06:01 14:03:22",
        gps={1: "S", 2: (33.0, 52.0, 0.0), 3: "W", 4: (151.0, 12.0, 0.0)},
    )

    fields = capture_metadata.read(photo, "image").fields()

    assert (fields["capture_lat"], fields["capture_lon"]) == (-33.866667, -151.2)


def test_a_malformed_capture_time_loses_only_the_time(tmp_path):
    photo = _jpeg(tmp_path / "bad.jpg", original="0000:00:00 00:00:00", make="Acme", gps=_HELSINKI)

    fields = capture_metadata.read(photo, "image").fields()

    assert "capture_time" not in fields
    assert fields["camera_make"] == "Acme"
    assert fields["capture_lat"] == 60.175


def test_hostile_text_in_the_camera_name_is_cleaned_and_capped(tmp_path):
    photo = _jpeg(tmp_path / "text.jpg", make="Ac\x01me" + "x" * 500, original="2024:06:01 14:03:22")

    make = capture_metadata.read(photo, "image").fields()["camera_make"]

    assert make.startswith("Acmexxx")
    assert len(make) == capture_metadata.TEXT_LIMIT


def test_a_file_that_is_not_an_image_states_nothing_and_does_not_raise(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00garbage")

    assert capture_metadata.read(broken, "image") is None


# ------------------------------------------------------------------ video


def _video(path: Path, container: str, metadata: dict[str, str], flags: str | None = None) -> Path:
    import av
    import numpy as np

    options = {"movflags": flags} if flags else {}
    with av.open(str(path), "w", format=container, options=options) as out:
        for key, value in metadata.items():
            out.metadata[key] = value
        stream = out.add_stream("mpeg4", rate=5)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        frame = av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), dtype=np.uint8), format="rgb24")
        for _ in range(3):
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    return path


def test_a_quicktime_video_states_the_local_time_it_was_recorded_with_its_offset(tmp_path):
    clip = _video(
        tmp_path / "clip.mov", "mov",
        {"com.apple.quicktime.creationdate": "2024-06-01T23:30:00+0300", "creation_time": "2024-06-01T20:30:00.000000Z"},
        flags="use_metadata_tags",
    )

    fields = capture_metadata.read(clip, "video").fields()

    assert fields["capture_time"] == "2024-06-01T23:30:00+03:00"
    assert fields["capture_date"] == "2024-06-01"
    assert "capture_date_basis" not in fields
    assert (fields["pixel_width"], fields["pixel_height"]) == (64, 48)


def test_a_video_with_only_a_utc_creation_time_dates_by_the_utc_day_and_says_so(tmp_path):
    clip = _video(tmp_path / "clip.mp4", "mp4", {"creation_time": "2024-06-01T23:30:00.000000Z"})

    fields = capture_metadata.read(clip, "video").fields()

    assert fields["capture_time"] == "2024-06-01T23:30:00Z"
    assert fields["capture_date"] == "2024-06-01"
    assert fields["capture_date_basis"] == "utc"


def test_a_video_that_states_no_time_still_states_its_size(tmp_path):
    clip = _video(tmp_path / "plain.mp4", "mp4", {})

    assert capture_metadata.read(clip, "video").fields() == {"pixel_width": 64, "pixel_height": 48}


# ------------------------------------------------------------------ camera raw


def _dng(tags: dict[int, object]) -> bytes:
    """A small monochrome DNG whose first directory declares `tags` (the layout the viewer tests use)."""
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    pixels = b"\x00\x08" * 64 * 64 + bytes(65536)
    directory = ImageFileDirectory_v2()
    fields = {
        254: 0, 256: 64, 257: 64, 258: 16, 259: 1, 262: 34892, 277: 1, 278: 64,
        273: 4096, 279: len(pixels), 50706: b"\x01\x04\x00\x00", 50717: 4095, **tags,
    }
    for tag, value in fields.items():
        if tag == 50706:
            directory.tagtype[tag] = 1
        directory[tag] = value
    out = io.BytesIO()
    out.write(b"II*\x00\x08\x00\x00\x00")
    directory.save(out)
    return out.getvalue().ljust(4096, b"\x00") + pixels


def test_a_camera_raw_states_its_capture_time_through_the_raw_decoder(tmp_path):
    raw = tmp_path / "shot.dng"
    raw.write_bytes(_dng({306: "2023:07:04 09:08:07"}))

    fields = capture_metadata.read(raw, "image").fields()

    assert fields["capture_time"] == "2023-07-04T09:08:07"
    assert fields["capture_date"] == "2023-07-04"
    assert (fields["pixel_width"], fields["pixel_height"]) == (64, 64)


def test_a_camera_raw_that_records_no_time_states_no_time(tmp_path):
    raw = tmp_path / "untimed.dng"
    raw.write_bytes(_dng({}))

    fields = capture_metadata.read(raw, "image").fields()

    assert "capture_time" not in fields
    assert fields["pixel_width"] == 64


def test_the_file_modification_time_never_becomes_a_capture_time(tmp_path):
    scan = _jpeg(tmp_path / "scan.jpg")
    os.utime(scan, (1_600_000_000, 1_600_000_000))

    assert "capture_time" not in capture_metadata.read(scan, "image").fields()


# ------------------------------------------------------------------ payload


def test_a_job_result_with_an_out_of_bounds_value_is_refused():
    with pytest.raises(ValueError):
        capture_metadata.CaptureFacts.from_payload({"capture_lat": 123.0, "capture_lon": 4.0})


# ------------------------------------------------------------------ extraction writes the sidecar fields


def _extract_into_sidecar(vault, monkeypatch, original: Path, *, category: str, media_type: str) -> str:
    """Preserve `original`, run the worker's extraction on it with OCR stubbed, and return the sidecar text."""
    from exomem import extract, media_worker, preserve
    from exomem.ocr_models import OcrReading

    monkeypatch.setattr(extract, "_ocr_png", lambda _png: OcrReading("", ()))
    monkeypatch.setattr(extract, "_has_audio_stream", lambda _path: False)
    stored = preserve.preserve_bytes(
        vault, scope="Rental", category=category, filename=original.name, data=original.read_bytes()
    )
    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / stored.path, sidecar_path=vault / stored.sidecar_path, media_type=media_type
        )
    )
    return (vault / stored.sidecar_path).read_text(encoding="utf-8")


def test_extraction_writes_exactly_the_stated_fields_into_the_sidecar(vault, tmp_path, monkeypatch):
    from exomem.vault import parse_frontmatter

    photo = _jpeg(
        tmp_path / "phone.jpg", original="2024:06:01 14:03:22", offset="+03:00", gps=_HELSINKI,
        make="Acme", model="Phone 9", size=(64, 48),
    )

    sidecar = _extract_into_sidecar(vault, monkeypatch, photo, category="image", media_type="image")

    stated = {key: value for key, value in parse_frontmatter(sidecar)[0].items() if key in capture_metadata.FIELDS}
    assert stated == {
        "capture_time": "2024-06-01T14:03:22+03:00", "capture_date": "2024-06-01",
        "capture_lat": 60.175, "capture_lon": 24.9375, "camera_make": "Acme", "camera_model": "Phone 9",
        "pixel_width": 64, "pixel_height": 48,
    }


def test_extraction_of_a_scan_records_no_capture_date_and_no_position(vault, tmp_path, monkeypatch):
    from exomem.vault import parse_frontmatter

    scan = _jpeg(tmp_path / "scan.jpg", modified="2025:03:09 10:00:00", gps={**_HELSINKI, 9: "V"})

    sidecar = _extract_into_sidecar(vault, monkeypatch, scan, category="image", media_type="image")

    stated = {key for key in parse_frontmatter(sidecar)[0] if key in capture_metadata.FIELDS}
    assert stated == {"pixel_width", "pixel_height"}


def test_extraction_of_a_video_records_its_utc_basis(vault, tmp_path, monkeypatch):
    from exomem import extract, media_worker, preserve
    from exomem.vault import parse_frontmatter

    clip = _video(tmp_path / "clip.mp4", "mp4", {"creation_time": "2024-06-01T23:30:00.000000Z"})
    monkeypatch.setattr(
        extract, "_transcribe",
        lambda *_a, **_kw: extract.ExtractResult(text="", media_type="video", engine="no-audio"),
    )
    stored = preserve.preserve_bytes(
        vault, scope="Rental", category="video", filename=clip.name, data=clip.read_bytes()
    )
    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(binary_path=vault / stored.path, sidecar_path=vault / stored.sidecar_path, media_type="video")
    )

    fields = parse_frontmatter((vault / stored.sidecar_path).read_text(encoding="utf-8"))[0]

    assert (fields["capture_time"], fields["capture_date"], fields["capture_date_basis"]) == (
        "2024-06-01T23:30:00Z", "2024-06-01", "utc",
    )
