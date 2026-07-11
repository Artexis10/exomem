"""Packaged Review Studio shell, static routes, and vault-data auth boundary."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import server, server_assets


def _client(vault: Path, monkeypatch: pytest.MonkeyPatch, *, api_key: str | None = None):
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    for name in (
        "EXOMEM_REST_API_KEY",
        "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
        "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(name, raising=False)
    if api_key:
        monkeypatch.setenv("EXOMEM_REST_API_KEY", api_key)
    return TestClient(server.build_server(require_auth=False).http_app())


def test_packaged_manifest_contains_every_source_controlled_asset() -> None:
    studio = files("exomem").joinpath("studio")
    manifest = json.loads(studio.joinpath("manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == 1
    assert set(manifest["assets"]) == {
        "index.html",
        "styles.v1.css",
        "api.v1.js",
        "state.v1.js",
        "model.v1.js",
        "app.v1.js",
        "studio-icon.v1.svg",
    }
    for asset in manifest["assets"]:
        assert studio.joinpath(asset).is_file(), asset


def test_studio_shell_and_versioned_assets_have_cache_and_security_headers(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(vault, monkeypatch)

    redirect = client.get("/studio", follow_redirects=False)
    shell = client.get("/studio/")
    script = client.get("/studio/assets/app.v1.js")

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/studio/"
    assert shell.status_code == 200, shell.text
    assert shell.headers["content-type"].startswith("text/html")
    assert shell.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in shell.headers["content-security-policy"]
    assert "connect-src 'self'" in shell.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in shell.headers["content-security-policy"]
    assert shell.headers["x-content-type-options"] == "nosniff"
    assert shell.headers["x-frame-options"] == "DENY"
    assert script.status_code == 200, script.text
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_shell_is_inert_and_client_keeps_key_session_scoped(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(vault, monkeypatch, api_key="top-secret-test-key")
    shell = client.get("/studio/").text
    api_client = client.get("/studio/assets/api.v1.js").text
    all_assets = shell + api_client + client.get("/studio/assets/app.v1.js").text

    assert "metabolic-literacy-curriculum" not in all_assets
    assert "top-secret-test-key" not in all_assets
    assert "sessionStorage" in api_client
    assert "localStorage" not in all_assets
    assert "Authorization" in api_client
    assert "credentials: \"same-origin\"" in api_client
    assert "https://" not in all_assets and "http://" not in all_assets


def test_asset_allowlist_rejects_unknown_nested_and_shell_aliases(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(vault, monkeypatch)

    for path in (
        "/studio/assets/not-packaged.js",
        "/studio/assets/nested/app.v1.js",
        "/studio/assets/index.html",
        "/studio/assets/%2e%2e%2ficon.svg",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in {404, 503}, (path, response.status_code, response.text)
        if response.headers.get("content-type", "").startswith("application/json"):
            assert response.json()["error"] == "STUDIO_ASSETS_UNAVAILABLE"


def test_unauthenticated_shell_exposes_no_api_data(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(vault, monkeypatch, api_key="sekret")

    assert client.get("/studio/").status_code == 200
    denied = client.post("/api/review_memory", json={"mode": "attention"})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED"
    allowed = client.post(
        "/api/review_memory",
        json={"mode": "attention", "limit": 1},
        headers={"Authorization": "Bearer sekret"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["success"] is True


def test_missing_studio_assets_soft_fail_without_breaking_other_routes(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "missing-studio"
    broken.mkdir()
    monkeypatch.setattr(server_assets, "_studio_dir", lambda: broken)
    client = _client(vault, monkeypatch, api_key="sekret")

    studio_response = client.get("/studio/")
    assert studio_response.status_code == 503
    assert studio_response.json()["error"] == "STUDIO_ASSETS_UNAVAILABLE"
    assert client.get("/favicon.svg").status_code == 200
    rest = client.post(
        "/api/read_memory",
        json={"path": "Knowledge Base/index.md"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert rest.status_code == 200, rest.text
