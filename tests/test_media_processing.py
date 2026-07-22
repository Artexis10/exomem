"""Canonical media classification and reconciliation contract (red phase)."""

from __future__ import annotations

import hashlib
import importlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from exomem import commands as commands_module
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


def test_empty_vault_reconcile_does_not_create_media_ledger(vault: Path) -> None:
    media_processing = _media_processing()
    ledger = media_jobs.job_store_path(vault)
    assert not ledger.exists()

    assert media_processing.reconcile_all_media(vault, limit=10) == 0

    assert not ledger.exists()

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


def test_passed_commit_guard_revalidates_planned_binary_before_mutation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    media_processing = _media_processing()
    binary = _drop_media(vault, "guarded-plan-drift.m4a")
    sidecar = binary.with_name(binary.name + ".md")
    entered = False

    @contextmanager
    def commit_guard():
        nonlocal entered
        entered = True
        binary.write_bytes(b"replacement after provenance planning")
        yield

    with pytest.raises(media_processing.MediaProcessingError) as raised:
        media_processing.reconcile_media(
            vault,
            binary,
            commit_guard=commit_guard,
        )

    assert entered is True
    assert raised.value.code == "MEDIA_CHANGED_DURING_RECONCILIATION"
    assert not sidecar.exists()
    assert not media_jobs.job_store_path(vault).exists()


def test_passed_commit_guard_revalidates_access_policy_before_mutation(
    vault: Path,
) -> None:
    from contextlib import contextmanager

    media_processing = _media_processing()
    binary = _drop_media(vault, "guarded-policy-drift.m4a")
    sidecar = binary.with_name(binary.name + ".md")

    @contextmanager
    def commit_guard():
        (vault / "Knowledge Base" / "_access.yaml").write_text(
            "readonly:\n  - Evidence/Audio\n",
            encoding="utf-8",
        )
        yield

    with pytest.raises(media_processing.MediaProcessingError) as raised:
        media_processing.reconcile_media(
            vault,
            binary,
            commit_guard=commit_guard,
        )

    assert raised.value.code == "MEDIA_PATH_ACCESS_DENIED"
    assert not sidecar.exists()
    assert not media_jobs.job_store_path(vault).exists()


def test_background_commit_fans_out_derived_indexes_after_guard_release(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import index_sync

    media_processing = _media_processing()
    binary = _drop_media(vault, "guarded-fanout.m4a")
    depth = 0
    fanout_depths: list[int] = []

    @contextmanager
    def commit_guard():
        nonlocal depth
        depth += 1
        try:
            yield
        finally:
            depth -= 1

    def observe_fanout(*_args, **_kwargs):
        fanout_depths.append(depth)
        return True

    monkeypatch.setattr(index_sync, "upsert_after_write", observe_fanout)

    result = media_processing.reconcile_media(
        vault,
        binary,
        commit_guard=commit_guard,
    )

    assert result is not None
    assert fanout_depths == [0]


def test_runtime_unavailable_background_commit_fans_out_once_after_guard_release(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import index_sync

    media_processing = _media_processing()
    binary = _drop_media(vault, "guarded-unavailable-fanout.m4a")
    media_processing.set_media_runtime_unavailable(
        vault,
        reason="ExtractionUnavailable: runtime absent",
        next_action="install the media runtime",
    )
    depth = 0
    fanout_depths: list[int] = []

    @contextmanager
    def commit_guard():
        nonlocal depth
        depth += 1
        try:
            yield
        finally:
            depth -= 1

    def observe_fanout(*_args, **_kwargs):
        fanout_depths.append(depth)
        return True

    monkeypatch.setattr(index_sync, "upsert_after_write", observe_fanout)

    result = media_processing.reconcile_media(
        vault,
        binary,
        explicit=False,
        commit_guard=commit_guard,
    )

    assert result is not None
    assert result.state == media_jobs.BLOCKED
    assert fanout_depths == [0]


def test_post_write_enqueue_failure_still_fans_out_after_guard_release(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import index_sync

    media_processing = _media_processing()
    binary = _drop_media(vault, "guarded-enqueue-failure.m4a")
    sidecar = binary.with_name(binary.name + ".md")
    depth = 0
    fanout_depths: list[int] = []

    @contextmanager
    def commit_guard():
        nonlocal depth
        depth += 1
        try:
            yield
        finally:
            depth -= 1

    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda *_args, **_kwargs: fanout_depths.append(depth) or True,
    )
    monkeypatch.setattr(
        media_jobs.MediaJobStore,
        "enqueue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )

    with pytest.raises(RuntimeError, match="ledger down"):
        media_processing.reconcile_media(
            vault,
            binary,
            explicit=False,
            commit_guard=commit_guard,
        )

    assert sidecar.exists()
    assert fanout_depths == [0]


def test_mark_processing_unavailable_commits_each_sidecar_before_fanout(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import index_sync

    media_processing = _media_processing()
    binary = _drop_media(vault, "startup-pending.m4a")
    initial = media_processing.reconcile_media(vault, binary)
    depth = 0
    fanout_depths: list[int] = []

    @contextmanager
    def commit_guard():
        nonlocal depth
        depth += 1
        try:
            yield
        finally:
            depth -= 1

    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda *_args, **_kwargs: fanout_depths.append(depth) or True,
    )

    changed = media_processing.mark_processing_unavailable(
        vault,
        reason="MediaRuntimeUnavailable: startup failed",
        next_action="fix the runtime and restart",
        commit_guard=commit_guard,
    )

    assert changed == 1
    assert fanout_depths == [0]
    assert "processing_state: blocked" in initial.sidecar_path.read_text(
        encoding="utf-8"
    )


def test_background_batch_rechecks_access_policy_changed_during_staging(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import vault as vault_module

    media_processing = _media_processing()
    binary = _drop_media(vault, "staging-policy-race.m4a")
    sidecar = binary.with_name(binary.name + ".md")
    original_create = vault_module._BatchWorkspace.create_artifact
    changed = False

    def create_then_protect(self, name, content):  # noqa: ANN001
        nonlocal changed
        artifact = original_create(self, name, content)
        if not changed and name.startswith("stage-"):
            changed = True
            (vault / "Knowledge Base" / "_access.yaml").write_text(
                "readonly:\n  - Evidence/Audio\n",
                encoding="utf-8",
            )
        return artifact

    @contextmanager
    def commit_guard():
        yield

    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "create_artifact",
        create_then_protect,
    )

    with pytest.raises(ValueError, match="WRITE_REFUSED"):
        media_processing.reconcile_media(
            vault,
            binary,
            explicit=False,
            commit_guard=commit_guard,
        )

    assert changed is True
    assert not sidecar.exists()
    assert not media_jobs.job_store_path(vault).exists()


def test_binary_verification_uses_open_handle_identity_on_windows_like_stat_disagreement(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "windows-stat-disagreement.m4a")
    resolved = binary.resolve(strict=True)
    provenance = media_processing._read_provenance(vault, binary, resolved)
    original_stat = Path.stat

    def _windows_like_path_stat(self: Path, *args, **kwargs):
        result = original_stat(self, *args, **kwargs)
        if self == resolved:
            return SimpleNamespace(
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns,
                st_ctime_ns=result.st_ctime_ns - 1_000_000_000,
            )
        return result

    monkeypatch.setattr(Path, "stat", _windows_like_path_stat)

    media_processing._verify_binary_identity(binary, resolved, provenance)


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


def test_reconcile_limit_caps_cheap_examination_without_hashing_converged_media(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_processing = _media_processing()
    binaries = [_drop_media(vault, f"converged-{index}.mp3") for index in range(3)]
    for binary in binaries:
        media_processing.reconcile_media(vault, binary)

    examined: list[Path] = []
    original_needs_reconciliation = media_processing._needs_reconciliation

    def track_examination(
        root: Path,
        binary: Path,
        store: media_jobs.MediaJobStore,
    ) -> bool:
        examined.append(binary)
        return original_needs_reconciliation(root, binary, store)

    def reject_hash(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bounded discovery must not hash converged binaries")

    monkeypatch.setattr(media_processing, "_needs_reconciliation", track_examination)
    monkeypatch.setattr(media_processing, "_read_provenance", reject_hash)

    assert media_processing.reconcile_all_media(vault, limit=1) == 0
    assert examined == [binaries[0]]


def test_rotating_cursor_reaches_later_missing_media_across_bounded_passes(
    vault: Path,
) -> None:
    media_processing = _media_processing()
    converged = vault / "Knowledge Base" / "Evidence" / "z-converged.mp3"
    converged.parent.mkdir(parents=True, exist_ok=True)
    converged.write_bytes(b"converged")
    media_processing.reconcile_media(vault, converged)
    missing = _drop_media(vault, "a-nested-missing.mp3")
    missing_sidecar = missing.with_name(missing.name + ".md")

    assert media_processing.reconcile_all_media(vault, limit=1) == 0
    assert not missing_sidecar.exists()
    assert media_processing.reconcile_all_media(vault, limit=1) == 1
    assert missing_sidecar.exists()

    assert media_processing.reconcile_all_media(vault, limit=1) == 0
    assert (
        media_jobs.MediaJobStore(vault, create=False).discovery_cursor()
        == converged.relative_to(vault).as_posix()
    )


def test_periodic_reconciliation_discards_stale_job_for_completed_sidecar(
    vault: Path,
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "completed-with-stale-job.mp3")
    result = media_processing.reconcile_media(vault, binary)
    completed_text = result.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed_text += "\n## Extracted text\n\n[0:00] Already complete.\n"
    result.sidecar_path.write_text(completed_text, encoding="utf-8")
    before = result.sidecar_path.read_bytes()
    store = media_jobs.MediaJobStore(vault, create=False)
    assert store.has_binary(binary) is True

    assert media_processing.reconcile_all_media(vault, limit=1) == 1

    assert result.sidecar_path.read_bytes() == before
    assert store.has_binary(binary) is False


@pytest.mark.parametrize(
    ("field", "replacement", "expected_state"),
    [
        ("binary_sha256", None, "completed"),
        ("binary_sha256", "malformed", "pending"),
        ("evidence_file", "Knowledge Base/Evidence/wrong.mp3", "pending"),
        ("binary_size", "999999", "pending"),
    ],
)
def test_completed_sidecar_with_invalid_cheap_provenance_is_reconciled(
    vault: Path,
    field: str,
    replacement: str | None,
    expected_state: str,
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, f"completed-invalid-{field}.mp3")
    result = media_processing.reconcile_media(vault, binary)
    completed_text = result.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    transcript = "[0:00] Preserve this completed transcript during repair."
    completed_text += f"\n## Extracted text\n\n{transcript}\n"
    prefix = f"{field}:"
    lines = []
    for line in completed_text.splitlines():
        if line.startswith(prefix):
            if replacement is not None:
                lines.append(f"{field}: {replacement}")
            continue
        lines.append(line)
    result.sidecar_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = media_jobs.MediaJobStore(vault)
    store.discard(
        media_jobs.MediaJob(
            binary_path=binary,
            sidecar_path=result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_processing.reconcile_all_media(vault, limit=1) == 1

    repaired, body = _frontmatter_and_body(result.sidecar_path)
    assert repaired["processing_state"] == expected_state
    assert repaired["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert repaired["evidence_file"] == binary.relative_to(vault).as_posix()
    assert repaired["binary_size"] == binary.stat().st_size
    assert transcript in body
    assert store.has_binary(binary) is (expected_state == "pending")


def test_valid_completed_sidecar_without_job_is_skipped_by_periodic_scan(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "completed-valid-no-job.mp3")
    result = media_processing.reconcile_media(vault, binary)
    completed_text = result.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed_text += "\n## Extracted text\n\n[0:00] Already complete.\n"
    result.sidecar_path.write_text(completed_text, encoding="utf-8")
    store = media_jobs.MediaJobStore(vault)
    store.discard(
        media_jobs.MediaJob(
            binary_path=binary,
            sidecar_path=result.sidecar_path,
            media_type="audio",
        )
    )
    before = result.sidecar_path.read_bytes()

    def reject_reconcile(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("valid completed media must remain skipped")

    monkeypatch.setattr(media_processing, "reconcile_media", reject_reconcile)

    assert media_processing.reconcile_all_media(vault, limit=1) == 0
    assert result.sidecar_path.read_bytes() == before
    assert store.has_binary(binary) is False


def test_legacy_completed_sidecar_without_any_provenance_is_preserved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "completed-legacy-no-provenance.mp3")
    result = media_processing.reconcile_media(vault, binary)
    completed_text = result.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: legacy-transcriber"
    ).replace("processing_state: pending", "processing_state: completed")
    completed_text += "\n## Extracted text\n\n[0:00] Preserve this legacy transcript.\n"
    provenance_fields = {
        "evidence_file",
        "original_filename",
        "binary_sha256",
        "binary_size",
        "binary_mtime_ns",
        "binary_ctime_ns",
    }
    completed_text = "\n".join(
        line
        for line in completed_text.splitlines()
        if line.partition(":")[0] not in provenance_fields
    ) + "\n"
    result.sidecar_path.write_text(completed_text, encoding="utf-8")
    store = media_jobs.MediaJobStore(vault)
    store.discard(
        media_jobs.MediaJob(
            binary_path=binary,
            sidecar_path=result.sidecar_path,
            media_type="audio",
        )
    )
    before_body = result.sidecar_path.read_text(encoding="utf-8").split("\n---\n", 1)[1]

    assert media_processing.reconcile_all_media(vault, limit=1) == 1
    frontmatter, body = _frontmatter_and_body(result.sidecar_path)
    assert body == before_body
    assert frontmatter["evidence_file"] == binary.relative_to(vault).as_posix()
    assert frontmatter["original_filename"] == binary.name
    assert frontmatter["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert store.has_binary(binary) is False


def test_legacy_completed_mp4_backfills_partial_provenance_without_rewriting_transcript(
    vault: Path,
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "legacy-review.mp4", b"legacy mp4 evidence")
    sidecar = binary.with_name(binary.name + ".md")
    body = (
        "\n# Incident review\n\n"
        "## Extracted text\n\n"
        "[0:00] [Speaker A]: Existing words stay byte-identical.\n"
        "[0:07] [Speaker B]: Including timestamps and labels.\n"
    )
    sidecar.write_text(
        "---\n"
        "type: source\n"
        "title: Legacy incident review\n"
        "media_type: video\n"
        "extracted_by: faster-whisper:large-v3+timed+diarized\n"
        "processing_state: completed\n"
        "evidence_file: Knowledge Base/Evidence/Audio/legacy-review.mp4\n"
        "speakers: [Speaker A, Speaker B]\n"
        "speaker_verification: anonymous\n"
        "governed_custom: retain-me\n"
        f"---{body}",
        encoding="utf-8",
    )

    result = media_processing.reconcile_media(vault, binary)

    assert result.state == "completed"
    assert result.job_id is None
    frontmatter, after_body = _frontmatter_and_body(sidecar)
    assert after_body == body.removeprefix("\n")
    assert frontmatter["extracted_by"] == "faster-whisper:large-v3+timed+diarized"
    assert frontmatter["speakers"] == ["Speaker A", "Speaker B"]
    assert frontmatter["speaker_verification"] == "anonymous"
    assert frontmatter["governed_custom"] == "retain-me"
    assert frontmatter["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert _job_count(vault) == 0


def test_caller_supplied_completed_text_backfills_missing_provenance_in_place(
    vault: Path,
) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "agent-upload.m4a", b"agent supplied audio")
    sidecar = binary.with_name(binary.name + ".md")
    body = "\n# Uploaded recording\n\n## Extracted text\n\n[0:00] Supplied transcript.\n"
    sidecar.write_text(
        "---\n"
        "type: source\n"
        "title: Agent upload\n"
        "media_type: audio\n"
        "extracted_by: upload\n"
        "processing_state: completed\n"
        f"evidence_file: {binary.relative_to(vault).as_posix()}\n"
        "speaker_verification: human-verified\n"
        f"---{body}",
        encoding="utf-8",
    )

    result = media_processing.reconcile_media(vault, binary)

    assert result.state == "completed"
    assert result.job_id is None
    frontmatter, after_body = _frontmatter_and_body(sidecar)
    assert after_body == body.removeprefix("\n")
    assert frontmatter["extracted_by"] == "upload"
    assert frontmatter["speaker_verification"] == "human-verified"
    assert frontmatter["original_filename"] == binary.name
    assert frontmatter["binary_size"] == len(b"agent supplied audio")
    assert _job_count(vault) == 0


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

    assert media_processing.reconcile_all_media(vault, limit=2) == 0
    assert media_processing.reconcile_all_media(vault, limit=2) == 0
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


def test_retry_all_media_reconciles_stale_completed_before_requeue(vault: Path) -> None:
    media_processing = _media_processing()
    stale_binary = _drop_media(vault, "retry-all-stale-completed.m4a")
    failed_binary = _drop_media(vault, "retry-all-real-failure.m4a")
    stale = media_processing.reconcile_media(vault, stale_binary)
    failed = media_processing.reconcile_media(vault, failed_binary)
    store = media_jobs.MediaJobStore(vault)
    stale_claim = store.claim_next()
    assert stale_claim is not None and stale_claim.id == stale.job_id
    store.mark(stale_claim.id, media_jobs.FAILED, "InvalidDataError: stale ledger row")
    failed_claim = store.claim_next()
    assert failed_claim is not None and failed_claim.id == failed.job_id
    store.mark(failed_claim.id, media_jobs.FAILED, "InvalidDataError: real failure")
    completed = stale.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed += "\n## Extracted text\n\n[0:00] Preserve this completed transcript.\n"
    stale.sidecar_path.write_text(completed, encoding="utf-8")
    before = stale.sidecar_path.read_bytes()

    requeued = media_processing.retry_all_media(vault, limit=10)

    assert requeued == 1
    assert stale.sidecar_path.read_bytes() == before
    jobs = {job["path"]: job for job in media_jobs.status(vault)["jobs"]}
    assert stale_binary.relative_to(vault).as_posix() not in jobs
    assert jobs[failed_binary.relative_to(vault).as_posix()]["state"] == media_jobs.PENDING


def test_retry_all_media_caps_each_pass(vault: Path) -> None:
    media_processing = _media_processing()
    store = media_jobs.MediaJobStore(vault)
    binaries = [_drop_media(vault, f"retry-all-bounded-{index}.m4a") for index in range(3)]
    for binary in binaries:
        reconciled = media_processing.reconcile_media(vault, binary)
        claimed = store.claim_next()
        assert claimed is not None and claimed.id == reconciled.job_id
        store.mark(claimed.id, media_jobs.FAILED, "InvalidDataError: retryable")

    assert media_processing.retry_all_media(vault, limit=2) == 2
    counts = media_jobs.status(vault)["counts"]
    assert counts[media_jobs.PENDING] == 2
    assert counts[media_jobs.FAILED] == 1


def test_process_media_product_leaf_dispatches_process_status_and_retry(vault: Path) -> None:
    binary = _drop_media(vault, "product-leaf.m4a")
    relative = binary.relative_to(vault).as_posix()

    processed = commands_module.op_process_media(vault, path=relative, operation="process")

    assert processed == {
        "operation": "process",
        "path": relative,
        "media_type": "audio",
        "state": media_jobs.PENDING,
        "sidecar_path": f"{relative}.md",
        "job_id": processed["job_id"],
        "index_refreshed": 0,
        "index_refresh_remaining": 0,
    }
    assert processed["job_id"] is not None

    status = commands_module.op_process_media(vault, operation="status")
    assert status["operation"] == "status"
    assert status["counts"][media_jobs.PENDING] == 1
    assert status == commands_module.op_process_media(vault, operation="status")

    store = media_jobs.MediaJobStore(vault)
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == processed["job_id"]
    store.mark(claimed.id, media_jobs.FAILED, "InvalidDataError: corrupt container")

    retried = commands_module.op_process_media(vault, path=relative, operation="retry")
    assert retried["operation"] == "retry"
    assert retried["path"] == relative
    assert retried["state"] == media_jobs.PENDING
    assert retried["requeued"] == 1


def test_process_media_surfaces_and_retries_targeted_full_index_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index, index_sync

    media_processing = _media_processing()
    binary = _drop_media(vault, "product-index-retry.m4a")
    relative = binary.relative_to(vault).as_posix()
    result = media_processing.reconcile_media(vault, binary)
    other = vault / "Knowledge Base" / "Evidence" / "Audio" / "other.m4a.md"
    other.write_text("# other\n", encoding="utf-8")
    deferred_index.add_full(
        vault,
        [
            result.sidecar_path.relative_to(vault).as_posix(),
            other.relative_to(vault).as_posix(),
        ],
    )
    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_a, **_kw: True)

    status = commands_module.op_process_media(vault, operation="status")
    assert status["index_refresh"]["count"] == 2
    assert status["index_refresh"]["retryable"] is True

    retried = commands_module.op_process_media(vault, path=relative, operation="retry")

    assert retried["index_refreshed"] == 1
    assert retried["index_refresh_remaining"] == 1
    assert deferred_index.full_status(vault)["paths"] == [
        other.relative_to(vault).as_posix()
    ]


def test_process_media_product_status_is_stably_bounded(vault: Path) -> None:
    store = media_jobs.MediaJobStore(vault)
    total = media_jobs.STATUS_JOB_LIMIT + 7
    for index in range(total):
        binary = vault / "Knowledge Base/Evidence/Audio" / f"status-{index}.m4a"
        store.enqueue(
            media_jobs.MediaJob(
                binary_path=binary,
                sidecar_path=binary.with_name(binary.name + ".md"),
                media_type="audio",
            )
        )

    first = commands_module.op_process_media(vault, operation="status")
    second = commands_module.op_process_media(vault, operation="status")

    assert first == second
    assert first["counts"][media_jobs.PENDING] == total
    assert len(first["jobs"]) == media_jobs.STATUS_JOB_LIMIT


def test_process_media_product_leaf_reconciles_and_retries_all_without_asr_wait(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_processing = _media_processing()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda vault_root, *, limit, reconcile_one, propagate_transient_errors: calls.append(
            (
                "reconcile",
                limit,
                callable(reconcile_one),
                propagate_transient_errors,
            )
        )
        or 3,
    )
    monkeypatch.setattr(
        media_processing,
        "retry_all_media",
        lambda vault_root, *, limit, commit_guard, propagate_transient_errors: calls.append(
            ("retry", limit, callable(commit_guard), propagate_transient_errors)
        )
        or 2,
    )

    processed = commands_module.op_process_media(vault, operation="process")
    retried = commands_module.op_process_media(vault, operation="retry")

    assert processed == {
        "operation": "process",
        "reconciled": 3,
        "index_refreshed": 0,
        "index_refresh_remaining": 0,
    }
    assert retried == {
        "operation": "retry",
        "requeued": 2,
        "index_refreshed": 0,
        "index_refresh_remaining": 0,
    }
    assert calls == [
        ("reconcile", media_processing.DEFAULT_RECONCILE_LIMIT, True, True),
        ("retry", media_jobs.STATUS_JOB_LIMIT, True, True),
    ]


def test_process_media_product_leaf_preserves_a_completed_transcript(vault: Path) -> None:
    media_processing = _media_processing()
    binary = _drop_media(vault, "product-completed.m4a")
    relative = binary.relative_to(vault).as_posix()
    initial = media_processing.reconcile_media(vault, binary)
    assert initial is not None
    store = media_jobs.MediaJobStore(vault)
    claimed = store.claim_next()
    assert claimed is not None
    store.complete(claimed)
    completed = initial.sidecar_path.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: faster-whisper:test+timed"
    ).replace("processing_state: pending", "processing_state: completed")
    completed += "\n## Extracted text\n\n[0:00] Already complete.\n"
    initial.sidecar_path.write_text(completed, encoding="utf-8")
    before = initial.sidecar_path.read_bytes()

    processed = commands_module.op_process_media(vault, path=relative, operation="process")
    retried = commands_module.op_process_media(vault, path=relative, operation="retry")

    assert processed["state"] == retried["state"] == media_jobs.COMPLETED
    assert processed["job_id"] is retried["job_id"] is None
    assert initial.sidecar_path.read_bytes() == before
    assert _job_count(vault) == 0


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
