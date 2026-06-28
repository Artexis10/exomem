"""Personal REST facade (/api/*) — auth matrix + leaf round-trips.

Drives the real FastMCP ASGI app via Starlette's sync TestClient (no live server,
no torch: find runs in keyword mode). `load_dotenv` is neutralized and ambient env
is cleared so the repo `.env` / dev shell can't clobber the per-test fixture vault.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kb_mcp import find as find_module
from kb_mcp import server


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "KB_MCP_REST_API_KEY", "KB_MCP_UPLOAD_TOKEN",
        "KB_MCP_CF_ACCESS_TEAM_DOMAIN", "KB_MCP_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def test_rest_disabled_without_key(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch)  # no KB_MCP_REST_API_KEY
    r = client.post("/api/find", json={"query": "metabolism", "mode": "keyword"})
    assert r.status_code == 503, r.text
    assert r.json() == {
        "success": False,
        "error": {
            "code": "REST_DISABLED",
            "message": "REST API is off: set KB_MCP_REST_API_KEY to enable the /api/* facade",
            "remediation": None,
        },
    }


def test_rest_wrong_key_unauthorized(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/find",
        json={"query": "metabolism", "mode": "keyword"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401, r.text


def test_rest_missing_key_unauthorized(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post("/api/find", json={"query": "metabolism", "mode": "keyword"})
    assert r.status_code == 401, r.text


def test_rest_find_roundtrip_matches_mcp_shape(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/find",
        json={"query": "metabolism", "mode": "keyword"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["success"] is True
    data = payload["data"]
    assert isinstance(data, list)
    # Same hit shape as the MCP `find` tool, wrapped in the shared envelope.
    find_module.clear_cache()
    expected = [h.as_dict() for h in find_module.find(vault, query="metabolism", mode="keyword")]
    assert data == expected
    assert data, "keyword find for 'metabolism' should surface fixture notes"
    assert {"path", "title", "type"} <= set(data[0].keys())


def test_rest_minted_rest_scoped_token_authorizes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from kb_mcp import upload_tokens

    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    token = upload_tokens.mint("sekret", scope="rest")
    r = client.post(
        "/api/find",
        json={"query": "metabolism", "mode": "keyword"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


def test_rest_upload_scoped_token_does_not_authorize_rest(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from kb_mcp import upload_tokens

    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    token = upload_tokens.mint("sekret", scope="upload")  # wrong scope
    r = client.post(
        "/api/find",
        json={"query": "x", "mode": "keyword"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401, r.text


def test_rest_get_roundtrip(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/get",
        json={"path": "Notes/Insights/progressive-disclosure-without-mode-fragmentation"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["frontmatter"]["type"] == "insight"
    assert "Progressive disclosure" in body["body"]
    assert "links" not in body  # links default off


def test_rest_get_not_found(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/get",
        json={"path": "Notes/Insights/does-not-exist"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 404, r.text


def test_rest_note_write_roundtrip(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/note",
        json={
            "note_type": "insight",
            "title": "REST facade is scriptable",
            "content": "# REST facade is scriptable\n\n## Claim\n\nScripts can write to the KB over HTTP.\n",
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    written = vault / r.json()["data"]["path"]
    assert written.exists()
    assert "scriptable" in written.read_text(encoding="utf-8")


def test_rest_note_validation_error_is_400(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/note",
        json={"note_type": "research-note", "title": "no project", "content": "x"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 400, r.text
    payload = r.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "INVALID_NOTE"
    assert payload["error"]["message"]


def test_rest_malformed_body_is_400(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/find",
        content=b"[1, 2, 3]",  # valid JSON but not an object
        headers={"Authorization": "Bearer sekret", "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_BODY"


def test_rest_openapi_self_doc(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.get("/api/openapi.json")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["openapi"].startswith("3.1")
    assert "/api/find" in doc["paths"] and "/api/note" in doc["paths"]


def test_rest_openapi_disabled_without_key(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch)
    r = client.get("/api/openapi.json")
    assert r.status_code == 503, r.text
