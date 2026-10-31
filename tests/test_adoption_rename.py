"""Adoption files a staged artifact by renaming it into the Knowledge Base, and every stop leaves it recoverable."""
from __future__ import annotations

import collections
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
    adoption_plan,
    adoption_run,
    backfill,
    import_stage,
    index_sync,
    media_jobs,
    media_processing,
    private_state,
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
    hold_all = rename_journal.hold_all

    def occupy_then_hold(vault_root, holds):
        hold_all(vault_root, holds)
        for hold in holds:
            if hold.path.endswith("scan.bin"):
                (vault_root / hold.path).parent.mkdir(parents=True, exist_ok=True)
                (vault_root / hold.path).write_bytes(occupant)

    monkeypatch.setattr(rename_journal, "hold_all", occupy_then_hold)

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


def _stop_inside_the_batch_after_its_commit(monkeypatch) -> None:
    """The process stops once the Source page is committed, inside the batch and before the run's call returns."""
    def stop(*_arguments, **_keywords):
        raise _Stopped

    monkeypatch.setattr(index_sync, "full_upsert_succeeded", stop)


def _journal_records(vault: Path) -> list[Path]:
    return list((state_paths.vault_state_dir(vault) / rename_journal.STORE).glob("*.json"))


@pytest.mark.parametrize("run_record_kept", [True, False], ids=["run-record-kept", "run-record-lost"])
def test_a_stop_inside_the_batch_after_its_commit_settles_as_already_applied_and_drops_the_hold(
    vault: Path, media_store_configured, monkeypatch, run_record_kept: bool
) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    run_file = vault / "Knowledge Base" / "_Adoption" / "runs" / run_id / "run.json"
    planned_only = run_file.read_bytes()
    with monkeypatch.context() as stopping:
        _stop_inside_the_batch_after_its_commit(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    assert _page_of(vault, artifact).is_file() and not _staged(vault, import_id).exists()
    if not run_record_kept:  # the stop came before the run saved the commit: replay resolves the item from its Source
        run_file.write_bytes(planned_only)

    with _counting_bytes_read() as read:
        reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "already-applied" and outcome["consumed"] is True
    assert read.bytes < len(BIG) // 2
    assert not _journal_records(vault)
    assert media_jobs.MediaJobStore(vault).get_by_binary(
        artifact, media_type=media_jobs.OFFLOAD_MEDIA_TYPE) is not None


def test_a_hold_stops_holding_once_the_source_page_beside_the_file_exists(vault: Path, monkeypatch) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    with monkeypatch.context() as stopping:
        _stop_inside_the_batch_after_its_commit(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"

    assert _journal_records(vault) and not rename_journal.is_held(vault, artifact)


def test_a_stop_between_the_commit_and_the_settlement_leaves_no_hold_after_the_next_call(
    vault: Path, monkeypatch
) -> None:
    import_id = _job_stage(vault, {"scan.bin": BIG})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    with monkeypatch.context() as stopping:
        def stop(*_arguments, **_keywords):
            raise _Stopped

        stopping.setattr(rename_journal, "release", stop)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    assert _journal_records(vault)

    reply = consume._apply(vault, run_id, plan_id)

    assert reply["outcomes"][_original(import_id)]["consumed"] is True
    assert not _journal_records(vault)


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



# 11


def _stop_after_the_rename_of(vault: Path, monkeypatch, import_id: str) -> tuple[str, str]:
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    with monkeypatch.context() as stopping:
        _stop_after_the_rename(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    return run_id, plan_id


_GUARDED = {
    "cancel": lambda vault, run_id, import_id: adoption_run.cancel(vault, run_id=run_id, why=None),
    "select": lambda vault, run_id, import_id: adoption_run.select(
        vault, run_id=run_id, include=[f"_Imports/{import_id}"]),
    "plan": lambda vault, run_id, import_id: adoption_run.plan(vault, run_id=run_id, today=consume.TODAY),
    "start": lambda vault, run_id, import_id: adoption_run.start(
        vault, path=f"_Imports/{import_id}", today=consume.TODAY),
}


@pytest.mark.parametrize("verb", _GUARDED)
def test_a_run_whose_renamed_file_waits_for_its_page_refuses_to_leave_it_and_apply_finishes_it(
    vault: Path, monkeypatch, verb: str
) -> None:
    """Catches a cancel, re-plan or restart that strands a renamed file: no stage lists it and no residual names it."""
    import_id = _job_stage(vault, {"scan.bin": BIG})
    run_id, plan_id = _stop_after_the_rename_of(vault, monkeypatch, import_id)
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        _GUARDED[verb](vault, run_id, import_id)

    assert refused.value.code == "RENAMES_PENDING"
    assert run_id in refused.value.reason and "Call apply" in refused.value.reason
    assert "keep the source" in refused.value.reason
    assert rename_journal.is_held(vault, artifact)

    consume._apply(vault, run_id, plan_id)

    assert _page_of(vault, artifact).is_file() and not rename_journal.pending(vault)


def test_a_refusal_tells_the_caller_to_move_the_file_when_apply_cannot_settle_it(vault: Path, monkeypatch) -> None:
    """Catches a refusal that sends the caller to an apply that cannot settle the file once the receipt is unreadable."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    run_id, _plan_id = _stop_after_the_rename_of(vault, monkeypatch, import_id)
    import_stage.receipt_path(vault, import_id).write_text("{ not a receipt", encoding="utf-8")

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.cancel(vault, run_id=run_id, why=None)

    assert refused.value.code == "RENAMES_PENDING"
    assert "Call apply" not in refused.value.reason and "keep the source" in refused.value.reason


def test_the_journal_record_names_the_import_the_run_the_staged_path_and_the_inode(vault: Path, monkeypatch) -> None:
    """The record is what a discard reads to find a run's renamed files without its plan."""
    import_id = _job_stage(vault, {"scan.bin": BIG})
    inode = _staged(vault, import_id).stat().st_ino
    run_id, _plan_id = _stop_after_the_rename_of(vault, monkeypatch, import_id)

    assert rename_journal.pending(vault, run_id=run_id) == [rename_journal.Hold(
        path="Knowledge Base/Sources/Imported/2026-10-27-scan.bin", inode=inode, import_id=import_id,
        run_id=run_id, staged_path=_original(import_id))]
    assert rename_journal.pending(vault, under=f"_Imports/{import_id}/Field Notes")
    assert not rename_journal.pending(vault, under=f"_Imports/{import_id}/Other")


@pytest.mark.parametrize("verb", _GUARDED)
def test_a_run_verb_goes_ahead_once_the_person_moves_or_deletes_the_renamed_file(
    vault: Path, monkeypatch, verb: str
) -> None:
    """Catches a refusal that no `apply` can lift: the person removed the renamed file, so nothing is left to finish."""
    import_id = _job_stage(vault, {"scan.bin": BIG})
    run_id, _plan_id = _stop_after_the_rename_of(vault, monkeypatch, import_id)
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        _GUARDED[verb](vault, run_id, import_id)
    assert refused.value.code == "RENAMES_PENDING"

    artifact.rename(artifact.with_name("moved-away.bin"))

    try:
        _GUARDED[verb](vault, run_id, import_id)
    except adoption_run.AdoptionRunError as error:  # a stage with no file left has nothing to start on
        assert error.code != "RENAMES_PENDING"


def test_a_file_that_a_failed_batch_could_not_put_back_stays_held_after_finish_until_the_person_moves_it(
    vault: Path, monkeypatch
) -> None:
    """Catches a finish that frees a file nobody moved: a discard of the stage would then pass over an unbound binary."""
    import_id = _job_stage(vault, {"scan.bin": PDF, "other.bin": OTHER})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    with monkeypatch.context() as failing:
        _fail_the_batch(failing, before=lambda: staged.write_bytes(b"put there meanwhile"))
        adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    adoption_run.apply(
        vault, run_id=run_id, plan_id=plan_id, retry_failed=True, only_paths=[_original(import_id, "other.bin")],
        today=consume.TODAY)
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"

    adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)

    assert rename_journal.is_held(vault, artifact) and artifact.read_bytes() == PDF
    artifact.rename(artifact.with_name("moved-away.bin"))
    assert not rename_journal.is_held(vault, artifact) and not rename_journal.pending(vault)


def test_a_replay_that_finds_other_bytes_keeps_the_hold_until_the_person_moves_the_file(
    vault: Path, monkeypatch
) -> None:
    """Catches a refused replay that frees its file: a discard of the stage would then pass over an unbound binary."""
    import_id = _job_stage(vault, {"scan.bin": BIG, "other.bin": OTHER})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    with monkeypatch.context() as stopping:
        _stop_after_the_rename(stopping)
        with pytest.raises(_Stopped):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    artifact.write_bytes(BIG[:-1] + b"Y")
    reply = consume._apply(vault, run_id, plan_id)
    assert reply["outcomes"][_original(import_id)]["code"] == "SOURCE_CHANGED"

    assert rename_journal.is_held(vault, artifact)
    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.start(vault, path=f"_Imports/{import_id}", today=consume.TODAY)
    assert refused.value.code == "RENAMES_PENDING"

    artifact.unlink()
    try:
        adoption_run.start(vault, path=f"_Imports/{import_id}", today=consume.TODAY)
    except adoption_run.AdoptionRunError as error:  # the stage holds nothing else to start on
        assert error.code != "RENAMES_PENDING"


def test_a_retry_after_an_unbound_file_still_names_the_unbound_path(vault: Path, monkeypatch) -> None:
    """Catches a retry that reports only the taken staged path while the member's file sits unbound in the vault."""
    import_id = _job_stage(vault, {"scan.bin": PDF, "other.bin": OTHER})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    with monkeypatch.context() as failing:
        _fail_the_batch(failing, before=lambda: staged.write_bytes(b"put there meanwhile"))
        adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    retried = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, retry_failed=True, today=consume.TODAY)

    outcome = retried["outcomes"][_original(import_id)]
    assert outcome["status"] == "failed" and outcome["code"] == "ARTIFACT_UNBOUND"
    assert "Knowledge Base/Sources/Imported/2026-10-27-scan.bin" in outcome["reason"]


# 12


def test_the_journal_is_flushed_once_for_the_chunk_before_its_first_rename(vault: Path, monkeypatch) -> None:
    """Catches a record that is written but not flushed before the rename: a power loss would keep the rename only."""
    names = ["a.bin", "b.bin", "c.bin"]
    import_id = _job_stage(vault, {name: name.encode() * 3 for name in names})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    flushes, durable = [], []
    flush, write = private_state.sync_directory, private_state.write_record

    def spy_flush(store: Path) -> None:
        flushes.append({
            "records": len(list(store.glob("*.json"))),
            "still_staged": sum(_staged(vault, import_id, name).exists() for name in names)})
        flush(store)

    def spy_write(store: Path, stem: str, record: dict, **keywords) -> None:
        if store.name == rename_journal.STORE:
            durable.append(keywords.get("durable"))
        write(store, stem, record, **keywords)

    monkeypatch.setattr(private_state, "sync_directory", spy_flush)
    monkeypatch.setattr(private_state, "write_record", spy_write)

    consume._apply(vault, run_id, plan_id)

    assert flushes == [{"records": 3, "still_staged": 3}] and durable == [True] * 3


def test_a_temp_file_left_by_a_stopped_record_write_is_swept_at_the_next_chunk(vault: Path) -> None:
    import_id = _job_stage(vault, {"scan.bin": PDF})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    store = private_state.store(vault, rename_journal.STORE)
    leftover, recent = store / "abc.stopped.tmp", store / "abc.writing.tmp"
    leftover.write_text("{")
    recent.write_text("{")
    old = leftover.stat().st_mtime - 2 * rename_journal.STALE_TEMP_SECONDS
    os.utime(leftover, (old, old))

    consume._apply(vault, run_id, plan_id)

    assert not leftover.exists() and recent.exists()


# 13


def test_a_rename_that_reports_an_error_after_it_took_place_still_files_the_member(vault: Path, monkeypatch) -> None:
    """Catches a failed sync after the rename that reports the member failed while its file sits unheld in the vault."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    staged = _staged(vault, import_id)
    inode = staged.stat().st_ino
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    state = {"renamed": False, "refused": False}
    real_rename, real_sync = consume._held_fs_posix._rename_noreplace, consume._held_fs_posix._fsync

    def rename_then_remember(*arguments, **keywords):
        real_rename(*arguments, **keywords)
        state["renamed"] = True

    def refuse_to_sync_once_after_the_rename(descriptor):
        if state["renamed"] and not state["refused"]:
            state["refused"] = True
            raise OSError(5, "input/output error")
        real_sync(descriptor)

    monkeypatch.setattr(consume._held_fs_posix, "_rename_noreplace", rename_then_remember)
    monkeypatch.setattr(consume._held_fs_posix, "_fsync", refuse_to_sync_once_after_the_rename)

    reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "applied" and (vault / outcome["artifact_path"]).stat().st_ino == inode


# 14


def test_an_in_place_write_followed_by_an_mtime_reset_does_not_file_by_rename(vault: Path) -> None:
    """Catches a manifest identity without the change time: an edit that restores the mtime would then pass for the manifest's bytes."""
    import_id = _job_stage(vault, {"scan.bin": BIG, "other.bin": OTHER})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    before = staged.stat()
    edited = BIG[:-1] + b"Y"
    staged.write_bytes(edited)
    os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert staged.stat().st_mtime_ns == before.st_mtime_ns and staged.stat().st_size == before.st_size

    reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "failed" and outcome["code"] == "SOURCE_CHANGED"
    assert staged.read_bytes() == edited
    assert not list((vault / "Knowledge Base" / "Sources" / "Imported").glob("*scan.bin"))


# 15


def test_a_failed_batch_after_a_replay_leaves_the_file_where_it_is_and_never_puts_it_into_the_stage(
    vault: Path, monkeypatch
) -> None:
    """Catches a rollback that treats a file found by replay as this call's rename: a discard of the stage would delete it."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    staged = _staged(vault, import_id)
    inode = staged.stat().st_ino
    run_id, plan_id = _stop_after_the_rename_of(vault, monkeypatch, import_id)
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"
    with monkeypatch.context() as failing:
        _fail_the_batch(failing)
        failed = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    assert failed["outcomes"][_original(import_id)]["code"] == "WRITE_FAILED"

    assert artifact.stat().st_ino == inode and not staged.exists()
    assert rename_journal.is_held(vault, artifact)

    retried = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, retry_failed=True, today=consume.TODAY)

    assert retried["outcomes"][_original(import_id)]["status"] == "applied"
    assert _page_of(vault, artifact).is_file()


# 16


def test_a_failed_batch_does_not_move_a_different_file_that_took_the_binarys_path(vault: Path, monkeypatch) -> None:
    """Catches a rollback that trusts the path: it would carry a stranger's file into the stage, and a discard deletes the stage."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    staged = _staged(vault, import_id)
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    artifact = vault / "Knowledge Base" / "Sources" / "Imported" / "2026-10-27-scan.bin"

    def swap_in_another_file() -> None:
        # Written beside the binary first, so the stranger has an inode of its own.
        stranger = artifact.with_name("stranger")
        stranger.write_bytes(b"a different file that arrived first")
        os.replace(stranger, artifact)

    _fail_the_batch(monkeypatch, before=swap_in_another_file)

    reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    assert reply["outcomes"][_original(import_id)]["code"] == "ARTIFACT_UNBOUND"
    assert artifact.read_bytes() == b"a different file that arrived first" and not staged.exists()


# 17


def test_a_copy_fallback_member_above_the_ceiling_fails_instead_of_copying_without_a_limit(
    vault: Path, monkeypatch
) -> None:
    """Catches the rename's ceiling exemption outliving the rename: a cross-device copy of any size would then pass."""
    import_id = _job_stage(vault, {"scan.bin": BIG, "other.bin": OTHER})
    staged = _staged(vault, import_id)
    monkeypatch.setenv("EXOMEM_ADOPTION_MAX_FILE_BYTES", str(MiB))
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    monkeypatch.setattr(consume._held_fs_posix, "_rename_noreplace",
                        consume._rename_across_devices(consume._held_fs_posix._rename_noreplace, "scan.bin"))

    reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "failed" and outcome["code"] == "OVER_ADOPTION_CEILING"
    assert staged.read_bytes() == BIG
    assert not list((vault / "Knowledge Base" / "Sources" / "Imported").glob("*scan.bin"))
    assert reply["outcomes"][_original(import_id, "other.bin")]["status"] == "applied"


# 18


def test_a_member_larger_than_the_free_space_files_by_rename(vault: Path, monkeypatch) -> None:
    """Catches a space check that prices a rename like a copy: the run would stop for space that the rename never uses."""
    import_id = _job_stage(vault, {"scan.bin": BIG})
    staged = _staged(vault, import_id)
    inode = staged.stat().st_ino
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    reserve = 100_000
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(reserve))
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(10 * len(BIG), 0, reserve + len(BIG) // 4))

    reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    assert "apply_stop" not in reply and "continuation" not in reply
    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "applied" and (vault / outcome["artifact_path"]).stat().st_ino == inode


def test_the_import_reserve_still_applies_to_a_member_filed_by_rename(vault: Path, monkeypatch) -> None:
    """Catches pricing a rename at zero without the margin: derived state still needs room."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    reserve = 100_000
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(reserve))
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(10 * reserve, 0, reserve // 2))

    reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=consume.TODAY)

    assert reply["apply_stop"]["code"] == "INSUFFICIENT_SPACE" and reply["apply_stop"]["chunk_bytes"] == 0
    assert _staged(vault, import_id).read_bytes() == PDF


# 19


def test_a_matched_member_above_the_ceiling_plans_eligible_with_no_byte_read_and_files_by_rename(
    vault: Path, monkeypatch
) -> None:
    """Catches a plan that hashes, or refuses, a member the manifest vouches for: a 5 GiB Cloud import would never file."""
    import_id = _job_stage(vault, {"scan.bin": BIG})
    inode = _staged(vault, import_id).stat().st_ino
    monkeypatch.setenv("EXOMEM_ADOPTION_MAX_FILE_BYTES", str(MiB))

    with _counting_bytes_read() as read:
        run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
        reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id)]
    assert outcome["status"] == "applied" and outcome["consumed"] is True
    assert (vault / outcome["artifact_path"]).stat().st_ino == inode
    assert read.bytes < len(BIG) // 2


def test_a_matched_text_member_above_the_inline_limit_is_an_artifact_and_files_by_rename(vault: Path, monkeypatch) -> None:
    """Catches a refusal of a large vouched text file by the ceiling: it is an artifact, so it files like any other."""
    notes = b"# Notes\n" + b"x" * adoption_plan.INLINE_TEXT_MAX_BYTES
    import_id = _job_stage(vault, {"notes.md": notes})
    inode = _staged(vault, import_id, "notes.md").stat().st_ino
    monkeypatch.setenv("EXOMEM_ADOPTION_MAX_FILE_BYTES", str(MiB))

    run_id, plan_id = consume._plan(vault, f"_Imports/{import_id}")
    reply = consume._apply(vault, run_id, plan_id)

    outcome = reply["outcomes"][_original(import_id, "notes.md")]
    assert outcome["status"] == "applied" and (vault / outcome["artifact_path"]).stat().st_ino == inode


def test_a_member_whose_identity_changed_keeps_the_ceiling_when_the_scan_lists_it(vault: Path, monkeypatch) -> None:
    """Catches a scan that exempts every manifest member from the ceiling, including one that may hold other bytes."""
    import_id = _job_stage(vault, {"scan.bin": BIG})
    _touch(_staged(vault, import_id))
    monkeypatch.setenv("EXOMEM_ADOPTION_MAX_FILE_BYTES", str(MiB))

    run_id = adoption_run.start(vault, path=f"_Imports/{import_id}", today=consume.TODAY)["run_id"]

    row = next(row for row in adoption_run.AdoptionRunStore(vault).load(run_id)["inventory"]
               if row["path"] == _original(import_id))
    assert row["eligible"] is False and row["reason"] == "OVER_ADOPTION_CEILING"
