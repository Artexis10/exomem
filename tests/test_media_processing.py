"""Canonical media classification and reconciliation contract (red phase)."""

from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path

import pytest

from exomem import media_jobs


def _media_processing():
    """Import inside each test so a missing leaf is reported as a feature failure."""
    return importlib.import_module("exomem.media_processing")


def _drop_media(
    vault: Path,
    name: str = "field-recording.m4a",
    data: bytes = b"\x00\x00\x00\x18ftypM4A fake audio",
) -> Path:
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(data)
    return binary


def test_classifies_m4a_case_insensitively_as_audio() -> None:
    media_processing = _media_processing()

    assert media_processing.classify_media("recording.m4a") == "audio"
    assert media_processing.classify_media(Path("recording.M4A")) == "audio"


def test_unsupported_media_is_ignored_automatically_and_errors_explicitly(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "recording.aac", b"unsupported")

    assert media_processing.classify_media(binary) is None
    assert media_processing.reconcile_media(vault, binary, explicit=False) is None
    assert not binary.with_name(binary.name + ".md").exists()
    assert media_jobs.status(vault)["counts"]["pending"] == 0

    with pytest.raises(media_processing.MediaProcessingError) as exc:
        media_processing.reconcile_media(vault, binary, explicit=True)
    assert exc.value.code == "UNSUPPORTED_MEDIA"


def test_missing_sidecar_becomes_canonical_pending_work(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault)

    result = media_processing.reconcile_media(vault, binary)

    sidecar = binary.with_name(binary.name + ".md")
    assert result.media_type == "audio"
    assert result.state == "pending"
    assert result.sidecar_path == sidecar
    assert result.job_id is not None
    assert sidecar.exists()
    body = sidecar.read_text(encoding="utf-8")
    assert "type: source" in body
    assert "media_type: audio" in body
    assert "extracted_by: pending" in body
    assert media_jobs.status(vault)["counts"]["pending"] == 1


def test_prose_only_sidecar_is_repaired_without_losing_notes(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "interview.m4a")
    sidecar = binary.with_name(binary.name + ".md")
    original = "Waiting for transcription.\nKeep the original cassette label: Side B.\n"
    sidecar.write_text(original, encoding="utf-8")

    result = media_processing.reconcile_media(vault, binary)

    body = sidecar.read_text(encoding="utf-8")
    assert result.state == "pending"
    assert body.startswith("---\n")
    assert "extracted_by: pending" in body
    assert f"## Preserved notes\n\n{original}" in body
    assert media_jobs.status(vault)["counts"]["pending"] == 1


def test_reconciliation_records_binary_provenance_without_mutating_evidence(vault: Path) -> None:
    media_processing = _media_processing()
    payload = b"immutable voice evidence\x00\x01"
    binary = _drop_media(vault, "Voice Memo.M4A", payload)
    os.utime(binary, ns=(1_700_000_000_000_000_000, 1_700_000_123_456_789_000))
    before = binary.stat()
    digest = hashlib.sha256(payload).hexdigest()

    media_processing.reconcile_media(vault, binary)

    after = binary.stat()
    assert binary.read_bytes() == payload
    assert (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    body = binary.with_name(binary.name + ".md").read_text(encoding="utf-8")
    assert "original_filename: Voice Memo.M4A" in body
    assert f"binary_sha256: {digest}" in body
    assert f"binary_size: {len(payload)}" in body
    assert f"binary_mtime_ns: {before.st_mtime_ns}" in body
    assert f"binary_ctime_ns: {before.st_ctime_ns}" in body


def test_valid_completed_transcript_is_preserved_and_not_requeued(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "completed.m4a")
    first = media_processing.reconcile_media(vault, binary)
    store = media_jobs.MediaJobStore(vault)
    claimed = store.claim_next()
    assert claimed is not None
    store.complete(claimed)

    sidecar = first.sidecar_path
    completed = sidecar.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed += (
        "\n## Extracted text\n\n"
        "[0:00] The durable transcript starts here and contains meaningful speech.\n"
        "[0:08] A second timestamped segment makes this an unmistakably valid transcript.\n"
    )
    sidecar.write_text(completed, encoding="utf-8")
    before = sidecar.read_bytes()

    result = media_processing.reconcile_media(vault, binary)

    assert result.state == "completed"
    assert result.job_id is None
    assert sidecar.read_bytes() == before
    assert media_jobs.status(vault)["counts"]["pending"] == 0


def test_reconciliation_is_byte_stable_and_job_deduplicated(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "repeat.m4a")

    first = media_processing.reconcile_media(vault, binary)
    sidecar = first.sidecar_path
    first_bytes = sidecar.read_bytes()
    first_mtime = sidecar.stat().st_mtime_ns
    second = media_processing.reconcile_media(vault, binary)

    assert second.job_id == first.job_id
    assert sidecar.read_bytes() == first_bytes
    assert sidecar.stat().st_mtime_ns == first_mtime
    assert media_jobs.status(vault)["counts"]["pending"] == 1


@pytest.mark.parametrize("location", ["outside-vault", "outside-knowledge-base"])
def test_reconciliation_confines_paths_to_governed_knowledge_base(
    vault: Path, tmp_path: Path, location: str
) -> None:
    media_processing = _media_processing()
    if location == "outside-vault":
        binary = tmp_path / "elsewhere" / "escape.m4a"
    else:
        binary = vault / "Attachments" / "escape.m4a"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"escape attempt")

    with pytest.raises(media_processing.MediaProcessingError) as exc:
        media_processing.reconcile_media(vault, binary)

    assert exc.value.code == "MEDIA_PATH_OUTSIDE_KB"
    assert not binary.with_name(binary.name + ".md").exists()
    assert media_jobs.status(vault)["counts"]["pending"] == 0
