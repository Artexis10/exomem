"""/download endpoint — out-of-band read of a vault file (the reverse of /upload)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kb_mcp import server, upload_tokens


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return TestClient(server.build_server(require_auth=False).http_app())


def _get(client: TestClient, path: str, token: str | None) -> object:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/download", params={"path": path}, headers=headers)


def test_download_requires_auth(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    assert _get(c, "Knowledge Base/index.md", None).status_code == 401


def test_download_disabled_without_token(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch)  # no credential configured at all
    assert _get(c, "Knowledge Base/index.md", None).status_code == 503


def test_download_streams_file_with_minted_token(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    r = _get(c, "Knowledge Base/index.md", tok)
    assert r.status_code == 200, r.text
    assert r.content == (vault / "Knowledge Base" / "index.md").read_bytes()


def test_download_master_token_works(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    assert _get(c, "Knowledge Base/index.md", "sek").status_code == 200


def test_upload_scoped_token_rejected_on_download(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # scope isolation: an upload token must not read
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    upload_tok = upload_tokens.mint("sek", scope="upload")
    assert _get(c, "Knowledge Base/index.md", upload_tok).status_code == 401


def test_download_scoped_token_rejected_on_upload(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # scope isolation, other direction: a download token must not write
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    dl_tok = upload_tokens.mint("sek", scope="download")
    r = c.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": f"Bearer {dl_tok}"},
    )
    assert r.status_code == 401, r.text


def test_download_path_traversal_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    assert _get(c, "../../../../etc/passwd", tok).status_code == 400


def test_download_missing_path(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    assert c.get("/download", headers={"Authorization": f"Bearer {tok}"}).status_code == 400


def test_download_nonexistent_file(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, KB_MCP_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    assert _get(c, "Knowledge Base/nope-does-not-exist.md", tok).status_code == 404
