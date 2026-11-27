"""A recording with declared tracks is transcribed track by track and merged into one transcript.

The speech model, the audio of each track and the diarizer are stubbed at their seams, so these
tests read what the multitrack code decides: which tracks run, which segments stay, how turns are
named. `test_speech_track_containers.py` reads real container files in the frame-codecs CI job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from exomem import extract, speech_tracks
from exomem.asr_facts import AsrFacts
from exomem.speaker_names import MAX_NAME_CHARS

RATE = 16000
HOST = {"name": "Host", "stream": 0}
GUEST = {"name": "Guest", "stream": 1}


@dataclass(frozen=True)
class Said:
    text: str
    start: float
    end: float


def level(seconds: float, *spans: tuple[float, float, float]) -> np.ndarray:
    """A track's audio: silence with a constant amplitude over each `(start, end, amplitude)` span."""
    audio = np.zeros(int(seconds * RATE), dtype=np.float32)
    for start, end, amplitude in spans:
        audio[int(start * RATE):int(end * RATE)] = amplitude
    return audio


class Model:
    """The speech model: what each track hears, and which tracks it was asked to read."""

    def __init__(self, heard: dict[str, list[Said]]) -> None:
        self.heard, self.asked = heard, []

    def transcribe(self, path, *, vault_root=None, language=None, multilingual=False, report=None, track=None):
        self.asked.append(track)
        report.policy, report.duration_seconds = "decoding-test", 12.0
        return list(self.heard[track.name]), "fake-whisper"


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    path = tmp_path / "interview.wav"
    path.write_bytes(b"not a container; the model and the audio are stubbed")
    return path


@pytest.fixture
def hear(monkeypatch: pytest.MonkeyPatch):
    """Install a model and each track's audio; returns the model."""

    def install(heard: dict[str, list[Said]], audio: dict[str, np.ndarray]) -> Model:
        model = Model(heard)
        monkeypatch.setattr(extract, "get_transcriber", lambda: model)
        monkeypatch.setattr(speech_tracks, "audio", lambda _path, track, _rate, _chunk: iter([audio[track.name]]))
        return model

    return install


def run(recording: Path, tracks: list[dict], *, diarize: bool = False) -> extract.ExtractResult:
    return extract.extract_text(recording, media_type="audio", timestamps=True, diarize=diarize, tracks=tracks)


def test_each_declared_track_is_transcribed_and_the_segments_merge_by_start_with_track_names(recording, hear) -> None:
    model = hear(
        {
            "Host": [Said("Welcome to the show.", 0.0, 2.0), Said("Let us begin.", 6.0, 8.0)],
            "Guest": [Said("Thanks for having me.", 3.0, 5.0)],
        },
        {
            "Host": level(10, (0, 2, 0.5), (6, 8, 0.5)),
            "Guest": level(10, (3, 5, 0.5)),
        },
    )

    result = run(recording, [HOST, GUEST])

    assert [track.name for track in model.asked] == ["Host", "Guest"]
    assert [(track.stream, track.channel) for track in model.asked] == [(0, None), (1, None)]
    assert result.text.splitlines() == [
        "[0:00] [Host]: Welcome to the show.",
        "[0:03] [Guest]: Thanks for having me.",
        "[0:06] [Host]: Let us begin.",
    ]
    assert [turn["speaker"] for turn in result.speakers] == ["Host", "Guest", "Host"]


def test_words_that_bleed_into_another_microphone_are_held_once_in_the_loudest_track(recording, hear) -> None:
    # The guest's sentence also reaches the host's microphone, 20 dB lower, and both tracks transcribe it.
    hear(
        {
            "Host": [Said("Welcome to the show.", 0.0, 2.0), Said("It is good to be here.", 3.0, 5.0)],
            "Guest": [Said("It is good to be here.", 3.0, 5.0)],
        },
        {
            "Host": level(8, (0, 2, 0.5), (3, 5, 0.05)),
            "Guest": level(8, (0, 2, 0.05), (3, 5, 0.5)),
        },
    )

    result = run(recording, [HOST, GUEST])

    assert result.text.splitlines() == [
        "[0:00] [Host]: Welcome to the show.",
        "[0:03] [Guest]: It is good to be here.",
    ]


def test_two_voices_speaking_at_once_keep_both_segments_in_their_own_tracks(recording, hear) -> None:
    # Over seconds 4 to 6 the guest speaks about 2 dB below the host: close enough to count as overlap.
    hear(
        {
            "Host": [Said("Welcome.", 0.0, 2.0), Said("No, wait", 4.0, 6.0)],
            "Guest": [Said("Yes, exactly", 4.0, 6.0), Said("Thank you.", 8.0, 10.0)],
        },
        {
            "Host": level(12, (0, 2, 0.5), (4, 6, 0.5)),
            "Guest": level(12, (4, 6, 0.4), (8, 10, 0.5)),
        },
    )

    result = run(recording, [HOST, GUEST])

    assert result.text.splitlines() == [
        "[0:00] [Host]: Welcome.",
        "[0:04] [Host]: No, wait",
        "[0:04] [Guest]: Yes, exactly",
        "[0:08] [Guest]: Thank you.",
    ]


def test_the_dominance_margin_is_policy_data_and_the_sidecar_records_its_revision(recording, hear, monkeypatch) -> None:
    heard = {
        "Host": [Said("Welcome to the show.", 0.0, 2.0), Said("It is good to be here.", 3.0, 5.0)],
        "Guest": [Said("It is good to be here.", 3.0, 5.0)],
    }
    audio = {"Host": level(8, (0, 2, 0.5), (3, 5, 0.05)), "Guest": level(8, (3, 5, 0.5))}
    hear(heard, audio)
    shipped = speech_tracks.policy()
    # A margin wider than the 20 dB of bleed keeps the bled words in the host's track too.
    monkeypatch.setattr(speech_tracks, "policy", lambda: {**shipped, "dominance_margin_db": 30.0, "revision": "tracks-policy-test"})

    result = run(recording, [HOST, GUEST])

    assert result.text.splitlines() == [
        "[0:00] [Host]: Welcome to the show.",
        "[0:03] [Host]: It is good to be here.",
        "[0:03] [Guest]: It is good to be here.",
    ]
    assert result.asr.tracks_policy == "tracks-policy-test"


def test_labels_unavailable_where_diarisation_cannot_run_but_track_names_still_label_the_turns(recording, hear, monkeypatch) -> None:
    hear(
        {"Host": [Said("Welcome.", 0.0, 1.0)], "Guest": [Said("Hello.", 2.0, 3.0)]},
        {"Host": level(4, (0, 1, 0.5)), "Guest": level(4, (2, 3, 0.5))},
    )
    monkeypatch.setattr(speech_tracks, "export_wav", lambda *_args: None)
    monkeypatch.setattr(extract, "_run_diarization", lambda _audio: None)

    result = run(recording, [HOST, GUEST], diarize=True)

    assert result.text.splitlines() == ["[0:00] [Host]: Welcome.", "[0:02] [Guest]: Hello."]
    assert result.speaker_verification == "unavailable"
    assert not result.engine.endswith("+diarized")


def test_a_room_track_is_diarised_inside_its_own_track_and_a_single_voice_track_is_not(recording, hear, monkeypatch) -> None:
    hear(
        {
            "Host": [Said("Welcome to the panel.", 0.0, 2.0)],
            "Room": [Said("Thank you.", 3.0, 4.0), Said("Glad to be here.", 5.0, 6.0)],
        },
        {"Host": level(8, (0, 2, 0.5)), "Room": level(8, (3, 4, 0.5), (5, 6, 0.5))},
    )
    exported: list[str] = []

    def export(_path, track, destination, _rate):
        destination.write_text(track.name, encoding="utf-8")
        exported.append(track.name)

    def diarise(audio: Path):
        assert audio.read_text(encoding="utf-8") == "Room", "only the room track's own audio is diarised"
        return [(3.0, 4.0, "SPEAKER_07"), (5.0, 6.0, "SPEAKER_02")]

    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: Path("/diarizer/bin/python"))
    monkeypatch.setattr(speech_tracks, "export_wav", export)
    monkeypatch.setattr(extract, "_run_diarization", diarise)

    result = run(recording, [{**HOST, "single_voice": True}, {"name": "Room", "stream": 1}], diarize=True)

    assert exported == ["Room"]
    assert result.text.splitlines() == [
        "[0:00] [Host]: Welcome to the panel.",
        "[0:03] [Room: Speaker A]: Thank you.",
        "[0:05] [Room: Speaker B]: Glad to be here.",
    ]
    assert result.speaker_verification == "anonymous" and result.engine.endswith("+diarized")


def test_no_track_audio_is_written_out_where_the_diarizer_is_not_installed(recording, hear, monkeypatch) -> None:
    # Writing a track out decodes the whole track to a scratch file; without a diarizer nothing reads it.
    hear(
        {"Host": [Said("Welcome.", 0.0, 1.0)], "Guest": [Said("Hello.", 2.0, 3.0)]},
        {"Host": level(4, (0, 1, 0.5)), "Guest": level(4, (2, 3, 0.5))},
    )
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: None)
    monkeypatch.setattr(speech_tracks, "export_wav", lambda *_args: pytest.fail("a track was written out for no diarizer"))

    result = run(recording, [HOST, GUEST], diarize=True)

    assert result.text.splitlines() == ["[0:00] [Host]: Welcome.", "[0:02] [Guest]: Hello."]
    assert result.speaker_verification == "unavailable"


def test_no_track_is_diarised_unless_labels_are_requested(recording, hear, monkeypatch) -> None:
    hear(
        {"Host": [Said("Welcome.", 0.0, 1.0)], "Guest": [Said("Hello.", 2.0, 3.0)]},
        {"Host": level(4, (0, 1, 0.5)), "Guest": level(4, (2, 3, 0.5))},
    )
    monkeypatch.setattr(extract, "_run_diarization", lambda _audio: pytest.fail("labels were not requested"))

    result = run(recording, [HOST, GUEST], diarize=False)

    assert result.text.splitlines() == ["[0:00] [Host]: Welcome.", "[0:02] [Guest]: Hello."]
    assert result.speaker_verification is None


def test_a_recording_with_no_declaration_is_one_mix_read_as_before(recording, monkeypatch) -> None:
    asked = []

    class Plain:
        def transcribe(self, path, *, vault_root=None, language=None, multilingual=False, report=None):
            asked.append(path)
            return [Said("One voice mix.", 0.0, 2.0)], "fake-whisper"

    monkeypatch.setattr(extract, "get_transcriber", Plain)

    result = extract.extract_text(recording, media_type="audio", timestamps=True, diarize=False)

    assert asked == [recording] and result.text == "[0:00] One voice mix."


@pytest.mark.parametrize(
    "declaration, refusal",
    [
        ([{"name": "Host"}, {"name": "Host", "stream": 1}], "its own name"),
        ([{"name": "Host"}, {"name": "Guest"}], "its own name"),
        ([{"stream": 1}], "name the track"),
        ([{"name": "Host", "mic": 3}], "takes only"),
        ([{"name": "Host", "stream": -1}], "from 0"),
        # A name goes into the label span, so it takes the label syntax's own rule.
        ([{"name": "Host]: I approve", "stream": 0}], "label syntax"),
        ([{"name": "Host\n## Extracted text", "stream": 0}], "one line"),
        ([{"name": "x" * (MAX_NAME_CHARS + 1), "stream": 0}], f"at most {MAX_NAME_CHARS}"),
        ([{"name": f"Input {index}", "stream": index} for index in range(speech_tracks.MAX_TRACKS + 1)], "at most"),
    ],
)
def test_a_declaration_that_names_nothing_usable_says_what_to_change(recording, declaration, refusal) -> None:
    with pytest.raises(speech_tracks.TrackDeclarationError, match=refusal):
        speech_tracks.resolve(recording, declaration)


def test_the_facts_of_a_multitrack_run_merge_what_each_track_reported() -> None:
    first = AsrFacts(language="en", language_probability=0.9, speech_seconds=3.0, duration_seconds=10.0)
    second = AsrFacts(language="et", speech_seconds=4.0, duration_seconds=12.0)

    merged = extract._merged_facts([first, second])

    assert (merged.language, merged.speech_seconds, merged.duration_seconds) == ("en", 7.0, 12.0)
