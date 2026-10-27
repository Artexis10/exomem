"""A Cloud cell serves an exact original only to the path and login the gateway proved.

The gateway here is a stand-in that signs the same version-1 proof as the Substrate
gateway. Requests go through the cell's real HTTP app, so the bearer check, proof
verification, governance and range handling all run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from urllib.parse import quote

import httpx2
import pytest
from test_cloud_upload_bridge import LOGIN_A, TOKEN, _mcp_origin, _run, _sign, _tool
from test_media_store import _offload, _preserved

from exomem import cloud_download_client, media_store
from exomem.cloud_download_client import CloudDownloadError
from exomem.cloud_import_client import CloudImportClientConfig

# The `cell` and `selected_store` fixtures: the cell's real app and a real age-encrypted store.
pytest_plugins = ["test_cloud_upload_bridge", "test_media_store"]

CONTENT = "Speaker A: an exact original, long enough to slice. " * 40


def _proof(key, path, **overrides):
    claims = {"purpose": "download", "target_sha256": hashlib.sha256(path.encode()).hexdigest(), **overrides}
    return _sign(key, {**LOGIN_A, **claims}, "exomem-cloud-origin", 60)


def _headers(key, path, **extra):
    return {"authorization": f"Bearer {TOKEN}", "x-exomem-cloud-origin": _proof(key, path), **extra}


async def _get(client, path, headers):
    return await client.get(f"/download?path={quote(path, safe='')}", headers=headers)


def _preserved_original(vault):
    original = _preserved(vault)
    return original, original.relative_to(vault).as_posix()


@pytest.mark.parametrize("fault", ["other_path", "other_purpose", "no_proof", "bad_bearer"])
def test_a_wrong_proof_is_refused_before_any_byte_is_read(cell, monkeypatch, fault):
    key, app, vault = cell
    _, path = _preserved_original(vault)
    from exomem import artifact_recovery

    def read(*args, **kwargs):
        raise AssertionError("a refused request must not resolve or read the path")

    monkeypatch.setattr(artifact_recovery, "stage", read)
    headers = {
        "other_path": _headers(key, path) | {"x-exomem-cloud-origin": _proof(key, "another/path.txt")},
        "other_purpose": _headers(key, path) | {"x-exomem-cloud-origin": _proof(key, path, purpose="mcp")},
        "no_proof": {"authorization": f"Bearer {TOKEN}"},
        "bad_bearer": _headers(key, path) | {"authorization": "Bearer not-the-cell-bearer"},
    }[fault]

    async def scenario(client):
        return await _get(client, path, headers)

    response = _run(app, scenario)
    assert response.status_code == 401
    assert response.json()["code"] == "DOWNLOAD_UNAVAILABLE"


def test_an_evicted_original_downloads_byte_identical_after_hydration(cell, selected_store):
    key, app, vault = cell
    original, path = _preserved_original(vault)
    expected = original.read_bytes()
    _offload(vault, original)
    assert media_store.evict(vault, original)
    assert not original.exists()

    response = _run(app, lambda client: _get(client, path, _headers(key, path)))
    assert response.status_code == 200, response.text
    assert response.content == expected
    assert response.headers["x-exomem-sha256"] == hashlib.sha256(expected).hexdigest()
    assert response.headers["x-exomem-size"] == str(len(expected))
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"


def test_an_unreachable_store_answers_retryable_unavailable(cell, selected_store):
    key, app, vault = cell
    original, path = _preserved_original(vault)
    _offload(vault, original)
    assert media_store.evict(vault, original)
    from pathlib import Path

    root = Path(selected_store[0]["root"])
    root.rename(root.with_name("offline-objects"))

    response = _run(app, lambda client: _get(client, path, _headers(key, path)))
    assert response.status_code == 503
    assert response.json()["code"] == "ORIGINAL_UNAVAILABLE"


def test_a_range_request_returns_the_slice_with_the_whole_files_identity(cell):
    key, app, vault = cell
    original, path = _preserved_original(vault)
    data = original.read_bytes()

    async def scenario(client):
        part = await _get(client, path, _headers(key, path, range="bytes=10-29"))
        past_end = await _get(client, path, _headers(key, path, range=f"bytes={len(data)}-"))
        return part, past_end

    part, past_end = _run(app, scenario)
    assert part.status_code == 206 and part.content == data[10:30]
    assert part.headers["content-range"] == f"bytes 10-29/{len(data)}"
    assert part.headers["x-exomem-sha256"] == hashlib.sha256(data).hexdigest()
    assert part.headers["x-exomem-size"] == str(len(data))
    assert past_end.status_code == 416
    assert past_end.headers["x-exomem-size"] == str(len(data))


def test_a_path_that_is_not_a_readable_original_answers_not_found(cell):
    key, app, vault = cell
    (vault / "plain.md").write_text("# a note, not a preserved original\n")
    missing = _run(app, lambda client: _get(client, "no/such/file.bin", _headers(key, "no/such/file.bin")))
    plain = _run(app, lambda client: _get(client, "plain.md", _headers(key, "plain.md")))
    assert (missing.status_code, plain.status_code) == (404, 404)
    assert missing.json() == plain.json()
    assert missing.json()["code"] == "ORIGINAL_NOT_FOUND"


def test_the_cloud_tool_returns_a_descriptor_and_a_command_but_no_link(cell):
    key, app, vault = cell
    original, path = _preserved_original(vault)
    data = original.read_bytes()

    async def scenario(client):
        listed = await client.post(
            "/mcp", headers={"accept": "application/json, text/event-stream", "content-type": "application/json",
                             "authorization": f"Bearer {TOKEN}", "mcp-protocol-version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        result = await _tool(client, "transfer_artifact", {"operation": "download", "artifact": path,
                                                          "expected_sha256": hashlib.sha256(data).hexdigest()},
                             _mcp_origin(key, LOGIN_A))
        return names, result

    names, result = _run(app, scenario)
    assert "transfer_artifact" in names
    assert result["artifact"] == {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                                  "file_name": "sample.txt", "mime_type": "text/plain",
                                  "mime_type_source": "filename"}
    assert result["next_step"] == "exomem cloud download " + shlex.quote(path)
    assert "deliveries" not in result and "http" not in json.dumps(result)


DATA = b"an exact original " * 500
DIGEST = hashlib.sha256(DATA).hexdigest()
CONFIG = CloudImportClientConfig("https://cloud.example/mcp", "https://cloud.example")


def _client_for(handler):
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), headers={"authorization": "Bearer example-session"})


class _Chunks(httpx2.AsyncByteStream):
    """A body that arrives as a real stream, whatever length its headers declared."""

    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def _served(body=DATA):
    headers = {"x-exomem-sha256": DIGEST, "x-exomem-size": str(len(DATA)), "content-length": str(len(DATA)),
               "content-disposition": "attachment; filename*=utf-8''sample.txt"}
    return httpx2.Response(200, headers=headers, stream=_Chunks(body))


def _fetch(tmp_path, response, path="Archive/sample.txt"):
    seen = []

    def handler(request):
        seen.append(request)
        return response

    outcome = asyncio.run(_capture(cloud_download_client.download_original(
        CONFIG, path, tmp_path / "out", api=_client_for(handler))))
    return outcome, seen


async def _capture(coroutine):
    try:
        return await coroutine
    except CloudDownloadError as error:
        return error


def test_the_cli_keeps_a_file_only_when_size_and_digest_verify(tmp_path):
    result, seen = _fetch(tmp_path, _served())
    assert result == {"path": str(tmp_path / "out" / "sample.txt"), "bytes": len(DATA), "sha256": DIGEST}
    assert (tmp_path / "out" / "sample.txt").read_bytes() == DATA
    assert str(seen[0].url) == "https://cloud.example/download?path=Archive%2Fsample.txt"
    assert list((tmp_path / "out").glob(".exomem-download-*")) == []


@pytest.mark.parametrize(("response", "code"), [
    (_served(DATA[:-1] + b"X"), "DIGEST_MISMATCH"),
    (_served(DATA[:-5]), "SIZE_MISMATCH"),
    (httpx2.Response(302, headers={"location": "https://elsewhere.example/blob"}), "DOWNLOAD_REDIRECTED"),
    (httpx2.Response(404, json={"code": "ORIGINAL_NOT_FOUND"}), "ORIGINAL_NOT_FOUND"),
], ids=["tampered", "truncated", "redirect", "refused"])
def test_the_cli_leaves_no_file_after_a_bad_answer(tmp_path, response, code):
    result, _ = _fetch(tmp_path, response)
    assert isinstance(result, CloudDownloadError) and result.code == code
    assert list((tmp_path / "out").iterdir()) == []


def test_the_cli_main_prints_json_and_is_wired_under_cloud(tmp_path, capsys, monkeypatch):
    from exomem.__main__ import main as exomem_main

    real = cloud_download_client.download_original
    monkeypatch.setattr(cloud_download_client, "download_original",
                        lambda config, path, out, api=None: real(config, path, out, api=_client_for(lambda r: _served())))
    code = exomem_main(["cloud", "download", "Archive/sample.txt", "--out", str(tmp_path),
                        "--mcp-url", "https://cloud.example/mcp", "--api-base-url", "https://cloud.example"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["sha256"] == DIGEST
