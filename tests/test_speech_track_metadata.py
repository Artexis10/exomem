"""What a recorder writes into a WAVE file about its tracks and its start decides which tracks a recording has.

The files are built in code: a RIFF container with the chunks a field recorder writes (`fmt `, `bext`,
`iXML`). Container tags (stream titles, timecode) need PyAV and live in `test_speech_track_containers.py`.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from exomem import speech_tracks

RATE = 48000


def chunk(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) % 2 else b"")


def bext(time_reference: int) -> bytes:
    """A Broadcast Wave `bext` chunk: 338 bytes of text fields, then the 64-bit sample count since midnight."""
    return chunk(b"bext", b"\x00" * 338 + struct.pack("<Q", time_reference) + b"\x00" * 256)


def ixml(*tracks: tuple[int, str], root: str = "BWFXML") -> bytes:
    entries = "".join(
        f"<TRACK><CHANNEL_INDEX>{index}</CHANNEL_INDEX><NAME>{name}</NAME></TRACK>" for index, name in tracks
    )
    return chunk(b"iXML", f"<?xml version='1.0'?><{root}><TRACK_LIST>{entries}</TRACK_LIST></{root}>".encode())


def wave(path: Path, *extra: bytes, channels: int = 2) -> Path:
    fmt = chunk(b"fmt ", struct.pack("<HHIIHH", 1, channels, RATE, RATE * 2 * channels, 2 * channels, 16))
    data = chunk(b"data", b"\x00" * 4 * channels)
    body = b"WAVE" + fmt + b"".join(extra) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def test_ixml_track_list_names_the_tracks_and_counts_channels_from_one(tmp_path: Path) -> None:
    path = wave(tmp_path / "panel.wav", ixml((1, "Host"), (2, "Guest")))

    resolved = speech_tracks.resolve(path)

    assert [(t.name, t.stream, t.channel, t.single_voice) for t in resolved.tracks] == [
        ("Host", 0, 0, False),
        ("Guest", 0, 1, False),
    ]


def test_bext_time_reference_is_the_recordings_start_in_seconds(tmp_path: Path) -> None:
    path = wave(tmp_path / "take.wav", bext(RATE * 3600 + RATE // 2))

    start = speech_tracks.resolve(path).start

    assert start == speech_tracks.Start("bwf-time-reference", 3600.5)


def test_a_file_that_publishes_nothing_has_no_tracks_and_no_start(tmp_path: Path) -> None:
    path = wave(tmp_path / "plain.wav")

    resolved = speech_tracks.resolve(path)

    assert resolved.tracks == () and resolved.start is None


@pytest.mark.parametrize(
    "ixml_body",
    [
        b"<BWFXML><TRACK_LIST><TRACK><CHANNEL_INDEX>1",  # cut off mid-element
        # A stock XML parser would read this as a track named "aaaa"; an upload's entity definitions are refused.
        b'<!DOCTYPE x [<!ENTITY a "aaaa">]><BWFXML><TRACK_LIST><TRACK><CHANNEL_INDEX>1</CHANNEL_INDEX><NAME>&a;</NAME></TRACK></TRACK_LIST></BWFXML>',
        b"<BWFXML><TRACK_LIST><TRACK><CHANNEL_INDEX>0</CHANNEL_INDEX><NAME>Zero</NAME></TRACK></TRACK_LIST></BWFXML>",
        b"<BWFXML><TRACK_LIST><TRACK><CHANNEL_INDEX>1</CHANNEL_INDEX><NAME> </NAME></TRACK></TRACK_LIST></BWFXML>",
    ],
    ids=["truncated", "entity-expansion", "channel-zero", "blank-name"],
)
def test_an_ixml_chunk_that_lists_no_usable_track_lists_none_and_never_stops_the_run(tmp_path: Path, ixml_body: bytes) -> None:
    path = wave(tmp_path / "bad.wav", chunk(b"iXML", ixml_body))

    assert speech_tracks.resolve(path).tracks == ()


def test_a_recorder_family_is_one_more_entry_in_the_data_and_needs_no_code(tmp_path: Path) -> None:
    # This recorder writes its microphones as <MICS><MIC><SLOT/><LABEL/></MIC></MICS>, not as TRACK_LIST.
    packet = b"<BWFXML><MICS><MIC><SLOT>2</SLOT><LABEL>Lapel</LABEL></MIC><MIC><SLOT>1</SLOT><LABEL>Overhead</LABEL></MIC></MICS></BWFXML>"
    path = wave(tmp_path / "other.wav", chunk(b"iXML", packet))
    shipped = speech_tracks.policy()
    own = {
        "key": "other-recorder-mics", "kind": "ixml", "track": "MICS/MIC", "channel": "SLOT", "name": "LABEL",
        "declares_microphone": True, "single_voice": True,
    }

    without = speech_tracks.resolve(path, data=shipped)
    with_family = speech_tracks.resolve(path, data={**shipped, "families": [*shipped["families"], own]})

    assert without.tracks == ()
    assert [(t.name, t.channel, t.single_voice) for t in with_family.tracks] == [("Lapel", 1, True), ("Overhead", 0, True)]


def test_an_owner_declaration_replaces_the_published_list_and_takes_a_missing_name_from_it(tmp_path: Path) -> None:
    path = wave(tmp_path / "panel.wav", ixml((1, "Host"), (2, "Guest")))

    resolved = speech_tracks.resolve(path, [{"channel": 1}, {"channel": 0, "name": "Presenter", "single_voice": True}])

    assert [(t.name, t.channel, t.single_voice) for t in resolved.tracks] == [("Guest", 1, False), ("Presenter", 0, True)]


def test_two_published_tracks_with_the_same_name_keep_distinct_labels(tmp_path: Path) -> None:
    path = wave(tmp_path / "pair.wav", ixml((1, "Input"), (2, "Input")))

    assert [t.name for t in speech_tracks.resolve(path).tracks] == ["Input", "Input 2"]
