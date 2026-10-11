"""The stopped-cell import boundary, with real age and invented archive bytes."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest

from exomem import archive_members, cloud_import_envelope, cloud_import_job

pytestmark = pytest.mark.skipif(not shutil.which("age") or not shutil.which("age-keygen"), reason="real age executables required")

CELL = "abcdefghijklmnop"


def _encrypted(tmp_path, recipient, content):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        encoded = cloud_import_envelope.Envelope(scope="Device", category="Exports", filename="export.zip").encoded()
        metadata = tarfile.TarInfo("import.json")
        metadata.size = len(encoded)
        tar.addfile(metadata, io.BytesIO(encoded))
        member = tarfile.TarInfo("archive.zip")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    return subprocess.run(
        ["age", "-r", recipient], input=payload.getvalue(), capture_output=True, check=True
    ).stdout


def test_protected_import_recovers_receipt_and_never_exposes_zip(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL_TOKEN", raising=False)
    import_id = str(uuid.uuid4())
    recipient = cloud_import_job.prepare(vault.parent, import_id, CELL)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("measurements.json", '{"value":23}')
    ciphertext = _encrypted(tmp_path, recipient, archive.getvalue())
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id=CELL, kind="archive_members",
        declared_bytes=len(archive.getvalue()) + len(cloud_import_envelope.Envelope(scope="Device", category="Exports", filename="export.zip").encoded()), declared_files=2,
        ciphertext_bytes=len(ciphertext), ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
    )
    result = cloud_import_job.run_import(
        request, vault, lambda: io.BytesIO(ciphertext)
    )
    assert result == {"ok": True, "files": 2, "bytes": request.declared_bytes}
    assert not (vault / "_Imports").exists()
    receipts = list(cloud_import_job.receipt_directory(vault).glob("*.json"))
    assert len(receipts) == 1 and receipts[0].stat().st_mode & 0o777 == 0o600
    receipt = json.loads(receipts[0].read_text())
    manifest = archive_members.read_manifest(vault, receipt["manifest"])
    assert manifest and len(manifest.members) == 1
    blob = vault / Path(receipt["manifest"]).parent / manifest.members[0]["blob"]
    assert gzip.decompress(blob.read_bytes()) == b'{"value":23}'
    receipts[0].unlink()  # process died after RAW commit, before durable receipt
    assert cloud_import_job.run_import(
        request, vault, lambda: io.BytesIO(ciphertext)
    ) == result
    assert len(list(cloud_import_job.receipt_directory(vault).glob("*.json"))) == 1
    cloud_import_job.discard(vault, import_id)
    cloud_import_job.discard(vault, import_id)  # Retry after cleanup completed before its controller journal.
    assert blob.exists() and archive_members.read_manifest(vault, receipt["manifest"])
    assert not (vault.parent / ".import-keys" / import_id).exists()
    assert cloud_import_job.run_import(request, vault, lambda: pytest.fail("completed replay fetched ciphertext")) == result


@pytest.mark.parametrize("failure", ["substitute", "truncated", "wrong-cell"])
def test_refused_import_leaves_canonical_content(vault, tmp_path, monkeypatch, failure):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL_TOKEN", raising=False)
    import_id = str(uuid.uuid4())
    recipient = cloud_import_job.prepare(vault.parent, import_id, CELL)
    ciphertext = _encrypted(tmp_path, recipient, b"not a zip")
    if failure == "truncated":
        ciphertext = ciphertext[:-16]
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id=CELL, kind="adopt", declared_files=2,
        declared_bytes=9 + len(cloud_import_envelope.Envelope(scope="Device", category="Exports", filename="export.zip").encoded()), ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
        ciphertext_bytes=len(ciphertext),
    )
    if failure == "wrong-cell":
        monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "ponmlkjihgfedcba")
    reads = iter([ciphertext, ciphertext[:-1] + b"x"] if failure == "substitute" else [ciphertext] * 2)
    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    with pytest.raises(cloud_import_job.ArchiveRefused):
        cloud_import_job.run_import(
            request, vault, lambda: io.BytesIO(next(reads))
        )
    assert before == {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert not (vault.parent / f".import-{import_id}").exists()


def test_envelope_refuses_hidden_authority_missing_version_and_duplicate_keys(tmp_path):
    (tmp_path / "archive.zip").write_bytes(b"zip")
    for metadata in (
        '{"schema_version":1,"scope":"Device","category":"Exports","filename":"export.zip"}'.encode("utf-16"),
        b'{"scope":"Device","category":"Exports","filename":"export.zip"}',
        b'{"schema_version":1,"scope":"Device","scope":"Other","category":"Exports","filename":"export.zip"}',
        b'{"schema_version":1,"scope":"Device","category":"Exports","filename":"export.zip","owner":true}',
    ):
        (tmp_path / "import.json").write_bytes(metadata)
        with pytest.raises(cloud_import_job.ArchiveRefused):
            cloud_import_envelope.read(tmp_path)


def test_cell_init_refuses_empty_vault_during_restore_swap(tmp_path):
    from exomem.cell_init import CellInitError, _ensure_vault

    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / f".restore-prior-{uuid.uuid4()}").mkdir()
    with pytest.raises(CellInitError, match="CELL_INIT_EMPTY_VOLUME_REFUSED"):
        _ensure_vault(vault, backed_up=False)
    assert list(vault.iterdir()) == []


@pytest.mark.parametrize("interrupted_move", [1, 2, 3])
def test_restore_resumes_each_swap_boundary_and_keeps_prior_state(vault, tmp_path, monkeypatch, interrupted_move):
    from exomem import init, state_paths

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    source = tmp_path / "replacement"
    init.init_vault(source, initialize_state=False)
    (source / "restored.txt").write_text("replacement")
    (vault / "original.txt").write_text("original")
    state = state_paths.ensure_vault_state_dir(vault)
    (state / "prior-index.txt").write_text("prior derived state")
    custody = state.parent.parent / "standalone-host-control-v1"
    custody.mkdir(exist_ok=True)
    (custody / "binding.json").write_text('{"binding":"retained"}')
    import_id = str(uuid.uuid4())
    recipient = cloud_import_job.prepare(vault.parent, import_id, CELL)
    tar = io.BytesIO()
    paths = list(source.rglob("*"))
    with tarfile.open(fileobj=tar, mode="w") as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(source), recursive=False)
    ciphertext = subprocess.run(["age", "-r", recipient], input=tar.getvalue(), capture_output=True, check=True).stdout
    files = [p for p in paths if p.is_file()]
    request = cloud_import_job.CloudImportRequest(import_id=import_id, cell_id=CELL, kind="restore",
        declared_files=len(files), declared_bytes=sum(p.stat().st_size for p in files),
        ciphertext_bytes=len(ciphertext), ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest())
    original_rename = cloud_import_job.os.rename
    moves = 0

    def crash_after_move(source, target):
        nonlocal moves
        original_rename(source, target)
        moves += 1
        if moves == interrupted_move:
            raise RuntimeError("simulated process loss")

    with monkeypatch.context() as patch:
        patch.setattr(cloud_import_job.os, "rename", crash_after_move)
        with pytest.raises(RuntimeError, match="simulated process loss"):
            cloud_import_job.run_import(request, vault, lambda: io.BytesIO(ciphertext))
    if not vault.exists():
        vault.mkdir()  # cell-init prepares the mount directory before refusing initialization.
    assert cloud_import_job.run_import(request, vault, lambda: io.BytesIO(ciphertext))["ok"]
    prior = vault.parent / f".restore-prior-{import_id}"
    assert (prior / "vault" / "original.txt").read_text() == "original"
    assert (prior / "state" / "prior-index.txt").read_text() == "prior derived state"
    assert (vault / "restored.txt").read_text() == "replacement"
    assert (custody / "binding.json").read_text() == '{"binding":"retained"}'
    assert not (vault / "original.txt").exists()


def test_command_wrong_key_reports_only_a_content_free_error(vault, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    import_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
    message = tmp_path / "termination.json"
    assert cloud_import_job.main(["prepare", "--vault", str(vault), "--import-id", import_id,
                                  "--termination-log", str(message)]) == 0
    recipient = cloud_import_job.prepare(vault.parent, other_id, CELL)
    ciphertext = _encrypted(tmp_path, recipient, b"private-member-canary")
    request = cloud_import_job.CloudImportRequest(import_id=import_id, cell_id=CELL, kind="adopt",
        declared_files=2, declared_bytes=999, ciphertext_bytes=len(ciphertext),
        ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest())
    monkeypatch.setattr(cloud_import_job, "urlopen", lambda *args, **kwargs: io.BytesIO(ciphertext))
    assert cloud_import_job.main(["import", "--vault", str(vault), "--import-id", import_id,
        "--request", request.model_dump_json(), "--url", "https://storage.example/import",
        "--termination-log", str(message)]) == 1
    assert json.loads(message.read_text()) == {"ok": False, "error": "ARCHIVE_UNDECRYPTABLE"}
    output = capsys.readouterr()
    assert output.out == output.err == ""
