"""Canonical media classification and reconciliation contract (red phase)."""

from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path

import pytest
import yaml

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


def _frontmatter_and_body(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    raw_frontmatter, body = text.removeprefix("---\n").split("\n---\n", 1)
    frontmatter = yaml.safe_load(raw_frontmatter)
    assert isinstance(frontmatter, dict)
    return frontmatter, body


def _job_count(vault: Path) -> int:
    return sum(media_jobs.status(vault)["counts"].values())


def test_classifies_m4a_case_insensitively_as_audio() -> None:
    media_processing = _media_processing()

    assert media_processing.classify_media("recording.m4a") == "audio"
    assert media_processing.classify_media(Path("recording.M4A")) == "audio"


def test_unsupported_media_is_ignored_automatically_and_errors_explicitly(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "recording.bin", b"unsupported")

    assert media_processing.classify_media(binary) is None
    assert media_processing.reconcile_media(vault, binary, explicit=False) is None
    assert not binary.with_name(binary.name + ".md").exists()
    assert _job_count(vault) == 0

    with pytest.raises(media_processing.MediaProcessingError) as exc:
        media_processing.reconcile_media(vault, binary, explicit=True)
    assert exc.value.code == "UNSUPPORTED_MEDIA"
    assert not binary.with_name(binary.name + ".md").exists()
    assert _job_count(vault) == 0


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
    frontmatter, _ = _frontmatter_and_body(sidecar)
    assert frontmatter["type"] == "source"
    assert frontmatter["media_type"] == "audio"
    assert frontmatter["extracted_by"] == "pending"
    assert media_jobs.status(vault)["counts"]["pending"] == 1


def test_prose_only_sidecar_is_repaired_without_losing_notes(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "interview.m4a")
    sidecar = binary.with_name(binary.name + ".md")
    original = "Waiting for transcription.\nKeep the original cassette label: Side B.\n"
    sidecar.write_text(original, encoding="utf-8")

    result = media_processing.reconcile_media(vault, binary)

    frontmatter, body = _frontmatter_and_body(sidecar)
    assert result.state == "pending"
    assert frontmatter["type"] == "source"
    assert frontmatter["media_type"] == "audio"
    assert frontmatter["extracted_by"] == "pending"
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
    frontmatter, _ = _frontmatter_and_body(binary.with_name(binary.name + ".md"))
    assert frontmatter["original_filename"] == "Voice Memo.M4A"
    assert frontmatter["binary_sha256"] == digest
    assert frontmatter["binary_size"] == len(payload)
    assert frontmatter["binary_mtime_ns"] == before.st_mtime_ns
    assert frontmatter["binary_ctime_ns"] == before.st_ctime_ns


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
    assert _job_count(vault) == 0


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
    assert _job_count(vault) == 1


@pytest.mark.parametrize(
    "location", ["outside-vault", "outside-knowledge-base", "symlink-escape"]
)
def test_reconciliation_confines_paths_to_governed_knowledge_base(
    vault: Path, tmp_path: Path, location: str
) -> None:
    media_processing = _media_processing()
    if location == "outside-vault":
        binary = tmp_path / "elsewhere" / "escape.m4a"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"escape attempt")
    elif location == "outside-knowledge-base":
        binary = vault / "Attachments" / "escape.m4a"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"escape attempt")
    else:
        target = tmp_path / "outside-target.m4a"
        target.write_bytes(b"symlink escape attempt")
        binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "escape-link.m4a"
        binary.parent.mkdir(parents=True, exist_ok=True)
        try:
            binary.symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(media_processing.MediaProcessingError) as exc:
        media_processing.reconcile_media(vault, binary)

    assert exc.value.code == "MEDIA_PATH_OUTSIDE_KB"
    assert not binary.with_name(binary.name + ".md").exists()
    assert _job_count(vault) == 0
