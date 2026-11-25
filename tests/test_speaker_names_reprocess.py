"""A reprocess carries a person's names to the new labels by the lines they overlap (design D13).

Only the speech engine is replaced. The sidecar, the job runtime, the child/parent handoff, the
rename operation and the atomic commit are real.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import commands, extract, media_processing, media_worker, preserve, prior_extraction
from exomem.vault import parse_frontmatter

FIRST = (
    "[0:00] [Speaker A]: Welcome back to the show.\n[0:10] [Speaker B]: Thanks for having me.\n"
    "[0:20] [Speaker A]: Let us begin.\n[0:30] [Speaker B]: Gladly.\n[0:40] [Speaker A]: First question."
)
#: The same voices under the other letters, as a second diarisation run may letter them.
SWAPPED = FIRST.replace("Speaker A", "Speaker X").replace("Speaker B", "Speaker A").replace("Speaker X", "Speaker B")
UNLABELLED = "[0:00] Welcome back to the show.\n[0:10] Thanks for having me."


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)


def _engine(monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[str, list[str] | None]]) -> None:
    """The speech engine returns each output in turn: its text, and the labels it carries."""
    remaining = iter(outputs)

    def extract_text(path, *, media_type=None, **_options):
        text, labels = next(remaining)
        turns = [{"speaker": label, "start": 0.0, "end": 1.0, "text": ""} for label in labels] if labels else None
        return extract.ExtractResult(text, "audio", "faster-whisper:test+timed", speakers=turns)

    monkeypatch.setattr(extract, "extract_text", extract_text)


def _run_to_idle(vault: Path, worker: media_worker.MediaWorker) -> None:
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()


def _named_recording(vault: Path) -> tuple[Path, Path, media_worker.MediaWorker]:
    """A transcribed recording whose Speaker A a person has named."""
    stub = preserve.preserve_bytes(vault, scope="Rental", category="audio", filename="talk.mp3", data=b"FAKEBYTES")
    binary, sidecar = vault / stub.path, vault / stub.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)
    _run_to_idle(vault, worker)
    commands.op_process_media(vault, path=stub.path, operation="rename-speakers", renames={"Speaker A": "Hugo"})
    assert "[0:00] [Hugo]: Welcome back to the show." in sidecar.read_text(encoding="utf-8")
    return binary, sidecar, worker


def test_a_reprocess_that_letters_the_voices_differently_names_the_label_that_overlaps(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine(monkeypatch, [(FIRST, ["Speaker A", "Speaker B"]), (SWAPPED, ["Speaker A", "Speaker B"])])
    binary, sidecar, worker = _named_recording(vault)

    media_processing.reprocess_media(vault, binary, language="en")
    _run_to_idle(vault, worker)

    text = sidecar.read_text(encoding="utf-8")
    fields = parse_frontmatter(text)[0]
    assert "[0:00] [Hugo]: Welcome back to the show." in text and "[0:10] [Speaker A]: Thanks for having me." in text
    assert fields["speaker_names"] == [{"name": "Hugo", "label": "Speaker B", "basis": "person"}]
    assert "speaker_names_not_carried" not in fields


def test_a_reprocess_without_labels_lists_the_name_as_not_carried_and_keeps_it_on_the_prior_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine(monkeypatch, [(FIRST, ["Speaker A", "Speaker B"]), (UNLABELLED, None)])
    binary, sidecar, worker = _named_recording(vault)

    media_processing.reprocess_media(vault, binary, language="en")
    _run_to_idle(vault, worker)

    now = sidecar.read_text(encoding="utf-8")
    fields = parse_frontmatter(now)[0]
    assert fields["speaker_names_not_carried"] == ["Hugo"] and "speaker_names" not in fields
    (link,) = prior_extraction.links_of(now)
    kept = (vault / f"{link}.md").read_text(encoding="utf-8")
    assert "[0:00] [Hugo]: Welcome back to the show." in kept and parse_frontmatter(kept)[0]["speaker_names"]
