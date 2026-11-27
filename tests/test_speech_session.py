"""Separate recordings of one event are grouped as one session page that stays current.

Each recording is processed and given its transcript through the door, as an agent does, then the
recordings are grouped. The page is read back from disk. Original files are built in code: a WAVE
container with the `bext` chunk a field recorder writes, so the alignment reads the real fields.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import (
    commands,
    extract,
    media_jobs,
    media_worker,
    note_types,
    speech_attach,
    speech_session,
)
from exomem.cli_ops import OpError
from exomem.vault import parse_frontmatter

RATE = 48000
HOST_SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Welcome to the show."},
    {"start": 8.0, "end": 12.0, "text": "Let us begin."},
]
GUEST_SEGMENTS = [{"start": 0.0, "end": 4.0, "text": "Thanks for having me."}]


@pytest.fixture(autouse=True)
def _states_its_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech_attach, "container_duration", lambda _path: 120.0)


def wave(time_reference: int | None, marker: int) -> bytes:
    """A WAVE file; `marker` makes the bytes differ, so each recording has its own identity."""
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
    bext = b""
    if time_reference is not None:
        body = b"\x00" * 338 + struct.pack("<Q", time_reference) + b"\x00" * 256
        bext = b"bext" + struct.pack("<I", len(body)) + body
    data = b"data" + struct.pack("<I", 4) + struct.pack("<i", marker)
    body = b"WAVE" + fmt + bext + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def recording(vault: Path, name: str, segments: list[dict], *, time_reference: int | None = None, turns=None) -> str:
    """A processed recording with an attached transcript; returns its path."""
    payload = wave(time_reference, len(name))
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(payload)
    relative = binary.relative_to(vault).as_posix()
    commands.op_process_media(vault, path=relative, operation="process")
    commands.op_process_media(
        vault, path=relative, operation="attach-speech", binary_sha256=hashlib.sha256(payload).hexdigest(),
        binary_size=len(payload), segments=segments, turns=turns or [], engine="whisper",
    )
    return relative


def group(vault: Path, members: dict[str, str], title: str = "Panel night") -> dict:
    return commands.op_process_media(vault, operation="group-session", members=members, title=title)


def read(vault: Path, relative: str) -> tuple[dict, str]:
    fields, body, _raw = parse_frontmatter((vault / relative).read_text(encoding="utf-8"))
    return fields, body


def transcript(body: str) -> list[str]:
    section = body.split("## Transcript", 1)[1].split("<!--", 1)[0]
    return [line for line in section.splitlines() if line.strip()]


def test_two_recordings_with_a_shared_recorder_clock_become_one_aligned_page_labelled_by_track(vault: Path) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS, time_reference=RATE * 3600)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS, time_reference=RATE * 3600 + RATE * 5)

    reply = group(vault, {"Host": host, "Guest": guest})

    fields, body = read(vault, reply["session_path"])
    assert reply["outcome"] == "grouped" and fields["alignment"] == "bwf-time-reference"
    assert fields["track_offsets"] == {"Host": 0.0, "Guest": 5.0}
    # The guest's file starts five seconds after the host's, so its first line lands at 0:05.
    assert transcript(body) == [
        "[0:00] [Host]: Welcome to the show.",
        "[0:05] [Guest]: Thanks for having me.",
        "[0:08] [Host]: Let us begin.",
    ]


def test_recordings_with_no_shared_time_are_aligned_by_their_start_and_every_line_stays(vault: Path) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS, time_reference=RATE * 3600)
    guest = recording(vault, "guest.wav", HOST_SEGMENTS)  # this file publishes no start

    reply = group(vault, {"Host": host, "Guest": guest})

    fields, body = read(vault, reply["session_path"])
    assert fields["alignment"] == "assumed-start" and fields["track_offsets"] == {"Host": 0.0, "Guest": 0.0}
    # The same words at the same times in both tracks are each kept: no rule drops one as bleed.
    assert transcript(body) == [
        "[0:00] [Host]: Welcome to the show.",
        "[0:00] [Guest]: Welcome to the show.",
        "[0:08] [Host]: Let us begin.",
        "[0:08] [Guest]: Let us begin.",
    ]
    assert "share no published start time" in body


def test_the_page_has_the_type_the_registry_marks_and_links_each_member_with_its_track(vault: Path) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS)

    reply = group(vault, {"Host": host, "Guest": guest})

    (marked,) = note_types.shipped(note_types.session_transcript)
    fields, _body = read(vault, reply["session_path"])
    assert fields["type"] == marked.key
    assert reply["session_path"].startswith(f"Knowledge Base/{marked.folder}/")
    assert fields["sources"] == [
        "[[Knowledge Base/Evidence/Audio/host.wav|Host]]",
        "[[Knowledge Base/Evidence/Audio/guest.wav|Guest]]",
    ]


def test_code_finds_the_session_type_by_its_attribute_and_not_by_its_key(vault: Path, monkeypatch) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS)
    (marked,) = note_types.shipped(note_types.session_transcript)
    renamed = replace(marked, key="meeting-log", folder="Notes/Meetings")
    monkeypatch.setattr(note_types, "shipped", lambda predicate=None: (renamed,) if predicate is note_types.session_transcript else ())

    reply = group(vault, {"Host": host, "Guest": guest})

    fields, _body = read(vault, reply["session_path"])
    assert fields["type"] == "meeting-log"
    assert reply["session_path"].startswith("Knowledge Base/Notes/Meetings/")


def test_a_repeat_of_the_same_call_writes_nothing_and_a_regroup_keeps_the_owners_section(vault: Path) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS)
    first = group(vault, {"Host": host, "Guest": guest})
    page = vault / first["session_path"]
    page.write_text(page.read_text(encoding="utf-8").rstrip("\n") + "\n\nMy own notes about the night.\n", encoding="utf-8")
    before = page.read_text(encoding="utf-8")

    repeat = group(vault, {"Host": host, "Guest": guest})
    assert repeat["outcome"] == "unchanged" and page.read_text(encoding="utf-8") == before

    regrouped = group(vault, {"Presenter": host, "Guest": guest})

    assert regrouped["outcome"] == "regrouped"
    assert "My own notes about the night." in page.read_text(encoding="utf-8")
    assert "[0:00] [Presenter]: Welcome to the show." in page.read_text(encoding="utf-8")


def test_a_member_rename_updates_its_session_page_and_reads_no_other_session_page(vault: Path, monkeypatch) -> None:
    turns = [{"start": 0.0, "end": 4.0, "label": "SPEAKER_00"}, {"start": 8.0, "end": 12.0, "label": "SPEAKER_01"}]
    host = recording(vault, "host.wav", HOST_SEGMENTS, turns=turns)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS)
    other_a = recording(vault, "other-a.wav", GUEST_SEGMENTS)
    other_b = recording(vault, "other-b.wav", GUEST_SEGMENTS)
    ours = group(vault, {"Host": host, "Guest": guest}, "Panel night")["session_path"]
    elsewhere = group(vault, {"One": other_a, "Two": other_b}, "Another night")["session_path"]
    page = vault / ours
    page.write_text(page.read_text(encoding="utf-8").rstrip("\n") + "\n\nMy own notes about the night.\n", encoding="utf-8")
    assert "[0:00] [Host: Speaker A]: Welcome to the show." in page.read_text(encoding="utf-8")
    read_pages: list[str] = []
    real = speech_session.read_guarded_text

    def spy(vault_root: Path, path: Path):
        read_pages.append(path.relative_to(vault_root).as_posix())
        return real(vault_root, path)

    monkeypatch.setattr(speech_session, "read_guarded_text", spy)

    reply = commands.op_process_media(vault, path=host, operation="rename-speakers", renames={"Speaker A": "Alice"})

    text = page.read_text(encoding="utf-8")
    assert "[0:00] [Host: Alice]: Welcome to the show." in text
    assert "My own notes about the night." in text
    assert reply["sessions"] == {"updated": [ours]}
    assert elsewhere not in read_pages and ours in read_pages


@pytest.mark.parametrize("execution_mode", ["inline", "process"])
def test_a_member_the_worker_transcribes_again_updates_its_session_page(vault: Path, monkeypatch, execution_mode) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS)
    ours = group(vault, {"Host": host, "Guest": guest})["session_path"]
    commands.op_process_media(vault, path=host, operation="reprocess")
    job = media_jobs.MediaJobStore(vault).claim_next()
    monkeypatch.setattr(
        extract, "extract_text",
        lambda _path, *, media_type, **_options: extract.ExtractResult(
            text="[0:00] Welcome back.", media_type=media_type, engine="test+timed"
        ),
    )

    worker = media_worker.MediaWorker(vault, execution_mode=execution_mode)
    worker._process(job)
    if execution_mode == "process":
        worker._drain_parent_results()  # a child's result reaches the page only when the parent publishes it

    _fields, body = read(vault, ours)
    assert transcript(body) == ["[0:00] [Host]: Welcome back.", "[0:00] [Guest]: Thanks for having me."]


def test_a_session_page_that_cannot_be_rendered_again_is_reported_stale_and_a_repeat_of_the_call_repairs_it(
    vault: Path, monkeypatch
) -> None:
    turns = [{"start": 0.0, "end": 4.0, "label": "SPEAKER_00"}]
    host = recording(vault, "host.wav", HOST_SEGMENTS[:1], turns=turns)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS)
    ours = group(vault, {"Host": host, "Guest": guest})["session_path"]
    real = speech_session.render_current

    def fails(*_args, **_kwargs):
        raise RuntimeError("the page cannot be rendered")

    monkeypatch.setattr(speech_session, "render_current", fails)
    renamed = commands.op_process_media(vault, path=host, operation="rename-speakers", renames={"Speaker A": "Alice"})
    monkeypatch.setattr(speech_session, "render_current", real)

    assert renamed["outcome"] != "unchanged" and renamed["sessions"] == {"stale": [ours]}
    assert "Speaker A" in read(vault, ours)[1]
    repaired = group(vault, {"Host": host, "Guest": guest})
    assert repaired["outcome"] == "regrouped" and "[0:00] [Host: Alice]: Welcome to the show." in read(vault, ours)[1]


def test_a_recording_that_changes_without_a_session_refreshes_nothing(vault: Path, monkeypatch) -> None:
    turns = [{"start": 0.0, "end": 4.0, "label": "SPEAKER_00"}]
    host = recording(vault, "host.wav", HOST_SEGMENTS[:1], turns=turns)
    monkeypatch.setattr(speech_session, "read_guarded_text", lambda *_args: pytest.fail("no session page links this recording"))

    reply = commands.op_process_media(vault, path=host, operation="rename-speakers", renames={"Speaker A": "Alice"})

    assert "sessions" not in reply


@pytest.mark.parametrize(
    "tracks, title, refusal",
    [
        (["Host"], "Night", "groups 2 to 16"),
        (["Host: 1", "Guest"], "Night", "track 'Host: 1'"),
        (["Host|1", "Guest"], "Night", "member link uses"),
        (["Host", "Guest"], "two\nlines", "one line"),
    ],
    ids=["one-member", "colon-in-track", "bar-in-track", "two-line-title"],
)
def test_a_call_that_names_no_usable_session_says_what_to_change(vault: Path, tracks, title, refusal) -> None:
    paths = [recording(vault, "host.wav", HOST_SEGMENTS), recording(vault, "guest.wav", GUEST_SEGMENTS)]

    with pytest.raises(OpError, match=refusal):
        group(vault, dict(zip(tracks, paths, strict=False)), title)


def test_two_tracks_that_name_one_recording_are_refused(vault: Path) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS)

    with pytest.raises(OpError, match="its own recording"):
        group(vault, {"Host": host, "Presenter": host})


def test_a_title_that_names_another_page_is_refused_and_the_page_is_not_touched(vault: Path) -> None:
    host = recording(vault, "host.wav", HOST_SEGMENTS)
    guest = recording(vault, "guest.wav", GUEST_SEGMENTS)
    taken = vault / "Knowledge Base" / "Notes" / "Sessions" / "panel-night.md"
    taken.parent.mkdir(parents=True)
    taken.write_text("---\ntype: insight\n---\n# Panel night\n\nMy own page.\n", encoding="utf-8")

    with pytest.raises(OpError, match="not a session page"):
        group(vault, {"Host": host, "Guest": guest})

    assert taken.read_text(encoding="utf-8").endswith("My own page.\n")
