"""A restarted CLI reuses ciphertext and part receipts without leaking OAuth to storage."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import httpx2
import pytest

from exomem import cloud_export_client, cloud_import_client
from exomem.cli_progress import Progress
from exomem.cloud_import import DEFAULT_RESERVE_BYTES
from exomem.cloud_import_client import (
    PRODUCTION_API_BASE_URL,
    PRODUCTION_MCP_URL,
    CloudImportClientConfig,
    CloudImportError,
    import_source,
)

CONFIG = CloudImportClientConfig("https://cloud.example/mcp", "https://cloud.example")
needs_age = pytest.mark.skipif(
    not shutil.which("age") or not shutil.which("age-keygen"), reason="real age executables required")


def _age_keys(directory: Path) -> tuple[Path, str]:
    identity = directory / "identity"
    subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, check=True)
    recipient = subprocess.run(["age-keygen", "-y", str(identity)], capture_output=True, check=True).stdout.decode().strip()
    return identity, recipient


class FakeCloud:
    """The Cloud import API and its storage service, as two in-process transports."""

    def __init__(self, recipient: str):
        self.row = {"import_id": None, "requested_state": "requested", "observed_state": "key_ready", "recipient": recipient}
        self.uploaded: dict[int, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.declared: dict = {}
        self.refuse_registration = False
        self.fail_part_once: int | None = None
        self.after_registration = lambda: None

    def _api(self, request):
        self.requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer example-session"
        if request.method == "POST":
            body = json.loads(request.content)
            if request.url.path.endswith("/prepare-upload"):
                self.row.update(body)
                size = body["ciphertext_bytes"]
                self.row["part_urls"] = [
                    {"part_number": n + 1, "part_size_bytes": min(5000, size - offset),
                     "url": f"https://storage.example/part/{n + 1}"}
                    for n, offset in enumerate(range(0, size, 5000))]
            elif request.url.path.endswith("/complete"):
                combined = b"".join(self.uploaded[n] for n in sorted(self.uploaded))
                assert hashlib.sha256(combined).hexdigest() == body["ciphertext_sha256"]
                assert body["part_etags"] == [{"part_number": n, "etag": f'"part-{n}"'} for n in sorted(self.uploaded)]
                self.row.update(requested_state="uploaded", observed_state="done")
            elif self.refuse_registration:
                return httpx2.Response(503, json={"error": {"code": "STORE_UNAVAILABLE"}})
            else:
                self.declared = body
                self.row["import_id"] = body["import_id"]
                self.after_registration()
        return httpx2.Response(200, json={"import": self.row})

    def _storage(self, request):
        assert "authorization" not in request.headers
        part = int(request.url.path.rsplit("/", 1)[1])
        if part == self.fail_part_once:
            self.fail_part_once = None
            raise httpx2.ConnectError("interrupted")
        assert part not in self.uploaded
        self.uploaded[part] = request.content
        return httpx2.Response(200, headers={"etag": f'"part-{part}"'})

    @property
    def ciphertext(self) -> bytes:
        return b"".join(self.uploaded[n] for n in sorted(self.uploaded))

    async def run(self, source: Path, checkpoint: Path, **kwargs):
        headers = {"Authorization": "Bearer example-session"}
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(self._api), headers=headers) as api:
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(self._storage)) as uploads:
                return await import_source(source, CONFIG, checkpoint, api=api, uploads=uploads, poll_seconds=0, **kwargs)


@needs_age
def test_interrupted_upload_resumes_same_ciphertext(tmp_path):
    source = tmp_path / "export.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sample.json", '{"value":23}')
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    cloud.fail_part_once = 2

    def run():
        return cloud.run(source, tmp_path / "checkpoint", scope="Device", category="Exports")
    with pytest.raises(CloudImportError) as failed:
        asyncio.run(run())
    assert (failed.value.phase, failed.value.code, failed.value.import_id) == (
        "upload", "NETWORK_ERROR", cloud.row["import_id"])
    encrypted = (tmp_path / "checkpoint/archive.age").read_bytes()
    assert (tmp_path / "checkpoint/archive.age").stat().st_mode & 0o777 == 0o600
    source.write_bytes(b"source changed after encryption")
    assert asyncio.run(run())["observed_state"] == "done"
    assert encrypted == cloud.ciphertext
    assert not (tmp_path / "checkpoint/archive.age").exists()


def _members(identity: Path, ciphertext: bytes) -> dict[str, bytes | None]:
    plain = subprocess.run(["age", "-d", "-i", str(identity)], input=ciphertext, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(plain)) as archive:
        return {member.name: archive.extractfile(member).read() if member.isfile() else None for member in archive}


@needs_age
def test_folder_import_skips_links_hidden_and_ignored_files_and_reports_them(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("CONTENT-OUTSIDE-THE-FOLDER")
    folder = tmp_path / "notes"
    (folder / "sub").mkdir(parents=True)
    (folder / ".hidden").mkdir()
    (folder / "a.txt").write_text("kept")
    (folder / "sub" / "b.txt").write_text("kept too")
    (folder / ".hidden" / "x.txt").write_text("hidden")
    (folder / ".gitignore").write_text("*.log\n")
    (folder / "debug.log").write_text("ignored")
    try:
        (folder / "link.txt").symlink_to(outside)
        (folder / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available here")
    identity, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    lines: list[str] = []

    result = asyncio.run(cloud.run(folder, tmp_path / "checkpoint", progress=Progress(lines.append)))

    assert result["observed_state"] == "done"
    assert _members(identity, cloud.ciphertext) == {"a.txt": b"kept", "sub": None, "sub/b.txt": b"kept too"}
    assert (cloud.declared["declared_files"], cloud.declared["declared_bytes"]) == (2, len(b"kept") + len(b"kept too"))
    report = "\n".join(lines)
    assert "symlink: 2 files" in report and "hidden: 2 files" in report and "gitignore: 1 file," in report
    # The encrypted copy is named as duplicate storage while it exists, and as removed at the end.
    assert "duplicates your data" in report and "Removed the encrypted copy" in report


def test_a_folder_with_nothing_left_to_import_is_refused_before_anything_is_sent(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / ".secret").write_text("hidden only")
    cloud = FakeCloud("age1unused")

    with pytest.raises(CloudImportError) as refused:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    assert (refused.value.phase, refused.value.code) == ("preflight", "NOTHING_TO_IMPORT")
    assert cloud.requests == []


def test_missing_age_is_refused_before_anything_is_sent(tmp_path, monkeypatch):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("kept")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    cloud = FakeCloud("age1unused")

    with pytest.raises(CloudImportError) as refused:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    assert (refused.value.phase, refused.value.code) == ("preflight", "AGE_MISSING")
    assert cloud.requests == []
    assert not (tmp_path / "checkpoint/checkpoint.json").exists()


@needs_age
def test_too_little_disk_for_the_ciphertext_is_refused_before_anything_is_sent(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"x" * 100_000)
    cloud = FakeCloud("age1unused")

    with pytest.raises(CloudImportError) as refused:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint", free_bytes=lambda path: 50_000))

    assert (refused.value.phase, refused.value.code) == ("preflight", "INSUFFICIENT_DISK")
    assert "50000 bytes are free" in refused.value.message
    assert cloud.requests == []
    assert not (tmp_path / "checkpoint/checkpoint.json").exists()
    assert not list((tmp_path / "checkpoint").glob("archive*"))


@needs_age
def test_restart_with_a_changed_source_drops_the_checkpoint_and_the_rerun_starts_a_new_import(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("declared")
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    cloud.refuse_registration = True
    with pytest.raises(CloudImportError) as first:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))
    assert (first.value.phase, first.value.code) == ("register", "STORE_UNAVAILABLE")

    cloud.refuse_registration = False
    (folder / "b.txt").write_text("added after the import was declared")
    with pytest.raises(CloudImportError) as second:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    # The failure cannot resume, so it must not say "resume": it says what a rerun does, and how to stop the old import.
    assert (second.value.phase, second.value.code, second.value.resume) == ("archive", "SOURCE_CHANGED", False)
    assert second.value.import_id == first.value.import_id
    assert "import_cancel" in second.value.message and "starts a new import" in second.value.message
    assert not (tmp_path / "checkpoint/checkpoint.json").exists()
    assert not (tmp_path / "checkpoint/archive.age").exists()

    third = asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    assert third["observed_state"] == "done"
    assert third["import_id"] != first.value.import_id
    assert cloud.declared["declared_files"] == 2


@needs_age
def test_a_file_that_changes_while_it_is_archived_fails_without_a_torn_archive_and_the_rerun_succeeds(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.bin").write_bytes(b"x" * 200_000)
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    touched = False

    def touch_the_file_while_it_is_read(line: str) -> None:
        # The first line from the archive phase is written after the first part of the file was read.
        nonlocal touched
        if line.startswith("Archiving and encrypting:") and not touched:
            touched = True
            later = (folder / "a.bin").stat().st_mtime_ns + 5_000_000_000
            os.utime(folder / "a.bin", ns=(later, later))

    with pytest.raises(CloudImportError) as failed:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint", progress=Progress(touch_the_file_while_it_is_read, interval=0)))

    assert (failed.value.phase, failed.value.code, failed.value.resume) == ("archive", "SOURCE_CHANGED", False)
    assert not (tmp_path / "checkpoint/archive.age").exists() and not (tmp_path / "checkpoint/checkpoint.json").exists()
    assert cloud.uploaded == {}

    assert asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))["observed_state"] == "done"


@needs_age
def test_a_same_size_save_between_the_scan_and_the_read_does_not_abandon_the_import(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("before the save")
    identity, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    cloud.after_registration = lambda: (folder / "a.txt").write_text("after the save!")

    result = asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    assert result["observed_state"] == "done"
    assert _members(identity, cloud.ciphertext) == {"a.txt": b"after the save!"}


@needs_age
def test_the_disk_check_never_understates_a_folder_of_long_and_non_ascii_names(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    for number in range(30):
        (folder / f"{'é' * 60}-{number}" / ("n" * 140 + ".md")).parent.mkdir()
        (folder / f"{'é' * 60}-{number}" / ("n" * 140 + ".md")).write_text("x")
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    asyncio.run(cloud.run(folder, tmp_path / "first", free_bytes=lambda path: 10**12))
    real = len(cloud.ciphertext)

    # A disk with one byte less than the real encrypted size plus the reserve must be refused.
    with pytest.raises(CloudImportError) as refused:
        asyncio.run(FakeCloud(recipient).run(
            folder, tmp_path / "second", free_bytes=lambda path: real + DEFAULT_RESERVE_BYTES - 1))
    assert refused.value.code == "INSUFFICIENT_DISK"

    # And the estimate is not wildly above it: twice the real size plus the reserve is enough.
    done = asyncio.run(FakeCloud(recipient).run(
        folder, tmp_path / "third", free_bytes=lambda path: 2 * real + DEFAULT_RESERVE_BYTES))
    assert done["observed_state"] == "done"


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


def test_failed_import_reports_phase_code_import_id_and_the_copy_it_still_holds(tmp_path, monkeypatch, capsys):
    import_id = "e91b0d91-79ab-4c9c-950b-a85e80c3a7ef"
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / "archive.age").write_bytes(b"x" * 123)

    async def failed(*args, **kwargs):
        raise CloudImportError("upload", "NETWORK_ERROR", "the connection to Cloud or storage failed", import_id)

    monkeypatch.setattr(cloud_import_client, "import_source", failed)
    status = cloud_import_client.main(["import", str(tmp_path / "notes"), "--checkpoint", str(checkpoint)])

    captured = capsys.readouterr()
    assert status == 1
    report = json.loads(captured.out)
    assert (report["ok"], report["phase"], report["code"], report["import_id"], report["ciphertext_bytes"]) == (
        False, "upload", "NETWORK_ERROR", import_id, 123)
    # The human report names the route that discards the second copy.
    assert "import_cancel" in captured.err and str(checkpoint) in captured.err


def _endpoints_used(monkeypatch, tmp_path, *arguments) -> CloudImportClientConfig:
    seen = []

    async def capture(source, config, checkpoint, **kwargs):
        seen.append(config)
        return {"import_id": "e91b0d91-79ab-4c9c-950b-a85e80c3a7ef", "observed_state": "done"}

    monkeypatch.setattr(cloud_import_client, "import_source", capture)
    assert cloud_import_client.main(["import", str(tmp_path), "--checkpoint", str(tmp_path / "cp"), *arguments]) == 0
    return seen[0]


def test_cloud_import_needs_no_endpoint_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("EXOMEM_CLOUD_MCP_URL", raising=False)
    monkeypatch.delenv("EXOMEM_CLOUD_API_BASE_URL", raising=False)

    config = _endpoints_used(monkeypatch, tmp_path)

    assert (config.mcp_url, config.api_base_url) == (PRODUCTION_MCP_URL, PRODUCTION_API_BASE_URL)


def test_environment_overrides_the_default_endpoints_and_a_flag_overrides_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_MCP_URL", "https://staging.example/mcp")
    monkeypatch.setenv("EXOMEM_CLOUD_API_BASE_URL", "https://staging.example")

    from_environment = _endpoints_used(monkeypatch, tmp_path)
    from_flag = _endpoints_used(monkeypatch, tmp_path, "--mcp-url", "https://flag.example/mcp")

    assert (from_environment.mcp_url, from_environment.api_base_url) == ("https://staging.example/mcp", "https://staging.example")
    assert (from_flag.mcp_url, from_flag.api_base_url) == ("https://flag.example/mcp", "https://staging.example")


def test_cloud_export_also_defaults_to_production(tmp_path, monkeypatch):
    monkeypatch.delenv("EXOMEM_CLOUD_MCP_URL", raising=False)
    monkeypatch.delenv("EXOMEM_CLOUD_API_BASE_URL", raising=False)
    seen = []

    async def capture(config, out_dir, **kwargs):
        seen.append(config)
        return {"export_id": "e91b0d91-79ab-4c9c-950b-a85e80c3a7ef", "path": str(out_dir), "bytes": 0, "sha256": "0" * 64}

    monkeypatch.setattr(cloud_export_client, "download_export", capture)
    assert cloud_export_client.main(["--out", str(tmp_path)]) == 0

    assert (seen[0].mcp_url, seen[0].api_base_url) == (PRODUCTION_MCP_URL, PRODUCTION_API_BASE_URL)


def test_production_mcp_url_is_the_cloud_plugin_resource():
    definition = Path(__file__).resolve().parents[1] / "plugins/cloud/definition.json"

    assert json.loads(definition.read_text(encoding="utf-8"))["resource"] == PRODUCTION_MCP_URL
