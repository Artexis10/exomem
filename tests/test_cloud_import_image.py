"""Run the protected import on the built cell image with all network access blocked."""
from __future__ import annotations

import os
import subprocess

import pytest


@pytest.mark.skipif(not os.environ.get("CLOUD_IMPORT_IMAGE"), reason="built Cloud image required")
def test_cell_image_preserves_encrypted_zip_without_runtime_or_network():
    script = '''
import hashlib, io, json, os, subprocess, tempfile, uuid, zipfile
from pathlib import Path
from exomem import archive_members, cloud_import_envelope, cloud_import_job, init
# The disposable container owns this tmpfs, including background index workers.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
    root = Path(temporary)
    os.environ.update(EXOMEM_STATE_ROOT=str(root / "state"), EXOMEM_WRITER_LEASE_STATE_DIR=str(root / "lease"),
                      EXOMEM_CLOUD_CELL="1", EXOMEM_CLOUD_CELL_ID="abcdefghijklmnop", XDG_STATE_HOME=str(root / "host"))
    vault = root / "vault"
    init.init_vault(vault, initialize_state=False)
    archive = root / "export.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("sample.json", '{"value":23}')
    envelope = cloud_import_envelope.Envelope(scope="Device", category="Exports", filename="export.zip")
    tar = io.BytesIO()
    cloud_import_envelope.write(tar, archive, envelope)
    import_id = str(uuid.uuid4())
    recipient = cloud_import_job.prepare(root, import_id, "abcdefghijklmnop")
    ciphertext = subprocess.run(["age", "-r", recipient], input=tar.getvalue(), capture_output=True, check=True).stdout
    request = cloud_import_job.CloudImportRequest(import_id=import_id, cell_id="abcdefghijklmnop", kind="archive_members",
        declared_files=2, declared_bytes=archive.stat().st_size + len(envelope.encoded()),
        ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(), ciphertext_bytes=len(ciphertext))
    result = cloud_import_job.run_import(request, vault, lambda: io.BytesIO(ciphertext))
    cloud_import_job.discard(vault, import_id)
    receipt = json.loads(next(cloud_import_job.receipt_directory(vault).glob("*.json")).read_text())
    assert archive_members.read_manifest(vault, receipt["manifest"])
    assert not (vault / "_Imports").exists()
    assert cloud_import_job.run_import(request, vault, lambda: None) == result
    print("protected import, cleanup and receipt replay passed")
'''
    result = subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,mode=1777,size=512m", "--entrypoint", "python",
        os.environ["CLOUD_IMPORT_IMAGE"], "-c", script,
    ], text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "protected import, cleanup and receipt replay passed" in result.stdout
