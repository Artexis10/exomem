"""The registry-driven REST facade: back-compat for the original 9 routes,
new routes for previously-unexposed ops, the shared envelope, registry-derived
OpenAPI, and the preserved binary-blob guard.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from exomem import find as find_module
from exomem import server

# The routes that existed before the registry migration — names + leaf calls preserved.
LEGACY_ROUTES = [
    "find", "get", "note", "add", "edit",
    "audit", "reconcile", "list_directory", "suggest_links",
]


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_REST_API_KEY", "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def _auth() -> dict:
    return {"Authorization": "Bearer sekret"}


def test_all_legacy_routes_still_exist(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    # A POST with an empty body reaches the handler (not 404); legacy routes resolve.
    for name in LEGACY_ROUTES:
        r = client.post(f"/api/{name}", json={}, headers=_auth())
        assert r.status_code != 404, f"/api/{name} missing: {r.status_code} {r.text}"


def test_find_route_calls_the_same_leaf(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/find", json={"query": "metabolism", "mode": "keyword"}, headers=_auth()
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["success"] is True
    find_module.clear_cache()
    expected = [h.as_dict() for h in find_module.find(vault, query="metabolism", mode="keyword")]
    assert payload["data"] == expected


def test_previously_unexposed_op_now_has_a_route(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """`replace` had no REST route before; the registry gives it one."""
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    # Route exists (not 404). A bad call returns the shared error envelope, not a crash.
    r = client.post("/api/replace", json={}, headers=_auth())
    assert r.status_code != 404, r.text
    body = r.json()
    assert body["success"] is False
    assert "code" in body["error"]


def test_link_and_provenance_routes_exist(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    for name in ("link", "provenance_report", "query_data", "list_inbound_links"):
        r = client.post(f"/api/{name}", json={}, headers=_auth())
        assert r.status_code != 404, f"/api/{name} missing"


def test_success_uses_envelope(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/audit", json={"categories": ["broken_wikilink"]}, headers=_auth()
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["success"] is True
    assert "findings" in payload["data"]


def test_validation_error_uses_envelope_with_code(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/note",
        json={"note_type": "research-note", "title": "no project", "content": "x"},
        headers=_auth(),
    )
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["code"] == "INVALID_NOTE"
    assert err["message"]


def test_unknown_param_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/find", json={"query": "x", "mode": "keyword", "bogus": 1}, headers=_auth()
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "UNKNOWN_PARAM"


def test_blob_guard_preserved(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    blob = "data:image/png;base64," + "A" * 40000
    r = client.post(
        "/api/note",
        json={"note_type": "insight", "title": "x", "content": blob},
        headers=_auth(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "BINARY_BLOB_REJECTED"


def test_blob_guard_nested_edits_preserved(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """`edit`'s batch mode carries the payload in edits[].new_string — REST must
    blob-guard each nested item, mirroring the MCP middleware (not only top-level)."""
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    blob = "data:image/png;base64," + "A" * 40000
    r = client.post(
        "/api/edit",
        json={
            "path": "Knowledge Base/Notes/Insights/x.md",
            "why": "nested blob",
            "edits": [{"old_string": "a", "new_string": blob}],
        },
        headers=_auth(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "BINARY_BLOB_REJECTED"


def test_openapi_lists_real_params(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    doc = client.get("/api/openapi.json").json()
    assert doc["openapi"].startswith("3.1")
    # Every registry rest op has a path...
    assert "/api/replace" in doc["paths"]  # newly exposed
    assert "/api/find" in doc["paths"]
    # ...with its actual parameters, not a generic {type: object}.
    find_schema = doc["paths"]["/api/find"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    props = find_schema["properties"]
    assert {"query", "limit", "scope", "mode", "tags"} <= set(props)
    assert props["limit"]["type"] == "integer"
    assert props["graph"]["type"] == "boolean"
    assert props["tags"]["type"] == "array"
    # get.path is required in the schema.
    get_schema = doc["paths"]["/api/get"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "path" in get_schema.get("required", [])


def test_attention_route_and_openapi_params(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """`attention` is exposed on REST from its single registry entry, with exactly
    `categories` + `limit` as documented parameters."""
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post("/api/attention", json={}, headers=_auth())
    assert r.status_code != 404, f"/api/attention missing: {r.status_code} {r.text}"
    body = r.json()
    assert body["success"] is True
    assert {"items", "summary", "shown", "total", "truncated", "upstream_truncated"} <= set(
        body["data"]
    )
    doc = client.get("/api/openapi.json").json()
    assert "/api/attention" in doc["paths"]
    schema = doc["paths"]["/api/attention"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert set(schema["properties"]) == {"categories", "limit"}
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["categories"]["type"] == "array"


def test_openapi_has_no_hand_list(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAPI is generated from the registry — tier-2 ops appear too (no frozen list)."""
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    doc = client.get("/api/openapi.json").json()
    # A tier-2 op (query_data) is documented, proving it's registry-sourced.
    assert "/api/query_data" in doc["paths"]
