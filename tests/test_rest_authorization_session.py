"""REST authorization-session admission precedes ordinary body work."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from exomem import server, server_rest, writer_lease

BEARER = (
    "as1.AQEBAQEBAQEBAQEBAQEBAQ."
    "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
)


@pytest.fixture(autouse=True)
def _reset_writer_state() -> None:
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _client(vault, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "service-key")
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "writer-lease-state")
    )
    for name in (
        "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
        "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(name, raising=False)
    return TestClient(server.build_server(require_auth=False).http_app())


def _headers(credential: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer service-key"}
    if credential is not None:
        headers["X-Exomem-Authorization-Session"] = credential
    return headers


def test_invalid_header_wins_before_rest_coercion(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(vault, monkeypatch)
    monkeypatch.setattr(
        server_rest.cli_ops,
        "coerce",
        lambda *args, **kwargs: pytest.fail("REST coercion ran before credential refusal"),
    )

    response = client.post(
        "/api/ask_memory",
        json={"limit": "malformed"},
        headers=_headers("not-a-bearer"),
    )

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "error": {
            "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
            "message": "authorization session is unavailable",
            "remediation": None,
        },
    }
    assert "not-a-bearer" not in response.text


def test_invalid_header_wins_before_rest_json_parsing(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _client(vault, monkeypatch).post(
        "/api/ask_memory",
        content=b"not-json",
        headers={**_headers("not-a-bearer"), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
        "message": "authorization session is unavailable",
        "remediation": None,
    }
    assert "not-json" not in response.text
    assert "not-a-bearer" not in response.text


def test_unknown_canonical_header_wins_before_rest_json_parsing(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _client(vault, monkeypatch).post(
        "/api/ask_memory",
        content=b"not-json",
        headers={**_headers(BEARER), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
        "message": "authorization session is unavailable",
        "remediation": None,
    }
    assert "not-json" not in response.text
    assert BEARER not in response.text


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (
            "/api/ask_memory",
            {"query": "x", "authorization_session_credential": BEARER},
        ),
        (
            f"/api/ask_memory?authorization_session_credential={BEARER}",
            {"query": "x"},
        ),
        (
            "/api/ask_memory",
            {"query": f"retrieved text {BEARER}"},
        ),
    ],
)
def test_rest_body_and_query_bearer_carriers_refuse_content_free(
    vault,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    body: dict[str, object],
) -> None:
    response = _client(vault, monkeypatch).post(url, json=body, headers=_headers())

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
        "message": "authorization session is unavailable",
        "remediation": None,
    }
    assert BEARER not in response.text


def test_absent_optional_rest_credential_preserves_service_authorization(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _client(vault, monkeypatch).post(
        "/api/browse_memory",
        json={"mode": "list"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


def test_rest_openapi_advertises_only_the_protected_header_carrier(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _client(vault, monkeypatch).get("/api/openapi.json").json()

    for path in document["paths"].values():
        operation = path["post"]
        assert operation["parameters"] == [
            {
                "name": "X-Exomem-Authorization-Session",
                "in": "header",
                "required": False,
                "description": (
                    "Optional opaque authorization-session capability; distinct "
                    "from service Authorization."
                ),
                "schema": {"type": "string", "minLength": 70, "maxLength": 70},
            }
        ]
        properties = operation["requestBody"]["content"]["application/json"][
            "schema"
        ]["properties"]
        assert "authorization_session_credential" not in properties
        assert "principal" not in properties
        assert "principal_scope" not in properties
        assert "issuer" not in properties
        assert "authorization_session_id" not in properties
