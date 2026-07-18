"""Brand-asset routes — the connector fetches the favicon from the domain root.

These must be PUBLIC (no OAuth): claude.ai loads the favicon before/without the
authenticated MCP session, so a 401 here would leave the connector unbranded.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from exomem import server


def _client(vault, monkeypatch: pytest.MonkeyPatch, *, require_auth: bool = False) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    if require_auth:
        monkeypatch.setenv("EXOMEM_BASE_URL", "https://example.test")
        monkeypatch.setenv("GITHUB_CLIENT_ID", "x")
        monkeypatch.setenv("GITHUB_CLIENT_SECRET", "y")
        monkeypatch.setenv("EXOMEM_GITHUB_USERNAME", "z")
        monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "123456")
        monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "stable-test-signing-root")
    return TestClient(server.build_server(require_auth=require_auth).http_app())


def test_favicon_ico_served(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _client(vault, monkeypatch).get("/favicon.ico")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO magic


def test_favicon_svg_served(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _client(vault, monkeypatch).get("/favicon.svg")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content


def test_favicon_public_even_with_auth_enabled(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point: no bearer token, auth turned ON, favicon still 200.
    r = _client(vault, monkeypatch, require_auth=True).get("/favicon.ico")
    assert r.status_code == 200, r.text


def test_health_endpoint_public_and_reports_version(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/health` is an unauthenticated liveness probe (tunnels/orchestrators need
    an HTTP readiness check; previously only CLI `doctor` existed). Public even
    with auth on, returns status + version, and leaks no vault data."""
    r = _client(vault, monkeypatch).get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "exomem"
    assert "version" in body
    # Public even with OAuth enabled.
    r2 = _client(vault, monkeypatch, require_auth=True).get("/health")
    assert r2.status_code == 200, r2.text


def test_readiness_endpoint_is_public_and_content_free(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_readiness

    monkeypatch.setattr(
        runtime_readiness,
        "runtime_readiness",
        lambda **_kwargs: {
            "status": "ready",
            "service": "exomem",
            "release": "1.2.3",
            "runtime_contract": 1,
            "transport": "streamable-http-stateless",
            "replica_id": "desktop",
            "coordination": {
                "enabled": True,
                "role": "writer",
                "coordinator_healthy": True,
            },
            "takeover_eligible": True,
            "reasons": [],
        },
    )

    response = _client(vault, monkeypatch, require_auth=True).get("/health/ready")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["runtime_contract"] == 1
    rendered = response.text.lower()
    assert "vault" not in rendered
    assert "token" not in rendered


def test_readiness_endpoint_returns_503_without_changing_liveness(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_readiness

    monkeypatch.setattr(
        runtime_readiness,
        "runtime_readiness",
        lambda **_kwargs: {
            "status": "not_ready",
            "service": "exomem",
            "release": "1.2.3",
            "runtime_contract": 1,
            "transport": "streamable-http-stateless",
            "replica_id": "desktop",
            "coordination": {
                "enabled": True,
                "role": "unknown",
                "coordinator_healthy": False,
            },
            "takeover_eligible": False,
            "reasons": ["coordinator_unavailable"],
        },
    )
    client = _client(vault, monkeypatch)

    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 503
