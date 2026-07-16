"""Canonical media classification and reconciliation contract (red phase)."""

from __future__ import annotations

import hashlib
import importlib
import os
import uuid
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


def test_short_completed_transcript_is_preserved_and_not_requeued(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "short-completed.m4a")
    first = media_processing.reconcile_media(vault, binary)
    store = media_jobs.MediaJobStore(vault)
    claimed = store.claim_next()
    assert claimed is not None
    store.complete(claimed)

    sidecar = first.sidecar_path
    completed = sidecar.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed += "\n## Extracted text\n\n[0:00] Yes.\n"
    sidecar.write_text(completed, encoding="utf-8")
    before = sidecar.read_bytes()

    result = media_processing.reconcile_media(vault, binary)

    assert result.state == "completed"
    assert result.job_id is None
    assert sidecar.read_bytes() == before
    assert _job_count(vault) == 0


def test_partial_pending_sidecar_is_canonically_repaired_with_prose(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "partial.m4a")
    sidecar = binary.with_name(binary.name + ".md")
    prose = "Keep this manually recorded note verbatim.\n"
    sidecar.write_text(
        "---\n"
        "type: source\n"
        "media_type: audio\n"
        "extracted_by: pending\n"
        "---\n\n"
        + prose,
        encoding="utf-8",
    )

    media_processing.reconcile_media(vault, binary)

    frontmatter, body = _frontmatter_and_body(sidecar)
    assert uuid.UUID(str(frontmatter["exomem_id"]))
    assert frontmatter["title"] == "Evidence: partial.m4a"
    assert frontmatter["source_type"] == "other"
    assert frontmatter["captured"]
    assert isinstance(frontmatter["tags"], list)
    assert frontmatter["ingested_into"] == []
    assert frontmatter["evidence_file"] == (
        "Knowledge Base/Evidence/Audio/partial.m4a"
    )
    assert frontmatter["media_type"] == "audio"
    assert f"## Preserved notes\n\n{prose}" in body


@pytest.mark.parametrize("terminal_state", ["pending", "completed"])
def test_revalidates_binary_before_no_write_terminal_paths(
    vault: Path, monkeypatch: pytest.MonkeyPatch, terminal_state: str
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, f"race-{terminal_state}.m4a")
    first = media_processing.reconcile_media(vault, binary)
    store = media_jobs.MediaJobStore(vault)
    if terminal_state == "completed":
        claimed = store.claim_next()
        assert claimed is not None
        store.complete(claimed)
        completed = first.sidecar_path.read_text(encoding="utf-8").replace(
            "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
        ).replace("processing_state: pending", "processing_state: completed")
        completed += (
            "\n## Extracted text\n\n"
            "[0:00] This transcript is long enough for the pre-fix completed path.\n"
        )
        first.sidecar_path.write_text(completed, encoding="utf-8")

    sidecar_before = first.sidecar_path.read_bytes()
    jobs_before = _job_count(vault)
    read_provenance = media_processing._read_provenance

    def _read_then_replace(*args, **kwargs):
        provenance = read_provenance(*args, **kwargs)
        binary.write_bytes(b"replacement media after provenance")
        return provenance

    monkeypatch.setattr(media_processing, "_read_provenance", _read_then_replace)

    with pytest.raises(media_processing.MediaProcessingError) as exc:
        media_processing.reconcile_media(vault, binary)

    assert exc.value.code == "MEDIA_CHANGED_DURING_RECONCILIATION"
    assert first.sidecar_path.read_bytes() == sidecar_before
    assert _job_count(vault) == jobs_before


def test_completed_sidecar_clears_stale_crash_window_job(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "crash-window.m4a")
    first = media_processing.reconcile_media(vault, binary)
    sidecar = first.sidecar_path
    completed = sidecar.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed += (
        "\n## Extracted text\n\n"
        "[0:00] The sidecar committed, but the worker crashed before ledger cleanup.\n"
    )
    sidecar.write_text(completed, encoding="utf-8")
    before = sidecar.read_bytes()
    assert _job_count(vault) == 1

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


def test_reconcile_all_media_is_bounded_pruned_and_soft_fails_per_artifact(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_processing = _media_processing()
    kb = vault / "Knowledge Base"
    first = _drop_media(vault, "a-broken.m4a")
    second = _drop_media(vault, "b-good.wav")
    _drop_media(vault, "c-bounded.mp3")
    _drop_media(vault, "unsupported.bin")
    hidden = kb / "Evidence" / ".hidden" / "hidden.m4a"
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"hidden")
    archived = kb / "_archive" / "archived.m4a"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"archived")
    outside = vault / "Attachments" / "outside.m4a"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"outside")
    calls: list[tuple[Path, bool]] = []

    def reconcile_media(
        root: Path, path: Path, *, explicit: bool = True
    ) -> None:
        assert root == vault
        calls.append((path, explicit))
        if path == first:
            raise OSError("one unreadable artifact must not abort the pass")

    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)

    attempted = media_processing.reconcile_all_media(vault, limit=2)

    assert attempted == 2
    assert calls == [(first, False), (second, False)]


def test_reconcile_all_media_repeats_without_duplicate_work(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "scan-repeat.m4a")

    assert media_processing.reconcile_all_media(vault, limit=10) == 1
    sidecar = binary.with_name(binary.name + ".md")
    first_bytes = sidecar.read_bytes()
    first_mtime = sidecar.stat().st_mtime_ns
    assert media_processing.reconcile_all_media(vault, limit=10) == 0

    assert sidecar.read_bytes() == first_bytes
    assert sidecar.stat().st_mtime_ns == first_mtime
    assert _job_count(vault) == 1


def test_bounded_reconcile_skips_converged_prefix_and_advances_later_work(
    vault: Path,
) -> None:
    media_processing = _media_processing()
    pending = [
        _drop_media(vault, name)
        for name in ("a#pending.mp3", "a-pending-1.mp3", "a-pending-2.mp3")
    ]
    for binary in pending:
        media_processing.reconcile_media(vault, binary)

    completed_binary = _drop_media(vault, "b-completed.mp3")
    completed = media_processing.reconcile_media(vault, completed_binary)
    completed_text = completed.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed_text += "\n## Extracted text\n\n[0:00] Already complete.\n"
    completed.sidecar_path.write_text(completed_text, encoding="utf-8")
    store = media_jobs.MediaJobStore(vault)
    store.discard(
        media_jobs.MediaJob(
            binary_path=completed_binary,
            sidecar_path=completed.sidecar_path,
            media_type="audio",
        )
    )

    orphan = _drop_media(vault, "x-orphan-pending.mp3")
    orphan_result = media_processing.reconcile_media(vault, orphan)
    store.discard(
        media_jobs.MediaJob(
            binary_path=orphan,
            sidecar_path=orphan_result.sidecar_path,
            media_type="audio",
        )
    )
    malformed = _drop_media(vault, "y-malformed.mp3")
    malformed_sidecar = malformed.with_name(malformed.name + ".md")
    malformed_sidecar.write_text("Waiting for canonical repair.\n", encoding="utf-8")
    missing = _drop_media(vault, "z-missing.mp3")

    assert media_processing.reconcile_all_media(vault, limit=2) == 2
    assert store.has_binary(orphan) is True
    assert "type: source" in malformed_sidecar.read_text(encoding="utf-8")
    assert not missing.with_name(missing.name + ".md").exists()

    assert media_processing.reconcile_all_media(vault, limit=2) == 1
    assert missing.with_name(missing.name + ".md").exists()
    assert store.has_binary(missing) is True


def test_reconciliation_honors_access_policy_and_allows_normal_evidence(
    vault: Path,
) -> None:
    media_processing = _media_processing()
    access_config = vault / "Knowledge Base" / "_access.yaml"
    access_config.write_text(
        "readonly:\n  - Reference\nexcluded:\n  - Private\n",
        encoding="utf-8",
    )

    excluded = vault / "Knowledge Base" / "Private" / "secret.m4a"
    readonly = vault / "Knowledge Base" / "Reference" / "recording.m4a"
    for binary in (excluded, readonly):
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"protected")
        assert media_processing.reconcile_media(vault, binary, explicit=False) is None
        assert not binary.with_name(binary.name + ".md").exists()
        with pytest.raises(media_processing.MediaProcessingError) as exc:
            media_processing.reconcile_media(vault, binary, explicit=True)
        assert exc.value.code == "MEDIA_PATH_ACCESS_DENIED"

    evidence = _drop_media(vault, "allowed.m4a")
    result = media_processing.reconcile_media(vault, evidence, explicit=False)
    assert result is not None
    assert result.sidecar_path.exists()
    assert _job_count(vault) == 1

    assert media_processing.reconcile_all_media(vault, limit=10) == 0
    assert not excluded.with_name(excluded.name + ".md").exists()
    assert not readonly.with_name(readonly.name + ".md").exists()


@pytest.mark.parametrize("terminal_state", [media_jobs.BLOCKED, media_jobs.FAILED])
def test_reconciliation_retains_actionable_terminal_ledger_state(
    vault: Path, terminal_state: str
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, f"retained-{terminal_state}.m4a")
    first = media_processing.reconcile_media(vault, binary)
    store = media_jobs.MediaJobStore(vault)
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == first.job_id
    error = "ExtractionUnavailable: engine absent" if terminal_state == media_jobs.BLOCKED else (
        "InvalidDataError: corrupt container"
    )
    store.mark(claimed.id, terminal_state, error)

    repeated = media_processing.reconcile_media(vault, binary)

    assert repeated.job_id == first.job_id
    assert repeated.state == terminal_state
    [job] = media_jobs.status(vault)["jobs"]
    assert job["state"] == terminal_state
    assert job["attempts"] == 1
    assert job["error"] == error


def test_retry_media_preserves_completed_transcript_and_discards_stale_job(
    vault: Path,
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "retry-completed.m4a")
    first = media_processing.reconcile_media(vault, binary)
    store = media_jobs.MediaJobStore(vault)
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == first.job_id
    store.mark(claimed.id, media_jobs.FAILED, "InvalidDataError: stale failure")
    completed = first.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed += "\n## Extracted text\n\n[0:00] This valid transcript must survive retry.\n"
    first.sidecar_path.write_text(completed, encoding="utf-8")
    before = first.sidecar_path.read_bytes()

    retried = media_processing.retry_media(vault, binary)

    assert retried.state == "completed"
    assert retried.job_id is None
    assert first.sidecar_path.read_bytes() == before
    assert media_jobs.status(vault)["jobs"] == []
    assert _job_count(vault) == 0


def test_retry_media_requeues_only_the_targeted_terminal_artifact(vault: Path) -> None:
    media_processing = _media_processing()
    blocked_binary = _drop_media(vault, "retry-blocked.m4a")
    failed_binary = _drop_media(vault, "retry-failed.m4a")
    blocked = media_processing.reconcile_media(vault, blocked_binary)
    failed = media_processing.reconcile_media(vault, failed_binary)
    store = media_jobs.MediaJobStore(vault)
    blocked_claim = store.claim_next()
    assert blocked_claim is not None and blocked_claim.id == blocked.job_id
    store.mark(blocked_claim.id, media_jobs.BLOCKED, "engine absent")
    failed_claim = store.claim_next()
    assert failed_claim is not None and failed_claim.id == failed.job_id
    store.mark(failed_claim.id, media_jobs.FAILED, "corrupt container")

    retried = media_processing.retry_media(vault, failed_binary)

    assert retried.job_id == failed.job_id
    assert retried.state == media_jobs.PENDING
    jobs = {job["path"]: job for job in media_jobs.status(vault)["jobs"]}
    assert jobs[failed_binary.relative_to(vault).as_posix()]["state"] == media_jobs.PENDING
    assert jobs[blocked_binary.relative_to(vault).as_posix()]["state"] == media_jobs.BLOCKED


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
