"""Adoption removes a staged artifact once its Knowledge Base copy matches, and only what a stage manifest lists."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import types
import uuid
from pathlib import Path

import pytest

from exomem import (
    adoption_consume,
    adoption_run,
    cloud_import_job,
    held_fs,
    local_import,
    state_migration,
    state_paths,
)
from exomem.cli_progress import Progress

TODAY = dt.date(2026, 10, 27)
PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"
IMAGE = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    """`exomem import` keeps its journals in the user's state folder; a test must never write the real one."""
    monkeypatch.setattr(local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


def _vault(tmp_path: Path) -> Path:
    """A vault with a Knowledge Base whose state placement is settled, as after its first start."""
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    (kb / "Notes").mkdir(parents=True)
    (kb / "Sources").mkdir()
    (kb / "Sources" / "index.md").write_text("# Sources - Index\n\n## By type\n\n## Recent captures\n\n", encoding="utf-8")
    (kb / "index.md").write_text("# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n", encoding="utf-8")
    (kb / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    assert state_migration.bootstrap_fresh_state(vault)
    return vault


def _folder(tmp_path: Path, files: dict[str, bytes], name: str = "Field Notes") -> Path:
    folder = tmp_path / "elsewhere" / name
    for rel, data in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
    return folder


def _stage(tmp_path: Path, vault: Path, files: dict[str, bytes]) -> dict:
    """Stage a folder with the real importer, as a person does before asking for adoption."""
    result = local_import.stage(_folder(tmp_path, files), vault)
    assert result["ok"] and result["stage_recorded"]
    return result


def test_start_records_a_stage_that_has_a_manifest_and_its_proof(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF})

    run = adoption_run.start(vault, path=staged["start_path"], today=TODAY)

    assert run["stage"] == {"stage": staged["import_id"], "proof": "local-record"}
    assert run["consume"] is True
    assert adoption_run.start(vault, path=staged["start_path"], consume=False, today=TODAY)["consume"] is False


def test_start_records_no_stage_for_a_hand_made_folder_or_an_import_that_is_still_open(tmp_path: Path) -> None:
    """Nothing in these has a manifest, so nothing in them may ever be removed."""
    vault = _vault(tmp_path)
    handmade = vault / "_Imports" / str(uuid.uuid4())
    handmade.mkdir(parents=True)
    (handmade / "paper.pdf").write_bytes(PDF)
    unnamed = vault / "_Imports" / "dropped by hand"
    unnamed.mkdir()
    (unnamed / "paper.pdf").write_bytes(PDF)

    assert adoption_run.start(vault, path=f"_Imports/{handmade.name}", today=TODAY)["stage"] is None
    assert adoption_run.start(vault, path="_Imports/dropped by hand", today=TODAY)["stage"] is None
    assert adoption_run.start(vault, path="Knowledge Base", today=TODAY)["stage"] is None

    folder = _folder(tmp_path, {"a.txt": b"first", "z.txt": b"last"}, name="Open Notes")
    changed = False

    def edit_the_last_file_while_copying(line: str) -> None:
        nonlocal changed
        if line.startswith("Copying:") and not changed:
            changed = True
            (folder / "z.txt").write_bytes(b"last, then edited while staging")

    open_import = local_import.stage(folder, vault, progress=Progress(edit_the_last_file_while_copying, interval=0))
    assert open_import["ok"] is False
    assert adoption_run.start(vault, path=open_import["start_path"], today=TODAY)["stage"] is None


def _cloud_stage(vault: Path, files: dict[str, bytes]) -> str:
    """A stage laid out as the Cloud import Job leaves it: `_Imports/<id>/` and a receipt that lists its files."""
    import_id = str(uuid.uuid4())
    members = []
    for rel, data in files.items():
        path = vault / "_Imports" / import_id / "Field Notes" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        members.append({"path": f"Field Notes/{rel}", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id="abcdefghijklmnop", kind="adopt", declared_files=len(files),
        declared_bytes=sum(len(data) for data in files.values()), ciphertext_bytes=1, ciphertext_sha256="0" * 64)
    receipts = state_paths.ensure_vault_state_dir(vault) / "cloud-imports"
    receipts.mkdir(exist_ok=True)
    (receipts / f"{import_id}.json").write_text(json.dumps({
        "request": request.model_dump(), "result": {"ok": True, "files": len(files)}, "members": members}))
    return import_id


def test_start_records_the_proof_of_a_cloud_import_that_has_a_receipt_manifest(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    import_id = _cloud_stage(vault, {"paper.pdf": PDF})

    run = adoption_run.start(vault, path=f"_Imports/{import_id}", today=TODAY)

    assert run["stage"] == {"stage": import_id, "proof": "cloud-receipt"}


def _plan(vault: Path, start_path: str, *, consume: bool = True) -> tuple[str, str]:
    """start, select everything and plan; returns the run id and its plan id."""
    run = adoption_run.start(vault, path=start_path, consume=consume, today=TODAY)
    run_id = run["run_id"]
    adoption_run.select(vault, run_id=run_id, include=[start_path])
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    while planned.get("continuation"):
        planned = adoption_run.plan(vault, run_id=run_id, continuation=planned["continuation"], today=TODAY)
    return run_id, planned["plan"]["plan_id"]


def _apply(vault: Path, run_id: str, plan_id: str, **options) -> dict:
    """apply, following every continuation; returns the last reply."""
    reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY, **options)
    while reply.get("continuation"):
        reply = adoption_run.apply(
            vault, run_id=run_id, plan_id=plan_id, continuation=reply["continuation"], today=TODAY)
    return reply


def _adopt(vault: Path, start_path: str, *, consume: bool = True) -> tuple[str, str, dict]:
    """start, select everything, plan and apply to the end; returns the run id, its plan id and the last reply."""
    run_id, plan_id = _plan(vault, start_path, consume=consume)
    return run_id, plan_id, _apply(vault, run_id, plan_id)


def _staged(vault: Path, staged: dict, rel: str) -> Path:
    return vault / staged["start_path"] / "Field Notes" / rel


def test_a_run_removes_each_staged_artifact_once_its_knowledge_base_copy_matches(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF, "pic.png": IMAGE})

    _run_id, _plan_id, reply = _adopt(vault, staged["start_path"])

    for original, data in (("paper.pdf", PDF), ("pic.png", IMAGE)):
        outcome = reply["outcomes"][f"{staged['start_path']}/Field Notes/{original}"]
        assert outcome["status"] == "applied" and outcome["consumed"] is True
        assert (vault / outcome["artifact_path"]).read_bytes() == data
        assert not _staged(vault, staged, original).exists()
    assert reply["consumed"] == 2


def test_a_staged_copy_stays_when_the_knowledge_base_copy_no_longer_matches_the_plan(tmp_path: Path) -> None:
    """The staged file may be the only good copy, so it goes only after a read-back of the other one matches."""
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF})
    _run_id, _plan_id, first = _adopt(vault, staged["start_path"], consume=False)
    artifact = vault / first["outcomes"][f"{staged['start_path']}/Field Notes/paper.pdf"]["artifact_path"]
    artifact.write_bytes(PDF[:-1] + b"X")

    _run_id, _plan_id, reply = _adopt(vault, staged["start_path"])

    outcome = reply["outcomes"][f"{staged['start_path']}/Field Notes/paper.pdf"]
    assert outcome["status"] == "already-applied" and not outcome.get("consumed")
    assert _staged(vault, staged, "paper.pdf").read_bytes() == PDF
    assert reply["consumed"] == 0


def test_text_items_keep_their_staged_copies(tmp_path: Path) -> None:
    """A text item is mostly a note that is still owed a draft note, so its staged copy stays until discard."""
    vault = _vault(tmp_path)
    note = b"# Field plan\n\nInvented plan for the week.\n"
    staged = _stage(tmp_path, vault, {"plan.md": note, "paper.pdf": PDF})

    _run_id, _plan_id, reply = _adopt(vault, staged["start_path"])

    assert reply["outcomes"][f"{staged['start_path']}/Field Notes/plan.md"]["status"] in ("applied", "already-applied")
    assert _staged(vault, staged, "plan.md").read_bytes() == note
    assert not _staged(vault, staged, "paper.pdf").exists()


def test_a_run_started_with_consume_false_keeps_every_staged_copy(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF, "pic.png": IMAGE})

    _run_id, _plan_id, reply = _adopt(vault, staged["start_path"], consume=False)

    assert reply["consumed"] == 0 and all(not o.get("consumed") for o in reply["outcomes"].values())
    assert _staged(vault, staged, "paper.pdf").read_bytes() == PDF
    assert _staged(vault, staged, "pic.png").read_bytes() == IMAGE
    assert all((vault / o["artifact_path"]).read_bytes() for o in reply["outcomes"].values())


def test_nothing_is_removed_from_a_folder_under_imports_that_has_no_manifest(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    folder = vault / "_Imports" / str(uuid.uuid4()) / "Field Notes"
    folder.mkdir(parents=True)
    (folder / "paper.pdf").write_bytes(PDF)

    _run_id, _plan_id, reply = _adopt(vault, str(folder.parent.relative_to(vault)))

    assert next(iter(reply["outcomes"].values()))["status"] == "applied" and reply["consumed"] == 0
    assert (folder / "paper.pdf").read_bytes() == PDF


def test_a_file_a_manifest_does_not_describe_is_never_removed(tmp_path: Path) -> None:
    """A sync tool can add a file to a stage, or change one, after the manifest was written."""
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"listed.pdf": PDF, "changed.pdf": PDF + b"original"})
    added = PDF + b"added after the manifest"
    changed = PDF + b"changed after the manifest"
    _staged(vault, staged, "added.pdf").write_bytes(added)
    _staged(vault, staged, "changed.pdf").write_bytes(changed)

    _run_id, _plan_id, reply = _adopt(vault, staged["start_path"])

    assert not _staged(vault, staged, "listed.pdf").exists()
    assert _staged(vault, staged, "added.pdf").read_bytes() == added
    assert _staged(vault, staged, "changed.pdf").read_bytes() == changed
    assert reply["consumed"] == 1


class _DiskRefusesUnlink:
    """A held filesystem whose removals the disk refuses; every other operation is the real one."""

    def __init__(self, filesystem):
        self._filesystem = filesystem

    def __enter__(self):
        self._filesystem.__enter__()
        return self

    def __exit__(self, *exception):
        return self._filesystem.__exit__(*exception)

    def __getattr__(self, name):
        return getattr(self._filesystem, name)

    def unlink(self, file):
        return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "the disk refused the removal"))


def test_a_removal_that_fails_leaves_the_item_applied_and_a_later_call_removes_the_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF})
    # Only adoption's removal sees the refusal; the batch writer keeps the real filesystem.
    refusing = types.SimpleNamespace(
        HeldFsError=held_fs.HeldFsError, HeldFile=held_fs.HeldFile,
        acquire=lambda root: held_fs.HeldResult(value=_DiskRefusesUnlink(held_fs.acquire(root).require())))
    real_held_fs = adoption_consume.held_fs
    monkeypatch.setattr(adoption_consume, "held_fs", refusing)

    run_id, plan_id, reply = _adopt(vault, staged["start_path"])

    outcome = reply["outcomes"][f"{staged['start_path']}/Field Notes/paper.pdf"]
    assert outcome["status"] == "applied" and not outcome.get("consumed") and reply["phase"] == "applied"
    assert reply["consumed"] == 0 and _staged(vault, staged, "paper.pdf").read_bytes() == PDF

    monkeypatch.setattr(adoption_consume, "held_fs", real_held_fs)
    again = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)

    assert again["outcomes"][f"{staged['start_path']}/Field Notes/paper.pdf"]["consumed"] is True
    assert not _staged(vault, staged, "paper.pdf").exists()


def _outcome(reply: dict, staged: dict, name: str) -> dict:
    return reply["outcomes"][f"{staged['start_path']}/Field Notes/{name}"]


def test_a_stop_after_the_commit_recovers_through_the_committed_source_with_no_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF, "pic.png": IMAGE})
    run_id, plan_id = _plan(vault, staged["start_path"])

    def stop(*_arguments):
        raise KeyboardInterrupt

    # The chunk has committed and removed its staged copies; the call dies before it returns.
    with monkeypatch.context() as stopped:
        stopped.setattr(adoption_run, "_request_extraction", stop)
        with pytest.raises(KeyboardInterrupt):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert not _staged(vault, staged, "paper.pdf").exists() and not _staged(vault, staged, "pic.png").exists()

    again = _apply(vault, run_id, plan_id)

    assert again["phase"] == "applied" and again["progress"]["failed"] == 0
    for name, data in (("paper.pdf", PDF), ("pic.png", IMAGE)):
        outcome = _outcome(again, staged, name)
        assert outcome["consumed"] is True and (vault / outcome["artifact_path"]).read_bytes() == data
    assert again["verify"]["consumed"] == 2


def test_a_run_object_that_never_recorded_the_commit_resolves_each_item_from_its_source(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF, "pic.png": IMAGE})
    run_id, plan_id = _plan(vault, staged["start_path"])
    run_file = vault / "Knowledge Base" / "_Adoption" / "runs" / run_id / "run.json"
    planned_only = run_file.read_bytes()
    _apply(vault, run_id, plan_id)
    run_file.write_bytes(planned_only)

    again = _apply(vault, run_id, plan_id)

    assert again["phase"] == "applied" and again["progress"]["failed"] == 0
    assert all(o["status"] == "already-applied" and o["consumed"] for o in again["outcomes"].values())
    assert again["verify"]["consumed"] == 2


def test_a_second_run_over_the_same_stage_resolves_what_the_first_run_consumed(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF})
    second_id, second_plan = _plan(vault, staged["start_path"])
    _adopt(vault, staged["start_path"])
    assert not _staged(vault, staged, "paper.pdf").exists()

    # The second run still sees the file as selected, and the probe must not call a consumed copy stale.
    assert adoption_run.status(vault, run_id=second_id)["stale_paths"] == []
    reply = _apply(vault, second_id, second_plan)

    outcome = _outcome(reply, staged, "paper.pdf")
    assert outcome["status"] == "already-applied" and outcome["consumed"] is True
    assert reply["phase"] == "applied"


def test_retry_failed_resolves_an_item_whose_copy_was_consumed_in_the_meantime(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF, "pic.png": IMAGE})
    waiting_id, waiting_plan = _plan(vault, staged["start_path"])
    path = _staged(vault, staged, "paper.pdf")
    aside = tmp_path / "paper.pdf"
    path.rename(aside)
    failed = _apply(vault, waiting_id, waiting_plan)
    assert _outcome(failed, staged, "paper.pdf")["code"] == "NOT_FOUND" and failed["phase"] == "partial"
    aside.rename(path)
    _adopt(vault, staged["start_path"])

    retried = _apply(vault, waiting_id, waiting_plan, retry_failed=True)

    outcome = _outcome(retried, staged, "paper.pdf")
    assert outcome["status"] == "already-applied" and outcome["consumed"] is True


def test_a_listed_copy_that_is_missing_with_no_source_still_fails_and_reads_as_stale(tmp_path: Path) -> None:
    """Without a committed Source nothing says adoption removed it, so the loss is reported."""
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"paper.pdf": PDF, "pic.png": IMAGE})
    run_id, plan_id = _plan(vault, staged["start_path"])
    _staged(vault, staged, "paper.pdf").unlink()

    assert adoption_run.status(vault, run_id=run_id)["stale_paths"] == [f"{staged['start_path']}/Field Notes/paper.pdf"]
    reply = _apply(vault, run_id, plan_id)

    assert _outcome(reply, staged, "paper.pdf")["code"] == "NOT_FOUND"


def test_finish_removes_the_folders_consuming_emptied_and_reports_what_still_sits_in_the_stage(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = b"# Field plan\n\nInvented plan for the week.\n"
    staged = _stage(tmp_path, vault, {
        "paper.pdf": PDF, "sub/inner.pdf": IMAGE, "plan.md": note, "left.pdf": PDF + b"left", "changed.pdf": PDF + b"old"})
    hidden = _staged(vault, staged, ".cache/blob.bin")
    hidden.parent.mkdir()
    hidden.write_bytes(b"hidden bytes")
    run = adoption_run.start(vault, path=staged["start_path"], today=TODAY)
    run_id = run["run_id"]
    adoption_run.select(vault, run_id=run_id, include=[staged["start_path"]],
                        exclude=[f"{staged['start_path']}/Field Notes/left.pdf"])
    plan_id = adoption_run.plan(vault, run_id=run_id, today=TODAY)["plan"]["plan_id"]
    changed = PDF + b"new"
    _staged(vault, staged, "changed.pdf").write_bytes(changed)
    applied = _apply(vault, run_id, plan_id)
    assert _outcome(applied, staged, "changed.pdf")["status"] == "failed"

    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)

    residual = finished["finish"]["residual"]
    assert finished["finish"]["consumed"] == 2 and not _staged(vault, staged, "sub").exists()
    assert residual["by_reason"] == {
        "text": {"files": 1, "bytes": len(note)}, "unselected": {"files": 1, "bytes": len(PDF + b"left")},
        "failed": {"files": 1, "bytes": len(changed)}, "ineligible": {"files": 1, "bytes": len(b"hidden bytes")}}
    assert residual["files"] == 4 and residual["removed_empty_folders"] >= 1
    assert staged["start_path"] in residual["discard"]
    assert _staged(vault, staged, "plan.md").read_bytes() == note


def test_finish_removes_the_whole_stage_when_nothing_is_left_and_names_the_cloud_route_when_something_is(
        tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    emptied = _cloud_stage(vault, {"paper.pdf": PDF})
    run_id, _plan_id, _reply = _adopt(vault, f"_Imports/{emptied}")

    residual = adoption_run.finish(vault, run_id=run_id, today=TODAY)["finish"]["residual"]

    assert not (vault / "_Imports" / emptied).exists()
    assert (residual["files"], residual["discard"]) == (0, None)

    note = b"# Field plan\n\nInvented plan for the week.\n"
    with_text = _cloud_stage(vault, {"paper.pdf": IMAGE, "plan.md": note})
    run_id, _plan_id, _reply = _adopt(vault, f"_Imports/{with_text}")

    residual = adoption_run.finish(vault, run_id=run_id, today=TODAY)["finish"]["residual"]

    assert residual["by_reason"] == {"text": {"files": 1, "bytes": len(note)}}
    assert "transfer_memory" in residual["discard"] and "import_discard" in residual["discard"]
    assert with_text in residual["discard"]


def test_finish_leaves_every_folder_and_file_of_a_run_started_with_consume_false(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    staged = _stage(tmp_path, vault, {"sub/inner.pdf": IMAGE})
    _staged(vault, staged, "empty").mkdir()
    run_id, _plan_id, _reply = _adopt(vault, staged["start_path"], consume=False)

    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)

    assert _staged(vault, staged, "sub/inner.pdf").read_bytes() == IMAGE and _staged(vault, staged, "empty").is_dir()
    assert finished["finish"]["residual"]["by_reason"] == {"kept": {"files": 1, "bytes": len(IMAGE)}}
    assert finished["finish"]["residual"]["removed_empty_folders"] == 0
