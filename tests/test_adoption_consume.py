"""Adoption removes a staged artifact once its Knowledge Base copy matches, and only what a stage manifest lists."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from exomem import adoption_run, cloud_import_job, local_import, state_migration, state_paths
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


def test_start_records_the_proof_of_a_cloud_import_that_has_a_receipt_manifest(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    import_id = str(uuid.uuid4())
    stage = vault / "_Imports" / import_id / "Field Notes"
    stage.mkdir(parents=True)
    (stage / "paper.pdf").write_bytes(PDF)
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id="abcdefghijklmnop", kind="adopt", declared_files=1, declared_bytes=len(PDF),
        ciphertext_bytes=1, ciphertext_sha256="0" * 64)
    members = [{"path": "Field Notes/paper.pdf", "size": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()}]
    receipts = state_paths.ensure_vault_state_dir(vault) / "cloud-imports"
    receipts.mkdir()
    (receipts / f"{import_id}.json").write_text(json.dumps(
        {"request": request.model_dump(), "result": {"ok": True, "files": 1, "bytes": len(PDF)}, "members": members}))

    run = adoption_run.start(vault, path=f"_Imports/{import_id}", today=TODAY)

    assert run["stage"] == {"stage": import_id, "proof": "cloud-receipt"}
