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
