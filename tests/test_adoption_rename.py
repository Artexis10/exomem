"""Adoption files a staged artifact by renaming it into the Knowledge Base, and every stop leaves it recoverable."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import types
import uuid
from pathlib import Path

import pytest
import test_adoption_consume as consume
from test_adoption_consume import PDF

from exomem import (
    adopt,
    adoption_run,
    backfill,
    import_stage,
    media_jobs,
    media_processing,
    rename_journal,
    state_paths,
)

MiB = 1024 * 1024
#: Sixteen MiB: far above the few MiB of imports and state that a run reads, and below any adoption ceiling a test sets.
#: A `.bin` file has no text extraction, so the run reads its bytes only to file it.
BIG = PDF + b"x" * (16 * MiB)
OTHER = b"another member, so one failure does not fail the whole run"


class _Stopped(BaseException):
    """A process stop: nothing in the run catches it, so no cleanup runs."""


def _job_stage(vault: Path, files: dict[str, bytes]) -> str:
    """A stage as the Cloud import Job leaves it: files under `_Imports/<id>/` and a receipt manifest with identities."""
    import_id = str(uuid.uuid4())
    members = []
    for rel, data in files.items():
        path = vault / "_Imports" / import_id / "Field Notes" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        members.append(import_stage.member(
            f"Field Notes/{rel}", len(data), hashlib.sha256(data).hexdigest(), import_stage.Identity.of(path.stat())))
    request = consume.cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id="abcdefghijklmnop", kind="adopt", declared_files=len(files),
        declared_bytes=sum(len(data) for data in files.values()), ciphertext_bytes=1, ciphertext_sha256="0" * 64)
    receipts = state_paths.ensure_vault_state_dir(vault) / "cloud-imports"
    receipts.mkdir(exist_ok=True)
    (receipts / f"{import_id}.json").write_text(json.dumps({
        "request": request.model_dump(), "result": {"ok": True, "files": len(files)}, "members": members}))
    return import_id


def _staged(vault: Path, import_id: str, rel: str = "scan.bin") -> Path:
    return vault / "_Imports" / import_id / "Field Notes" / rel


def _original(import_id: str, rel: str = "scan.bin") -> str:
    return f"_Imports/{import_id}/Field Notes/{rel}"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return consume._vault(tmp_path)


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


@pytest.fixture
def media_store_configured(vault: Path, tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "media.json"
    config.write_text(json.dumps({"vaults": {str(vault.resolve()): {
        "store": "test-store", "adapter": "filesystem-test", "root": str(tmp_path / "objects"), "key_version": "1",
        "enabled": True, "allowance_bytes": 10_000_000_000, "max_temporary_bytes": 10_000_000_000}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))


@contextlib.contextmanager
def _counting_bytes_read():
    """Count the bytes this process reads through the kernel, whatever code reads them.

    A spy on one helper would miss a new route to the bytes; the kernel's read counter cannot. The run's own reads
    (state, pages, index) come to a few KiB, so a read of a `BIG` file shows as a difference of megabytes.
    """
    if not Path("/proc/self/io").exists():
        pytest.skip("the kernel read counter is a Linux feature")

    def read_characters() -> int:
        return next(int(line.split()[1]) for line in Path("/proc/self/io").read_text().splitlines()
                    if line.startswith("rchar:"))

    count = types.SimpleNamespace(bytes=0)
    before = read_characters()
    try:
        yield count
    finally:
        count.bytes = read_characters() - before


def _page_of(vault: Path, artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".md")


def _touch(path: Path) -> None:
    """Change the file's identity (mtime and ctime) and nothing else."""
    stamp = path.stat().st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _stop_after_the_rename(monkeypatch) -> None:
    """The process stops once the binary is renamed and before the batch that commits its Source page."""
    def stop(*_arguments, **_keywords):
        raise _Stopped

    monkeypatch.setattr(adopt, "commit_import_items", stop)


# 1


def test_a_member_whose_identity_matches_files_by_rename_and_has_its_offload_job_on_return(
    vault: Path, media_store_configured
) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    staged = _staged(vault, import_id)
    inode = staged.stat().st_ino
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")

    with _counting_bytes_read() as read:
        reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    artifact = vault / outcome["artifact_path"]
    assert outcome["status"] == "applied" and outcome["consumed"] is True
    assert artifact.stat().st_ino == inode and not staged.exists()
    assert read.bytes < len(BIG) // 2
    assert media_jobs.MediaJobStore(vault).get_by_binary(
        artifact, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is not None


# 2


def test_a_member_whose_identity_changed_is_hashed_once_and_filed_by_rename_when_its_bytes_match(vault: Path) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    staged = _staged(vault, import_id)
    _touch(staged)
    inode = staged.stat().st_ino
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")

    with _counting_bytes_read() as read:
        reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "applied" and outcome["consumed"] is True
    assert (vault / outcome["artifact_path"]).stat().st_ino == inode
    assert len(BIG) <= read.bytes < len(BIG) + len(BIG) // 2


def test_a_member_whose_bytes_changed_stays_in_its_stage_and_is_reported_as_changed(vault: Path) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG, "other.bin": OTHER})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    changed = BIG[:-1] + b"Y"
    staged.write_bytes(changed)

    reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "failed" and outcome["code"] == "SOURCE_CHANGED"
    assert staged.read_bytes() == changed
    assert not (vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin").exists()
    assert adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)["finish"]["residual"]["by_reason"] == {
        "failed": {"files": 1, "bytes": len(changed)}}


# 3


def test_a_mismatched_member_above_the_ceiling_goes_to_the_residual_with_no_byte_read(
    vault: Path, monkeypatch
) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG, "other.bin": OTHER})
    staged = _staged(vault, import_id)
    _touch(staged)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    monkeypatch.setenv("EXOMEM_ADOPTION_MAX_FILE_BYTES", str(MiB))

    with _counting_bytes_read() as read:
        reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "failed" and outcome["code"] == "OVER_ADOPTION_CEILING"
    assert read.bytes < len(BIG) // 2
    assert staged.read_bytes() == BIG
    assert adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)["finish"]["residual"]["by_reason"] == {
        "over_ceiling": {"files": 1, "bytes": len(BIG)}}


# 4


def test_a_rename_across_devices_falls_back_to_the_streamed_copy(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": PDF})
    staged = _staged(vault, import_id)
    inode = staged.stat().st_ino
    monkeypatch.setattr(consume._held_fs_posix, "_rename_noreplace",
                        consume._rename_across_devices(consume._held_fs_posix._rename_noreplace, "scan.bin"))

    _run_id, _plan_id, reply = consume._adopt(vault, f"_Imports/{import_id}")

    outcome = reply["outcomes"][_original(import_id)]
    artifact = vault / outcome["artifact_path"]
    assert outcome["status"] == "applied" and outcome["consumed"] is True
    assert artifact.read_bytes() == PDF and artifact.stat().st_ino != inode
    assert not staged.exists() and not list((state_paths.vault_state_dir(vault) / rename_journal.STORE).glob("*"))


# 5


def test_a_rename_never_replaces_a_file_that_appears_at_the_knowledge_base_path(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": PDF, "other.bin": OTHER})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    occupant = b"another capture, filed meanwhile"
    held = rename_journal.hold

    def occupy_then_hold(vault_root, relative_path, inode):
        if relative_path.endswith("scan.bin"):
            (vault_root / relative_path).write_bytes(occupant)
        held(vault_root, relative_path, inode)

    monkeypatch.setattr(rename_journal, "hold", occupy_then_hold)

    reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    assert outcome["status"] == "failed" and outcome["code"] == "TARGET_OCCUPIED"
    assert artifact.read_bytes() == occupant and staged.read_bytes() == PDF
    assert not _page_of(vault, artifact).exists()


# 6


def test_a_stop_after_the_rename_commits_the_source_page_on_replay(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    staged = _staged(vault, import_id)
    inode = staged.stat().st_ino
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    with monkeypatch.context() as stopping:
        _stop_after_the_rename(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    assert artifact.stat().st_ino == inode and not staged.exists() and not _page_of(vault, artifact).exists()

    with _counting_bytes_read() as read:
        reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "applied" and outcome["consumed"] is True
    assert _page_of(vault, artifact).is_file() and artifact.stat().st_ino == inode
    assert read.bytes < len(BIG) // 2
    assert not rename_journal.is_held(vault, artifact)


def test_a_replay_hashes_a_changed_identity_in_place_and_commits_when_the_bytes_match(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    with monkeypatch.context() as stopping:
        _stop_after_the_rename(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    _touch(artifact)

    with _counting_bytes_read() as read:
        reply = consume._apply(vault, run_id, plan_id)

    assert reply["outcomes"][_original(import_id)]["status"] == "applied"
    assert _page_of(vault, artifact).is_file() and not _staged(vault, import_id).exists()
    assert len(BIG) <= read.bytes < len(BIG) + len(BIG) // 2


def test_a_replay_that_finds_other_bytes_leaves_the_file_unbound_and_moves_nothing_into_the_stage(
    vault: Path, monkeypatch
) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG, "other.bin": OTHER})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    with monkeypatch.context() as stopping:
        _stop_after_the_rename(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    edited = BIG[:-1] + b"Y"
    artifact.write_bytes(edited)

    reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "failed" and outcome["code"] == "SOURCE_CHANGED"
    assert "stays unbound" in outcome["reason"]
    assert artifact.read_bytes() == edited and not _page_of(vault, artifact).exists()
    assert not _staged(vault, import_id).exists()


# 7


def test_a_stop_after_the_commit_settles_as_already_applied_with_no_byte_read(
    vault: Path, media_store_configured, monkeypatch
) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")

    def stop(*_arguments, **_keywords):
        raise _Stopped

    with monkeypatch.context() as stopping:
        stopping.setattr(adoption_run, "_settle_staged", stop)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    assert _page_of(vault, artifact).is_file() and not _staged(vault, import_id).exists()

    with _counting_bytes_read() as read:
        reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "already-applied" and outcome["consumed"] is True
    assert read.bytes < len(BIG) // 2
    assert media_jobs.MediaJobStore(vault).get_by_binary(
        artifact, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is not None


# 8


def _fail_the_batch(monkeypatch, *, before=lambda: None) -> None:
    def fail(*_arguments, **_keywords):
        before()
        raise adopt.AdoptError("WRITE_FAILED", "the volume refused the batch")

    monkeypatch.setattr(adopt, "commit_import_items", fail)


def test_a_failed_batch_renames_the_file_back_to_its_stage(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": PDF})
    staged = _staged(vault, import_id)
    inode = staged.stat().st_ino
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    _fail_the_batch(monkeypatch)

    reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "failed" and outcome["code"] == "WRITE_FAILED"
    assert staged.stat().st_ino == inode and staged.read_bytes() == PDF
    assert not list((vault / "Knowledge Base" / "Sources" / "Imported").glob("*.bin"))
    assert not list((state_paths.vault_state_dir(vault) / rename_journal.STORE).glob("*"))


def test_a_failed_batch_recreates_a_staged_folder_that_went_while_the_file_was_away(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": PDF})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    _fail_the_batch(monkeypatch, before=lambda: shutil.rmtree(staged.parent))

    adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    assert staged.read_bytes() == PDF


def test_a_failed_batch_never_removes_the_file_when_its_staged_path_is_taken(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": PDF})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    _fail_the_batch(monkeypatch, before=lambda: staged.write_bytes(b"put there meanwhile"))

    reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    outcome = reply["outcomes"][_original(import_id)]
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    assert outcome["status"] == "failed" and outcome["code"] == "ARTIFACT_UNBOUND"
    assert artifact.read_bytes() == PDF and staged.read_bytes() == b"put there meanwhile"
    assert rename_journal.is_held(vault, artifact)


# 9


def test_the_batch_writer_is_never_given_the_renamed_binary_as_a_destination(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": PDF})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    destinations: list[Path] = []
    batch = adopt.batch_atomic_write

    def record(writes, **keywords):
        writes = list(writes)
        destinations.extend(write.path for write in writes)
        return batch(writes, **keywords)

    monkeypatch.setattr(adopt, "batch_atomic_write", record)

    consume._apply(vault, run_id, plan_id)

    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    assert _page_of(vault, artifact) in destinations and artifact not in destinations


# 10


def test_no_scan_binds_a_renamed_file_before_a_replay_settles_it(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.pdf": PDF, "blob.bin": b"\x00\x01binary\x02"})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    imported = vault / "Knowledge Base" / "Sources" / "Imported"
    with monkeypatch.context() as stopping:
        _stop_after_the_rename(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    renamed = sorted(imported.glob("*.pdf")) + sorted(imported.glob("*.bin"))
    assert len(renamed) == 2

    for binary in renamed:
        media_processing.reconcile_media(vault, binary, explicit=False)
    media_processing.reconcile_all_media(vault)
    backfill.backfill_media(vault)
    backfill.backfill_artifact_pages(vault)

    assert not list(imported.glob("*.md"))
    assert media_jobs.MediaJobStore(vault).get_by_binary(renamed[0]) is None
    consume._apply(vault, run_id, plan_id)
    assert len(list(imported.glob("*.md"))) == 2

