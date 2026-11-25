"""Capture-date sources and file-name formats are registry data, not code.

A pass means the pack or the vault overlay decided the date, so a new naming shape needs no code edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import capture_dates
from exomem.vocabulary import registry


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Knowledge Base" / "_Schema").mkdir(parents=True)
    return root


def _overlay(root: Path, text: str) -> None:
    capture_dates.registry_path(root).write_text(text, encoding="utf-8")
    registry.invalidate(root)


@pytest.mark.parametrize(
    ("name", "time", "format_key"),
    [
        ("Screenshot from 2026-07-04 09-15-30.png", "2026-07-04T09:15:30", "gnome-screenshot"),
        ("20200314_101112.jpg", "2020-03-14T10:11:12", "android-camera"),
        ("PXL_20230102_030405678.MP.jpg", "2023-01-02T03:04:05", "android-camera"),
        ("Screenshot_20240229-235959_Chrome.jpg", "2024-02-29T23:59:59", "android-screenshot-compact"),
        ("Screenshot_2024-02-29-23-59-59-123_com.example.app.jpg", "2024-02-29T23:59:59", "android-screenshot-dashed"),
        # A raw-protected capture is stored with a marker in front of the client's name.
        ("__exomem_raw_v1__20200314_101112.jpg", "2020-03-14T10:11:12", "android-camera"),
        # A set number in front of the name orders the set and states no date.
        ("01-Screenshot from 2026-07-04 09-15-30.png", "2026-07-04T09:15:30", "gnome-screenshot"),
    ],
)
def test_a_shipped_format_reads_the_time_a_name_states(vault, name, time, format_key):
    found = capture_dates.from_file_name(vault, name)
    assert found == capture_dates.NameDate(time=time, date=time[:10], format=format_key)


@pytest.mark.parametrize(
    "name",
    [
        "IMG-20200314-WA0001.jpg",  # a send date, not a capture
        "image(20200314-101112).png",  # a paste time
        "1696150000000.jpg",  # an epoch save time
        "20201332_101112.jpg",  # month 13 is not a moment
        "18500101_101112.jpg",  # before any camera in this registry
        "holiday.jpg",
        "",
    ],
)
def test_a_name_that_states_no_capture_time_yields_nothing(vault, name):
    assert capture_dates.from_file_name(vault, name) is None


def test_a_name_beyond_next_year_is_not_a_capture_time(vault):
    from datetime import datetime

    now = datetime(2026, 11, 25)
    assert capture_dates.from_file_name(vault, "20270101_101112.jpg", now=now) is not None
    assert capture_dates.from_file_name(vault, "20280101_101112.jpg", now=now) is None


def test_an_overlay_format_extends_the_shipped_ones_without_replacing_them(vault):
    assert capture_dates.from_file_name(vault, "scan 2019.04.05 06.07.08.jpg") is None
    _overlay(vault, (
        "schema_version: 1\nentries:\n  office-scanner:\n    label: Office scanner\n"
        "    description: The scanner names a file after the moment it scanned.\n"
        "    attributes:\n      maker: Office scanner\n"
        "      pattern: 'scan (?P<year>\\d{4})\\.(?P<month>\\d{2})\\.(?P<day>\\d{2}) (?P<hour>\\d{2})\\.(?P<minute>\\d{2})\\.(?P<second>\\d{2})'\n"
    ))
    found = capture_dates.from_file_name(vault, "scan 2019.04.05 06.07.08.jpg")
    assert found is not None and found.format == "office-scanner" and found.time == "2019-04-05T06:07:08"
    assert capture_dates.from_file_name(vault, "20200314_101112.jpg") is not None


def test_a_malformed_overlay_leaves_the_shipped_formats_in_force(vault):
    _overlay(vault, (
        "schema_version: 1\nentries:\n  broken:\n    label: Broken\n"
        "    description: A shape without a time.\n"
        "    attributes: {maker: Nobody, pattern: 'photo (?P<year>\\d{4})'}\n"
    ))
    assert capture_dates.from_file_name(vault, "20200314_101112.jpg") is not None
    snapshot = registry.load(capture_dates.SPEC, vault)
    assert snapshot.findings and "hour" in str(snapshot.findings[0]["detail"])


def test_precedence_follows_the_pack_order_and_never_downgrades():
    assert capture_dates.outranks("camera-exif", "matched-original")
    assert capture_dates.outranks("matched-original", "file-name")
    assert not capture_dates.outranks("file-name", "camera-exif")
    assert not capture_dates.outranks("camera-exif", "camera-exif")
    # A fallback and an unknown key are never evidence, so neither takes precedence nor is displaced.
    assert not capture_dates.outranks("source-date", "file-name")
    assert not capture_dates.outranks("file-name", "source-date")
    assert not capture_dates.outranks("camera-exif", "hand-edited")


def test_each_producer_writes_exactly_one_source():
    assert capture_dates.source_for("file-metadata") == "camera-exif"
    assert capture_dates.is_evidence("matched-original")
    assert not capture_dates.is_evidence("source-date")
    assert capture_dates.is_known("source-date") and not capture_dates.is_known("hand-edited")


def _sidecar(**facts) -> str:
    from exomem import preserve
    from exomem.capture_metadata import CaptureFacts

    return preserve.set_capture_fields("---\ntitle: x\n---\nbody\n", CaptureFacts(**facts))


def _stated(content: str) -> dict:
    from exomem.vault import parse_frontmatter

    return {key: value for key, value in parse_frontmatter(content)[0].items() if key.startswith("capture_")}


MATCHED = {
    "time": "2021-05-06T07:08:09", "date": "2021-05-06", "date_source": "matched-original",
    "date_matched_from": "Phone/IMG_0001.jpg",
}


def test_a_re_read_that_states_no_date_keeps_a_date_recovered_from_a_matched_original():
    from exomem import preserve
    from exomem.capture_metadata import CaptureFacts

    recovered = _sidecar(**MATCHED)

    rewritten = preserve.set_capture_fields(recovered, CaptureFacts(width=64, height=48))

    assert _stated(rewritten) == {**_stated(recovered), "capture_date_source": "matched-original"}
    assert _stated(rewritten)["capture_date_matched_from"] == "Phone/IMG_0001.jpg"


def test_a_re_read_that_states_a_camera_date_replaces_a_date_recovered_from_a_matched_original():
    from exomem import preserve
    from exomem.capture_metadata import CaptureFacts

    camera = CaptureFacts(time="2020-01-02T03:04:05", date="2020-01-02", date_source="camera-exif")

    rewritten = preserve.set_capture_fields(_sidecar(**MATCHED), camera)

    assert _stated(rewritten) == {
        "capture_time": "2020-01-02T03:04:05", "capture_date": "2020-01-02", "capture_date_source": "camera-exif",
    }


def test_a_file_name_date_never_replaces_a_date_recovered_from_a_matched_original():
    from exomem import preserve
    from exomem.capture_metadata import CaptureFacts

    named = CaptureFacts(time="2020-01-02T03:04:05", date="2020-01-02", date_source="file-name")

    rewritten = preserve.set_capture_fields(_sidecar(**MATCHED), named)

    assert _stated(rewritten)["capture_date"] == "2021-05-06"


def test_a_re_read_replaces_a_file_name_date_because_the_file_still_states_it():
    from exomem import preserve
    from exomem.capture_metadata import CaptureFacts

    named = _sidecar(time="2020-01-02T03:04:05", date="2020-01-02", date_source="file-name")

    rewritten = preserve.set_capture_fields(named, CaptureFacts(width=64, height=48))

    assert "capture_date" not in _stated(rewritten)
