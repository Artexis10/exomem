"""A discard of an adopt import's stage keeps a residual record, and never passes over a member that is not filed."""
from __future__ import annotations

import ast
import json
import shutil
import uuid
from pathlib import Path

import pytest
import test_adoption_consume as consume
from test_adoption_consume import PDF
from test_adoption_rename import OTHER, _job_stage, _stop_after_the_rename_of

from exomem import adoption_run, cloud_import_job, import_stage
from exomem.cloud_import import ArchiveRefused

KB_SCAN = "Knowledge Base/Sources/Imported/2026-10-27-scan.bin"


def _cellctl_constants(*names: str) -> tuple[str, ...]:
    """cellctl's own values for `names`, read from its source: cellctl needs dependencies this suite lacks.

    cellctl reads the Job's owner mark by these names, so a rename on either side fails the tests that use them."""
    path = Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src" / "cellctl" / "imports.py"
    found = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            found[target.id] = node.value.value
    missing = [name for name in names if name not in found]
    assert not missing, f"cellctl's imports.py no longer assigns {missing} a constant"
    return tuple(found[name] for name in names)


OWNER_MARK = dict([_cellctl_constants("RETRY_OWNER_KEY", "RETRY_OWNER")])


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return consume._vault(tmp_path)


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


def _receipt_file(vault: Path, import_id: str) -> Path:
    return import_stage.receipt_path(vault, import_id)


def _receipt(vault: Path, import_id: str) -> dict:
    return json.loads(_receipt_file(vault, import_id).read_text(encoding="utf-8"))


def _stage_dir(vault: Path, import_id: str) -> Path:
    return vault / "_Imports" / import_id


def _file_all_but(vault: Path, import_id: str, *left: str) -> str:
    """Adopt the stage with `left` (stage-relative paths) unselected, and finish the run; returns the run id."""
    start_path = f"_Imports/{import_id}"
    run_id = adoption_run.start(vault, path=start_path, today=consume.TODAY)["run_id"]
    adoption_run.select(
        vault, run_id=run_id, include=[start_path], exclude=[f"{start_path}/{path}" for path in left])
    plan_id = adoption_run.plan(vault, run_id=run_id, today=consume.TODAY)["plan"]["plan_id"]
    consume._apply(vault, run_id, plan_id)
    adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)
    return run_id


def _run_discard(monkeypatch, vault: Path, import_id: str, *flags: str, tmp_path: Path) -> dict:
    """Run the Job's `discard` command as the cell does and return its termination message."""
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "abcdefghijklmnop")
    message = tmp_path / "termination.json"
    cloud_import_job.main([
        "discard", "--vault", str(vault), "--import-id", import_id, "--termination-log", str(message), *flags])
    return json.loads(message.read_text(encoding="ascii"))


# 1


def test_a_discard_refuses_while_a_renamed_member_has_no_page_and_names_it_in_the_receipt(
    vault: Path, monkeypatch
) -> None:
    """Catches a discard that drops the manifest and the stage of a renamed binary: its only copy would have no record."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    run_id, plan_id = _stop_after_the_rename_of(vault, monkeypatch, import_id)

    with pytest.raises(ArchiveRefused) as refused:
        cloud_import_job.discard(vault, import_id)

    receipt = _receipt(vault, import_id)
    assert refused.value.code == "IMPORT_RENAMES_PENDING"
    assert receipt[import_stage.RENAMES_PENDING_KEY] == [KB_SCAN]
    assert import_stage.MEMBERS_KEY in receipt and import_stage.RESIDUAL_KEY not in receipt
    assert _stage_dir(vault, import_id).is_dir() and (vault / KB_SCAN).read_bytes() == PDF

    consume._apply(vault, run_id, plan_id)
    cloud_import_job.discard(vault, import_id)

    receipt = _receipt(vault, import_id)
    assert receipt[import_stage.RESIDUAL_KEY] == import_stage.residual_record([])
    assert import_stage.RENAMES_PENDING_KEY not in receipt and not _stage_dir(vault, import_id).exists()


def test_a_discard_lists_a_renamed_member_whose_replay_was_refused_once_the_person_moves_it(
    vault: Path, tmp_path: Path, monkeypatch
) -> None:
    """Catches a record built from the stage alone: a member that left it by rename and got no page reads as filed."""
    import_id = _job_stage(vault, {"scan.bin": PDF, "zz.bin": OTHER})
    run_id, plan_id = _stop_after_the_rename_of(vault, monkeypatch, import_id)
    (vault / KB_SCAN).write_bytes(PDF + b"changed since the rename")
    consume._apply(vault, run_id, plan_id)
    adoption_run.finish(vault, run_id=run_id, today=consume.TODAY)
    with pytest.raises(ArchiveRefused) as refused:
        cloud_import_job.discard(vault, import_id)
    assert refused.value.code == "IMPORT_RENAMES_PENDING"

    (vault / KB_SCAN).rename(tmp_path / "moved-scan.bin")
    cloud_import_job.discard(vault, import_id)

    assert import_stage.read_residual(vault, import_id) == [
        {"path": "Field Notes/scan.bin", "bytes": len(PDF), "code": "failed"}]


# 2


def test_a_discard_keeps_the_unfiled_members_in_the_receipt_in_the_write_that_drops_the_manifest(
    vault: Path, monkeypatch
) -> None:
    """Catches a discard that erases the only sign that a member was never filed, or that writes it in a second step."""
    import_id = _job_stage(vault, {"filed.pdf": PDF, "left.pdf": PDF + b"left", "plan.md": b"# Plan\n\nInvented plan.\n"})
    (_stage_dir(vault, import_id) / import_stage.SERIES_FILE).write_text("{}", encoding="utf-8")
    _file_all_but(vault, import_id, "Field Notes/left.pdf")
    writes: list[dict] = []
    real = cloud_import_job._private_json
    monkeypatch.setattr(
        cloud_import_job, "_private_json", lambda path, value: (writes.append(json.loads(json.dumps(value))), real(path, value)))

    cloud_import_job.discard(vault, import_id)

    assert len(writes) == 1 and import_stage.MEMBERS_KEY not in writes[0]
    assert writes[0][import_stage.RESIDUAL_KEY] == import_stage.residual_record([
        {"path": "Field Notes/left.pdf", "bytes": len(PDF + b"left"), "code": "unselected"}])
    assert import_stage.read_residual(vault, import_id) == writes[0][import_stage.RESIDUAL_KEY][import_stage.MEMBERS_KEY]
    assert not _stage_dir(vault, import_id).exists()


# 3


def test_a_retried_discard_keeps_the_record_that_the_first_one_wrote(vault: Path) -> None:
    """Catches a retry after a half-removed stage that records the few files left and forgets the others."""
    import_id = _job_stage(vault, {"filed.pdf": PDF, "left.pdf": PDF + b"left", "also.pdf": PDF + b"also"})
    _file_all_but(vault, import_id, "Field Notes/left.pdf", "Field Notes/also.pdf")
    cloud_import_job.discard(vault, import_id)
    first = _receipt(vault, import_id)[import_stage.RESIDUAL_KEY]
    assert len(first[import_stage.MEMBERS_KEY]) == 2
    left_behind = _stage_dir(vault, import_id) / "Field Notes" / "left.pdf"
    left_behind.parent.mkdir(parents=True)
    left_behind.write_bytes(PDF + b"left")

    cloud_import_job.discard(vault, import_id)

    assert _receipt(vault, import_id)[import_stage.RESIDUAL_KEY] == first
    assert not _stage_dir(vault, import_id).exists()


def test_a_discard_writes_an_empty_record_when_every_member_was_filed(vault: Path) -> None:
    """Catches a window with no record: the status could not tell a window that was filed whole from one never read."""
    import_id = _job_stage(vault, {"filed.pdf": PDF})
    _file_all_but(vault, import_id)
    assert not _stage_dir(vault, import_id).exists()

    cloud_import_job.discard(vault, import_id)

    assert _receipt(vault, import_id)[import_stage.RESIDUAL_KEY] == import_stage.residual_record([])
    assert import_stage.read_residual(vault, import_id) == []


def test_a_discard_that_finds_no_manifest_no_record_and_no_stage_records_nothing(vault: Path) -> None:
    """Catches a retry of an older discard that reports a window as filed whole after that discard removed the evidence."""
    import_id = _job_stage(vault, {"left.pdf": PDF})
    receipt = _receipt(vault, import_id)
    del receipt[import_stage.MEMBERS_KEY]
    _receipt_file(vault, import_id).write_text(json.dumps(receipt), encoding="utf-8")
    shutil.rmtree(_stage_dir(vault, import_id))

    cloud_import_job.discard(vault, import_id)

    assert import_stage.read_residual(vault, import_id) is None


def test_a_discard_that_waits_on_a_renamed_member_tells_the_controller_that_the_owner_settles_it(
    vault: Path, tmp_path: Path, monkeypatch
) -> None:
    """Catches a refusal that only the person can settle but looks retryable: the controller would stop the cell hourly."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    _stop_after_the_rename_of(vault, monkeypatch, import_id)

    result = _run_discard(monkeypatch, vault, import_id, tmp_path=tmp_path)

    assert result == {"ok": False, "error": "IMPORT_RENAMES_PENDING", **OWNER_MARK}


def test_a_discard_that_fails_for_a_reason_a_retry_may_fix_carries_no_retry_value(
    vault: Path, tmp_path: Path, monkeypatch
) -> None:
    """Catches a retry value on every failure: a transient fault would then wait a day."""
    import_id = _job_stage(vault, {"scan.bin": PDF})

    def broken(*_args, **_kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(cloud_import_job, "discard", broken)

    result = _run_discard(monkeypatch, vault, import_id, tmp_path=tmp_path)

    assert result == {"ok": False, "error": "IMPORT_FAILED"}


# 4


@pytest.mark.parametrize("damaged", ["{ not a receipt", "{}"])
def test_a_discard_refuses_and_keeps_the_stage_when_it_cannot_read_the_receipt(
    vault: Path, tmp_path: Path, monkeypatch, damaged: str
) -> None:
    """Catches a discard that deletes a stage it cannot account for: the person would delete the source."""
    import_id = _job_stage(vault, {"scan.bin": PDF, "other.bin": OTHER})
    _receipt_file(vault, import_id).write_text(damaged, encoding="utf-8")

    result = _run_discard(monkeypatch, vault, import_id, tmp_path=tmp_path)

    assert result == {"ok": False, "error": "IMPORT_RECEIPT_UNREADABLE", **OWNER_MARK}
    assert (_stage_dir(vault, import_id) / "Field Notes" / "scan.bin").read_bytes() == PDF
    assert _receipt_file(vault, import_id).read_text(encoding="utf-8") == damaged


# 5


def test_a_walking_discard_records_each_staged_file_but_the_series_file_and_leaves_the_receipt(
    vault: Path, tmp_path: Path, monkeypatch
) -> None:
    """Catches a way out of the refusal that either loses the files or overwrites the receipt it could not read."""
    import_id = _job_stage(vault, {"scan.bin": PDF, "deep/other.bin": OTHER})
    (_stage_dir(vault, import_id) / import_stage.SERIES_FILE).write_text("{}", encoding="utf-8")
    _receipt_file(vault, import_id).write_text("{ not a receipt", encoding="utf-8")

    result = _run_discard(monkeypatch, vault, import_id, "--walk-stage", tmp_path=tmp_path)

    assert result == {"ok": True}
    code = import_stage.UNREADABLE_CODE
    assert sorted(import_stage.read_residual(vault, import_id), key=lambda member: member["path"]) == [
        {"path": "Field Notes/deep/other.bin", "bytes": len(OTHER), "code": code},
        {"path": "Field Notes/scan.bin", "bytes": len(PDF), "code": code}]
    assert _receipt_file(vault, import_id).read_text(encoding="utf-8") == "{ not a receipt"
    assert not _stage_dir(vault, import_id).exists()


# 6


def test_a_walking_discard_refuses_when_the_receipt_is_readable_and_keeps_the_stage(vault: Path) -> None:
    """Catches a walk that skips the settling of renamed members and records the wrong codes."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    before = _receipt_file(vault, import_id).read_bytes()

    with pytest.raises(ArchiveRefused) as refused:
        cloud_import_job.discard(vault, import_id, walk_stage=True)

    assert refused.value.code == "IMPORT_REQUEST_INVALID"
    assert (_stage_dir(vault, import_id) / "Field Notes" / "scan.bin").is_file()
    assert _receipt_file(vault, import_id).read_bytes() == before
    assert import_stage.read_residual(vault, import_id) is None


# 7


def test_a_retried_walk_keeps_the_record_that_the_first_walk_wrote(vault: Path) -> None:
    """Catches a retry after a half-removed stage that overwrites the full list with the few files left."""
    import_id = _job_stage(vault, {"scan.bin": PDF, "other.bin": OTHER})
    _receipt_file(vault, import_id).write_text("{ not a receipt", encoding="utf-8")
    cloud_import_job.discard(vault, import_id, walk_stage=True)
    first = import_stage.read_residual(vault, import_id)
    assert len(first) == 2
    left_behind = _stage_dir(vault, import_id) / "Field Notes" / "other.bin"
    left_behind.parent.mkdir(parents=True)
    left_behind.write_bytes(OTHER)

    cloud_import_job.discard(vault, import_id, walk_stage=True)

    assert import_stage.read_residual(vault, import_id) == first
    assert not _stage_dir(vault, import_id).exists()



def test_a_walking_discard_that_a_renamed_member_refuses_names_it_in_the_walk_file_and_a_retry_still_walks(
    vault: Path, monkeypatch
) -> None:
    """Catches a refusal that names the renamed member nowhere when the receipt cannot carry it: the status goes blind."""
    import_id = _job_stage(vault, {"scan.bin": PDF})
    _stop_after_the_rename_of(vault, monkeypatch, import_id)
    _receipt_file(vault, import_id).write_text("{ not a receipt", encoding="utf-8")

    with pytest.raises(ArchiveRefused) as refused:
        cloud_import_job.discard(vault, import_id, walk_stage=True)

    assert refused.value.code == "IMPORT_RENAMES_PENDING"
    assert import_stage.read_walk(vault, import_id)[import_stage.RENAMES_PENDING_KEY] == [KB_SCAN]
    assert import_stage.read_residual(vault, import_id) is None and _stage_dir(vault, import_id).is_dir()

    (vault / KB_SCAN).unlink()
    cloud_import_job.discard(vault, import_id, walk_stage=True)

    assert import_stage.read_residual(vault, import_id) == []
    assert import_stage.RENAMES_PENDING_KEY not in import_stage.read_walk(vault, import_id)
    assert not _stage_dir(vault, import_id).exists()


def _walk_then_make_the_receipt_readable_again(vault: Path, import_id: str) -> None:
    receipt = _receipt_file(vault, import_id)
    readable = receipt.read_text(encoding="utf-8")
    receipt.write_text("{ not a receipt", encoding="utf-8")
    cloud_import_job.discard(vault, import_id, walk_stage=True)
    receipt.write_text(readable, encoding="utf-8")


def test_a_plain_discard_after_a_walk_still_lists_the_walked_members(vault: Path) -> None:
    """Catches a record built from the removed stage: it is empty, outranks the walk's record and hides its members."""
    import_id = _job_stage(vault, {"scan.bin": PDF, "other.bin": OTHER})
    _walk_then_make_the_receipt_readable_again(vault, import_id)

    cloud_import_job.discard(vault, import_id)

    assert sorted(member["path"] for member in import_stage.read_residual(vault, import_id)) == [
        "Field Notes/other.bin", "Field Notes/scan.bin"]


def test_a_plain_discard_after_a_walk_writes_no_empty_record_over_the_walk(vault: Path) -> None:
    """Catches an empty record for a window that a walk listed: the receipt's record outranks the walk's and says all filed."""
    import_id = _job_stage(vault, {"plan.md": b"# Plan\n\nInvented plan.\n"})
    _file_all_but(vault, import_id)
    _walk_then_make_the_receipt_readable_again(vault, import_id)

    cloud_import_job.discard(vault, import_id)

    assert import_stage.RESIDUAL_KEY not in _receipt(vault, import_id)
    assert import_stage.read_residual(vault, import_id) == [
        {"path": "Field Notes/plan.md", "bytes": len(b"# Plan\n\nInvented plan.\n"), "code": "receipt_unreadable"}]


def _walk_a_stage_with_a_file_the_manifest_does_not_list(vault: Path) -> str:
    """A stage of one manifest member, `left.bin`, and `extra.bin` put there later, walked while the receipt is unreadable."""
    import_id = _job_stage(vault, {"left.bin": PDF})
    (_stage_dir(vault, import_id) / "Field Notes" / "extra.bin").write_bytes(OTHER)
    _walk_then_make_the_receipt_readable_again(vault, import_id)
    return import_id


def test_a_plain_discard_after_a_walk_keeps_the_members_that_only_the_walk_listed(vault: Path) -> None:
    """Catches a record built from the manifest alone: it outranks the walk's, which listed a file the manifest did not."""
    import_id = _walk_a_stage_with_a_file_the_manifest_does_not_list(vault)

    cloud_import_job.discard(vault, import_id)

    assert {member["path"]: member["code"] for member in import_stage.read_residual(vault, import_id)} == {
        "Field Notes/left.bin": "outside_run", "Field Notes/extra.bin": import_stage.UNREADABLE_CODE}


def test_a_plain_discard_after_a_walk_keeps_a_member_that_the_person_cleared(vault: Path) -> None:
    """Catches a record that forgets a clearance: the member comes back as unfiled, and a naive merge lists it twice."""
    import_id = _walk_a_stage_with_a_file_the_manifest_does_not_list(vault)
    import_stage.clear_residual(vault, import_id, {"Field Notes/left.bin"}, "2026-11-01T00:00:00Z")

    cloud_import_job.discard(vault, import_id)

    record = import_stage.window_record(vault, import_id)
    assert (record.kind, record.source) == (import_stage.WINDOW_DISCARDED, import_stage.RECEIPT_SOURCE)
    assert [member["path"] for member in record.members] == ["Field Notes/extra.bin"]
    assert [member["path"] for member in record.acknowledged] == ["Field Notes/left.bin"]


def test_a_walking_discard_of_an_import_with_no_stage_records_nothing(vault: Path) -> None:
    """Catches an empty record for an import nobody staged: the status would read it as a window filed whole."""
    import_id = str(uuid.uuid4())

    cloud_import_job.discard(vault, import_id, walk_stage=True)

    assert import_stage.read_residual(vault, import_id) is None
