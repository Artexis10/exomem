"""/upload endpoint — out-of-band binary upload straight into Evidence/.

Drives the real FastMCP ASGI app via Starlette's sync TestClient (no
pytest-asyncio dependency). `load_dotenv` is neutralized so the repo `.env`
can't clobber the per-test fixture vault.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from exomem import server


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    # Hermeticity: the /upload route is gated by these env vars, and a developer's
    # ambient shell may already have them set (the author's deployment box does).
    # Clear them first so a test that doesn't pass one sees the "disabled" default
    # instead of silently inheriting the ambient value (caught a false 401 vs 503).
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_UPLOAD_MAX_BYTES"):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def test_upload_requires_auth(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.post(
        "/upload",
        files={"file": ("shot.png", b"\x89PNGdata", "image/png")},
        data={"scope": "Yolo", "category": "01 - Check-in"},
    )
    assert r.status_code == 401


def test_upload_happy_path_lands_in_evidence(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.post(
        "/upload",
        files={"file": ("shot.png", b"\x89PNGrealbytes", "image/png")},
        data={"scope": "Yolo", "category": "01 - Check-in"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "Evidence/Yolo/01 - Check-in/shot.png" in body["path"]
    written = vault / body["path"]
    assert written.read_bytes() == b"\x89PNGrealbytes"


def test_spoofed_cf_access_header_is_not_trusted(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # A client-supplied Cf-Access-* header is spoofable and must NEVER authorize.
    # Regression for the spoofable-field auth-bypass finding.
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"bytes", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-authenticated-user-email": "attacker@evil.com"},
    )
    assert r.status_code == 401, r.text


def test_upload_disabled_ignores_spoofed_header(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # No token configured → off, and a spoofed CF header cannot re-enable it.
    client = _client(vault, monkeypatch)
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"bytes", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-authenticated-user-email": "attacker@evil.com"},
    )
    assert r.status_code == 503


def test_upload_rejects_oversize(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(
        vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret", EXOMEM_UPLOAD_MAX_BYTES="16"
    )
    r = client.post(
        "/upload",
        files={"file": ("big.bin", b"x" * 64, "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 413, r.text


def test_upload_duplicate_is_conflict(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    headers = {"Authorization": "Bearer sekret"}
    first = client.post(
        "/upload",
        files={"file": ("dupe.bin", b"first", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers=headers,
    )
    assert first.status_code == 201
    second = client.post(
        "/upload",
        files={"file": ("dupe.bin", b"second", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers=headers,
    )
    assert second.status_code == 409, second.text


def test_minted_short_lived_token_authorizes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import upload_tokens

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    minted = upload_tokens.mint("sekret")  # valid ~15 min
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"viaminted", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": f"Bearer {minted}"},
    )
    assert r.status_code == 201, r.text


def test_expired_minted_token_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import upload_tokens

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    expired = upload_tokens.mint("sekret", ttl=-10)  # already past exp
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401, r.text


def test_cf_access_valid_jwt_authorizes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # No bearer token configured; a *verified* CF Access JWT is the credential.
    monkeypatch.setattr("exomem.cf_access.verify", lambda *a, **k: True)
    client = _client(
        vault,
        monkeypatch,
        EXOMEM_CF_ACCESS_TEAM_DOMAIN="t.cloudflareaccess.com",
        EXOMEM_CF_ACCESS_AUD="aud123",
    )
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"viacfaccess", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-jwt-assertion": "fake.jwt.token"},
    )
    assert r.status_code == 201, r.text


def test_cf_access_invalid_jwt_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("exomem.cf_access.verify", lambda *a, **k: False)
    client = _client(
        vault,
        monkeypatch,
        EXOMEM_CF_ACCESS_TEAM_DOMAIN="t.cloudflareaccess.com",
        EXOMEM_CF_ACCESS_AUD="aud123",
    )
    r = client.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"cf-access-jwt-assertion": "bad"},
    )
    assert r.status_code == 401, r.text


def test_upload_text_field_writes_searchable_sidecar(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # The OCR companion over HTTP: the `text` form field becomes the embedded,
    # keyword-findable sidecar body so the binary is searchable by its content.
    from exomem import find as find_module

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.post(
        "/upload",
        files={"file": ("invoice.png", b"\x89PNGbytes", "image/png")},
        data={
            "scope": "Yolo",
            "category": "01 - Check-in",
            "text": "Invoice total 4200 EUR, vendor Acme Plumbing, dated 2026-05-20.",
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 201, r.text
    sidecar_rel = r.json()["sidecar_path"]
    assert sidecar_rel and sidecar_rel.endswith("invoice.png.md")
    sidecar = vault / sidecar_rel
    assert "Acme Plumbing" in sidecar.read_text(encoding="utf-8")
    find_module.clear_cache()
    hits = find_module.find(vault, query="Acme Plumbing", mode="keyword")
    assert any("invoice.png.md" in h.path for h in hits), [h.path for h in hits]


def test_upload_get_serves_prefilled_form(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
    r = client.get("/upload?scope=Yolo&category=01%20-%20Check-in")
    assert r.status_code == 200
    assert "Add evidence" in r.text
    assert 'value="Yolo"' in r.text
    assert "name=text" in r.text  # searchable-text field present
