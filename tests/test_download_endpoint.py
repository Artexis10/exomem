"""/download endpoint — out-of-band read of a vault file (the reverse of /upload)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import index_paths, server, upload_tokens


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return TestClient(server.build_server(require_auth=False).http_app())


def _get(client: TestClient, path: str, token: str | None) -> object:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/download", params={"path": path}, headers=headers)


def test_download_requires_auth(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    assert _get(c, "Knowledge Base/index.md", None).status_code == 401


def test_download_disabled_without_token(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch)  # no credential configured at all
    assert _get(c, "Knowledge Base/index.md", None).status_code == 503


def test_download_streams_file_with_minted_token(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    r = _get(c, "Knowledge Base/index.md", tok)
    assert r.status_code == 200, r.text
    assert r.content == (vault / "Knowledge Base" / "index.md").read_bytes()


def test_download_master_token_works(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    assert _get(c, "Knowledge Base/index.md", "sek").status_code == 200


def test_download_enters_consolidation_transfer_admission(
    vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_runtime

    events: list[str] = []

    @contextmanager
    def admission(_vault_root: Path):
        events.append("transfer-enter")
        try:
            yield
        finally:
            events.append("transfer-exit")

    monkeypatch.setattr(
        consolidation_runtime,
        "admit_transfer",
        admission,
        raising=False,
    )
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")

    response = _get(client, "Knowledge Base/index.md", "sek")

    assert response.status_code == 200
    assert events == ["transfer-enter", "transfer-exit"]


def test_upload_scoped_token_rejected_on_download(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # scope isolation: an upload token must not read
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    upload_tok = upload_tokens.mint("sek", scope="upload")
    assert _get(c, "Knowledge Base/index.md", upload_tok).status_code == 401


def test_download_scoped_token_rejected_on_upload(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # scope isolation, other direction: a download token must not write
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    dl_tok = upload_tokens.mint("sek", scope="download")
    r = c.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": f"Bearer {dl_tok}"},
    )
    assert r.status_code == 401, r.text


def test_download_path_traversal_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    assert _get(c, "../../../../etc/passwd", tok).status_code == 400


def test_download_missing_path(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    assert c.get("/download", headers={"Authorization": f"Bearer {tok}"}).status_code == 400


def test_download_nonexistent_file(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    tok = upload_tokens.mint("sek", scope="download")
    assert _get(c, "Knowledge Base/nope-does-not-exist.md", tok).status_code == 404


def test_download_hides_private_state_hardlink_before_release(vault, monkeypatch) -> None:
    private = index_paths.sidecar_path(vault)
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b"private-index")
    alias = vault / "Knowledge Base" / "Notes" / "ordinary-looking.bin"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sek")
    token = upload_tokens.mint("sek", scope="download")
    hidden = _get(client, "Knowledge Base/Notes/ordinary-looking.bin", token)
    alias.unlink()
    missing = _get(client, "Knowledge Base/Notes/ordinary-looking.bin", token)

    assert hidden.status_code == 404
    assert hidden.status_code == missing.status_code
    assert hidden.content == missing.content
    assert b"private-index" not in hidden.content
