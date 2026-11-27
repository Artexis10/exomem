"""The tracks of a recording: what a person declares and what the file publishes about its streams.

A track is one audio stream or one channel of a stream that is declared a separate microphone.
The audio never declares one (a full mix would pass for a speaker's track), so a recording with
no declaration is one mixed track, read from its first audio stream. A declaration is the
sidecar's `speech_tracks` field, or published fields that the speech data maps to a microphone
(`media_models.json`, `speech.tracks`). The shipped data maps none: iXML and stream titles name
tracks, and a stereo mix is named the same way. A recorder family that writes its facts in its own
fields is one more entry in that data, and no code changes.

A published name comes from the upload, so it labels a track only where it can stand in a label span
(`speaker_names.name_problem`); a track whose name cannot is read in the mix.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: One iXML chunk is a few kilobytes; a larger one is not a track list.
_MAX_IXML_BYTES = 1 << 20
#: `bext` holds 602 bytes before its coding history; the start sits at byte 338.
_BEXT_TIME_REFERENCE_AT = 338
#: A WAVE file with more chunks than this is not a recording that a recorder wrote.
_MAX_CHUNKS = 10_000
# SMPTE timecode is a closed published format (hours:minutes:seconds, then frames after `:` or `;`).
_TIMECODE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})[:;](\d{2,3})$")


class TrackDeclarationError(ValueError):
    """A declaration of tracks that names nothing usable; the message says what to change."""


class TrackUnavailable(ValueError):
    """A declared stream or channel that the file does not have."""


@dataclass(frozen=True)
class Track:
    name: str
    #: The index among the file's audio streams.
    stream: int = 0
    #: A channel of the stream; None reads the stream as one mix.
    channel: int | None = None
    #: A declared single-voice track is not diarised: its name labels its turns.
    single_voice: bool = False

    @property
    def key(self) -> str:
        """What audio the track reads, so that its progress follows the audio and not its name."""
        return f"{self.stream}:{'mix' if self.channel is None else self.channel}"


@dataclass(frozen=True)
class PublishedTrack:
    #: None where the file publishes no name, or one that cannot stand in a label span (`name_refused`).
    name: str | None
    stream: int
    channel: int | None
    declares_microphone: bool
    single_voice: bool
    name_refused: bool = False


@dataclass(frozen=True)
class Start:
    """When a recording began, by one published field; two starts compare only when their `kind` matches."""

    kind: str
    seconds: float


@dataclass(frozen=True)
class Published:
    tracks: tuple[PublishedTrack, ...] = ()
    start: Start | None = None


def policy() -> dict[str, Any]:
    from . import media_models

    return media_models.model_spec("speech")["tracks"]


def _riff_chunks(path: Path) -> dict[bytes, bytes]:
    """The `fmt `, `bext` and `iXML` chunks of a WAVE file; empty for any other file.

    Reads chunk headers and skips the audio, so a long recording costs a few seeks. RF64 keeps the
    size of a chunk over 4 GiB in its `ds64` table.
    """
    wanted = {b"fmt ": 64, b"bext": 4096, b"iXML": _MAX_IXML_BYTES}
    found: dict[bytes, bytes] = {}
    try:
        with path.open("rb") as handle:
            head = handle.read(12)
            if head[:4] not in (b"RIFF", b"RF64", b"BW64") or head[8:12] != b"WAVE":
                return {}
            data_size: int | None = None
            for _ in range(_MAX_CHUNKS):
                header = handle.read(8)
                if len(header) < 8:
                    break
                chunk, size = header[:4], int.from_bytes(header[4:], "little")
                if chunk == b"ds64":
                    body = handle.read(min(size, 28))
                    data_size = int.from_bytes(body[8:16], "little")
                    handle.seek(size - len(body) + (size & 1), 1)
                    continue
                if chunk == b"data" and size == 0xFFFFFFFF and data_size is not None:
                    size = data_size
                body = handle.read(min(size, wanted[chunk])) if chunk in wanted else b""
                if chunk in wanted:
                    found[chunk] = body
                handle.seek(size - len(body) + (size & 1), 1)
    except OSError:
        return {}
    return found


def _published_track(text: str, stream: int, channel: int | None, family: Mapping[str, Any]) -> PublishedTrack:
    """A track that one published field lists; its name labels it only where the name rule admits it."""
    # `extract` imports this module, so the label grammar loads only when a name is checked.
    from .speaker_names import name_problem

    name = text.strip()
    usable = bool(name) and name_problem(name) is None
    return PublishedTrack(
        name if usable else None, stream, channel, bool(family["declares_microphone"]), bool(family["single_voice"]),
        name_refused=bool(name) and not usable,
    )


def _ixml_tracks(ixml: bytes, family: Mapping[str, Any], channels: int | None) -> list[PublishedTrack]:
    """The tracks that one iXML family lists at a place the file has; an unreadable packet lists none."""
    try:
        # An iXML chunk comes from an upload, so a parser that refuses entity expansion reads it.
        from defusedxml import ElementTree

        root = ElementTree.fromstring(ixml.rstrip(b"\x00").strip())
    except Exception:  # noqa: BLE001 - defusedxml raises its own refusals beside ParseError; no parse means no tracks
        return []
    found: list[PublishedTrack] = []
    for entry in root.findall(family["track"]):
        index = (entry.findtext(family["channel"]) or "").strip()
        if not index.isdigit() or int(index) < 1 or (channels is not None and int(index) > channels):
            continue
        # iXML counts from one; a track names a channel from zero, as the decoder does.
        found.append(_published_track(entry.findtext(family["name"]) or "", 0, int(index) - 1, family))
    return found


def _container_facts(path: Path) -> tuple[list[dict[str, str]], dict[str, str], float | None]:
    """Each audio stream's tags, the container's tags and the first video stream's frame rate."""
    try:
        import av

        with av.open(str(path)) as container:
            streams = [dict(stream.metadata) for stream in container.streams.audio]
            rate = next((float(s.average_rate) for s in container.streams.video if s.average_rate), None)
            return streams, dict(container.metadata), rate
    except Exception:  # noqa: BLE001 - a file that PyAV cannot open publishes no stream facts
        return [], {}, None


def _tag(tags: Mapping[str, str], name: str) -> str:
    """A tag's text; containers differ in the case of a tag's key (Matroska upper-cases `timecode`)."""
    return next((value for key, value in tags.items() if key.lower() == name.lower()), "").strip()


def _timecode_seconds(text: str, frame_rate: float | None) -> float | None:
    found = _TIMECODE.match(text.strip())
    if found is None:
        return None
    hours, minutes, seconds, frames = (int(part) for part in found.groups())
    # Frames count only against a known frame rate; an audio file states none, so its frames add nothing.
    return hours * 3600 + minutes * 60 + seconds + (frames / frame_rate if frame_rate else 0.0)


def read_published(path: Path, data: Mapping[str, Any] | None = None) -> Published:
    """What the file publishes about its tracks and its start, as the shipped speech data maps it."""
    data = policy() if data is None else data
    chunks = _riff_chunks(path)
    streams, container_tags, frame_rate = _container_facts(path)
    fmt = chunks.get(b"fmt ", b"")
    channels = int.from_bytes(fmt[2:4], "little") if len(fmt) >= 4 else None
    listed: list[PublishedTrack] = []
    for family in data["families"]:
        if family["kind"] == "ixml" and b"iXML" in chunks:
            listed.extend(_ixml_tracks(chunks[b"iXML"], family, channels))
        elif family["kind"] == "stream_tag":
            for index, tags in enumerate(streams):
                if text := _tag(tags, family["tag"]):
                    listed.append(_published_track(text, index, None, family))
    # One entry per audio, the first to list it; one past the bound is enough to say the list is too long.
    tracks: dict[tuple[int, int | None], PublishedTrack] = {}
    for track in listed:
        if len(tracks) > MAX_TRACKS:
            break
        tracks.setdefault((track.stream, track.channel), track)
    start: Start | None = None
    for source in data["start_sources"]:
        seconds: float | None = None
        if source["kind"] == "bext_time_reference":
            bext, fmt = chunks.get(b"bext", b""), chunks.get(b"fmt ", b"")
            if len(bext) >= _BEXT_TIME_REFERENCE_AT + 8 and len(fmt) >= 8:
                rate = int.from_bytes(fmt[4:8], "little")
                samples = int.from_bytes(bext[_BEXT_TIME_REFERENCE_AT:_BEXT_TIME_REFERENCE_AT + 8], "little")
                seconds = samples / rate if rate else None
        elif source["kind"] == "timecode_tag":
            text = next((found for tags in (container_tags, *streams) if (found := _tag(tags, source["tag"]))), "")
            seconds = _timecode_seconds(text, frame_rate)
        if seconds is not None:
            start = Start(source["key"], seconds)
            break
    return Published(_unique(list(tracks.values())), start)


def _unique(tracks: Sequence[PublishedTrack]) -> tuple[PublishedTrack, ...]:
    """Published names are not unique by contract; a repeat takes the first count that no track holds."""
    held = {track.name for track in tracks if track.name is not None}
    seen: set[str] = set()
    out: list[PublishedTrack] = []
    for track in tracks:
        name = track.name
        if name is not None and name in seen:
            base, count = name, 2
            while (name := _numbered(base, count)) in held:
                count += 1
            held.add(name)
        if name is not None:
            seen.add(name)
        out.append(replace(track, name=name))
    return tuple(out)


def _numbered(name: str, count: int) -> str:
    """`name` with a count, cut so that the label stays within the label length."""
    from .speaker_names import MAX_NAME_CHARS

    suffix = f" {count}"
    return f"{name[:MAX_NAME_CHARS - len(suffix)].rstrip()}{suffix}"


# The keys of a declaration are this module's own schema (the `Track` fields), a closed set that code implements.
_DECLARATION_KEYS = {"name", "stream", "channel", "single_voice"}
#: Each track is one full transcription of the recording, so this bounds what one recording costs.
MAX_TRACKS = 16


def declaration(value: object, published: Published | None = None) -> tuple[Track, ...]:
    """Tracks from a declaration, a list of `{name, stream, channel, single_voice}`.

    A track that names no `name` takes the one the file publishes for its stream and channel. A name
    goes into the label span, so a name that the label syntax refuses refuses the declaration.
    """
    if value is None:
        return ()
    from .speaker_names import name_problem

    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise TrackDeclarationError("`speech_tracks` is a list of tracks, each with a name, a stream and a channel")
    if len(value) > MAX_TRACKS:
        raise TrackDeclarationError(f"declare at most {MAX_TRACKS} tracks; each track is one full transcription")
    named = {(t.stream, t.channel): t.name for t in (published.tracks if published else ()) if t.name is not None}
    refused = {(t.stream, t.channel) for t in (published.tracks if published else ()) if t.name_refused}
    tracks: list[Track] = []
    for entry in value:
        if isinstance(entry, Track):
            tracks.append(entry)
            continue
        if not isinstance(entry, Mapping) or set(entry) - _DECLARATION_KEYS:
            raise TrackDeclarationError(
                f"a declared track takes only {', '.join(sorted(_DECLARATION_KEYS))}; change {entry!r}"
            )
        stream, channel = entry.get("stream", 0), entry.get("channel")
        if isinstance(stream, bool) or not isinstance(stream, int) or stream < 0:
            raise TrackDeclarationError("`stream` counts audio streams from 0; give a whole number")
        if channel is not None and (isinstance(channel, bool) or not isinstance(channel, int) or channel < 0):
            raise TrackDeclarationError("`channel` counts channels from 0; give a whole number or leave it out")
        single = entry.get("single_voice", False)
        if not isinstance(single, bool):
            raise TrackDeclarationError("`single_voice` is true or false")
        name = entry.get("name") or named.get((stream, channel))
        if not isinstance(name, str) or not name.strip():
            unnamed = (
                "" if published is None
                else "; the name the file publishes for it cannot label a line" if (stream, channel) in refused
                else "; the file publishes no name for it"
            )
            raise TrackDeclarationError(f"name the track on stream {stream} channel {channel}{unnamed}")
        if problem := name_problem(name.strip(), "a track's name"):
            raise TrackDeclarationError(f"{problem}; change the name of the track on stream {stream} channel {channel}")
        tracks.append(Track(name.strip(), stream, channel, single))
    names = [t.name for t in tracks]
    audio = [(t.stream, t.channel) for t in tracks]
    if len(set(names)) != len(names) or len(set(audio)) != len(audio):
        raise TrackDeclarationError("each declared track needs its own name and its own stream and channel")
    return tuple(tracks)


def as_data(tracks: Sequence[Track]) -> list[dict[str, Any]]:
    """A declaration as the sidecar and the job store keep it: every field of each track, so two compare by value."""
    return [asdict(track) for track in tracks]


@dataclass(frozen=True)
class Resolved:
    tracks: tuple[Track, ...]
    start: Start | None


def resolve(path: Path, declared: object = None, data: Mapping[str, Any] | None = None) -> Resolved:
    """The tracks of a recording: its declaration, else the tracks that the file's published fields declare.

    A declaration replaces the published list whole and takes names from it only where it names none.
    No declaration and no published microphones leave no tracks: the first audio stream is read as one mix.
    An empty declaration reads that mix too, whatever the file publishes.
    """
    published = read_published(path, data)
    tracks = declaration(declared, published)
    microphones = [t for t in published.tracks if t.declares_microphone]
    # One published microphone is the recording itself, and a track with no usable name cannot be labelled.
    # Either way the mix is read, which loses no track's words.
    if declared is None and 2 <= len(microphones) <= MAX_TRACKS and all(t.name is not None for t in microphones):
        tracks = tuple(Track(t.name, t.stream, t.channel, t.single_voice) for t in microphones if t.name is not None)
    return Resolved(tracks, published.start)


def audio(path: Path, track: Track, sample_rate: int, chunk_samples: int):
    """The track's audio as mono float32 chunks, from its own stream or channel."""
    from . import onnx_asr_runtime

    return onnx_asr_runtime.audio_chunks(path, sample_rate, chunk_samples, track.stream, track.channel)


def export_wav(path: Path, track: Track, destination: Path, sample_rate: int) -> None:
    """Write the track's audio as a 16-bit mono WAVE file, so that a tool that reads a whole file can diarise it."""
    import wave

    import numpy as np

    with wave.open(str(destination), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        for chunk in audio(path, track, sample_rate, sample_rate * 30):
            out.writeframes((np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2").tobytes())
