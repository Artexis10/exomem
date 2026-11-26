"""A recording that was already transcribed keeps its transcript when its folder is adopted.

The owner's vault finishes real transcripts with the real media worker (only the speech model is stubbed). The
folder is then staged in another vault and adopted through the staged route, in one or more windows.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest
import test_adoption_consume as consume
from conftest import FIXTURE_VAULT, initialize_vault_state_offline
from test_adoption_series import _window

from exomem import (
    adoption_run,
    cloud_import_job,
    commands,
    embeddings,
    extract,
    extraction_parts,
    import_filing,
    import_stage,
    kept_speech,
    lexstore,
    media_jobs,
    media_processing,
    media_worker,
    preserve,
    speech_attach,
    write_intake,
    writer_lease,
)
from exomem.governance import principal as principal_module
from exomem.vault import parse_frontmatter

TODAY = dt.date(2026, 11, 26)
STAGE = "_Imports/6619ea22-235a-40fb-9664-46cde7cde292"
LATER = "_Imports/0d1c1f10-7f4b-4a0e-9a57-6f0e2b4c9a11"
THIRD = "_Imports/b8d5a6a2-1f3c-4a5e-8f0b-2f6c1d9e7a33"


@pytest.fixture
def owner(tmp_path: Path) -> Path:
    """The vault where the recordings were transcribed before the import."""
    dest = tmp_path / "owner"
    shutil.copytree(FIXTURE_VAULT, dest)
    initialize_vault_state_offline(dest, source="owner vault fixture")
    return dest


def _transcript(lines: int) -> list[dict]:
    """Turns that the speech model stub returns: one speaker per line, alternating."""
    return [
        {"speaker": f"Speaker {'AB'[index % 2]}", "start": float(index * 3), "end": float(index * 3 + 2), "text": f"Line {index} of the talk."}
        for index in range(lines)
    ]


def _timed(turns: list[dict]) -> str:
    """The labelled, timed lines that a diarised extraction holds."""
    return "\n".join(
        f"[{int(t['start']) // 60}:{int(t['start']) % 60:02d}] [{t['speaker']}]: {t['text']}" for t in turns
    )


def _transcribed(
    owner: Path, monkeypatch: pytest.MonkeyPatch, name: str, *, lines: int = 6, media: str = "audio", payload: bytes | None = None
) -> tuple[Path, Path]:
    """A recording with a finished transcript, as the media worker writes it; returns the original and its page."""
    stub = preserve.preserve_bytes(owner, scope="Rental", category=media, filename=name, data=payload or f"bytes of {name}".encode())
    binary, page = owner / stub.path, owner / stub.sidecar_path
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    media_processing.reconcile_media(owner, binary, explicit=False)  # the pending page that records the original's identity
    turns = _transcript(lines)
    monkeypatch.setattr(
        extract, "extract_text",
        lambda *_a, **_k: extract.ExtractResult(text=_timed(turns), media_type=media, engine="whisper-test", speakers=turns),
    )
    media_worker.MediaWorker(owner, execution_mode="inline")._process(
        media_worker._Job(binary_path=binary, sidecar_path=page, media_type=media)
    )
    assert "extracted_by: whisper-test" in page.read_text(encoding="utf-8")
    return binary, page


def _stage(
    vault: Path, binary: Path, page: Path, *, folder: str = STAGE, carry: tuple[str, ...] = ("binary", "page", "parts")
) -> dict[str, str]:
    """Copy a recording, its page and the page's part folder into the vault's stage; returns their vault paths."""
    staged = {"binary": f"{folder}/{binary.name}", "page": f"{folder}/{page.name}"}
    (vault / folder).mkdir(parents=True, exist_ok=True)
    if "binary" in carry:
        shutil.copy2(binary, vault / staged["binary"])
    if "page" in carry:
        shutil.copy2(page, vault / staged["page"])
    parts = extraction_parts.folder_for(page)
    if "parts" in carry and parts.is_dir():
        shutil.copytree(parts, vault / folder / parts.name, dirs_exist_ok=True)
        staged["parts"] = f"{folder}/{parts.name}"
    return staged


def _plan(vault: Path, folder: str = STAGE) -> tuple[str, dict]:
    run = adoption_run.start(vault, path=folder, today=TODAY)
    adoption_run.select(vault, run_id=run["run_id"], include=[folder])
    planned = adoption_run.plan(vault, run_id=run["run_id"], today=TODAY)
    return run["run_id"], planned


def _adopt(vault: Path, folder: str = STAGE) -> tuple[dict, dict]:
    run_id, planned = _plan(vault, folder)
    applied = adoption_run.apply(vault, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY)
    return planned, applied


def _fields(page: Path) -> dict:
    return parse_frontmatter(page.read_text(encoding="utf-8"))[0]


def _whole(vault: Path, page: Path) -> str:
    return extraction_parts.whole_text(vault, page, page.read_text(encoding="utf-8"))


@pytest.fixture
def long_transcripts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make 40 lines of transcript run over several part pages."""
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 600)


def test_a_transcribed_recording_keeps_its_transcript_and_parts_when_its_folder_is_adopted(
    vault: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    assert extraction_parts.count_of(_fields(old_page)) > 2
    _stage(vault, binary, old_page)
    pages = _stored_pages(vault)

    planned, applied = _adopt(vault)

    outcome = applied["outcomes"][f"{STAGE}/{binary.name}"]
    page = vault / outcome["target_path"]
    old = _fields(old_page)
    assert outcome["status"] == "applied" and applied["phase"] == "applied"
    assert _whole(vault, page) == _whole(owner, old_page)
    assert "[0:03] [Speaker B]: Line 1 of the talk." in page.read_text(encoding="utf-8")
    assert _fields(page)["speakers"] == old["speakers"] and _fields(page)["extracted_by"] == "whisper-test"
    assert extraction_parts.count_of(_fields(page)) == extraction_parts.count_of(old)
    assert all(extraction_parts.part_path(page, n).is_file() for n in range(2, extraction_parts.count_of(old) + 1))
    kept = applied["outcomes"][f"{STAGE}/{old_page.name}"]
    assert kept["target_path"] == outcome["target_path"] and kept["kept_speech"]["outcome"] == "filled"
    assert [stored for stored in _stored_pages(vault) if stored not in pages] == [page]  # no text Source holds the old page
    assert planned["plan"]["kept_speech"]["staged"] == 1
    assert planned["plan"]["kept_speech"]["part_pages"] == extraction_parts.count_of(old) - 1
    assert applied["apply_result"]["kept_speech"]["filled"] == 1


def _stored_pages(vault: Path) -> list[Path]:
    """The Source pages of the vault, without the part pages that belong to one."""
    pages = (vault / "Knowledge Base" / "Sources").rglob("*.md")
    return sorted(page for page in pages if extraction_parts.source_page_for(page) is None)


def _edit(path: Path, before: str, after: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert before in text
    path.write_text(text.replace(before, after, 1), encoding="utf-8")


@pytest.mark.parametrize(
    ("unpaired", "code"),
    [
        ("hash", kept_speech.MISMATCH),
        ("size", kept_speech.MISMATCH),
        ("pending", kept_speech.INCOMPLETE),
        ("failed", kept_speech.INCOMPLETE),
        ("part_gone", kept_speech.PARTS_MISSING),
    ],
)
def test_a_page_that_is_not_a_finished_transcript_of_its_recording_files_as_text_with_the_reason_counted(
    vault: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None, unpaired: str, code: str
) -> None:
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    staged = _stage(vault, binary, old_page)
    page = vault / staged["page"]
    sha = _fields(old_page)["binary_sha256"]
    if unpaired == "hash":
        _edit(page, f"binary_sha256: {sha}", f"binary_sha256: {'0' * 64}")
    elif unpaired == "size":
        _edit(page, "binary_size: 22", "binary_size: 23")
    elif unpaired == "pending":
        _edit(page, "extracted_by: whisper-test", "extracted_by: pending")
        _edit(page, "processing_state: completed", "processing_state: pending")
    elif unpaired == "failed":
        _edit(page, "processing_state: completed", "processing_state: failed")
    else:
        extraction_parts.part_path(vault / staged["page"], 2).unlink()

    planned, applied = _adopt(vault)

    counts = planned["plan"]["kept_speech"]
    assert counts["unpaired"] == {code: 1} and counts["staged"] == 0
    assert counts["unpaired_part_pages"] == extraction_parts.count_of(_fields(old_page)) - 1  # part pages only, not the page
    assert applied["outcomes"][staged["page"]]["status"] == "applied"  # the page files as a text Source, as before
    recording = vault / applied["outcomes"][staged["binary"]]["target_path"]
    assert _fields(recording)["extracted_by"] == "pending"  # the recording keeps no transcript


def test_a_page_that_changed_after_the_plan_is_not_kept_and_its_recording_files_nothing(
    vault: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a")
    staged = _stage(vault, binary, old_page)
    (vault / STAGE / "other.m4a").write_bytes(b"a recording that nothing was said about")
    run_id, planned = _plan(vault)
    _edit(vault / staged["page"], "Line 2 of the talk.", "Line 2 of another talk.")

    applied = adoption_run.apply(vault, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY)

    outcome = applied["outcomes"][staged["page"]]
    assert outcome["status"] == "failed" and outcome["code"] == "SOURCE_CHANGED"
    assert applied["outcomes"][staged["binary"]]["status"] == "applied"  # the recording files, with no transcript
    assert applied["outcomes"][f"{STAGE}/other.m4a"]["status"] == "applied"
    assert applied["apply_result"]["kept_speech"]["failed"] == 1
    recording = vault / applied["outcomes"][staged["binary"]]["target_path"]
    assert _fields(recording)["extracted_by"] == "pending"
    assert [page for page in _stored_pages(vault) if "interview" in page.name] == [recording]  # no text Source holds the page


def test_a_page_whose_lists_an_editor_wrote_as_blocks_keeps_every_item_of_them(
    vault: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a")
    _edit(
        old_page, "speakers: [Speaker A, Speaker B]",
        'speaker_names:\n  - label: "Speaker A"\n    name: "Ana"\nspeakers:\n  - Speaker A\n  - Speaker B',
    )
    assert _fields(old_page)["speaker_names"] == [{"label": "Speaker A", "name": "Ana"}]
    _stage(vault, binary, old_page)

    _, applied = _adopt(vault)

    kept = _fields(vault / applied["outcomes"][f"{STAGE}/{binary.name}"]["target_path"])
    assert kept["speakers"] == ["Speaker A", "Speaker B"]
    assert kept["speaker_names"] == [{"label": "Speaker A", "name": "Ana"}]


def test_a_card_number_in_a_kept_transcript_is_removed_and_the_reply_warns(
    vault: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a")
    _edit(old_page, "Line 2 of the talk.", "My card is 4111 1111 1111 1111.")
    _stage(vault, binary, old_page)

    _planned, applied = _adopt(vault)

    page = vault / applied["outcomes"][f"{STAGE}/{binary.name}"]["target_path"]
    assert "4111" not in page.read_text(encoding="utf-8") and "Line 3 of the talk." in page.read_text(encoding="utf-8")
    assert any("card" in warning.lower() or "redact" in warning.lower() for warning in applied["warnings"])


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _knowledge(vault: Path) -> dict[str, bytes]:
    """Every page and original that the vault keeps as knowledge: the Sources folder, byte for byte."""
    base = vault / "Knowledge Base" / "Sources"
    return {path.relative_to(vault).as_posix(): path.read_bytes() for path in sorted(base.rglob("*")) if path.is_file()}


def _window_one(vault: Path, binary: Path, old_page: Path) -> Path:
    """The recording arrives in an earlier window, with no page; returns its stored Source page."""
    _stage(vault, binary, old_page, carry=("binary",))
    _, applied = _adopt(vault, STAGE)
    return vault / applied["outcomes"][f"{STAGE}/{binary.name}"]["target_path"]


def _prior_pages(page: Path) -> list[Path]:
    return sorted(page.parent.glob(f"{page.name.removesuffix('.md')}.prior-*.md"))


def _assert_a_repeat_writes_nothing(vault: Path, binary: Path, old_page: Path) -> None:
    """Adopt the same page again from a new stage: the Sources and their prior pages stay byte for byte."""
    kept = _knowledge(vault)
    _stage(vault, binary, old_page, folder=THIRD, carry=("page", "parts"))

    _, repeated = _adopt(vault, THIRD)

    assert repeated["outcomes"][f"{THIRD}/{old_page.name}"]["kept_speech"]["outcome"] == "unchanged"
    assert _knowledge(vault) == kept


def test_a_later_window_fills_the_pending_source_of_a_recording_with_its_transcript_and_a_repeat_writes_nothing(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    vault = duplicate_index
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    source = _window_one(vault, binary, old_page)
    assert _fields(source)["extracted_by"] == "pending"
    staged = _stage(vault, binary, old_page, folder=LATER, carry=("page", "parts"))
    pages = _stored_pages(vault)

    planned, applied = _adopt(vault, LATER)

    outcome = applied["outcomes"][staged["page"]]
    assert planned["plan"]["kept_speech"]["stored"] == 1
    assert outcome["status"] == "applied" and outcome["kept_speech"]["outcome"] == "filled"
    assert _whole(vault, source) == _whole(owner, old_page)
    assert _fields(source)["speakers"] == _fields(old_page)["speakers"]
    assert _stored_pages(vault) == pages  # no text Source holds the old page
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 0
    _assert_a_repeat_writes_nothing(vault, binary, old_page)


def test_a_transcript_that_the_cell_finished_between_windows_is_replaced_and_kept_on_a_prior_page(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = duplicate_index
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a")
    source = _window_one(vault, binary, old_page)
    preserve.update_sidecar_extraction(vault, source, text="A plain in-cell transcript.", engine="faster-whisper")
    staged = _stage(vault, binary, old_page, folder=LATER, carry=("page", "parts"))

    _, applied = _adopt(vault, LATER)

    [prior] = _prior_pages(source)
    outcome = applied["outcomes"][staged["page"]]
    assert outcome["kept_speech"]["outcome"] == "replaced"
    assert _whole(vault, source) == _whole(owner, old_page) and "A plain in-cell" not in source.read_text(encoding="utf-8")
    assert "A plain in-cell transcript." in prior.read_text(encoding="utf-8")
    assert _fields(source)["prior_extractions"] == [f"[[{prior.relative_to(vault).as_posix().removesuffix('.md')}]]"]
    _assert_a_repeat_writes_nothing(vault, binary, old_page)


def test_a_labelled_source_keeps_its_result_and_the_transcript_that_arrives_goes_to_a_prior_page_with_its_parts(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    vault = duplicate_index
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    source = _window_one(vault, binary, old_page)
    monkeypatch.setattr(speech_attach, "container_duration", lambda _path: 600.0)
    commands.op_process_media(
        vault, path=source.with_suffix("").relative_to(vault).as_posix(), operation="attach-speech",
        binary_sha256=_fields(old_page)["binary_sha256"], binary_size=22, engine="the owner's engine",
        segments=[{"start": 0.0, "end": 4.0, "text": "A labelled transcript."}],
        turns=[{"start": 0.0, "end": 4.0, "label": "SPEAKER_00"}],
    )
    held = _whole(vault, source)
    staged = _stage(vault, binary, old_page, folder=LATER, carry=("page", "parts"))
    pages = _stored_pages(vault)

    _, applied = _adopt(vault, LATER)

    [prior] = [page for page in _prior_pages(source)]
    outcome = applied["outcomes"][staged["page"]]
    assert outcome["kept_speech"]["outcome"] == "kept-out"
    assert _whole(vault, source) == held and _fields(source)["speakers"] == ["Speaker A"]
    assert _whole(vault, prior) == _whole(owner, old_page)
    assert extraction_parts.count_of(_fields(prior)) == extraction_parts.count_of(_fields(old_page)) > 2
    assert [page for page in _stored_pages(vault) if page not in pages] == [prior]  # no text Source holds the page
    assert _fields(source)["prior_extractions"] == [f"[[{prior.relative_to(vault).as_posix().removesuffix('.md')}]]"]
    _assert_a_repeat_writes_nothing(vault, binary, old_page)


def test_a_page_that_differs_from_the_kept_result_only_in_a_later_part_is_not_taken_as_held(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    vault = duplicate_index
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    source = _window_one(vault, binary, old_page)
    _stage(vault, binary, old_page, folder=LATER, carry=("page", "parts"))
    _adopt(vault, LATER)  # the Source now holds the result
    last = extraction_parts.part_path(old_page, extraction_parts.count_of(_fields(old_page)))
    _edit(last, "Line 39 of the talk.", "Line 39 of another talk.")
    staged = _stage(vault, binary, old_page, folder=THIRD, carry=("page", "parts"))

    _, applied = _adopt(vault, THIRD)

    assert applied["outcomes"][staged["page"]]["kept_speech"]["outcome"] == "kept-out"
    assert "Line 39 of the talk." in _whole(vault, source)
    assert any("Line 39 of another talk." in _whole(vault, prior) for prior in _prior_pages(source))


def test_a_video_that_keeps_its_transcript_in_a_later_window_keeps_its_frame_vector_stage_and_loses_its_speech_stage(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = duplicate_index
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    binary, old_page = _transcribed(owner, monkeypatch, "clip.mp4", media="video")
    source = _window_one(vault, binary, old_page)
    stored = vault / source.with_suffix("").relative_to(vault)
    ledger = media_jobs.MediaJobStore(vault)
    assert ledger.get_by_binary(stored, media_type="video").do_ocr  # the pending Source waits for speech
    _stage(vault, binary, old_page, folder=LATER, carry=("page", "parts"))

    _, applied = _adopt(vault, LATER)

    job = ledger.get_by_binary(stored, media_type="video")
    assert job is not None and job.do_clip and not job.do_ocr
    assert _whole(vault, source) == _whole(owner, old_page)


def test_the_result_of_a_job_that_was_running_while_a_later_window_kept_the_transcript_is_not_published(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = duplicate_index
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a")
    source = _window_one(vault, binary, old_page)
    _stage(vault, binary, old_page, folder=LATER, carry=("page", "parts"))
    cell_text = "[0:01] the cell's own transcript"

    def adopt_while_the_engine_runs(*_args, **_kwargs):
        _adopt(vault, LATER)
        return extract.ExtractResult(cell_text, "audio", "faster-whisper:test+timed")

    monkeypatch.setattr(extract, "extract_text", adopt_while_the_engine_runs)
    worker = media_worker.MediaWorker(vault, execution_mode="process")

    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()

    assert cell_text not in _whole(vault, source) and _whole(vault, source) == _whole(owner, old_page)
    assert not _prior_pages(source)
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 0


def test_a_page_is_held_not_filed_as_text_while_the_duplicate_index_cannot_answer_and_pairs_when_it_can(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = duplicate_index
    binary, old_page = _transcribed(owner, monkeypatch, "interview.m4a")
    source = _window_one(vault, binary, old_page)
    _stage(vault, binary, old_page, folder=LATER, carry=("page", "parts"))
    pages = _stored_pages(vault)
    with monkeypatch.context() as outage:
        outage.setattr(lexstore, "payload_candidates", lambda *_a, **_k: None)  # the index cannot answer
        run_id, planned = _plan(vault, LATER)
        plan_id = planned["plan"]["plan_id"]

        held = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)

    assert planned["plan"]["kept_speech"]["awaiting"] == 1 and not planned["plan"]["kept_speech"]["unpaired"]
    assert held["phase"] == "applying" and "continuation" in held
    assert f"{LATER}/{old_page.name}" not in held["outcomes"]  # nothing is filed for the page
    assert _stored_pages(vault) == pages  # and no text Source holds it
    assert held["apply_result"]["kept_speech"]["held"] == 1
    assert _fields(source)["extracted_by"] == "pending"

    applied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, continuation=held["continuation"], today=TODAY)

    assert applied["phase"] == "applied" and applied["outcomes"][f"{LATER}/{old_page.name}"]["kept_speech"]["outcome"] == "filled"
    assert _whole(vault, source) == _whole(owner, old_page)
    assert _stored_pages(vault) == pages


def test_with_the_speech_engine_on_a_kept_recording_queues_no_speech_job_and_a_kept_video_is_indexed_by_frame(
    vault: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)
    audio, audio_page = _transcribed(owner, monkeypatch, "interview.m4a")
    video, video_page = _transcribed(owner, monkeypatch, "clip.mp4", media="video")
    _stage(vault, audio, audio_page)
    _stage(vault, video, video_page)
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_k: pytest.fail("the speech engine ran"))
    frames = [(1.0, np.ones(embeddings.CLIP_DIM, dtype=np.float32)), (2.0, np.ones(embeddings.CLIP_DIM, dtype=np.float32))]
    monkeypatch.setattr(embeddings, "embed_video_frames", lambda _view: frames)

    _, applied = _adopt(vault)

    stored_audio = vault / applied["outcomes"][f"{STAGE}/{audio.name}"]["artifact_path"]
    stored_video = vault / applied["outcomes"][f"{STAGE}/{video.name}"]["artifact_path"]
    ledger = media_jobs.MediaJobStore(vault)
    assert ledger.get_by_binary(stored_audio) is None  # acknowledged, no job
    job = ledger.get_by_binary(stored_video, media_type="video")
    assert job is not None and job.do_clip and not job.do_ocr and ledger.counts()["pending"] == 1

    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    media_worker.MediaWorker(vault, execution_mode="process")._drain_parent_results()

    assert embeddings.ClipIndex(vault).has_frames(stored_video.relative_to(vault).as_posix())
    assert ledger.counts()["pending"] == 0


def test_a_restored_vault_with_complete_audio_and_video_pages_queues_no_speech_job_and_keeps_its_pages(
    owner: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    _transcribed(owner, monkeypatch, "clip.mp4", media="video")
    waiting = preserve.preserve_bytes(owner, scope="Rental", category="audio", filename="waiting.m4a", data=b"not transcribed yet")
    media_processing.reconcile_media(owner, owner / waiting.path, explicit=False)
    restored = tmp_path / "restored"
    shutil.copytree(owner, restored)  # a restore puts the files back byte for byte; the job ledger is not part of it
    initialize_vault_state_offline(restored, source="restored vault fixture")
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_k: pytest.fail("the speech engine ran for a completed page"))
    before = _tree(restored)

    queued = media_worker.MediaWorker(restored, execution_mode="process").scan_pending()

    assert queued == 1 and media_jobs.MediaJobStore(restored).get_by_binary(restored / waiting.path).do_ocr  # only the page that waits
    assert media_jobs.MediaJobStore(restored).counts()["pending"] == 1
    assert _tree(restored) == before


def _members(path: Path) -> dict[str, bytes]:
    """The files that a Cloud import stages for one original of the owner's vault: the file, and a page's part folder."""
    members = {path.name: path.read_bytes()}
    parts = extraction_parts.folder_for(path)
    if path.suffix == ".md" and parts.is_dir():
        members.update({f"{parts.name}/{part.name}": part.read_bytes() for part in sorted(parts.iterdir())})
    return members


def _series_window(vault: Path, *files: Path, series: tuple[Path, ...], filing: str | None = None) -> str:
    """A window of a series as the Cloud import Job leaves it: its files, a receipt and the series file of the whole series."""
    paths = [path for original in series for path in _members(original)]
    import_id = _window(vault, {path: data for original in files for path, data in _members(original).items()}, series=paths)
    if filing is not None:  # the series file that asks the cell to file the stage itself
        (vault / "_Imports" / import_id / import_stage.SERIES_FILE).write_text(
            json.dumps({"label": "Field Notes", "paths": paths, "filing": filing})
        )
    return import_id


def test_a_page_whose_recording_is_in_an_earlier_window_that_is_not_applied_yet_waits_and_pairs_after_it_files(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    vault = duplicate_index
    audio, audio_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    recording_window = _series_window(vault, audio, series=(audio, audio_page))
    page_window = _series_window(vault, audio_page, series=(audio, audio_page))
    pages = _stored_pages(vault)
    recording_run, recording_plan = consume._plan(vault, f"_Imports/{recording_window}")  # planned, not applied
    page_run, page_plan = consume._plan(vault, f"_Imports/{page_window}")

    held = adoption_run.apply(vault, run_id=page_run, plan_id=page_plan, today=TODAY)

    assert held["phase"] == "applying" and "continuation" in held and not held["outcomes"]
    assert _stored_pages(vault) == pages  # no text Source holds the page, and the recording is not transcribed again
    assert held["apply_result"]["kept_speech"]["held"] == 1
    consume._apply(vault, recording_run, recording_plan)
    [source] = [page for page in _stored_pages(vault) if page not in pages]
    assert _fields(source)["extracted_by"] == "pending"

    applied = adoption_run.apply(vault, run_id=page_run, plan_id=page_plan, continuation=held["continuation"], today=TODAY)

    assert applied["phase"] == "applied"
    assert applied["outcomes"][f"_Imports/{page_window}/{audio_page.name}"]["kept_speech"]["outcome"] == "filled"
    assert _whole(vault, source) == _whole(owner, audio_page)
    assert [page for page in _stored_pages(vault) if page not in pages] == [source]
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 0


def test_a_page_with_no_recording_in_its_series_files_as_text_and_the_plan_counts_the_reason(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    vault = duplicate_index
    audio, audio_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    page_window = _series_window(vault, audio_page, series=(audio_page,))  # no window of the series holds the recording
    pages = _stored_pages(vault)

    run_id, plan_id = consume._plan(vault, f"_Imports/{page_window}")
    plan = adoption_run.AdoptionRunStore(vault).load(run_id)["plan"]
    applied = consume._apply(vault, run_id, plan_id)

    assert plan["kept_speech"]["unpaired"] == {kept_speech.NO_RECORDING: 1}  # not a mismatch: the recording is simply not stored
    assert applied["phase"] == "applied"
    [text] = [page for page in _stored_pages(vault) if page not in pages]
    assert _fields(text)["type"] == "source" and "Line 7 of the talk." in text.read_text(encoding="utf-8")


def test_a_held_page_files_as_text_when_the_window_that_held_its_recording_is_discarded(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None
) -> None:
    vault = duplicate_index
    audio, audio_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    recording_window = _series_window(vault, audio, series=(audio, audio_page))
    page_window = _series_window(vault, audio_page, series=(audio, audio_page))
    pages = _stored_pages(vault)
    consume._plan(vault, f"_Imports/{recording_window}")
    run_id, plan_id = consume._plan(vault, f"_Imports/{page_window}")
    held = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert held["phase"] == "applying" and _stored_pages(vault) == pages
    cloud_import_job.discard(vault, recording_window)  # the person discards the window that held the recording

    applied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, continuation=held["continuation"], today=TODAY)

    assert applied["phase"] == "applied"
    assert applied["apply_result"]["kept_speech"]["unpaired"] == {kept_speech.NO_RECORDING: 1}
    [text] = [page for page in _stored_pages(vault) if page not in pages]
    assert "Line 7 of the talk." in text.read_text(encoding="utf-8")


@pytest.fixture
def filer_cell():
    yield
    writer_lease.reset_managers_for_tests()


def _owner() -> dict:
    return write_intake.principal_record(principal_module.owner_principal(surface="library"))


def _run_until_due(filer: import_filing.Filer, import_id: str) -> float | None:
    """Make the filer's calls on one stage in turn, as its thread does, until it is due later or leaves the list."""
    for _ in range(60):
        delay = filer.run_once(import_id)
        if delay != 0.0:
            return delay
    raise AssertionError("the stage was still working after the calls")


def test_a_cloud_import_in_two_windows_keeps_each_transcript_with_no_speech_job_when_the_page_window_files_first(
    duplicate_index: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, long_transcripts: None, filer_cell: None
) -> None:
    vault = duplicate_index
    audio, audio_page = _transcribed(owner, monkeypatch, "interview.m4a", lines=40)
    video, video_page = _transcribed(owner, monkeypatch, "clip.mp4", media="video")
    everything = (audio, audio_page, video, video_page)
    pages_window = _series_window(vault, audio_page, video_page, series=everything, filing="cell")
    recordings_window = _series_window(vault, audio, video, series=everything, filing="cell")
    before = _stored_pages(vault)
    filer = import_filing.Filer(vault)
    filer.register(pages_window, principal=_owner(), surface=None)
    filer.register(recordings_window, principal=_owner(), surface=None)

    delay = _run_until_due(filer, pages_window)  # the filer meets the pages first

    held = import_filing.read_view(vault, pages_window)
    assert delay == import_filing.HOLD_CHECK_SECONDS
    assert held["state"] == "parked" and held["stop"]["code"] == kept_speech.RECORDING_PENDING and held["needs"] == []
    assert _stored_pages(vault) == before  # no text Source holds a page, and no recording is transcribed again
    assert _run_until_due(filer, recordings_window) is None
    assert _run_until_due(filer, pages_window) is None

    sources = {name: next(page for page in _stored_pages(vault) if page.name.endswith(f"{name}.md")) for name in (audio.name, video.name)}
    assert set(_stored_pages(vault)) - set(before) == set(sources.values())
    for original, old_page in ((audio, audio_page), (video, video_page)):
        assert _whole(vault, sources[original.name]) == _whole(owner, old_page)
        assert _fields(sources[original.name])["speakers"] == ["Speaker A", "Speaker B"]
    pages_view = import_filing.read_view(vault, pages_window)
    assert pages_view["state"] == "done" and not pages_view["left_out"]["by_reason"]
    residual = adoption_run.file(vault, path=f"_Imports/{pages_window}")["filed"]["residual"]
    held_back = extraction_parts.count_of(_fields(audio_page)) + extraction_parts.count_of(_fields(video_page))
    # Each page and each of its part pages stays staged as a transcript kept with its recording, and none is of a kind
    # that adoption does not import.
    assert set(residual["by_reason"]) == {"kept_speech"} and residual["by_reason"]["kept_speech"]["files"] == held_back
    recordings_view = import_filing.read_view(vault, recordings_window)
    assert recordings_view["state"] == "done" and not recordings_view["left_out"]["by_reason"]
    assert adoption_run.file(vault, path=f"_Imports/{recordings_window}")["filed"]["residual"]["files"] == 0

    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_k: pytest.fail("the speech engine ran"))
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    media_worker.MediaWorker(vault, execution_mode="process")._drain_parent_results()
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 0  # no speech job, with the worker's speech engine on
    assert extraction_parts.count_of(_fields(sources[audio.name])) > 2
    found = commands.op_find(vault, "Line 7 of the talk", speakers=["Speaker A"])
    hits = found["hits"] if isinstance(found, dict) else found  # a find answers a list, or an envelope that carries one
    assert sources[audio.name].relative_to(vault).as_posix() in {hit["path"] for hit in hits}
