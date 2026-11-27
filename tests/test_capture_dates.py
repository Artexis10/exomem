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
        ("IMG_20230102_030405678.MP.jpg", "2023-01-02T03:04:05", "android-camera"),
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
        "PXL_20230102_030405678.jpg",  # a Pixel name states UTC, and the registry has no clock basis
        "IMG12-20230115_123456.jpg",  # a number inside a name is the maker's own shape, never a set number
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


def _pack() -> list[registry.Entry]:
    return list(registry.pack_entries(capture_dates.SOURCES_PACK))


def test_precedence_follows_the_pack_order_and_never_downgrades():
    pack = _pack()
    evidence = [entry.key for entry in pack if entry.attributes["class"] == "evidence"]
    fallback = [entry.key for entry in pack if entry.attributes["class"] == "fallback"]
    assert len(evidence) > 1 and fallback  # the pack states an order to read and a fallback to leave out
    for earlier, key in enumerate(evidence):
        for later in evidence[earlier + 1:]:
            assert capture_dates.outranks(key, later)
            assert not capture_dates.outranks(later, key)
        assert not capture_dates.outranks(key, key)
        # A fallback and an unknown key are never evidence, so neither takes precedence nor is displaced.
        assert not capture_dates.outranks(key, "hand-edited")
        for other in fallback:
            assert not capture_dates.outranks(key, other) and not capture_dates.outranks(other, key)


def test_each_source_is_written_by_its_own_producer_and_carries_the_pack_class():
    for entry in _pack():
        assert capture_dates.source_for(entry.attributes["producer"]) == entry.key
        assert capture_dates.is_evidence(entry.key) == (entry.attributes["class"] == "evidence")
        assert capture_dates.is_known(entry.key)
    assert not capture_dates.is_known("hand-edited")


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


TAKEOUT = {
    "time": "2019-05-04T10:00:00Z", "date": "2019-05-04", "date_basis": "utc", "date_source": "export-sidecar",
    "date_matched_from": "Takeout/Google Photos/Trip/IMG_0001.jpg.json",
}


def test_a_re_read_that_states_no_date_keeps_a_date_from_a_takeout_sidecar_and_its_origin():
    """Catches an extraction that wipes a sidecar date because the photo's own bytes state none."""
    from exomem import preserve
    from exomem.capture_metadata import CaptureFacts

    dated = _sidecar(**TAKEOUT)

    rewritten = preserve.set_capture_fields(dated, CaptureFacts(width=64, height=48))

    assert _stated(rewritten) == _stated(dated)
    assert _stated(rewritten)["capture_date_matched_from"] == TAKEOUT["date_matched_from"]


@pytest.mark.parametrize("source", ["camera-exif", "matched-original", "file-name"])
def test_a_source_that_outranks_a_takeout_sidecar_replaces_its_date(source):
    from exomem import preserve
    from exomem.capture_metadata import CaptureFacts

    stronger = CaptureFacts(time="2020-01-02T03:04:05", date="2020-01-02", date_source=source)

    rewritten = preserve.set_capture_fields(_sidecar(**TAKEOUT), stronger)

    assert _stated(rewritten)["capture_date"] == "2020-01-02"
    assert _stated(rewritten)["capture_date_source"] == source
    assert "capture_date_matched_from" not in _stated(rewritten)

