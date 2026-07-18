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

from exomem.auth_sessions import InvalidRefreshToken
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
        self.refresh_validations: list[str] = []
        self.revocations: list[str] = []
        self.sessions = {"new-session": object()}

    async def validate(self, token: str) -> Any:
        self.validations.append(token)
        return None

    async def validate_refresh(self, token: str, *, client_id: str) -> Any:
        del client_id
        self.refresh_validations.append(token)
        return None

    async def rotate_refresh(self, token: str, **kwargs: Any) -> Any:
        del kwargs
        raise InvalidRefreshToken(f"invalid Exomem refresh token: {token[:8]}")

    async def revoke_bearer(self, token: str, *, reason: str) -> bool:
        del reason
        self.revocations.append(token)
        return False


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


def _registration_payload(grant_types: list[str]) -> dict[str, Any]:
    return {
        "client_name": "Codex compatibility client",
        "redirect_uris": ["http://127.0.0.1:8765/callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": grant_types,
        "response_types": ["code"],
        "scope": "exomem:read",
    }


def _stable_registration_fields(body: dict[str, Any]) -> dict[str, Any]:
    unstable = {
        "client_id",
        "client_secret",
        "client_id_issued_at",
        "client_secret_expires_at",
    }
    return {key: value for key, value in body.items() if key not in unstable}


@pytest.mark.anyio
async def test_discovery_and_real_http_dcr_match_legacy_fastmcp() -> None:
    legacy = OAuthProxy(**_kwargs(MemoryStore()))
    durable = ExomemSessionOAuthProxy(
        session_authority=Authority(),
        **_kwargs(MemoryStore()),
    )
    legacy_app = Starlette(routes=legacy.get_routes("/mcp"))
    durable_app = Starlette(routes=durable.get_routes("/mcp"))
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=legacy_app),
            base_url="https://memory.example",
        ) as legacy_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=durable_app),
            base_url="https://memory.example",
        ) as durable_client,
    ):
        legacy_metadata = await legacy_client.get(
            "/.well-known/oauth-authorization-server"
        )
        durable_metadata = await durable_client.get(
            "/.well-known/oauth-authorization-server"
        )
        auth_code_only = _registration_payload(["authorization_code"])
        legacy_auth_code_only = await legacy_client.post(
            "/register", json=auth_code_only
        )
        durable_auth_code_only = await durable_client.post(
            "/register", json=auth_code_only
        )
        both_grants = _registration_payload(
            ["authorization_code", "refresh_token"]
        )
        legacy_both = await legacy_client.post("/register", json=both_grants)
        durable_both = await durable_client.post("/register", json=both_grants)

    assert legacy_metadata.status_code == durable_metadata.status_code == 200
    assert durable_metadata.json()["grant_types_supported"] == legacy_metadata.json()[
        "grant_types_supported"
    ]
    assert durable_metadata.json()["grant_types_supported"] == [
        "authorization_code",
        "refresh_token",
    ]
    durable_metadata_without_local_revocation = durable_metadata.json()
    durable_metadata_without_local_revocation.pop("revocation_endpoint")
    durable_metadata_without_local_revocation.pop(
        "revocation_endpoint_auth_methods_supported"
    )
    assert durable_metadata_without_local_revocation == legacy_metadata.json()

    assert durable_auth_code_only.status_code == legacy_auth_code_only.status_code
    assert durable_auth_code_only.json() == legacy_auth_code_only.json()

    assert durable_both.status_code == legacy_both.status_code == 201
    durable_registration = durable_both.json()
    legacy_registration = legacy_both.json()
    assert _stable_registration_fields(durable_registration) == (
        _stable_registration_fields(legacy_registration)
    )
    assert durable_registration["grant_types"] == legacy_registration["grant_types"] == [
        "authorization_code",
        "refresh_token",
    ]
    assert durable_registration["client_id"]
    assert legacy_registration["client_id"]


@pytest.mark.anyio
async def test_canonical_discovery_separates_offline_and_resource_scopes() -> None:
    proxy = ExomemSessionOAuthProxy(
        session_authority=Authority(),
        valid_scopes=["offline_access", "exomem:read", "exomem:write"],
        **_kwargs(MemoryStore()),
    )
    app = Starlette(routes=proxy.get_routes("/mcp"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://memory.example",
    ) as client:
        authorization = await client.get("/.well-known/oauth-authorization-server")
        resource = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert authorization.json()["scopes_supported"] == [
        "offline_access",
        "exomem:read",
        "exomem:write",
    ]
    assert resource.json()["scopes_supported"] == ["exomem:read", "exomem:write"]


@pytest.mark.anyio
async def test_authorize_preserves_downstream_state_pkce_resource_and_redirect() -> None:
    proxy = ExomemSessionOAuthProxy(
        session_authority=Authority(),
        **_kwargs(MemoryStore()),
    )
    proxy.get_routes("/mcp")
    params = AuthorizationParams(
        state="downstream-state",
        scopes=["offline_access", "exomem:read"],
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
    assert transaction.scopes == ["offline_access", "exomem:read"]
    assert transaction.resource == "https://memory.example/mcp"
    assert transaction.proxy_code_verifier
    assert upstream_query["code_challenge"][0] != "downstream-code-challenge"
    assert "scope" not in upstream_query


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
    with pytest.raises(TokenError, match="invalid Exomem refresh token"):
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
    assert authority.refresh_validations
    assert authority.revocations == []
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
