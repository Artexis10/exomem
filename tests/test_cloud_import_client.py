"""A restarted CLI reuses ciphertext and part receipts without leaking OAuth to storage."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import zipfile

import httpx2
import pytest

from exomem import cloud_import_client
from exomem.cloud_import_client import CloudImportClientConfig, import_source


@pytest.mark.skipif(not shutil.which("age") or not shutil.which("age-keygen"), reason="real age executables required")
def test_interrupted_upload_resumes_same_ciphertext(tmp_path):
    source = tmp_path / "export.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sample.json", '{"value":23}')
    identity = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, check=True)
    recipient = subprocess.run(["age-keygen", "-y", str(identity)], capture_output=True, check=True).stdout.decode().strip()
    row = {"import_id": None, "requested_state": "requested", "observed_state": "key_ready", "recipient": recipient}
    uploaded = {}
    fail_once = True

    def api_handler(request):
        assert request.headers["authorization"] == "Bearer example-session"
        if request.method == "POST":
            body = json.loads(request.content)
            if request.url.path.endswith("/prepare-upload"):
                row.update(body)
                size = body["ciphertext_bytes"]
                row["part_urls"] = [
                    {"part_number": n + 1, "part_size_bytes": min(5000, size - offset),
                     "url": f"https://storage.example/part/{n + 1}"}
                    for n, offset in enumerate(range(0, size, 5000))]
            elif request.url.path.endswith("/complete"):
                combined = b"".join(uploaded[n] for n in sorted(uploaded))
                assert hashlib.sha256(combined).hexdigest() == body["ciphertext_sha256"]
                assert body["part_etags"] == [{"part_number": n, "etag": f'"part-{n}"'} for n in sorted(uploaded)]
                row.update(requested_state="uploaded", observed_state="done")
            else:
                row["import_id"] = body["import_id"]
        return httpx2.Response(200, json={"import": row})

    def upload_handler(request):
        nonlocal fail_once
        assert "authorization" not in request.headers
        part = int(request.url.path.rsplit("/", 1)[1])
        if part == 2 and fail_once:
            fail_once = False
            raise httpx2.ConnectError("interrupted")
        assert part not in uploaded
        uploaded[part] = request.content
        return httpx2.Response(200, headers={"etag": f'"part-{part}"'})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(api_handler), headers={"Authorization": "Bearer example-session"}) as api:
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(upload_handler)) as uploads:
                return await import_source(source, CloudImportClientConfig("https://cloud.example/mcp", "https://cloud.example"),
                                           tmp_path / "checkpoint", scope="Device", category="Exports",
                                           api=api, uploads=uploads, poll_seconds=0)
    with pytest.raises(httpx2.ConnectError):
        asyncio.run(run())
    encrypted = (tmp_path / "checkpoint/archive.age").read_bytes()
    assert (tmp_path / "checkpoint/archive.age").stat().st_mode & 0o777 == 0o600
    source.write_bytes(b"source changed after encryption")
    assert asyncio.run(run())["observed_state"] == "done"
    assert encrypted == b"".join(uploaded[n] for n in sorted(uploaded))
    assert not (tmp_path / "checkpoint/archive.age").exists()


def test_completed_c5_import_reports_success_without_crashing(tmp_path, monkeypatch, capsys):
    """C5 returns import_id; the command must not crash after the server commits the import."""
    import_id = "e91b0d91-79ab-4c9c-950b-a85e80c3a7ef"

    async def completed(*args, **kwargs):
        return {"import_id": import_id, "observed_state": "done"}

    monkeypatch.setattr(cloud_import_client, "import_source", completed)
    status = cloud_import_client.main([
        "import", str(tmp_path / "notes"), "--checkpoint", str(tmp_path / "checkpoint"),
        "--mcp-url", "https://cloud.example/mcp", "--api-base-url", "https://cloud.example",
    ])

    assert status == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "import_id": import_id, "state": "done"}
