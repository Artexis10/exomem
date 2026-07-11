"""The registry-driven REST facade exposes product commands, shared envelopes,
registry-derived OpenAPI, and the preserved binary-blob guard.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from exomem import find as find_module
from exomem import server

PRODUCT_ROUTES = [
    "bootstrap",
    "ask_memory",
    "read_memory",
    "browse_memory",
    "remember",
    "edit_memory",
    "replace_memory",
    "capture_source",
    "compile_source",
    "preserve_evidence",
    "transfer_artifact",
    "review_memory",
    "review_item_context",
    "triage_memory",
    "connect_memory",
    "adopt_vault",
    "maintain_memory",
    "schema_memory",
    "manage_memory_file",
    "query_dataset",
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


def test_all_product_routes_exist(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    for name in PRODUCT_ROUTES:
        r = client.post(f"/api/{name}", json={}, headers=_auth())
        assert r.status_code != 404, f"/api/{name} missing: {r.status_code} {r.text}"


def test_ask_memory_route_calls_the_same_find_leaf(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/ask_memory", json={"query": "metabolism", "mode": "keyword", "detail": "full"}, headers=_auth()
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["success"] is True
    find_module.clear_cache()
    expected = [h.as_dict() for h in find_module.find(vault, query="metabolism", mode="keyword")]
    assert payload["data"] == expected


def test_replace_memory_route_exists(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post("/api/replace_memory", json={}, headers=_auth())
    assert r.status_code != 404, r.text
    body = r.json()
    assert body["success"] is False
    assert "code" in body["error"]


def test_product_review_connection_dataset_and_file_routes_exist(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    for name in (
        "connect_memory",
        "review_memory",
        "triage_memory",
        "query_dataset",
        "manage_memory_file",
    ):
        r = client.post(f"/api/{name}", json={}, headers=_auth())
        assert r.status_code != 404, f"/api/{name} missing"


def test_success_uses_envelope(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/review_memory",
        json={"mode": "audit", "categories": ["broken_wikilink"]},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["success"] is True
    assert "findings" in payload["data"]


def test_validation_error_uses_envelope_with_code(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/remember",
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
        "/api/ask_memory", json={"query": "x", "mode": "keyword", "bogus": 1}, headers=_auth()
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "UNKNOWN_PARAM"


def test_blob_guard_preserved(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    blob = "data:image/png;base64," + "A" * 40000
    r = client.post(
        "/api/remember",
        json={"note_type": "insight", "title": "x", "content": blob},
        headers=_auth(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "BINARY_BLOB_REJECTED"


def test_blob_guard_nested_edits_preserved(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    blob = "data:image/png;base64," + "A" * 40000
    r = client.post(
        "/api/edit_memory",
        json={
            "path": "Knowledge Base/Notes/Insights/x.md",
            "why": "nested blob",
            "edits": [{"old_string": "a", "new_string": blob}],
        },
        headers=_auth(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "BINARY_BLOB_REJECTED"


def test_openapi_lists_real_product_params(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    doc = client.get("/api/openapi.json").json()
    assert doc["openapi"].startswith("3.1")
    assert "/api/replace_memory" in doc["paths"]
    assert "/api/ask_memory" in doc["paths"]
    ask_schema = doc["paths"]["/api/ask_memory"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    props = ask_schema["properties"]
    assert {"query", "limit", "scope", "mode", "tags", "deep"} <= set(props)
    assert props["limit"]["type"] == "integer"
    assert props["graph"]["type"] == "boolean"
    assert props["tags"]["type"] == "array"
    read_schema = doc["paths"]["/api/read_memory"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "path" in read_schema.get("required", [])
    schema_contract = doc["paths"]["/api/schema_memory"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert set(schema_contract.get("required", [])) == {"operation"}
    assert {"name", "subject", "proposal", "include_model_suggestions"} <= set(
        schema_contract["properties"]
    )
    assert {"project", "page_type", "save", "expected_hash", "strict", "compare_to"} <= set(
        schema_contract["properties"]
    )


def test_review_memory_route_and_openapi_params(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post("/api/review_memory", json={"mode": "attention"}, headers=_auth())
    assert r.status_code != 404, f"/api/review_memory missing: {r.status_code} {r.text}"
    body = r.json()
    assert body["success"] is True
    assert {"items", "summary", "shown", "total", "truncated", "upstream_truncated"} <= set(
        body["data"]
    )
    doc = client.get("/api/openapi.json").json()
    assert "/api/review_memory" in doc["paths"]
    schema = doc["paths"]["/api/review_memory"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert {"mode", "categories", "limit", "query", "sources", "state", "ref"} <= set(
        schema["properties"]
    )
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["categories"]["type"] == "array"

    item = body["data"]["items"][0]
    triage = client.post(
        "/api/triage_memory",
        json={"ref": item["ref"], "action": "snooze", "until": "2099-01-01"},
        headers=_auth(),
    )
    assert triage.status_code == 200, triage.text
    assert triage.json()["data"]["state"] == "snoozed"
    assert "/api/triage_memory" in doc["paths"]
    triage_schema = doc["paths"]["/api/triage_memory"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert {"ref", "action"} <= set(triage_schema.get("required", []))


def test_review_memory_activation_route(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    response = client.post(
        "/api/review_memory",
        json={"mode": "activation", "limit": 3},
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["coverage"]["eligible_pages"] > 0
    assert data["shown"] == len(data["items"]) <= 3
    assert all(item["ref"].startswith("exomem://review/") for item in data["items"])


def test_review_item_context_route_and_openapi(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    review = client.post(
        "/api/review_memory",
        json={"mode": "activation", "limit": 1},
        headers=_auth(),
    ).json()["data"]
    item = review["items"][0]

    response = client.post(
        "/api/review_item_context",
        json={
            "ref": item["ref"],
            "expected_fingerprint": item["fingerprint"],
            "max_body_chars": 200,
        },
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["item"]["ref"] == item["ref"]
    doc = client.get("/api/openapi.json").json()
    schema = doc["paths"]["/api/review_item_context"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert "ref" in schema.get("required", [])
    assert {
        "expected_fingerprint",
        "max_body_chars",
        "max_related_pages",
        "max_graph_nodes",
        "max_graph_edges",
        "max_history",
        "max_evolution_versions",
    } <= set(schema["properties"])


def test_openapi_has_no_hand_list(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    doc = client.get("/api/openapi.json").json()
    assert "/api/query_dataset" in doc["paths"]
