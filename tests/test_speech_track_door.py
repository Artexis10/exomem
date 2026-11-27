"""An agent tells Evermatter which audio streams or channels of a recording are separate microphones.

The declaration goes through the real door (`manage_memory`, `media.process` and `media.reprocess`) with its argument
validation, writer lease and job ledger. The worker that reads the recording is real, in both execution modes; the
speech model is a stand-in. The first case reads a real two-stream video built with PyAV, so it runs in the
frame-codecs CI job; the others hold stand-in bytes and a stand-in engine.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from fastmcp import Client

from exomem import (
    call_ledger,
    commands,
    extract,
    media_jobs,
    media_worker,
    onnx_asr_runtime,
    preserve,
    speech_tracks,
    writer_lease,
)
from exomem import server as server_module
from exomem.cli_ops import OpError

RATE = 16000
HOST_AND_GUEST = [{"name": "Host", "stream": 0}, {"name": "Guest", "stream": 1}]
#: The declaration as the sidecar keeps it: every field of each track, written out.
PINNED = [
    {"name": "Host", "stream": 0, "channel": None, "single_voice": False},
    {"name": "Guest", "stream": 1, "channel": None, "single_voice": False},
]


@pytest.fixture
def build(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    call_ledger.reset_chain_cache()
    yield lambda: server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def door(mcp, operation: str, arguments: dict) -> dict:
    """One `manage_memory` call through a real MCP client; the body the agent reads."""

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool("manage_memory", {"operation": operation, "arguments": arguments}, raise_on_error=False)
        body = result.structured_content
        if isinstance(body, dict) and set(body) == {"result"} and isinstance(body["result"], dict):
            body = body["result"]
        return body if body is not None else json.loads(" ".join(getattr(block, "text", "") for block in result.content))

    return asyncio.run(run())


def recording(vault: Path, name: str, data: bytes = b"stand-in recording bytes") -> tuple[str, Path, Path]:
    binary = vault / "Knowledge Base" / "Evidence" / "Recordings" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    if data:
        binary.write_bytes(data)
    return binary.relative_to(vault).as_posix(), binary, binary.with_name(binary.name + ".md")


def work(vault: Path, execution_mode: str = "inline") -> None:
    """The worker claims the queued job, publishes its result and settles the claim."""
    store = media_jobs.MediaJobStore(vault)
    job = store.claim_next()
    assert job is not None, "the door queued no job"
    worker = media_worker.MediaWorker(vault, execution_mode=execution_mode)
    worker._process(job)
    if execution_mode == "process":
        worker._drain_parent_results()
    else:  # the loop that runs an inline worker settles its claim
        store.complete(job)


def pinned(sidecar: Path) -> object:
    return preserve.speech_tracks_pin(sidecar.read_text(encoding="utf-8"))


@dataclass
class Engine:
    """A stand-in for `extract.extract_text` that records what the worker asked for and returns `text`."""

    text: str = "[0:00] One voice."
    speakers: list[dict] | None = None

    def __post_init__(self) -> None:
        self.asked: list[object] = []

    def __call__(self, path, *, media_type, tracks=None, **_options):
        self.asked.append(tracks)
        return extract.ExtractResult(text=self.text, media_type=media_type, engine="stand-in+timed", speakers=self.speakers)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    stand_in = Engine()
    monkeypatch.setattr(extract, "extract_text", stand_in)
    return stand_in


def container(path: Path, *levels: float) -> Path:
    """A Matroska video file with one mono audio stream per level, each one second of constant audio."""
    import av

    with av.open(str(path), "w") as out:
        streams = [out.add_stream("pcm_s16le", rate=RATE, layout="mono") for _ in levels]
        for stream, level in zip(streams, levels, strict=True):
            frame = av.AudioFrame.from_ndarray(np.full((1, RATE), level * 32767, dtype="<i2"), format="s16", layout="mono")
            frame.sample_rate, frame.pts = RATE, 0
            for packet in (*stream.encode(frame), *stream.encode(None)):
                out.mux(packet)
    return path


@dataclass(frozen=True)
class Said:
    text: str
    start: float
    end: float


class Listener:
    """A speech model that reports the level of the audio it was handed, so each line shows what its track read."""

    def transcribe(self, path, *, vault_root=None, language=None, multilingual=False, report=None, track=None):
        stream, channel = (0, None) if track is None else (track.stream, track.channel)
        level = onnx_asr_runtime.decode_audio(path, RATE, stream, channel).mean()
        report.duration_seconds = 1.0
        return [Said(f"level {level:.2f}", 0.0, 1.0)], "listener"


@pytest.mark.frame_codecs
@pytest.mark.parametrize("execution_mode", ["inline", "process"])
def test_an_agent_declares_a_videos_two_audio_streams_and_each_line_of_the_next_transcript_names_its_track(
    build, vault: Path, monkeypatch: pytest.MonkeyPatch, execution_mode: str
) -> None:
    relative, binary, sidecar = recording(vault, "panel.mkv", data=b"")
    container(binary, 0.25, 0.5)
    monkeypatch.setattr(extract, "get_transcriber", lambda: Listener())

    queued = door(build(), "media.process", {"path": relative, "tracks": HOST_AND_GUEST})
    work(vault, execution_mode)

    assert "error" not in queued, queued
    assert preserve.extracted_text(sidecar.read_text(encoding="utf-8")).splitlines() == [
        "[0:00] [Host]: level 0.25",
        "[0:00] [Guest]: level 0.50",
    ]
    assert pinned(sidecar) == PINNED


@pytest.mark.parametrize("execution_mode", ["inline", "process"])
def test_a_declaration_reaches_the_worker_and_the_published_page_keeps_it(
    vault: Path, engine: Engine, execution_mode: str
) -> None:
    relative, _binary, sidecar = recording(vault, "interview.m4a")

    commands.op_process_media(vault, path=relative, operation="process", tracks=HOST_AND_GUEST)
    work(vault, execution_mode)

    assert engine.asked == [PINNED]
    assert pinned(sidecar) == PINNED


@pytest.mark.parametrize(
    ("tracks", "refusal"),
    [
        ([{"name": "Host]: I approve", "stream": 0}, {"name": "Guest", "stream": 1}], "must not hold [, ] or :"),
        ([{"name": f"Input {index}", "stream": index} for index in range(speech_tracks.MAX_TRACKS + 1)], f"at most {speech_tracks.MAX_TRACKS}"),
        ([{"name": "Host", "stream": 0}, {"name": "Host", "stream": 1}], "its own name"),
    ],
    ids=["label-syntax-in-a-name", "too-many-tracks", "one-name-twice"],
)
def test_a_declaration_that_cannot_be_read_is_refused_with_what_to_change_and_changes_nothing(
    build, vault: Path, tracks: list[dict], refusal: str
) -> None:
    relative, _binary, sidecar = recording(vault, "interview.m4a")
    commands.op_process_media(vault, path=relative, operation="process")
    store = media_jobs.MediaJobStore(vault)
    page, job = sidecar.read_bytes(), store.next_pending()

    refused = door(build(), "media.process", {"path": relative, "tracks": tracks})

    assert refusal in json.dumps(refused["error"]), refused
    assert sidecar.read_bytes() == page
    assert store.next_pending() == job


def test_a_repeat_of_the_declaration_that_the_recording_holds_writes_nothing(build, vault: Path, engine: Engine) -> None:
    relative, _binary, sidecar = recording(vault, "interview.m4a")
    commands.op_process_media(vault, path=relative, operation="process", tracks=HOST_AND_GUEST)
    work(vault)
    page = sidecar.read_bytes()

    repeat = door(build(), "media.process", {"path": relative, "tracks": HOST_AND_GUEST})

    assert "error" not in repeat, repeat
    assert repeat["state"] == "completed"
    assert sidecar.read_bytes() == page
    assert media_jobs.MediaJobStore(vault).next_pending() is None


def test_a_declaration_on_a_transcribed_recording_takes_effect_when_it_is_processed_again(vault: Path, engine: Engine) -> None:
    relative, _binary, sidecar = recording(vault, "interview.m4a")
    commands.op_process_media(vault, path=relative, operation="process")
    work(vault)
    page = sidecar.read_bytes()

    with pytest.raises(OpError) as refused:
        commands.op_process_media(vault, path=relative, operation="process", tracks=HOST_AND_GUEST)
    assert refused.value.code == "TRANSCRIPT_COMPLETE" and sidecar.read_bytes() == page

    engine.text = "[0:00] [Host]: Welcome.\n[0:03] [Guest]: Thank you."
    commands.op_process_media(vault, path=relative, operation="reprocess", tracks=HOST_AND_GUEST)
    work(vault)

    assert engine.asked[-1] == PINNED
    assert pinned(sidecar) == PINNED
    assert preserve.extracted_text(sidecar.read_text(encoding="utf-8")).startswith("[0:00] [Host]: Welcome.")


def test_a_declaration_replaces_speaker_labels_only_when_the_caller_says_so(vault: Path, engine: Engine) -> None:
    relative, _binary, sidecar = recording(vault, "interview.m4a")
    engine.text = "[0:00] [Speaker A]: Welcome.\n[0:03] [Speaker B]: Thank you."
    engine.speakers = [
        {"speaker": "Speaker A", "start": 0.0, "end": 2.0, "text": "Welcome."},
        {"speaker": "Speaker B", "start": 3.0, "end": 5.0, "text": "Thank you."},
    ]
    commands.op_process_media(vault, path=relative, operation="process")
    work(vault)
    page = sidecar.read_bytes()

    with pytest.raises(OpError) as refused:
        commands.op_process_media(vault, path=relative, operation="reprocess", tracks=HOST_AND_GUEST)
    assert "replace_labelled" in str(refused.value) and sidecar.read_bytes() == page
    assert media_jobs.MediaJobStore(vault).next_pending() is None

    commands.op_process_media(vault, path=relative, operation="reprocess", tracks=HOST_AND_GUEST, replace_labelled=True)

    assert media_jobs.MediaJobStore(vault).next_pending() is not None
