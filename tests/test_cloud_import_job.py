"""The stopped-cell import boundary, with real age and invented archive bytes."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
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
    # A bit flip always changes ciphertext; a fixed replacement byte can already match.
    substituted = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
    reads = iter([ciphertext, substituted] if failure == "substitute" else [ciphertext] * 2)
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


def test_commands_keep_identity_private_and_discard_only_the_named_import(vault, tmp_path, monkeypatch, capsys, caplog):
    """A leaked identity or an overbroad discard would expose or erase another import."""
    caplog.set_level("DEBUG")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    import_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
    message = tmp_path / "termination.json"
    command = ["--vault", str(vault), "--import-id", import_id, "--termination-log", str(message)]
    assert cloud_import_job.main(["prepare", *command]) == 0
    identity = vault.parent / ".import-keys" / import_id / "identity"
    assert identity.stat().st_mode & 0o777 == 0o600
    assert not identity.is_relative_to(vault) and not identity.is_relative_to(vault.parent / "host")
    recipient = subprocess.run(["age-keygen", "-y", str(identity)], capture_output=True, check=True).stdout.decode().strip()
    assert recipient.startswith("age1")
    assert json.loads(message.read_text()) == {"ok": True, "recipient": recipient}
    output = capsys.readouterr()
    # age fixes this identity prefix; it is a wire token, not user vocabulary.
    assert "AGE-SECRET-KEY-" not in output.out + output.err + caplog.text + message.read_text()

    content = b"Invented imported note\n"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo("member-canary.md")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    ciphertext = subprocess.run(["age", "-r", recipient], input=archive.getvalue(), capture_output=True, check=True).stdout
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id=CELL, kind="adopt", declared_files=1, declared_bytes=len(content),
        ciphertext_bytes=len(ciphertext), ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
    )
    monkeypatch.setattr(cloud_import_job, "urlopen", lambda *args, **kwargs: io.BytesIO(ciphertext))
    assert cloud_import_job.main(["import", *command, "--request", request.model_dump_json(),
                                  "--url", "https://storage.example/import"]) == 0
    assert (vault / "_Imports" / import_id / "member-canary.md").read_bytes() == content
    assert json.loads(message.read_text()) == {"ok": True, "files": 1, "bytes": len(content)}
    output = capsys.readouterr()
    public_output = output.out + output.err + caplog.text + message.read_text()
    assert "member-canary.md" not in public_output and content.decode().strip() not in public_output
    assert "AGE-SECRET-KEY-" not in public_output

    staging = vault.parent / f".import-{import_id}"
    staging.mkdir()
    (staging / "member-canary.md").write_bytes(content)
    unrelated = [vault.parent / ".import-keys" / other_id,
                 vault.parent / f".import-{other_id}", vault / "_Imports" / other_id]
    for directory in unrelated:
        directory.mkdir(parents=True)
        (directory / "retained.md").write_bytes(b"Retained unrelated import\n")
    outside = {path.relative_to(vault.parent): path.read_bytes() for path in vault.parent.rglob("*")
               if path.is_file() and path != message and not any(path.is_relative_to(target) for target in
                   [identity.parent, staging, vault / "_Imports" / import_id])}
    assert cloud_import_job.main(["discard", *command]) == 0
    assert json.loads(message.read_text()) == {"ok": True}
    assert not identity.parent.exists() and not staging.exists() and not (vault / "_Imports" / import_id).exists()
    for relative, expected in outside.items():
        assert (vault.parent / relative).read_bytes() == expected
    assert not any(path.is_file() and path.read_bytes() == content for path in vault.rglob("*"))
    output = capsys.readouterr()
    public_output = output.out + output.err + caplog.text + message.read_text()
    assert "member-canary.md" not in public_output and content.decode().strip() not in public_output
    assert "AGE-SECRET-KEY-" not in public_output


@pytest.mark.parametrize(("failure", "code"), [("digest", "ARCHIVE_DIGEST_MISMATCH"),
                                               ("age-final-chunk", "ARCHIVE_UNDECRYPTABLE")])
def test_command_refuses_digest_or_late_decryptor_failure_without_committing(vault, tmp_path, monkeypatch, capsys, caplog, failure, code):
    """A complete tar must not commit when its ciphertext or final age chunk is invalid."""
    caplog.set_level("DEBUG")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    import_id = str(uuid.uuid4())
    recipient = cloud_import_job.prepare(vault.parent, import_id, CELL)
    archive = io.BytesIO()
    content = b"Invented refused note\n"
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo("refused-member-canary.md")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    # Tar padding lets age emit a complete archive before rejecting its final chunk.
    ciphertext = subprocess.run(["age", "-r", recipient], input=archive.getvalue() + bytes(65536),
                                capture_output=True, check=True).stdout
    if failure == "age-final-chunk":
        ciphertext = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
        decrypted = subprocess.run(["age", "-d", "-i", str(vault.parent / ".import-keys" / import_id / "identity")],
                                   input=ciphertext, capture_output=True)
        assert decrypted.returncode != 0
        with tarfile.open(fileobj=io.BytesIO(decrypted.stdout)) as tar:
            assert tar.extractfile("refused-member-canary.md").read() == content
    digest = "0" * 64 if failure == "digest" else hashlib.sha256(ciphertext).hexdigest()
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id=CELL, kind="adopt", declared_files=1, declared_bytes=len(content),
        ciphertext_bytes=len(ciphertext), ciphertext_sha256=digest,
    )
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    monkeypatch.setattr(cloud_import_job, "urlopen", lambda *args, **kwargs: io.BytesIO(ciphertext))
    message = tmp_path / "termination.json"
    assert cloud_import_job.main(["import", "--vault", str(vault), "--import-id", import_id,
        "--request", request.model_dump_json(), "--url", "https://storage.example/import",
        "--termination-log", str(message)]) == 1
    assert json.loads(message.read_text()) == {"ok": False, "error": code}
    assert before == {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    assert not (vault.parent / f".import-{import_id}").exists()
    output = capsys.readouterr()
    public_output = output.out + output.err + caplog.text + message.read_text()
    assert "refused-member-canary.md" not in public_output and content.decode().strip() not in public_output
    assert "AGE-SECRET-KEY-" not in public_output


def test_a_wrong_key_reads_as_undecryptable_however_slowly_age_exits(vault, tmp_path, monkeypatch):
    """Defect: a decryptor that closes its output before it exits makes a wrong key read as a malformed archive."""
    real_age = shutil.which("age")
    slow = tmp_path / "bin" / "age"
    slow.parent.mkdir()
    slow.write_text(f'#!/bin/sh\n"{real_age}" "$@"\ncode=$?\nexec 1>&-\nsleep 1\nexit $code\n')
    slow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{slow.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    import_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
    cloud_import_job.prepare(vault.parent, import_id, CELL)
    ciphertext = _encrypted(tmp_path, cloud_import_job.prepare(vault.parent, other_id, CELL), b"member")
    request = cloud_import_job.CloudImportRequest(import_id=import_id, cell_id=CELL, kind="adopt",
        declared_files=2, declared_bytes=999, ciphertext_bytes=len(ciphertext),
        ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest())
    with pytest.raises(cloud_import_job.ArchiveRefused) as refused:
        cloud_import_job.run_import(request, vault, lambda: io.BytesIO(ciphertext))
    assert refused.value.code == "ARCHIVE_UNDECRYPTABLE"


@pytest.mark.parametrize("member_limit", [None, "1"])
def test_job_environment_member_limit_refuses_before_vault_write(vault, tmp_path, monkeypatch, member_limit):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    monkeypatch.delenv("EXOMEM_IMPORT_RESERVE_BYTES", raising=False)
    if member_limit is None:
        monkeypatch.delenv("EXOMEM_IMPORT_MAX_MEMBERS", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_IMPORT_MAX_MEMBERS", member_limit)
    import_id = str(uuid.uuid4())
    recipient = cloud_import_job.prepare(vault.parent, import_id, CELL)
    content = b"example"
    ciphertext = _encrypted(tmp_path, recipient, content)
    request = cloud_import_job.CloudImportRequest(import_id=import_id, cell_id=CELL, kind="adopt",
        declared_files=2, declared_bytes=len(content) + len(cloud_import_envelope.Envelope(
            scope="Device", category="Exports", filename="export.zip").encoded()),
        ciphertext_bytes=len(ciphertext), ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest())
    monkeypatch.setattr(cloud_import_job, "urlopen", lambda *args, **kwargs: io.BytesIO(ciphertext))
    message = tmp_path / "termination.json"
    code = cloud_import_job.main(["import", "--vault", str(vault), "--import-id", import_id,
        "--request", request.model_dump_json(), "--url", "https://storage.example/import",
        "--termination-log", str(message)])
    report = json.loads(message.read_text())
    if member_limit is None:
        assert code == 0 and report == {"ok": True, "files": 2, "bytes": request.declared_bytes}
    else:
        assert code == 1 and report == {"ok": False, "error": "ARCHIVE_TOO_LARGE"}
        assert not (vault / "_Imports").exists()


def _adopt_import(vault, tmp_path, monkeypatch, files):
    """Run a real adopt import of `files` ({member path: bytes}); return its id."""
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    import_id = str(uuid.uuid4())
    recipient = cloud_import_job.prepare(vault.parent, import_id, CELL)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    ciphertext = subprocess.run(["age", "-r", recipient], input=archive.getvalue(), capture_output=True, check=True).stdout
    request = cloud_import_job.CloudImportRequest(
        import_id=import_id, cell_id=CELL, kind="adopt", declared_files=len(files),
        declared_bytes=sum(len(content) for content in files.values()),
        ciphertext_bytes=len(ciphertext), ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest())
    cloud_import_job.run_import(request, vault, lambda: io.BytesIO(ciphertext))
    return import_id


def _receipt(vault, import_id):
    return json.loads((cloud_import_job.receipt_directory(vault) / f"{import_id}.json").read_text())


def test_adopt_receipt_lists_exactly_the_staged_files_with_their_size_and_hash(vault, tmp_path, monkeypatch):
    """Adoption removes a staged file only if the receipt vouches for it, so the list must match the disk."""
    import_id = _adopt_import(vault, tmp_path, monkeypatch, {
        "notes/alpha.md": b"Invented alpha note\n", "notes/deep/beta.pdf": b"%PDF invented bytes", "empty.txt": b""})
    staged = vault / "_Imports" / import_id
    on_disk = {path.relative_to(staged).as_posix(): (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
               for path in staged.rglob("*") if path.is_file()}
    listed = {entry["path"]: (entry["size"], entry["sha256"]) for entry in _receipt(vault, import_id)["members"]}
    assert len(on_disk) == 3 and listed == on_disk


def test_discard_removes_the_manifest_with_the_stage_and_keep_adoption_keeps_both(vault, tmp_path, monkeypatch):
    """A discarded stage must not stay consumable, and the receipt must still answer a replay."""
    kept = _adopt_import(vault, tmp_path, monkeypatch, {"kept.md": b"Invented kept note\n"})
    discarded = _adopt_import(vault, tmp_path, monkeypatch, {"gone.md": b"Invented discarded note\n"})
    cloud_import_job.discard(vault, kept, keep_adoption=True)
    assert (vault / "_Imports" / kept / "kept.md").exists() and _receipt(vault, kept)["members"]
    cloud_import_job.discard(vault, discarded)
    assert not (vault / "_Imports" / discarded).exists()
    receipt = _receipt(vault, discarded)
    assert "members" not in receipt and receipt["request"]["import_id"] == discarded


def test_adopt_receipt_records_each_staged_files_identity_as_the_job_left_it(vault, tmp_path, monkeypatch):
    """Catches an identity read before the Job sets the file's time, or one that carries the device number."""
    import_id = _adopt_import(vault, tmp_path, monkeypatch, {
        "notes/alpha.md": b"Invented alpha note\n", "scans/deep/beta.pdf": b"%PDF invented bytes"})
    staged = vault / "_Imports" / import_id
    members = _receipt(vault, import_id)["members"]
    assert len(members) == 2
    for entry in members:
        info = (staged / entry["path"]).stat()
        assert entry["identity"] == {
            "inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
