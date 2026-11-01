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

from exomem import cloud_export_client, cloud_import_client, cloud_import_windows
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
        self.registration_refusal = (503, "STORE_UNAVAILABLE")
        self.fail_part_once: int | None = None
        self.after_registration = lambda: None
        self.after_prepare = lambda: None
        self.listing = {}
        self.refusal_limit = None
        self.prepare_refusal = None
        self.holding = False  # Cloud keeps the import `uploaded` after /complete until the third read of its row.
        self.fail_next_read = False
        self.reads = 0

    def _api(self, request):
        self.requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer example-session"
        if request.method == "GET" and self.row["observed_state"] == "uploaded":
            if self.fail_next_read:
                self.fail_next_read = False
                raise httpx2.ConnectError("interrupted")
            self.reads += 1
            if self.reads >= 3:
                self.row.update(observed_state="done")
        if request.method == "GET" and request.url.path == "/api/exomem/cloud/imports":
            return httpx2.Response(200, json={"imports": [], **self.listing})
        if request.method == "POST":
            body = json.loads(request.content)
            if request.url.path.endswith("/prepare-upload"):
                if self.prepare_refusal:
                    return httpx2.Response(422, json={"error": {"code": self.prepare_refusal, "limit": self.refusal_limit}})
                self.row.update(body)
                size = body["ciphertext_bytes"]
                self.row["part_urls"] = [
                    {"part_number": n + 1, "part_size_bytes": min(5000, size - offset),
                     "url": f"https://storage.example/part/{n + 1}"}
                    for n, offset in enumerate(range(0, size, 5000))]
                self.after_prepare()
            elif request.url.path.endswith("/complete"):
                combined = b"".join(self.uploaded[n] for n in sorted(self.uploaded))
                assert hashlib.sha256(combined).hexdigest() == body["ciphertext_sha256"]
                assert body["part_etags"] == [{"part_number": n, "etag": f'"part-{n}"'} for n in sorted(self.uploaded)]
                self.row.update(requested_state="uploaded", observed_state="uploaded" if self.holding else "done")
            elif self.refuse_registration:
                status, code = self.registration_refusal
                return httpx2.Response(status, json={"error": {"code": code, "limit": self.refusal_limit}})
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


@needs_age
def test_a_missing_encrypted_copy_after_complete_resumes_the_wait_instead_of_abandoning_the_import(tmp_path):
    """Catches an abandon that removes the checkpoint of an import that Cloud is still importing."""
    source = tmp_path / "export.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sample.json", '{"value":23}')
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    cloud.holding = cloud.fail_next_read = True
    checkpoint = tmp_path / "checkpoint"

    def run():
        return cloud.run(source, checkpoint, scope="Device", category="Exports")
    with pytest.raises(CloudImportError) as failed:
        asyncio.run(run())
    assert failed.value.code == "NETWORK_ERROR"
    (checkpoint / "archive.age").unlink()

    assert asyncio.run(run())["observed_state"] == "done"


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
    # Only the hidden line offers the flag that takes hidden entries; a link cannot be taken.
    (hint,) = [line for line in lines if "--include-hidden" in line]
    assert hint.lstrip().startswith("hidden:")
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
    assert cloud.requests == [("GET", "/api/exomem/cloud/imports")]


def test_a_folder_with_more_entries_than_the_cell_accepts_is_refused_before_encrypting(tmp_path, monkeypatch):
    folder = tmp_path / "notes"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "a.txt").write_text("one")
    (folder / "sub" / "b.txt").write_text("two")
    # Two files and the folder that holds them are three archive entries.
    monkeypatch.setattr(cloud_import_client, "DEFAULT_MAX_MEMBERS", 2)
    cloud = FakeCloud("age1unused")

    with pytest.raises(CloudImportError) as refused:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    assert (refused.value.phase, refused.value.code) == ("preflight", "ARCHIVE_TOO_LARGE")
    assert "3 entries" in refused.value.message and "at most 2" in refused.value.message
    assert cloud.requests == [("GET", "/api/exomem/cloud/imports")] and not (tmp_path / "checkpoint/archive.age").exists()


@needs_age
def test_a_registration_refused_for_an_open_import_says_to_cancel_or_wait_and_the_rerun_then_registers(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("kept")
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    cloud.refuse_registration = True
    cloud.registration_refusal = (409, "IMPORT_CONFLICT")

    with pytest.raises(CloudImportError) as refused:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    # The cell allows one open import, so "run the same command again to resume" would only meet the same refusal.
    assert (refused.value.phase, refused.value.code, refused.value.resume) == ("register", "IMPORT_CONFLICT", False)
    assert "another import is open" in refused.value.message and "cancel it in Home" in refused.value.message

    cloud.refuse_registration = False
    done = asyncio.run(cloud.run(folder, tmp_path / "checkpoint"))

    assert done["observed_state"] == "done" and done["import_id"] == refused.value.import_id


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
    assert cloud.requests == [("GET", "/api/exomem/cloud/imports")]
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
    assert cloud.requests == [("GET", "/api/exomem/cloud/imports")]
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
    assert "import_cancel" in second.value.message and "Cancel in Home" in second.value.message
    assert "starts a new import" in second.value.message
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
@pytest.mark.skipif(os.name == "nt", reason="creating a symbolic link needs a privilege on Windows")
def test_a_folder_swapped_for_a_link_while_it_is_archived_refuses_the_import_and_archives_no_outside_byte(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.bin").write_bytes(b"o" * 200_000)
    folder = tmp_path / "notes"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "a.bin").write_bytes(b"x" * 200_000)
    (folder / "sub" / "b.bin").write_bytes(b"y" * 200_000)
    # The outside file has the declared size and modification time, so only its identity tells it apart.
    declared = (folder / "sub" / "b.bin").stat().st_mtime_ns
    os.utime(outside / "b.bin", ns=(declared, declared))
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    swapped = False

    def swap_the_folder_for_a_link_after_the_first_file_was_started(line: str) -> None:
        # The folder entry was checked before the first file; the second file is opened through the link, same size.
        nonlocal swapped
        if line.startswith("Archiving and encrypting:") and not swapped:
            swapped = True
            shutil.move(folder / "sub", tmp_path / "moved-away")
            (folder / "sub").symlink_to(outside, target_is_directory=True)

    progress = Progress(swap_the_folder_for_a_link_after_the_first_file_was_started, interval=0)
    with pytest.raises(CloudImportError) as failed:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint", progress=progress))

    assert swapped
    assert (failed.value.phase, failed.value.code) == ("archive", "SOURCE_CHANGED")
    assert cloud.uploaded == {} and not (tmp_path / "checkpoint/archive.age").exists()


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
    assert "import_cancel" in captured.err and "Cancel in Home" in captured.err and str(checkpoint) in captured.err


def _endpoints_used(monkeypatch, tmp_path, *arguments) -> CloudImportClientConfig:
    seen = []

    class NoCell:
        def __init__(self, config, oauth):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *error):
            return None

    async def capture(source, config, checkpoint, **kwargs):
        seen.append(config)
        return {"import_id": "e91b0d91-79ab-4c9c-950b-a85e80c3a7ef", "windows": 1}

    # A folder goes by windows, through the cell's tools; this test only reads which endpoints the command takes.
    monkeypatch.setattr(cloud_import_windows, "McpCell", NoCell)
    monkeypatch.setattr(cloud_import_windows, "import_folder", capture)
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


@pytest.mark.parametrize("published", ["absent", None, {"max_members": 1, "reserve_bytes": 0}])
def test_published_limits_control_preflight_before_registration(tmp_path, monkeypatch, published):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "one.md").write_text("one")
    (folder / "two.md").write_text("two")
    cloud = FakeCloud("age1unused")
    cloud.listing = {} if published == "absent" else {"limits": published}
    monkeypatch.setattr(cloud_import_client.shutil, "which", lambda name: "age")
    cloud.refuse_registration = True
    lines = []
    with pytest.raises(CloudImportError) as failed:
        asyncio.run(cloud.run(folder, tmp_path / "checkpoint", free_bytes=lambda path: 0,
                              progress=Progress(lines.append)))
    if published == "absent":
        assert failed.value.code == "INSUFFICIENT_DISK"
        assert cloud.declared == {}
    elif published is None:
        assert failed.value.code == "STORE_UNAVAILABLE"
        assert any("not published" in line for line in lines)
    else:
        assert failed.value.code == "ARCHIVE_TOO_LARGE"
        assert "2 entries" in failed.value.message and "at most 1" in failed.value.message
        assert cloud.declared == {}


@needs_age
@pytest.mark.parametrize("code,phase", [("IMPORT_TOO_MANY_FILES", "register"),
                                       ("IMPORT_STORAGE_LIMIT", "register"), ("IMPORT_PARTS_LIMIT", "upload")])
def test_typed_refusals_name_the_phase_and_cannot_resume(tmp_path, code, phase):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "one.md").write_text("one")
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    cloud.refuse_registration = phase == "register"
    cloud.registration_refusal = (422, code)
    cloud.prepare_refusal = code if phase == "upload" else None
    cloud.refusal_limit = {"unit": "bytes", "value": 100, "requested": 101}
    checkpoint = tmp_path / "checkpoint"
    with pytest.raises(CloudImportError) as failed:
        asyncio.run(cloud.run(folder, checkpoint))
    assert (failed.value.code, failed.value.phase, failed.value.resume) == (code, phase, False)
    assert "100 bytes" in failed.value.message and "101 bytes" in failed.value.message
    if phase == "upload":
        assert "Cancel" in failed.value.message and "start a new run" in failed.value.message
        assert not (checkpoint / "checkpoint.json").exists()
        assert not (checkpoint / "archive.age").exists()


@needs_age
@pytest.mark.parametrize("broken", ["size", "order", "short"])
def test_invalid_upload_copy_or_parts_abandons_checkpoint(tmp_path, monkeypatch, broken):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "one.md").write_text("one")
    _, recipient = _age_keys(tmp_path)
    cloud = FakeCloud(recipient)
    checkpoint = tmp_path / "checkpoint"
    ciphertext = checkpoint / "archive.age"
    opened = []
    original_open, original_unlink = Path.open, Path.unlink
    def open_file(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == ciphertext:
            opened.append(stream)
        return stream
    def unlink_file(path, *args, **kwargs):
        # Model Windows sharing rules on every platform: an open copy cannot be removed.
        if path == ciphertext and any(not stream.closed for stream in opened):
            raise PermissionError("file is open")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(Path, "unlink", unlink_file)
    def break_upload():
        if broken == "size":
            cloud.row["part_urls"][0]["part_size_bytes"] += 1
        elif broken == "order":
            cloud.row["part_urls"][0]["part_number"] = 2
        else:
            (checkpoint / "archive.age").write_bytes(b"")
    cloud.after_prepare = break_upload
    with pytest.raises(CloudImportError) as failed:
        asyncio.run(cloud.run(folder, checkpoint))
    assert (failed.value.phase, failed.value.resume) == ("upload", False)
    assert failed.value.code == ("CIPHERTEXT_SHORT" if broken == "short" else "PARTS_INVALID")
    assert "Cancel" in failed.value.message and "start a new run" in failed.value.message
    assert not (checkpoint / "checkpoint.json").exists() and not (checkpoint / "archive.age").exists()


@pytest.mark.parametrize("counts", [{}, {"staged_files": 0, "staged_bytes": 0}, {"staged_files": 12, "staged_bytes": 3400}])
def test_final_cli_json_includes_only_available_staged_counts(tmp_path, monkeypatch, capsys, counts):
    async def completed(*args, **kwargs):
        return {"import_id": "example-import", "observed_state": "done", **counts}
    monkeypatch.setattr(cloud_import_client, "import_source", completed)
    assert cloud_import_client.main(["import", str(tmp_path / "notes")]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "import_id": "example-import", "state": "done", **counts}
