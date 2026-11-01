"""A large extracted document stays searchable and readable end to end as one document.

Real preserve, real text extraction through the job runtime, real find and real
read_memory. Nothing is replaced: the original is plain UTF-8 text, which needs no engine.
"""

import os
import random

import pytest

from exomem import (
    audit,
    commands,
    extract,
    extraction_parts,
    media_processing,
    media_worker,
    preserve,
    reconcile,
    vault_portability,
)
from exomem import find as find_module

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


def _small_parted_source(vault, monkeypatch: pytest.MonkeyPatch, filename: str = "note.txt"):
    """A Source whose text needs several parts at a 4 KB page, written the way the worker writes it."""
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 4000)
    stub = preserve.preserve_bytes(
        vault, scope="Test", category="docs", filename=filename, data=b"original bytes"
    )
    text = "\n\n".join(
        f"paragraph {number} " + "alpha beta gamma " * 20 + (PLANTED if number == 55 else "")
        for number in range(60)
    ).strip()
    preserve.update_sidecar_extraction(vault, vault / stub.sidecar_path, text=text, engine="text")
    return stub, text


def test_reading_a_part_page_returns_its_source_as_one_document(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    part = extraction_parts.part_path(page, 3).relative_to(vault).as_posix()

    from_part = commands.op_read_memory(vault, path=part, offset=0)
    from_source = commands.op_read_memory(vault, path=stub.sidecar_path, offset=0)

    assert from_part["path"] == stub.sidecar_path
    assert from_part["frontmatter"]["exomem_id"] == from_source["frontmatter"]["exomem_id"]
    assert from_part["body"] == from_source["body"]


def test_an_offset_outside_the_text_is_refused(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)

    with pytest.raises(ValueError, match="INVALID_OFFSET"):
        commands.op_read_memory(vault, path=stub.sidecar_path, offset=len(text))


def test_a_shorter_extraction_leaves_no_stale_part(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    assert extraction_parts.folder_for(page).is_dir()

    preserve.update_sidecar_extraction(vault, page, text="a short replacement", engine="text")

    assert not extraction_parts.folder_for(page).exists()
    assert "extraction_parts" not in page.read_text(encoding="utf-8")
    assert find_module.find(vault, query=PLANTED) == []


def test_moving_a_source_moves_its_parts_with_it(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    old_page = vault / stub.sidecar_path

    commands.op_move_file(vault, old_path=stub.path, new_path=stub.path.replace("docs", "moved"))

    new_page = vault / stub.sidecar_path.replace("docs", "moved")
    assert not extraction_parts.folder_for(old_page).exists()
    assert extraction_parts.whole_text(vault, new_page, new_page.read_text(encoding="utf-8")) == text
    hits = find_module.find(vault, query=PLANTED)
    assert [hit.path for hit in hits] == [new_page.relative_to(vault).as_posix()]


def test_deleting_a_source_page_removes_its_parts(vault, monkeypatch) -> None:
    from exomem import index_sync

    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    page.unlink()

    index_sync.delete_after_remove(vault, [stub.sidecar_path])

    assert not extraction_parts.folder_for(page).exists()
    assert find_module.find(vault, query=PLANTED) == []


def test_reconcile_removes_stray_parts_and_writes_a_missing_one_from_the_original(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    (vault / stub.path).write_text(text, encoding="utf-8")  # the original now holds the text itself
    folder = extraction_parts.folder_for(page)
    (folder / "part-09.md").write_text((folder / "part-06.md").read_text(encoding="utf-8"), encoding="utf-8")
    (folder / "part-03.md").unlink()

    report = reconcile.reconcile(vault).as_dict()

    assert (report["part_orphans_removed"], report["parts_rebuilt"], report["parts_missing"]) == (1, 1, [])
    assert extraction_parts.whole_text(vault, page, page.read_text(encoding="utf-8")) == text


def test_reconcile_reports_a_missing_part_of_a_recording_without_running_its_engine(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch, filename="call.mp3")
    page = vault / stub.sidecar_path
    extraction_parts.part_path(page, 3).unlink()

    report = reconcile.reconcile(vault).as_dict()

    assert report["parts_missing"] == [stub.sidecar_path]
    with pytest.raises(ValueError, match="PART_MISSING"):
        offset: int | None = 0
        while offset is not None:
            offset = commands.op_read_memory(vault, path=stub.sidecar_path, offset=offset)["window"]["next_offset"]


def test_export_classifies_part_pages_as_canonical_vault_content(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    part = extraction_parts.part_path(page, 2).relative_to(vault).as_posix()
    other_hidden = part.replace("part-02.md", "cache.bin")

    assert vault_portability.classify_artifact(part).artifact_class.value == "canonical"
    assert vault_portability.classify_artifact(other_hidden).artifact_class.value == "disposable-runtime"


def test_parts_are_not_counted_as_unprocessed_sources(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    parts = {p.relative_to(vault).as_posix() for p in extraction_parts.folder_for(page).glob("part-*.md")}
    assert parts

    report = audit.audit(vault, categories=["unprocessed_source"])

    flagged = {finding.path for finding in report.findings}
    assert stub.sidecar_path in flagged
    assert not parts & flagged
