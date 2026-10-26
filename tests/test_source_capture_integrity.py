"""Capture refuses altered originals rather than silently preserving a retyped copy."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import commands, source_capture


def test_retyped_timestamp_is_refused_before_source_capture(vault, source_schema):
    original = "00:12:04 Speaker A: The shipment arrived.\r\n"
    before = sorted(vault.rglob("*.md"))

    with pytest.raises(source_capture.SourceCaptureError, match="SOURCE_HASH_MISMATCH"):
        commands.op_capture_source(
            vault, source_schema, title="Shipping call", source_kind="session",
            content=original.replace("00:12:04", "00:12:05"),
            expected_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
        )

    assert sorted(vault.rglob("*.md")) == before


def test_text_integrity_assertion_describes_supplied_utf8_bytes(vault, source_schema):
    original = "  Notes: café\r\nA second line.\r\n"

    result = commands.op_capture_source(
        vault, source_schema, title="Shipping notes", source_kind="session", content=original,
        expected_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
    )

    assert (vault / result["source"]["path"]).is_file()


def test_file_hash_mismatch_does_not_publish_original(vault, source_schema, tmp_path, monkeypatch):
    from exomem import client_artifacts

    original = b"00:12:04 Shipping call\r\n\x00exact bytes\xff"
    staged = tmp_path / "original.bin"
    staged.write_bytes(original)
    monkeypatch.setattr(
        client_artifacts, "stage_artifact",
        lambda *a, **kw: client_artifacts.StagedArtifact(
            "original", staged, len(original), hashlib.sha256(original).hexdigest(),
            "application/octet-stream", "original.bin",
        ),
    )
    before = sorted(vault.rglob("*"))

    with pytest.raises(source_capture.SourceCaptureError, match="SOURCE_HASH_MISMATCH"):
        commands.op_capture_source(
            vault, source_schema, title="Original bytes", source_kind="session",
            files=[{"download_url": "https://example.invalid/original", "file_id": "original"}],
            expected_sha256=hashlib.sha256(b"different").hexdigest(),
        )

    assert sorted(vault.rglob("*")) == before


def test_exact_original_file_is_preserved_with_its_hash(vault, source_schema, tmp_path, monkeypatch):
    from exomem import client_artifacts

    original = b"\xef\xbb\xbfFirst line\r\n\r\nLast line\x00\xff"
    staged = tmp_path / "original.bin"
    staged.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    monkeypatch.setattr(
        client_artifacts, "stage_artifact",
        lambda *a, **kw: client_artifacts.StagedArtifact(
            "original", staged, len(original), digest, "application/octet-stream", "original.bin",
        ),
    )

    result = commands.op_capture_source(
        vault, source_schema, title="Original bytes", source_kind="session",
        files=[{"download_url": "https://example.invalid/original", "file_id": "original"}],
        expected_sha256=digest,
    )

    [stored] = result["files"]
    assert stored["outcome"] == "stored"
    assert Path(vault / stored["stored_path"]).read_bytes() == original
    assert stored["hash"] == digest
