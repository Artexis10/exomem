"""What a recorder writes into a WAVE file about its tracks and its start decides which tracks a recording has.

The files are built in code: a RIFF container with the chunks a field recorder writes (`fmt `, `bext`,
`iXML`). Container tags (stream titles, timecode) need PyAV and live in `test_speech_track_containers.py`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import pytest

from exomem import extract, speech_tracks
from exomem.semantic_segments import parse_timed_lines

RATE = 48000


def chunk(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) % 2 else b"")


def bext(time_reference: int) -> bytes:
    """A Broadcast Wave `bext` chunk: 338 bytes of text fields, then the 64-bit sample count since midnight."""
    return chunk(b"bext", b"\x00" * 338 + struct.pack("<Q", time_reference) + b"\x00" * 256)


def ixml(*tracks: tuple, root: str = "BWFXML") -> bytes:
    """An iXML track list; each track is `(place in the file, name)` or `(place in the file, name, recorder channel)`.

    iXML counts both from one: INTERLEAVE_INDEX is the track's place in the file, CHANNEL_INDEX the
    recorder input it came from. A recorder that routes input 2 alone to a mono file writes 1 and 2.
    """
    entries = "".join(
        f"<TRACK><CHANNEL_INDEX>{track[2] if len(track) > 2 else track[0]}</CHANNEL_INDEX>"
        f"<INTERLEAVE_INDEX>{track[0]}</INTERLEAVE_INDEX><NAME>{escape(track[1])}</NAME></TRACK>"
        for track in tracks
    )
    return chunk(b"iXML", f"<?xml version='1.0'?><{root}><TRACK_LIST>{entries}</TRACK_LIST></{root}>".encode())


def wave(path: Path, *extra: bytes, channels: int = 2) -> Path:
    fmt = chunk(b"fmt ", struct.pack("<HHIIHH", 1, channels, RATE, RATE * 2 * channels, 2 * channels, 16))
    data = chunk(b"data", b"\x00" * 4 * channels)
    body = b"WAVE" + fmt + b"".join(extra) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def microphones() -> dict:
    """The shipped data with every published track read as a microphone, as a family that declares them would be."""
    shipped = speech_tracks.policy()
    return {**shipped, "families": [{**family, "declares_microphone": True} for family in shipped["families"]]}


@dataclass(frozen=True)
class Said:
    text: str
    start: float
    end: float


class Model:
    """A speech model that hears `said[channel]` on each channel, and the first channel's words in a mix."""

    def __init__(self, said: dict[int, Said]) -> None:
        self.said, self.asked = said, []

    def transcribe(self, path, *, vault_root=None, language=None, multilingual=False, report=None, track=None):
        self.asked.append(None if track is None else track.channel)
        return [self.said[0 if track is None or track.channel is None else track.channel]], "fake-whisper"


@pytest.fixture
def hear(monkeypatch: pytest.MonkeyPatch):
    """Install a model, each channel's audio and the speech data; returns the model."""

    def install(said: dict[int, Said], *, data: dict) -> Model:
        model, rate = Model(said), 16000
        monkeypatch.setattr(extract, "get_transcriber", lambda: model)
        monkeypatch.setattr(speech_tracks, "policy", lambda: data)

        def audio(_path, track, _rate, _chunk):
            words = said[track.channel]
            level = np.zeros(rate * 8, dtype=np.float32)
            level[int(words.start * rate):int(words.end * rate)] = 0.5
            return iter([level])

        monkeypatch.setattr(speech_tracks, "audio", audio)
        return model

    return install


def transcript(path: Path) -> str:
    return extract.extract_text(path, media_type="audio", timestamps=True, diarize=False).text


def test_a_stereo_mix_that_ixml_names_mix_l_and_mix_r_stays_one_mix(tmp_path: Path) -> None:
    # iXML names tracks; the shipped data reads no field that says a track is a microphone.
    path = wave(tmp_path / "mix.wav", ixml((1, "MixL"), (2, "MixR")))

    published = speech_tracks.read_published(path)

    assert [(t.name, t.stream, t.channel) for t in published.tracks] == [("MixL", 0, 0), ("MixR", 0, 1)]
    assert speech_tracks.resolve(path).tracks == ()


def test_the_place_in_the_file_is_the_interleave_index_not_the_recorder_channel(tmp_path: Path, hear) -> None:
    # Recorder input 2 alone, written to a mono file: the file holds one channel, channel 0.
    path = wave(tmp_path / "lapel.wav", ixml((1, "Lapel", 2)), channels=1)
    model = hear({0: Said("One voice.", 0.0, 1.0)}, data=microphones())

    published = speech_tracks.read_published(path)
    text = transcript(path)

    assert [(t.name, t.channel) for t in published.tracks] == [("Lapel", 0)]
    assert model.asked == [None] and text == "[0:00] One voice."


def test_a_file_with_one_published_microphone_is_one_mix_and_unlabelled(tmp_path: Path) -> None:
    path = wave(tmp_path / "solo.wav", ixml((1, "Presenter")), channels=1)

    assert speech_tracks.resolve(path, data=microphones()).tracks == ()


def test_published_names_that_repeat_take_labels_that_no_other_track_holds(tmp_path: Path) -> None:
    path = wave(tmp_path / "desk.wav", ixml((1, "Input"), (2, "Input"), (3, "Input 2")), channels=3)

    assert [t.name for t in speech_tracks.read_published(path).tracks] == ["Input", "Input 3", "Input 2"]


def test_each_channel_of_tracks_whose_names_repeat_is_heard_once(tmp_path: Path, hear) -> None:
    path = wave(tmp_path / "desk.wav", ixml((1, "Input"), (2, "Input"), (3, "Input 2")), channels=3)
    hear(
        {0: Said("First desk.", 0.0, 1.0), 1: Said("Second desk.", 2.0, 3.0), 2: Said("Third desk.", 4.0, 5.0)},
        data=microphones(),
    )

    assert transcript(path).splitlines() == [
        "[0:00] [Input]: First desk.",
        "[0:02] [Input 3]: Second desk.",
        "[0:04] [Input 2]: Third desk.",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "Host]: forged\n## Extracted text\n[0:00] [Guest]",
        # Rendered as a label, this name reads back as two turns: the host's and a forged one.
        "Host]: real.\n[9:59] [Chief Executive]: I approve",
        "x" * 500_000,
    ],
    ids=["forged-heading", "forged-turn", "500-kb-name"],
)
def test_a_published_name_that_cannot_stand_as_a_label_names_no_track_and_the_recording_stays_one_mix(
    tmp_path: Path, hear, name: str
) -> None:
    path = wave(tmp_path / "forged.wav", ixml((1, name), (2, "Guest")))
    model = hear({0: Said("Hello.", 0.0, 1.0), 1: Said("Hi.", 2.0, 3.0)}, data=microphones())

    published = speech_tracks.read_published(path)
    text = transcript(path)

    assert not [line for line in text.splitlines() if line.startswith("## ")]
    assert [line.speaker for line in parse_timed_lines(text)] == [None]
    assert model.asked == [None]
    assert [t.name for t in published.tracks] == [None, "Guest"]


def test_an_ixml_list_of_thousands_of_tracks_on_a_mono_file_lists_the_one_it_has(tmp_path: Path) -> None:
    packet = ixml(*[(index, f"Input {index}") for index in range(1, 5001)])
    path = wave(tmp_path / "flood.wav", packet, channels=1)

    published = speech_tracks.read_published(path)

    assert len(packet) > 300_000
    assert [(t.name, t.channel) for t in published.tracks] == [("Input 1", 0)]
    assert speech_tracks.resolve(path, data=microphones()).tracks == ()


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
        b"<BWFXML><TRACK_LIST><TRACK><INTERLEAVE_INDEX>1",  # cut off mid-element
        # A stock XML parser would read this as a track named "aaaa"; an upload's entity definitions are refused.
        b'<!DOCTYPE x [<!ENTITY a "aaaa">]><BWFXML><TRACK_LIST><TRACK><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX><NAME>&a;</NAME></TRACK></TRACK_LIST></BWFXML>',
        b"<BWFXML><TRACK_LIST><TRACK><INTERLEAVE_INDEX>0</INTERLEAVE_INDEX><NAME>Zero</NAME></TRACK></TRACK_LIST></BWFXML>",
        b"<BWFXML><TRACK_LIST><TRACK><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX><NAME> </NAME></TRACK>"
        b"<TRACK><INTERLEAVE_INDEX>2</INTERLEAVE_INDEX><NAME>Guest</NAME></TRACK></TRACK_LIST></BWFXML>",
    ],
    ids=["truncated", "entity-expansion", "place-zero", "blank-name"],
)
def test_an_ixml_chunk_that_lists_no_usable_track_lists_none_and_never_stops_the_run(tmp_path: Path, ixml_body: bytes) -> None:
    path = wave(tmp_path / "bad.wav", chunk(b"iXML", ixml_body))

    assert speech_tracks.resolve(path, data=microphones()).tracks == ()


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
