from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.models import (
    JTIMapping,
    RefreshTokenMetadata,
    UpstreamTokenSet,
    _hash_token,
)
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import (
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.server.auth.routes import RevocationHandler, TokenHandler
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request

from exomem.session_oauth import ExomemSessionOAuthProxy


class Verifier:
    required_scopes = ["exomem:read"]

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.calls.append(token)
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


def _client(client_id: str = "codex-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="client-secret",
        redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
        token_endpoint_auth_method="client_secret_post",
    )


class ClientAuthenticator:
    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        return _client()


def _form_request(path: str, values: dict[str, str]) -> Request:
    body = urlencode(values).encode()
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("memory.example", 443),
        },
        receive,
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
async def test_discovery_and_dcr_advertise_authorization_code_without_refresh() -> None:
    proxy = ExomemSessionOAuthProxy(
        session_authority=Authority(),
        **_kwargs(MemoryStore()),
    )
    app = Starlette(routes=proxy.get_routes("/mcp"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://memory.example",
    ) as client:
        metadata = await client.get("/.well-known/oauth-authorization-server")

    assert metadata.status_code == 200
    assert metadata.json()["grant_types_supported"] == ["authorization_code"]

    registered = _client("registered-client")
    await proxy.register_client(registered)
    assert registered.grant_types == ["authorization_code"]
    assert (await proxy.get_client("registered-client")).grant_types == [
        "authorization_code"
    ]

    legacy = _client("legacy-client")
    await OAuthProxy.register_client(proxy, legacy)
    assert "refresh_token" in (await proxy._client_store.get(key="legacy-client")).grant_types
    assert (await proxy.get_client("legacy-client")).grant_types == [
        "authorization_code"
    ]


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
async def test_refresh_and_revocation_paths_never_read_or_mutate_legacy_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MemoryStore()
    authority = Authority()
    proxy = ExomemSessionOAuthProxy(
        session_authority=authority,
        **_kwargs(storage),
    )
    proxy.get_routes("/mcp")
    refresh_jti = "legacy-refresh-jti"
    refresh_token = proxy.jwt_issuer.issue_refresh_token(
        client_id="codex-client",
        scopes=["exomem:read"],
        jti=refresh_jti,
        expires_in=3600,
    )
    real_refresh_key = _hash_token(refresh_token)
    await proxy._refresh_token_store.put(
        key=real_refresh_key,
        value=RefreshTokenMetadata(
            client_id="codex-client",
            scopes=["exomem:read"],
            expires_at=int(time.time() + 3600),
            created_at=time.time(),
        ),
        ttl=3600,
    )
    # Seed recognizable preserved records in every legacy collection.
    await proxy._jti_mapping_store.put(
        key=refresh_jti,
        value=JTIMapping(
            jti=refresh_jti,
            upstream_token_id="legacy-upstream",
            created_at=time.time(),
        ),
        ttl=3600,
    )
    await proxy._upstream_token_store.put(
        key="legacy-upstream",
        value=UpstreamTokenSet(
            upstream_token_id="legacy-upstream",
            access_token="legacy-access-secret",
            refresh_token="legacy-refresh-secret",
            refresh_token_expires_at=time.time() + 3600,
            expires_at=time.time() + 3600,
            token_type="Bearer",
            scope="exomem:read",
            client_id="codex-client",
            created_at=time.time(),
            raw_token_data={"access_token": "legacy-access-secret"},
        ),
        ttl=3600,
    )

    before = {
        "refresh": await storage.get(real_refresh_key, collection="mcp-refresh-tokens"),
        "jti": await storage.get(refresh_jti, collection="mcp-jti-mappings"),
        "upstream": await storage.get(
            "legacy-upstream", collection="mcp-upstream-tokens"
        ),
    }

    async def forbidden_legacy_access(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("durable provider touched a preserved legacy OAuth store")

    for adapter in (
        proxy._refresh_token_store,
        proxy._jti_mapping_store,
        proxy._upstream_token_store,
    ):
        monkeypatch.setattr(adapter, "get", forbidden_legacy_access)
        monkeypatch.setattr(adapter, "put", forbidden_legacy_access)
        monkeypatch.setattr(adapter, "delete", forbidden_legacy_access)

    assert await proxy.load_refresh_token(_client(), refresh_token) is None
    with pytest.raises(TokenError, match="Refresh tokens are not supported"):
        await proxy.exchange_refresh_token(
            _client(),
            RefreshToken(
                token=refresh_token,
                client_id="codex-client",
                scopes=["exomem:read"],
                expires_at=int(time.time() + 3600),
            ),
            ["exomem:read"],
        )

    token_response = await TokenHandler(proxy, ClientAuthenticator()).handle(
        _form_request(
            "/token",
            {
                "grant_type": "refresh_token",
                "client_id": "codex-client",
                "client_secret": "client-secret",
                "refresh_token": refresh_token,
            },
        )
    )
    assert token_response.status_code == 400
    assert json.loads(token_response.body)["error"] == "invalid_grant"

    revoke_response = await RevocationHandler(proxy, ClientAuthenticator()).handle(
        _form_request(
            "/revoke",
            {
                "token": refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": "codex-client",
                "client_secret": "client-secret",
            },
        )
    )
    assert revoke_response.status_code == 200
    assert authority.validations == [refresh_token]
    assert proxy._token_validator.calls == []

    after = {
        "refresh": await storage.get(real_refresh_key, collection="mcp-refresh-tokens"),
        "jti": await storage.get(refresh_jti, collection="mcp-jti-mappings"),
        "upstream": await storage.get(
            "legacy-upstream", collection="mcp-upstream-tokens"
        ),
    }
    assert after == before


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
