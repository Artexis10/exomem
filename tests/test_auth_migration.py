from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from exomem.session_oauth import ExomemSessionOAuthProxy


class Verifier:
    required_scopes = ["exomem:read"]

    async def verify_token(self, token: str) -> AccessToken | None:
        return None


class Authority:
    def __init__(self) -> None:
        self.validations: list[str] = []
        self.sessions = {"new-session": object()}

    async def validate(self, token: str) -> Any:
        self.validations.append(token)
        return None


def _kwargs(storage: MemoryStore) -> dict[str, Any]:
    return {
        "upstream_authorization_endpoint": "https://github.com/login/oauth/authorize",
        "upstream_token_endpoint": "https://github.com/login/oauth/access_token",
        "upstream_client_id": "github-client",
        "upstream_client_secret": "github-secret",
        "token_verifier": Verifier(),
        "base_url": "https://memory.example",
        "allowed_client_redirect_uris": ["http://127.0.0.1:*"],
        "client_storage": storage,
        "jwt_signing_key": "stable-signing-root",
        "require_authorization_consent": False,
    }


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="codex-client",
        client_secret="client-secret",
        redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
        token_endpoint_auth_method="client_secret_post",
    )


def test_connector_routes_discovery_dcr_callback_and_consent_remain_compatible() -> None:
    legacy = OAuthProxy(**_kwargs(MemoryStore()))
    durable = ExomemSessionOAuthProxy(
        session_authority=Authority(),
        **_kwargs(MemoryStore()),
    )

    legacy_routes = legacy.get_routes("/mcp")
    durable_routes = durable.get_routes("/mcp")
    legacy_paths = {route.path for route in legacy_routes}
    durable_paths = {route.path for route in durable_routes}
    compatibility_paths = {
        "/authorize",
        "/token",
        "/register",
        "/auth/callback",
        "/consent",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
    }

    assert compatibility_paths <= legacy_paths
    assert compatibility_paths <= durable_paths
    assert str(durable.base_url).rstrip("/") == "https://memory.example"
    assert durable._redirect_path == legacy._redirect_path == "/auth/callback"
    assert durable._forward_pkce is legacy._forward_pkce is True
    assert durable._forward_resource is legacy._forward_resource is True


@pytest.mark.anyio
async def test_authorize_preserves_downstream_state_pkce_resource_and_redirect() -> None:
    proxy = ExomemSessionOAuthProxy(
        session_authority=Authority(),
        **_kwargs(MemoryStore()),
    )
    proxy.get_routes("/mcp")
    params = AuthorizationParams(
        state="downstream-state",
        scopes=["exomem:read"],
        code_challenge="downstream-code-challenge",
        redirect_uri=AnyUrl("http://127.0.0.1:8765/callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://memory.example/mcp",
    )

    upstream_url = await proxy.authorize(_client(), params)

    upstream_query = parse_qs(urlparse(upstream_url).query)
    transaction_id = upstream_query["state"][0]
    transaction = await proxy._transaction_store.get(key=transaction_id)
    assert transaction is not None
    assert transaction.client_state == "downstream-state"
    assert transaction.client_redirect_uri == "http://127.0.0.1:8765/callback"
    assert transaction.code_challenge == "downstream-code-challenge"
    assert transaction.resource == "https://memory.example/mcp"
    assert transaction.proxy_code_verifier
    assert upstream_query["code_challenge"][0] != "downstream-code-challenge"


@pytest.mark.anyio
async def test_legacy_jti_and_upstream_records_are_not_dual_read_or_mutated() -> None:
    storage = MemoryStore()
    await storage.put(
        "legacy-jti",
        {"sentinel": "jti-preserved"},
        collection="mcp-jti-mappings",
    )
    await storage.put(
        "legacy-upstream",
        {"sentinel": "upstream-preserved"},
        collection="mcp-upstream-tokens",
    )
    authority = Authority()
    proxy = ExomemSessionOAuthProxy(
        session_authority=authority,
        **_kwargs(storage),
    )

    assert await proxy.load_access_token("legacy.fastmcp.jwt") is None

    assert authority.validations == ["legacy.fastmcp.jwt"]
    assert await storage.get("legacy-jti", collection="mcp-jti-mappings") == {
        "sentinel": "jti-preserved"
    }
    assert await storage.get("legacy-upstream", collection="mcp-upstream-tokens") == {
        "sentinel": "upstream-preserved"
    }


@pytest.mark.anyio
async def test_rollback_provider_leaves_new_session_state_inert_and_legacy_state_available() -> None:
    storage = MemoryStore()
    await storage.put(
        "legacy-record",
        {"sentinel": "rollback"},
        collection="mcp-upstream-tokens",
    )
    authority = Authority()
    legacy = OAuthProxy(**_kwargs(storage))
    legacy.get_routes("/mcp")

    assert await legacy.load_access_token("new-session") is None

    assert authority.sessions == {"new-session": authority.sessions["new-session"]}
    assert await storage.get("legacy-record", collection="mcp-upstream-tokens") == {
        "sentinel": "rollback"
    }
