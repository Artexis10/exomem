"""A large extracted document stays searchable and readable end to end as one document.

Real preserve, real text extraction through the job runtime, real find and real
read_memory. Nothing is replaced: the original is plain UTF-8 text, which needs no engine.
"""

import os
import random

import pytest

from exomem import commands, extract, find as find_module, media_processing, media_worker, preserve

PLANTED = "zorblax quillfeather"


def _document(size: int) -> str:
    """Paragraphs of invented words, `size` characters in all, with `PLANTED` in the last 4 KB."""
    rng = random.Random(7)
    vocabulary = [f"w{rng.randrange(10**6):06d}" for _ in range(500)]
    paragraphs: list[str] = []
    total = 0
    while total < size:
        paragraph = " ".join(rng.choice(vocabulary) for _ in range(rng.randrange(40, 120)))
        paragraphs.append(paragraph)
        total += len(paragraph) + 2
    text = "\n\n".join(paragraphs)[:size].rstrip()
    near_end = len(text) - 2000
    return text[:near_end] + f" {PLANTED} " + text[near_end:]


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)


def _extract(vault, filename: str, text: str):
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="docs", filename=filename, data=text.encode("utf-8")
    )
    binary = vault / stub.path
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()
    return stub


def test_a_five_mebibyte_document_is_one_findable_document_read_in_windows(vault) -> None:
    text = _document(5 * 1024 * 1024)
    stub = _extract(vault, "big-report.txt", text)

    hits = find_module.find(vault, query=PLANTED)

    assert [hit.path for hit in hits] == [stub.sidecar_path]
    hit = hits[0]
    assert hit.matched_part is not None and hit.matched_part > 1
    assert PLANTED in hit.passage["text"]

    found = commands.op_read_memory(vault, path=stub.sidecar_path, offset=hit.passage["offset"])
    assert PLANTED in found["body"]
    assert found["window"]["part"] == hit.matched_part

    walked: list[str] = []
    offset: int | None = 0
    while offset is not None:
        window = commands.op_read_memory(vault, path=stub.sidecar_path, offset=offset)
        assert window["window"]["offset"] == offset
        assert len(window["body"]) <= 48 * 1024
        walked.append(window["body"])
        offset = window["window"]["next_offset"]
    assert "".join(walked) == text

    listing = commands.op_browse_memory(vault, path=os.path.dirname(stub.path), mode="list")
    assert sorted(entry["name"] for entry in listing["entries"]) == [
        os.path.basename(stub.path),
        os.path.basename(stub.sidecar_path),
    ]
