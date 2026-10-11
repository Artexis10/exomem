"""Verified client identity and live login authority survive delegated downloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.server import dependencies
from fastmcp.server.auth.auth import AccessToken
from starlette.testclient import TestClient

from exomem import local_ingress, server, server_auth, upload_tokens
from exomem.auth_sessions import SessionAuthority, SessionIdentity
from exomem.governance import principal
from exomem.session_oauth import ExomemSessionOAuthProxy

ISSUER = "https://memory.example.test"
SIGNING_ROOT = "temporary-connector-private-signing-root"
IDENTITY = SessionIdentity(github_user_id=4242, github_login="fixture-owner")


@pytest.fixture
def authority(tmp_path):
    return SessionAuthority.local(
        directory=tmp_path / "sessions", signing_root=SIGNING_ROOT,
        issuer=ISSUER, audience=f"{ISSUER}/mcp",
    )


async def authenticated_owner(authority, monkeypatch):
    bearer, record, refresh = await authority.issue_offline(
        client_id="configured-client", scopes=["exomem:read", "exomem:write", "offline_access"], identity=IDENTITY,
    )
    token = await ExomemSessionOAuthProxy.load_access_token(
        SimpleNamespace(_session_authority=authority), bearer,
    )
    monkeypatch.setenv("EXOMEM_BASE_URL", ISSUER)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "4242")
    monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", "github:4242")
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    return principal.resolve_mcp_principal(), record, refresh


def test_verified_client_and_login_survive_owner_normalization_and_transfer(authority, monkeypatch):
    who, record, _ = asyncio.run(authenticated_owner(authority, monkeypatch))
    assert who.audience_id == principal.OWNER_AUDIENCE
    assert who.client_binding.issuer == ISSUER
    assert who.client_binding.client_id == record.client_id
    assert who.origin_session.session_id == record.session_id
    assert who.origin_session.generation == record.generation
    layered = who.with_purpose("recall").with_authorization_session("conversation")
    capability = upload_tokens.mint_principal(SIGNING_ROOT, layered)
    restored = upload_tokens.bound_principal(capability, SIGNING_ROOT)
    assert restored == layered
    assert restored.client_binding.client_id == record.client_id
    assert restored.origin_session.session_id == record.session_id
    assert record.token_digest not in repr(restored)


def test_arbitrary_verified_claims_cannot_create_a_connector_binding(monkeypatch):
    token = AccessToken(
        token="ordinary-token", client_id="configured-client", scopes=[],
        claims={"iss": ISSUER, "sub": "4242", "client_id": "configured-client"},
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    resolved = principal.resolve_mcp_principal()
    assert resolved.client_binding is None
    assert resolved.origin_session is None


def test_local_grant_retains_client_and_login_on_mcp_and_rest(tmp_path, monkeypatch):
    local = SessionAuthority.local(
        directory=tmp_path / "sessions", signing_root=SIGNING_ROOT,
        issuer=local_ingress.LOCAL_ISSUER, audience=local_ingress.LOCAL_AUDIENCE,
    )

    async def issue():
        bearer, record = await local.issue(
            client_id="local-client", scopes=list(local_ingress.LOCAL_SCOPES), identity=IDENTITY,
        )
        grant = await local_ingress.LocalCredentialVerifier(local).verify(bearer)
        return grant, record

    grant, record = asyncio.run(issue())
    binding = local_ingress._GRANT.set(grant)
    monkeypatch.setattr(dependencies, "get_access_token", lambda: grant.access_token)
    try:
        for who in (principal.resolve_mcp_principal(), principal.local_owner_principal(surface="rest")):
            assert who.local_owner
            assert who.client_binding.client_id == "local-client"
            assert who.client_binding.issuer == local_ingress.LOCAL_ISSUER
            assert who.origin_session.session_id == record.session_id
            assert who.origin_session.generation == record.generation
    finally:
        local_ingress._GRANT.reset(binding)


@pytest.mark.parametrize("invalidation", ["revoke", "generation", "expiry", "refresh-family"])
def test_delegated_download_stops_when_origin_login_ends(
    vault, authority, monkeypatch, invalidation,
):
    who, record, refresh = asyncio.run(authenticated_owner(authority, monkeypatch))
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "temporary-public-transfer-token")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", SIGNING_ROOT)
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **kwargs: authority)
    capability = upload_tokens.mint_principal(SIGNING_ROOT, who)
    headers = {"Authorization": f"Bearer {capability}"}
    with TestClient(server.build_server(require_auth=False).http_app()) as client:
        before = client.get("/download", params={"path": "Knowledge Base/index.md"}, headers=headers)
        assert before.status_code == 200, before.text
        assert before.content == (vault / "Knowledge Base/index.md").read_bytes()
        if invalidation == "revoke":
            asyncio.run(authority.tombstone(record.session_id, reason="operator"))
        elif invalidation == "generation":
            asyncio.run(authority.replace_generation())
        elif invalidation == "expiry":
            authority.clock = lambda: record.expires_at + 1
        else:
            asyncio.run(authority.revoke_bearer(refresh, reason="operator"))
        after = client.get("/download", params={"path": "Knowledge Base/index.md"}, headers=headers)
        assert after.status_code == 401, after.text
        assert b"Knowledge Base" not in after.content
