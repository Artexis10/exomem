"""`exomem cloud export` keeps an archive only when it verifies, and never sends the bearer to storage."""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx2
import pytest

from exomem import cloud_export_client
from exomem.cloud_export_client import CloudExportError
from exomem.cloud_import_client import CloudImportClientConfig

EXPORT_ID = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
ARCHIVE = b"PK-archive-bytes" * 64
LINK = "https://storage.example/exports/archive.zip?signature=secret-signature"
CONFIG = CloudImportClientConfig("https://cloud.example/mcp", "https://cloud.example")


class _Chunks(httpx2.AsyncByteStream):
    """A response body that arrives as a real stream, as storage sends it."""

    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def _served(data: bytes, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, stream=_Chunks(data))


def _display(**overrides):
    ready = {"state": "ready", "export_id": EXPORT_ID, "requested_at": "2026-10-01T00:00:00Z",
             "ready_at": "2026-10-01T00:05:00Z", "expires_at": "2026-10-08T00:05:00Z",
             "archive_bytes": len(ARCHIVE), "archive_sha256": hashlib.sha256(ARCHIVE).hexdigest(),
             "pauses_assistants": False}
    return {**ready, **overrides}


def _download(tmp_path, *, storage, link=LINK, display=None):
    """Run the client against stand-in API and storage servers and return the outcome."""
    api_requests = []
    pending_once = {"left": 1}

    def api(request):
        api_requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer example-session"
        if request.method == "GET":
            return httpx2.Response(200, json={"export": display or _display()})
        if pending_once["left"]:
            pending_once["left"] -= 1
            return httpx2.Response(202, json={"state": "pending"})
        return httpx2.Response(200, json={"url": link, "url_expires_at": "2026-10-01T00:10:00Z"})

    storage_requests = []

    def storage_handler(request):
        storage_requests.append(request)
        assert "authorization" not in request.headers
        return storage(request)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(api),
                                      headers={"Authorization": "Bearer example-session"}) as client:
            return await cloud_export_client.download_export(
                CONFIG, tmp_path / "out", api=client, poll_seconds=0,
                download_transport=httpx2.MockTransport(storage_handler))

    try:
        return asyncio.run(run()), api_requests, storage_requests
    except CloudExportError as error:
        return error, api_requests, storage_requests


def test_a_verified_export_lands_privately_and_storage_never_sees_the_bearer(tmp_path):
    result, api_requests, _ = _download(tmp_path, storage=lambda request: _served(ARCHIVE))

    archive = tmp_path / "out" / f"exomem-export-{EXPORT_ID}.zip"
    assert result["path"] == str(archive) and archive.read_bytes() == ARCHIVE
    assert archive.stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [archive.name]
    # The link request retries a 202 until the controller issues the link.
    assert api_requests.count(("POST", f"/api/exomem/cloud/exports/{EXPORT_ID}/link")) == 2


@pytest.mark.parametrize(
    ("served", "code"),
    [
        (ARCHIVE[:-1] + b"X", "DIGEST_MISMATCH"),
        (ARCHIVE[:-1], "SIZE_MISMATCH"),
        (ARCHIVE + b"X", "SIZE_MISMATCH"),
    ],
    ids=["tampered-byte", "truncated", "oversized"],
)
def test_a_download_that_fails_verification_leaves_no_file(tmp_path, served, code):
    error, _, _ = _download(tmp_path, storage=lambda request: _served(served))

    assert isinstance(error, CloudExportError) and error.code == code
    assert list((tmp_path / "out").iterdir()) == []


def test_a_link_that_is_not_https_is_refused_before_any_storage_request(tmp_path):
    error, _, storage_requests = _download(tmp_path, storage=lambda request: _served(ARCHIVE),
                                           link="http://storage.example/exports/archive.zip")

    assert error.code == "LINK_NOT_HTTPS"
    assert storage_requests == [] and list((tmp_path / "out").iterdir()) == []


def test_a_redirecting_link_is_not_followed_and_leaves_no_file(tmp_path):
    def redirect(request):
        if request.url.host == "elsewhere.example":
            raise AssertionError("the client followed a redirect to another host")
        return httpx2.Response(302, headers={"location": "https://elsewhere.example/archive.zip"})

    error, _, storage_requests = _download(tmp_path, storage=redirect)

    assert error.code == "LINK_REDIRECTED"
    assert [request.url.host for request in storage_requests] == ["storage.example"]
    assert list((tmp_path / "out").iterdir()) == []


def test_a_command_refusal_prints_a_code_and_no_link(tmp_path, monkeypatch, capsys):
    async def refused(*args, **kwargs):
        raise CloudExportError("LINK_REDIRECTED", "the download link redirected; refusing to follow it")

    monkeypatch.setattr(cloud_export_client, "download_export", refused)
    status = cloud_export_client.main(["--out", str(tmp_path), "--mcp-url", "https://cloud.example/mcp",
                                       "--api-base-url", "https://cloud.example"])

    assert status == 1
    assert json.loads(capsys.readouterr().out)["error"] == "LINK_REDIRECTED"
