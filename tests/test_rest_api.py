"""Personal REST facade (/api/*) — auth matrix + leaf round-trips.

Drives the real FastMCP ASGI app via Starlette's sync TestClient (no live server,
no torch: ask_memory runs find in keyword mode). `load_dotenv` is neutralized and ambient env
is cleared so the repo `.env` / dev shell can't clobber the per-test fixture vault.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from exomem import cf_access, commands, semantic_index, server
from exomem import find as find_module


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_REST_API_KEY", "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def test_rest_disabled_without_key(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch)  # no EXOMEM_REST_API_KEY
    r = client.post("/api/ask_memory", json={"query": "metabolism", "mode": "keyword", "detail": "full"})
    assert r.status_code == 503, r.text
    assert r.json() == {
        "success": False,
        "error": {
            "code": "REST_DISABLED",
            "message": "REST API is off: set EXOMEM_REST_API_KEY to enable the /api/* facade",
            "remediation": None,
        },
    }


def test_rest_wrong_key_unauthorized(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/ask_memory",
        json={"query": "metabolism", "mode": "keyword", "detail": "full"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401, r.text


def test_rest_missing_key_unauthorized(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post("/api/ask_memory", json={"query": "metabolism", "mode": "keyword", "detail": "full"})
    assert r.status_code == 401, r.text


def test_rest_ask_memory_roundtrip_matches_leaf_shape(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/ask_memory",
        json={"query": "metabolism", "mode": "keyword", "detail": "full"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["success"] is True
    # The fixture vault holds uncompiled Sources, so the session's first recall carries the
    # due-state block and the hits arrive in the recall envelope.
    data = payload["data"]["hits"]
    assert isinstance(data, list)
    # Same hit shape as the product `ask_memory` tool, wrapped in the shared envelope.
    find_module.clear_cache()
    expected = [h.as_dict() for h in find_module.find(vault, query="metabolism", mode="keyword")]
    assert data == expected
    assert data, "keyword ask_memory for 'metabolism' should surface fixture notes"
    assert {"path", "title", "type"} <= set(data[0].keys())


def test_rest_minted_rest_scoped_token_authorizes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import upload_tokens

    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    token = upload_tokens.mint("sekret", scope="rest")
    r = client.post(
        "/api/ask_memory",
        json={"query": "metabolism", "mode": "keyword", "detail": "full"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


def test_rest_upload_scoped_token_does_not_authorize_rest(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import upload_tokens

    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    token = upload_tokens.mint("sekret", scope="upload")  # wrong scope
    r = client.post(
        "/api/ask_memory",
        json={"query": "x", "mode": "keyword"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401, r.text


def test_rest_read_memory_roundtrip(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/read_memory",
        json={"path": "Notes/Insights/progressive-disclosure-without-mode-fragmentation"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["frontmatter"]["type"] == "insight"
    assert "Progressive disclosure" in body["body"]
    assert "links" not in body  # links default off


def test_rest_read_memory_resolves_exact_semantic_unit(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/Insights/rest-exact-unit.md"
    (vault / rel).write_text(
        "---\n"
        "type: insight\n"
        "exomem_id: 12345678-1234-5678-1234-567812345678\n"
        "title: REST exact unit\n"
        "status: active\n"
        "updated: 2026-07-16\n"
        "---\n\n"
        "- [config] REST can read this unit ^rest-unit\n",
        encoding="utf-8",
    )
    state = semantic_index.current_parent_index_state(vault, rel)
    unit_ref = state.document.units[0].unit_ref
    assert unit_ref is not None
    expected = commands.op_read_memory(vault, path=rel, unit_ref=unit_ref)
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")

    response = client.post(
        "/api/read_memory",
        json={"path": rel, "unit_ref": unit_ref},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data == expected
    assert data["status"] == "found"
    assert data["unit"]["unit_ref"] == unit_ref
    assert data["unit"]["content"] == "REST can read this unit"


def test_rest_read_memory_not_found(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/read_memory",
        json={"path": "Notes/Insights/does-not-exist"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 404, r.text


def test_rest_remember_write_roundtrip(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/remember",
        json={
            "note_type": "insight",
            "title": "REST facade is scriptable",
            "content": "# REST facade is scriptable\n\n## Claim\n\nScripts can write to the KB over HTTP.\n",
            "status": "draft",
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    written = vault / r.json()["data"]["path"]
    assert written.exists()
    assert "scriptable" in written.read_text(encoding="utf-8")


def _unkeyed_write(title: str) -> dict:
    return {
        "note_type": "insight",
        "title": title,
        "content": f"# {title}\n\n## Claim\n\nA resend must not write this twice.\n",
        "status": "draft",
    }


def test_rest_resend_without_a_key_replays_instead_of_committing_twice(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that lost the reply and resent the write must not duplicate the note."""
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    auth = {"Authorization": "Bearer sekret"}
    body = _unkeyed_write("Resent over REST")

    first = client.post("/api/remember", json=body, headers=auth)
    resend = client.post("/api/remember", json=body, headers=auth)

    assert first.status_code == resend.status_code == 200, (first.text, resend.text)
    assert resend.json()["data"]["path"] == first.json()["data"]["path"]
    assert len(list((vault / "Knowledge Base").rglob("resent-over-rest*.md"))) == 1


def test_rest_principals_with_different_audiences_do_not_share_an_unkeyed_result(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Access identities sending the same bytes each write; neither gets the other's note."""
    client = _client(
        vault,
        monkeypatch,
        EXOMEM_REST_API_KEY="sekret",
        EXOMEM_CF_ACCESS_TEAM_DOMAIN="team.example.test",
        EXOMEM_CF_ACCESS_AUD="audience-tag",
    )
    # The JWT signature check belongs to the Access library; the identity it returns is what matters here.
    monkeypatch.setattr(
        cf_access,
        "verified_claims",
        lambda token, **_: {"sub": token, "iss": "https://team.example.test"} if token else None,
    )
    body = _unkeyed_write("Same bytes two principals")

    def post(subject: str):
        return client.post(
            "/api/remember", json=body, headers={"Cf-Access-Jwt-Assertion": subject}
        )

    alice, bob, alice_again = post("alice"), post("bob"), post("alice")

    assert [r.status_code for r in (alice, bob, alice_again)] == [200, 200, 200]
    assert alice.json()["data"]["path"] != bob.json()["data"]["path"]
    assert alice_again.json()["data"]["path"] == alice.json()["data"]["path"]
    assert len(list((vault / "Knowledge Base").rglob("same-bytes-two-principals*.md"))) == 2


def test_rest_remember_validation_error_is_400(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/remember",
        json={"note_type": "research-note", "title": "no project", "content": "x"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 400, r.text
    payload = r.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "INVALID_NOTE"
    assert payload["error"]["message"]


def test_rest_malformed_body_is_400(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/ask_memory",
        content=b"[1, 2, 3]",  # valid JSON but not an object
        headers={"Authorization": "Bearer sekret", "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_BODY"


def test_rest_openapi_self_doc(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.get("/api/openapi.json")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["openapi"].startswith("3.1")
    assert "/api/search_memory" in doc["paths"] and "/api/create_note" in doc["paths"]


def test_rest_openapi_disabled_without_key(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch)
    r = client.get("/api/openapi.json")
    assert r.status_code == 503, r.text


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_rest_command_timer_covers_response_construction_and_ingress_exclusions(
    vault, monkeypatch
) -> None:
    import time

    from exomem import latency_watch, server_rest

    watch = latency_watch.Watch(started_at=time.time() - 601)
    monkeypatch.setattr(latency_watch, "_WATCH", watch)
    original_render = server_rest.RestJSONResponse.render

    def delayed_render(self, content):
        time.sleep(0.015)
        return original_render(self, content)

    monkeypatch.setattr(server_rest.RestJSONResponse, "render", delayed_render)
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="service-key")
    headers = {"Authorization": "Bearer service-key"}
    successful = client.post("/api/activate_context", json={"turn": " "}, headers=headers)
    assert successful.status_code == 200
    refused = client.post(
        "/api/activate_context", json={"turn": " ", "max_chars": "invalid"}, headers=headers
    )
    assert refused.status_code == 400
    unauthorized = client.post("/api/activate_context", json={"turn": " "})
    assert unauthorized.status_code == 401
    rows = watch.verdicts()
    command = next(row for row in rows if row["surface"] == "rest")
    leaf = next(row for row in rows if row["surface"] == "activation")
    assert command["observed"] == 3 and command["samples"] == 2
    assert command["refusals"] == 2 and command["excluded"] == 1
    assert command["p50_ms"] >= 15
    assert leaf["observed"] == 1 and leaf["abstentions"] == 1


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_rest_observation_failure_preserves_the_response(vault, monkeypatch) -> None:
    from exomem import latency_watch

    def broken(**kwargs):
        raise RuntimeError("observation unavailable")

    monkeypatch.setattr(latency_watch, "observe", broken)
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="service-key")
    response = client.post(
        "/api/activate_context", json={"turn": " "}, headers={"Authorization": "Bearer service-key"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["abstained"] is True
    assert latency_watch.current().failures >= 1
