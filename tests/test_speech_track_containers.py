"""Real container files decide which audio a track reads: streams, channels, stream titles and timecode.

The files are built in code with PyAV, so this module runs in the frame-codecs CI job. The speech model
is a stand-in that reports the level of the audio it was handed, so the transcript shows which stream or
channel each track really read. Segment placement and dominance are in `test_speech_tracks.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from exomem import extract, onnx_asr_runtime, speech_tracks

pytestmark = pytest.mark.frame_codecs

RATE = 16000


@dataclass(frozen=True)
class Said:
    text: str
    start: float
    end: float


def container(path: Path, *streams: tuple[str, tuple[float, ...]], tags: dict[str, str] | None = None) -> Path:
    """A Matroska file with one second of constant audio per stream: `(title, level of each channel)`."""
    import av

    layouts = {1: "mono", 2: "stereo"}
    with av.open(str(path), "w") as out:
        encoders = []
        for title, levels in streams:
            stream = out.add_stream("pcm_s16le", rate=RATE, layout=layouts[len(levels)])
            if title:
                stream.metadata["title"] = title
            encoders.append((stream, levels))
        for key, value in (tags or {}).items():
            out.metadata[key] = value
        for stream, levels in encoders:
            samples = np.repeat(np.array(levels) * 32767, RATE).reshape(len(levels), RATE).T.astype("<i2")
            frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout=layouts[len(levels)])
            frame.sample_rate, frame.pts = RATE, 0
            for packet in (*stream.encode(frame), *stream.encode(None)):
                out.mux(packet)
    return path


class Listener:
    """A speech model that reports the level of the audio it was handed, and which tracks it was asked for."""

    def __init__(self) -> None:
        self.asked: list[object] = []

    def transcribe(self, path, *, vault_root=None, language=None, multilingual=False, report=None, track=None):
        self.asked.append(track)
        stream, channel = (0, None) if track is None else (track.stream, track.channel)
        level = onnx_asr_runtime.decode_audio(path, RATE, stream, channel).mean()
        report.duration_seconds = 1.0
        return [Said(f"level {level:.2f}", 0.0, 1.0)], "listener"


@pytest.fixture
def listener(monkeypatch: pytest.MonkeyPatch) -> Listener:
    model = Listener()
    monkeypatch.setattr(extract, "get_transcriber", lambda: model)
    return model


def transcribe(path: Path, tracks: object = None) -> list[str]:
    return extract.extract_text(path, media_type="audio", timestamps=True, diarize=False, tracks=tracks).text.splitlines()


def test_two_declared_streams_are_each_read_from_their_own_stream(tmp_path: Path, listener: Listener) -> None:
    path = container(tmp_path / "panel.mkv", ("Host", (0.25,)), ("Guest", (0.5,)))

    lines = transcribe(path, [{"name": "Host", "stream": 0}, {"name": "Guest", "stream": 1}])

    assert lines == ["[0:00] [Host]: level 0.25", "[0:00] [Guest]: level 0.50"]


def test_an_undeclared_commentary_stream_beside_a_full_mix_stays_out(tmp_path: Path, listener: Listener) -> None:
    # Stream titles name streams, they do not say that a stream is a speaker's microphone.
    path = container(tmp_path / "match.mkv", ("Full mix", (0.25,)), ("Commentary", (0.5,)))

    lines = transcribe(path)

    assert lines == ["[0:00] level 0.25"]
    assert listener.asked == [None]


def test_an_undeclared_stereo_file_is_one_mix_of_both_channels(tmp_path: Path, listener: Listener) -> None:
    path = container(tmp_path / "stereo.mkv", ("", (0.25, 0.5)))

    lines = transcribe(path)

    assert listener.asked == [None]
    level = float(lines[0].removeprefix("[0:00] level "))
    assert 0.25 < level and level != 0.5, "a blend of the two channels, not either channel alone"


def test_a_declared_dual_mono_file_reads_each_channel_as_its_own_track(tmp_path: Path, listener: Listener) -> None:
    path = container(tmp_path / "dual.mkv", ("", (0.25, 0.5)))

    lines = transcribe(path, [{"name": "Left mic", "channel": 0}, {"name": "Right mic", "channel": 1}])

    assert lines == ["[0:00] [Left mic]: level 0.25", "[0:00] [Right mic]: level 0.50"]


def test_a_stream_title_declares_no_track_until_the_data_says_a_title_is_a_microphone(tmp_path: Path) -> None:
    path = container(tmp_path / "titled.mkv", ("Host", (0.25,)), ("Guest", (0.5,)))
    shipped = speech_tracks.policy()
    titles_are_microphones = {
        **shipped,
        "families": [{**family, "declares_microphone": True} for family in shipped["families"]],
    }

    published = speech_tracks.read_published(path)
    resolved = speech_tracks.resolve(path)
    resolved_with_data = speech_tracks.resolve(path, data=titles_are_microphones)

    assert [t.name for t in published.tracks] == ["Host", "Guest"]
    assert resolved.tracks == ()
    assert [(t.name, t.stream) for t in resolved_with_data.tracks] == [("Host", 0), ("Guest", 1)]


def test_container_timecode_is_the_recordings_start_in_seconds(tmp_path: Path) -> None:
    path = container(tmp_path / "timed.mkv", ("", (0.25,)), tags={"timecode": "01:02:03:00"})

    assert speech_tracks.resolve(path).start == speech_tracks.Start("container-timecode", 3723.0)


def test_a_declared_stream_the_file_does_not_have_says_how_many_it_has(tmp_path: Path) -> None:
    path = container(tmp_path / "one.mkv", ("", (0.25,)))

    with pytest.raises(speech_tracks.TrackUnavailable, match="the file has 1 audio streams; stream 3 does not exist"):
        list(speech_tracks.audio(path, speech_tracks.Track("Ghost", stream=3), RATE, RATE))


def test_a_declared_track_the_file_does_not_have_is_skipped_and_the_reason_is_kept(tmp_path: Path, listener: Listener) -> None:
    path = container(tmp_path / "pair.mkv", ("", (0.25,)), ("", (0.5,)))
    declared = [{"name": "Host", "stream": 0}, {"name": "Guest", "stream": 1}, {"name": "Ghost", "stream": 3}]

    result = extract.extract_text(path, media_type="audio", timestamps=True, diarize=False, tracks=declared)

    assert result.text.splitlines() == ["[0:00] [Host]: level 0.25", "[0:00] [Guest]: level 0.50"]
    assert "Ghost" in result.asr.tracks_skipped and "stream 3 does not exist" in result.asr.tracks_skipped


def test_a_recording_that_has_none_of_its_declared_tracks_is_read_as_its_first_stream(tmp_path: Path, listener: Listener) -> None:
    path = container(tmp_path / "one.mkv", ("", (0.25,)))
    declared = [{"name": "Ghost", "stream": 3}, {"name": "Phantom", "channel": 1}]

    result = extract.extract_text(path, media_type="audio", timestamps=True, diarize=False, tracks=declared)

    assert result.text.splitlines() == ["[0:00] level 0.25"]
    assert listener.asked[-1] is None
    assert "Ghost" in result.asr.tracks_skipped and "Phantom" in result.asr.tracks_skipped
