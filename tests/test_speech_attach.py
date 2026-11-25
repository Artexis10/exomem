"""Attaching a speech result that the owner's agent computed elsewhere to a recording's Source.

The recording is a real Source on disk with a real job ledger. Only the container's duration is
stated by the test, because the fake audio bytes have no container to read.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import commands, media_jobs, preserve, speech_attach
from exomem.cli_ops import OpError
from exomem.vault import parse_frontmatter

DURATION = 120.0
PAYLOAD = b"fake audio bytes, no container"
SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Welcome back to the show."},
    {"start": 4.0, "end": 9.0, "text": "Thanks for having me."},
    {"start": 9.0, "end": 14.0, "text": "Let us start with the move."},
]
TURNS = [
    {"start": 0.0, "end": 4.0, "label": "SPEAKER_00"},
    {"start": 4.0, "end": 9.0, "label": "SPEAKER_01"},
    {"start": 9.0, "end": 14.0, "label": "SPEAKER_00"},
]


@pytest.fixture(autouse=True)
def _states_its_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech_attach, "container_duration", lambda _path: DURATION)


def _recording(vault: Path, name: str = "interview.m4a", payload: bytes = PAYLOAD) -> tuple[str, Path]:
    """A recording with its pending Source page; returns its vault path and the page."""
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(payload)
    relative = binary.relative_to(vault).as_posix()
    commands.op_process_media(vault, path=relative, operation="process")
    return relative, binary.with_name(binary.name + ".md")


def _attach(vault: Path, relative: str, payload: bytes = PAYLOAD, **fields) -> dict:
    call = {
        "binary_sha256": hashlib.sha256(payload).hexdigest(), "binary_size": len(payload),
        "segments": SEGMENTS, "engine": "whisper-large-v3 + pyannote-3.1", **fields,
    }
    return commands.op_process_media(vault, path=relative, operation="attach-speech", **call)


def _fields(page: Path) -> dict:
    return parse_frontmatter(page.read_text(encoding="utf-8"))[0]


def test_an_attach_fills_a_pending_source_with_a_labelled_transcript(vault: Path) -> None:
    relative, page = _recording(vault)

    reply = _attach(vault, relative, turns=TURNS)

    fields = _fields(page)
    body = page.read_text(encoding="utf-8")
    assert reply["outcome"] == "attached" and reply["state"] == "completed"
    assert fields["extraction_origin"] == "attached"
    assert fields["extracted_by"] == "whisper-large-v3 + pyannote-3.1"
    assert fields["speakers"] == ["Speaker A", "Speaker B"]
    assert fields["speaker_verification"] == "anonymous"
    assert "[0:00] [Speaker A]: Welcome back to the show." in body
    assert "[0:04] [Speaker B]: Thanks for having me." in body
    assert fields["asr_duration_seconds"] == DURATION


def test_an_attach_without_turns_stores_unlabelled_timed_lines(vault: Path) -> None:
    relative, page = _recording(vault)

    _attach(vault, relative)

    fields = _fields(page)
    assert "[0:04] Thanks for having me." in page.read_text(encoding="utf-8")
    assert "speakers" not in fields and fields["speaker_verification"] == "unavailable"


def test_a_name_in_the_call_labels_its_speaker_and_a_person_who_names_all_verifies(vault: Path) -> None:
    relative, page = _recording(vault)
    names = [
        {"label": "SPEAKER_00", "name": "Host", "basis": "person"},
        {"label": "SPEAKER_01", "name": "Guest", "basis": "person"},
    ]

    _attach(vault, relative, turns=TURNS, names=names)

    fields = _fields(page)
    assert fields["speakers"] == ["Host", "Guest"]
    assert fields["speaker_verification"] == "human-verified"
    assert "[0:04] [Guest]: Thanks for having me." in page.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        ("sha", "ATTACH_BINARY_MISMATCH"),
        ("size", "ATTACH_BINARY_MISMATCH"),
        ("late_segment", "ATTACH_PAYLOAD_OUT_OF_BOUNDS"),
        ("too_much_text", "ATTACH_PAYLOAD_OUT_OF_BOUNDS"),
        ("no_duration", "ATTACH_DURATION_UNKNOWN"),
    ],
)
def test_a_result_that_does_not_fit_the_recording_is_refused_and_the_page_stays(
    vault: Path, monkeypatch: pytest.MonkeyPatch, refusal: str, code: str
) -> None:
    relative, page = _recording(vault)
    before = page.read_bytes()
    call = {
        "sha": {"binary_sha256": hashlib.sha256(b"other bytes").hexdigest()},
        "size": {"binary_size": len(PAYLOAD) + 1},
        "late_segment": {"segments": [{"start": DURATION + 60, "text": "after the end"}]},
        "too_much_text": {"segments": [{"start": 0.0, "end": 5.0, "text": "word " * 2000}]},
        "no_duration": {},
    }[refusal]
    if refusal == "no_duration":
        monkeypatch.setattr(speech_attach, "container_duration", lambda _path: None)

    with pytest.raises(OpError) as refused:
        _attach(vault, relative, **call)

    assert refused.value.code == code
    assert page.read_bytes() == before


def test_a_source_that_is_no_recording_is_refused(vault: Path) -> None:
    stub = preserve.preserve_bytes(vault, scope="Notes", category="docs", filename="memo.txt", data=b"text")
    relative = stub.path
    before = (vault / stub.sidecar_path).read_bytes()

    with pytest.raises(OpError) as refused:
        _attach(vault, relative, payload=b"text")

    assert refused.value.code == "ATTACH_NOT_MEDIA"
    assert (vault / stub.sidecar_path).read_bytes() == before


def test_the_speech_stage_of_a_waiting_job_is_retired_and_no_speech_job_remains(vault: Path) -> None:
    relative, _page = _recording(vault)
    assert media_jobs.status(vault)["counts"][media_jobs.PENDING] == 1

    reply = _attach(vault, relative)

    assert reply["speech_job"] == "retired"
    assert media_jobs.status(vault)["counts"][media_jobs.PENDING] == 0


def _plain_transcript(vault: Path, page: Path) -> None:
    """The in-cell result of a recording with no speaker labels."""
    preserve.update_sidecar_extraction(vault, page, text="A plain in-cell transcript.", engine="faster-whisper")


def _prior_pages(page: Path) -> list[Path]:
    return sorted(page.parent.glob(f"{page.name.removesuffix('.md')}.prior-*.md"))


def test_an_attach_replaces_an_unlabelled_transcript_and_keeps_it_on_a_prior_page(vault: Path) -> None:
    relative, page = _recording(vault)
    _plain_transcript(vault, page)

    reply = _attach(vault, relative, turns=TURNS)

    [prior] = _prior_pages(page)
    assert reply["outcome"] == "replaced" and reply["prior_extraction"].endswith(prior.name.removesuffix(".md"))
    assert "A plain in-cell transcript." in prior.read_text(encoding="utf-8")
    assert "A plain in-cell transcript." not in page.read_text(encoding="utf-8")
    assert _fields(page)["prior_extractions"] == [f"[[{reply['prior_extraction']}]]"]


def test_a_video_keeps_its_frame_vector_stage_when_its_speech_stage_is_retired(vault: Path) -> None:
    relative, page = _recording(vault, "clip.mp4")
    ledger = media_jobs.MediaJobStore(vault)
    binary = vault / relative
    ledger.enqueue(media_jobs.MediaJob(binary, page, "video", do_ocr=False, do_clip=True))
    assert ledger.get_by_binary(binary, media_type="video").do_ocr

    _attach(vault, relative)

    job = ledger.get_by_binary(binary, media_type="video")
    assert job is not None and job.do_clip and not job.do_ocr


def test_a_labelled_result_is_refused_unless_the_call_says_to_replace_it(vault: Path) -> None:
    relative, page = _recording(vault)
    _attach(vault, relative, turns=TURNS)
    before = page.read_bytes()
    other = [{"start": 0.0, "end": 5.0, "text": "A different result entirely."}]

    with pytest.raises(OpError) as refused:
        _attach(vault, relative, segments=other)
    assert refused.value.code == "ATTACH_REPLACES_LABELS" and page.read_bytes() == before

    reply = _attach(vault, relative, segments=other, replace_labelled=True)
    assert reply["outcome"] == "replaced"
    assert len(_prior_pages(page)) == 1
    assert "[0:00] A different result entirely." in page.read_text(encoding="utf-8")


def test_a_named_result_is_refused_unless_the_call_says_to_replace_it(vault: Path) -> None:
    relative, page = _recording(vault)
    _attach(vault, relative, turns=TURNS, names=[{"label": "SPEAKER_00", "name": "Host", "basis": "person"}])
    before = page.read_bytes()

    with pytest.raises(OpError) as refused:
        _attach(vault, relative, turns=TURNS, engine="another engine")

    assert refused.value.code == "ATTACH_REPLACES_LABELS" and page.read_bytes() == before


def test_a_replacing_attach_carries_a_persons_name_to_the_label_that_overlaps_its_lines(vault: Path) -> None:
    relative, page = _recording(vault)
    names = [{"label": "SPEAKER_00", "name": "Host", "basis": "person"}]
    _attach(vault, relative, turns=TURNS, names=names)
    again = [{**turn, "label": {"SPEAKER_00": "voice-a", "SPEAKER_01": "voice-b"}[turn["label"]]} for turn in TURNS]

    reply = _attach(vault, relative, turns=again, engine="a second engine", replace_labelled=True)

    fields = _fields(page)
    assert fields["speakers"] == ["Host", "Speaker B"]
    assert fields["speaker_names"] == [{"name": "Host", "label": "Speaker A", "basis": "person"}]
    assert reply["names_not_carried"] == []
    assert "[0:04] [Speaker B]: Thanks for having me." in page.read_text(encoding="utf-8")


def test_a_name_on_merged_turns_is_listed_as_not_carried(vault: Path) -> None:
    relative, page = _recording(vault)
    _attach(vault, relative, turns=TURNS, names=[{"label": "SPEAKER_00", "name": "Host", "basis": "person"}])
    # A page from before timed lines: its turns are merged, so no line has a time to overlap.
    merged = page.read_text(encoding="utf-8")
    for stamp in ("[0:00] ", "[0:04] ", "[0:09] "):
        merged = merged.replace(stamp, "")
    page.write_text(merged, encoding="utf-8")

    reply = _attach(vault, relative, turns=TURNS, engine="a second engine", replace_labelled=True)

    assert reply["names_not_carried"] == ["Host"]
    assert _fields(page)["speaker_names_not_carried"] == ["Host"]
    assert "speaker_names" not in _fields(page)


def test_names_in_the_call_enrol_no_voice_profile(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import voice_profiles

    relative, _page = _recording(vault)
    saved: list[object] = []
    monkeypatch.setattr(voice_profiles, "save_profile", lambda *args, **kwargs: saved.append(args))

    _attach(vault, relative, turns=TURNS, names=[{"label": "SPEAKER_00", "name": "Host", "basis": "profile"}])

    assert saved == []
    assert not voice_profiles.voice_profiles_path(vault).exists()


def test_a_repeat_of_the_same_call_writes_nothing_and_returns_the_earlier_outcome(vault: Path) -> None:
    relative, page = _recording(vault)
    first = _attach(vault, relative, turns=TURNS)
    written = page.read_bytes()

    again = _attach(vault, relative, turns=TURNS)

    assert again["outcome"] == "unchanged"
    assert again["attached_digest"] == first["attached_digest"]
    assert page.read_bytes() == written


def test_a_repeat_after_a_replacing_call_writes_nothing_and_adds_no_prior_page(vault: Path) -> None:
    relative, page = _recording(vault)
    _attach(vault, relative, turns=TURNS)
    other = [{"start": 0.0, "end": 5.0, "text": "A different result entirely."}]
    _attach(vault, relative, segments=other, replace_labelled=True)
    written = page.read_bytes()

    again = _attach(vault, relative, segments=other, replace_labelled=True)

    assert again["outcome"] == "unchanged"
    assert page.read_bytes() == written and len(_prior_pages(page)) == 1


def test_a_retry_with_the_same_mutation_identity_replays_the_first_reply(vault: Path) -> None:
    from exomem import writer_lease

    relative, page = _recording(vault)
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "process_media")
    call = {
        "path": relative, "operation": "attach-speech", "binary_sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "binary_size": len(PAYLOAD), "segments": SEGMENTS, "engine": "whisper", "idempotency_key": "attach-once",
    }

    first = writer_lease.invoke_command(command, vault, **call)
    written = page.read_bytes()
    retry = writer_lease.invoke_command(command, vault, **call)

    assert first["outcome"] == "attached" and first["names_not_carried"] == []
    assert retry["outcome"] == "attached" and retry["replayed"] is True
    assert page.read_bytes() == written


def test_an_attach_renders_the_page_the_in_cell_diarizer_renders_for_the_same_segments_and_turns(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from exomem import extract

    relative, page = _recording(vault)
    in_cell = [SimpleNamespace(text=item["text"], start=item["start"], end=item["end"]) for item in SEGMENTS]
    monkeypatch.setattr(extract, "_run_diarization", lambda _path: [(t["start"], t["end"], t["label"]) for t in TURNS])
    monkeypatch.setattr(extract, "_resolve_named_labels", lambda *_args, **_kwargs: {})

    text, speakers, verification = extract._diarize(Path("x.wav"), in_cell, timestamps=True)
    _attach(vault, relative, turns=TURNS)

    assert preserve.extracted_text(page.read_text(encoding="utf-8")) == text
    assert _fields(page)["speakers"] == list(dict.fromkeys(turn["speaker"] for turn in speakers))
    assert _fields(page)["speaker_verification"] == verification
