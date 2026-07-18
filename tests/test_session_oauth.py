from __future__ import annotations

import base64
import hashlib
import inspect
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.models import (
    ClientCode,
    OAuthTransaction,
)
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.server.auth.routes import TokenHandler
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request

from exomem.auth_sessions import (
    ACCESS_TOKEN_TTL_SECONDS,
    RefreshGrant,
    SessionIdentity,
    SessionStoreUnavailable,
)
from exomem.session_oauth import ExomemSessionOAuthProxy


def test_fastmcp_private_adapter_contract_is_pinned() -> None:
    assert version("fastmcp") == "3.4.4"
    assert list(inspect.signature(OAuthProxy._handle_idp_callback).parameters) == [
        "self",
        "request",
    ]
    assert list(inspect.signature(OAuthProxy.exchange_authorization_code).parameters) == [
        "self",
        "client",
        "authorization_code",
    ]
    assert list(inspect.signature(OAuthProxy.load_access_token).parameters) == ["self", "token"]
    assert list(inspect.signature(OAuthProxy.revoke_token).parameters) == ["self", "token"]
    assert list(inspect.signature(OAuthProxy.get_middleware).parameters) == ["self"]
    assert list(inspect.signature(OAuthProxy.load_refresh_token).parameters) == [
        "self",
        "client",
        "refresh_token",
    ]
    assert list(inspect.signature(OAuthProxy.exchange_refresh_token).parameters) == [
        "self",
        "client",
        "refresh_token",
        "scopes",
    ]
    for seam in (
        "load_refresh_token",
        "exchange_refresh_token",
    ):
        assert seam in ExomemSessionOAuthProxy.__dict__
        assert list(
            inspect.signature(getattr(ExomemSessionOAuthProxy, seam)).parameters
        ) == list(inspect.signature(getattr(OAuthProxy, seam)).parameters)
    for inherited_compatibility_seam in ("register_client", "get_client"):
        assert inherited_compatibility_seam not in ExomemSessionOAuthProxy.__dict__
    assert list(inspect.signature(ExomemSessionOAuthProxy.get_routes).parameters) == [
        "self",
        "mcp_path",
    ]
    assert list(
        inspect.signature(OAuthProxy._validate_client_redirect_uri).parameters
    ) == ["self", "redirect_uri"]
    assert list(
        inspect.signature(OAuthProxy._verify_consent_binding_cookie).parameters
    ) == ["self", "request", "txn_id", "expected_token"]
    assert list(
        inspect.signature(OAuthProxy._clear_consent_binding_cookie).parameters
    ) == ["self", "request", "response", "txn_id"]
    assert list(
        inspect.signature(OAuthProxy._prepare_scopes_for_token_exchange).parameters
    ) == ["self", "scopes"]
    assert list(inspect.signature(OAuthProxy._upstream_oauth_client).parameters) == [
        "self"
    ]
    assert set(AccessToken.model_fields) == {
        "token",
        "client_id",
        "scopes",
        "expires_at",
        "resource",
        "claims",
    }
    assert set(OAuthToken.model_fields) == {
        "access_token",
        "token_type",
        "expires_in",
        "scope",
        "refresh_token",
    }
    assert {
        "code",
        "client_id",
        "redirect_uri",
        "code_challenge",
        "code_challenge_method",
        "scopes",
        "idp_tokens",
        "expires_at",
        "created_at",
    } == set(ClientCode.model_fields)
    assert {
        "txn_id",
        "client_id",
        "client_redirect_uri",
        "client_state",
        "code_challenge",
        "code_challenge_method",
        "scopes",
        "created_at",
        "resource",
        "proxy_code_verifier",
        "csrf_token",
        "csrf_expires_at",
        "consent_token",
    } == set(OAuthTransaction.model_fields)


@dataclass
class FakeRecord:
    session_id: str
    client_id: str
    scopes: tuple[str, ...]
    issuer: str = "https://memory.example"
    audience: str = "https://memory.example/mcp"
    github_user_id: int = 123456
    github_login: str = "person"
    expires_at: float | None = None


class FakeAuthority:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeRecord] = {}
        self.issue_calls: list[dict[str, Any]] = []
        self.offline_issue_calls: list[dict[str, Any]] = []
        self.rotate_calls: list[dict[str, Any]] = []
        self.tombstones: list[tuple[str, str]] = []
        self.revoked_bearers: list[tuple[str, str]] = []
        self.refreshes: dict[str, RefreshGrant] = {}
        self.validation_error: Exception | None = None

    async def issue(
        self,
        *,
        client_id: str,
        scopes: list[str],
        identity: SessionIdentity,
    ) -> tuple[str, FakeRecord]:
        token = f"local-session-{len(self.issue_calls)}"
        record = FakeRecord(
            session_id=f"session-{len(self.issue_calls)}",
            client_id=client_id,
            scopes=tuple(scopes),
            github_user_id=identity.github_user_id,
            github_login=identity.github_login,
        )
        self.issue_calls.append(
            {"client_id": client_id, "scopes": list(scopes), "identity": identity}
        )
        self.sessions[token] = record
        return token, record

    async def issue_offline(
        self,
        *,
        client_id: str,
        scopes: list[str],
        identity: SessionIdentity,
    ) -> tuple[str, FakeRecord, str]:
        index = len(self.offline_issue_calls)
        access = f"local-access-{index}"
        refresh = f"local-refresh-{index}"
        record = FakeRecord(
            session_id=f"access-session-{index}",
            client_id=client_id,
            scopes=tuple(scopes),
            github_user_id=identity.github_user_id,
            github_login=identity.github_login,
            expires_at=time.time() + ACCESS_TOKEN_TTL_SECONDS,
        )
        call = {"client_id": client_id, "scopes": list(scopes), "identity": identity}
        self.offline_issue_calls.append(call)
        self.sessions[access] = record
        self.refreshes[refresh] = RefreshGrant(
            family_id=f"family-{index:016d}",
            sequence=0,
            client_id=client_id,
            scopes=tuple(scopes),
        )
        return access, record, refresh

    async def validate_refresh(
        self, token: str, *, client_id: str
    ) -> RefreshGrant | None:
        grant = self.refreshes.get(token)
        return grant if grant is not None and grant.client_id == client_id else None

    async def rotate_refresh(
        self,
        token: str,
        *,
        client_id: str,
        scopes: list[str],
    ) -> tuple[str, FakeRecord, str]:
        grant = await self.validate_refresh(token, client_id=client_id)
        if grant is None:
            raise AssertionError("proxy attempted to rotate an invalid fake refresh")
        index = len(self.rotate_calls)
        access = f"rotated-access-{index}"
        refresh = f"rotated-refresh-{index}"
        record = FakeRecord(
            session_id=f"rotated-session-{index}",
            client_id=client_id,
            scopes=tuple(scopes),
            expires_at=time.time() + ACCESS_TOKEN_TTL_SECONDS,
        )
        self.rotate_calls.append(
            {"token": token, "client_id": client_id, "scopes": list(scopes)}
        )
        self.sessions[access] = record
        self.refreshes[refresh] = RefreshGrant(
            family_id=grant.family_id,
            sequence=grant.sequence + 1,
            client_id=client_id,
            scopes=grant.scopes,
        )
        return access, record, refresh

    async def validate(self, token: str) -> FakeRecord | None:
        if self.validation_error is not None:
            raise self.validation_error
        return self.sessions.get(token)

    async def tombstone(self, session_id: str, *, reason: str) -> bool:
        self.tombstones.append((session_id, reason))
        for token, record in list(self.sessions.items()):
            if record.session_id == session_id:
                del self.sessions[token]
                return True
        return False

    async def revoke_bearer(self, token: str, *, reason: str) -> bool:
        self.revoked_bearers.append((token, reason))
        self.sessions.pop(token, None)
        self.refreshes.pop(token, None)
        return True


class StubVerifier:
    required_scopes: list[str] = []

    def __init__(self, result: AccessToken | None) -> None:
        self.result = result
        self.calls: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.calls.append(token)
        return self.result


def _verified_identity() -> AccessToken:
    return AccessToken(
        token="temporary-github-token",
        client_id="123456",
        scopes=["user"],
        expires_at=None,
        claims={"sub": "123456", "login": "person"},
    )


def _proxy(
    *,
    authority: FakeAuthority | None = None,
    verifier: StubVerifier | None = None,
    cleanup_transport: httpx.AsyncBaseTransport | None = None,
    require_consent: bool = False,
) -> ExomemSessionOAuthProxy:
    return ExomemSessionOAuthProxy(
        session_authority=authority or FakeAuthority(),
        github_cleanup_transport=cleanup_transport,
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id="github-client",
        upstream_client_secret="github-secret",
        upstream_revocation_endpoint=None,
        token_verifier=verifier or StubVerifier(_verified_identity()),
        base_url="https://memory.example",
        allowed_client_redirect_uris=["http://127.0.0.1:*"],
        client_storage=MemoryStore(),
        jwt_signing_key="stable-signing-root",
        require_authorization_consent=require_consent,
    )


async def _seed_transaction(
    proxy: ExomemSessionOAuthProxy,
    *,
    consent_token: str | None = None,
) -> OAuthTransaction:
    transaction = OAuthTransaction(
        txn_id="transaction-id",
        client_id="codex-client",
        client_redirect_uri="http://127.0.0.1:8765/callback",
        client_state="client-state",
        code_challenge="downstream-pkce-challenge",
        code_challenge_method="S256",
        scopes=["exomem:read", "exomem:write"],
        created_at=time.time(),
        resource="https://memory.example/mcp",
        proxy_code_verifier="forwarded-upstream-verifier",
        consent_token=consent_token,
    )
    await proxy._transaction_store.put(key=transaction.txn_id, value=transaction, ttl=900)
    return transaction


def _callback_request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/auth/callback",
            "raw_path": b"/auth/callback",
            "query_string": b"code=github-code&state=transaction-id",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("memory.example", 443),
        }
    )


def _install_token_exchange(
    proxy: ExomemSessionOAuthProxy,
    tokens: dict[str, Any],
) -> None:
    class Client:
        async def fetch_token(self, **params: Any) -> dict[str, Any]:
            assert params["code"] == "github-code"
            assert params["redirect_uri"] == "https://memory.example/auth/callback"
            assert params["code_verifier"] == "forwarded-upstream-verifier"
            return dict(tokens)

    @asynccontextmanager
    async def client_factory():
        yield Client()

    proxy._upstream_oauth_client = client_factory


def _client(client_id: str = "codex-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="client-secret",
        redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
        token_endpoint_auth_method="client_secret_post",
    )


def _authorization_code(code: str) -> AuthorizationCode:
    return AuthorizationCode(
        code=code,
        scopes=["exomem:read", "exomem:write"],
        expires_at=time.time() + 300,
        client_id="codex-client",
        code_challenge="downstream-pkce-challenge",
        redirect_uri=AnyUrl("http://127.0.0.1:8765/callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://memory.example/mcp",
    )


class _ClientAuthenticator:
    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        return _client()


def _token_request(*, code: str, code_verifier: str) -> Request:
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": "codex-client",
            "client_secret": "client-secret",
            "code": code,
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "code_verifier": code_verifier,
        }
    ).encode()
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
            "path": "/token",
            "raw_path": b"/token",
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


def _refresh_request(refresh_token: str, *, scope: str | None = None) -> Request:
    values = {
            "grant_type": "refresh_token",
            "client_id": "codex-client",
            "client_secret": "client-secret",
            "refresh_token": refresh_token,
    }
    if scope is not None:
        values["scope"] = scope
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
            "path": "/token",
            "raw_path": b"/token",
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


def _contains_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"access_token", "refresh_token"} or _contains_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_credentials(item) for item in value)
    return False


@pytest.mark.anyio
async def test_callback_stores_token_free_proof_then_exchange_issues_local_session() -> None:
    cleanup_requests: list[httpx.Request] = []

    def cleanup(request: httpx.Request) -> httpx.Response:
        cleanup_requests.append(request)
        return httpx.Response(204)

    authority = FakeAuthority()
    verifier = StubVerifier(_verified_identity())
    proxy = _proxy(
        authority=authority,
        verifier=verifier,
        cleanup_transport=httpx.MockTransport(cleanup),
    )
    await _seed_transaction(proxy)
    _install_token_exchange(
        proxy,
        {
            "access_token": "temporary-github-token",
            "refresh_token": "must-not-survive",
            "scope": "repo user",
            "token_type": "bearer",
        },
    )

    response = await proxy._handle_idp_callback(_callback_request())

    assert response.status_code == 302
    location = response.headers["location"]
    params = parse_qs(urlparse(location).query)
    code = params["code"][0]
    assert params["state"] == ["client-state"]
    stored_code = await proxy._code_store.get(key=code)
    assert stored_code is not None
    assert not _contains_credentials(stored_code.idp_tokens)
    assert stored_code.idp_tokens == {
        "exomem_identity": {"github_user_id": 123456, "github_login": "person"}
    }
    assert await proxy._transaction_store.get(key="transaction-id") is None
    assert verifier.calls == ["temporary-github-token"]
    [request] = cleanup_requests
    assert request.method == "DELETE"
    assert request.url == "https://api.github.com/applications/github-client/token"
    assert request.headers["authorization"].startswith("Basic ")
    assert json.loads(request.content) == {"access_token": "temporary-github-token"}
    assert authority.issue_calls == []  # an abandoned downstream code issues no session

    loaded = await proxy.load_authorization_code(_client(), code)
    assert loaded is not None
    assert loaded.code_challenge == "downstream-pkce-challenge"
    token = await proxy.exchange_authorization_code(_client(), _authorization_code(code))

    assert token.access_token.startswith("local-session-")
    assert token.expires_in is None
    assert token.refresh_token is None
    assert token.scope == "exomem:read exomem:write"
    assert authority.issue_calls == [
        {
            "client_id": "codex-client",
            "scopes": ["exomem:read", "exomem:write"],
            "identity": SessionIdentity(github_user_id=123456, github_login="person"),
        }
    ]
    assert await proxy._client_storage.keys(collection="mcp-upstream-tokens") == []
    assert await proxy._client_storage.keys(collection="mcp-jti-mappings") == []
    with pytest.raises(TokenError, match="Authorization code not found"):
        await proxy.exchange_authorization_code(_client(), _authorization_code(code))


@pytest.mark.anyio
async def test_callback_preserves_consent_binding_verification_and_cookie_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _proxy(
        cleanup_transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        require_consent=True,
    )
    await _seed_transaction(proxy, consent_token="consent-token")
    _install_token_exchange(proxy, {"access_token": "temporary-github-token"})
    calls: list[tuple[str, str]] = []

    def verify(request: Request, txn_id: str, expected_token: str) -> bool:
        calls.append((txn_id, expected_token))
        return True

    def clear(request: Request, response: Any, txn_id: str) -> None:
        calls.append((txn_id, "cleared"))

    monkeypatch.setattr(proxy, "_verify_consent_binding_cookie", verify)
    monkeypatch.setattr(proxy, "_clear_consent_binding_cookie", clear)

    response = await proxy._handle_idp_callback(_callback_request())

    assert response.status_code == 302
    assert calls == [
        ("transaction-id", "consent-token"),
        ("transaction-id", "cleared"),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "verified",
    [
        None,
        AccessToken(token="t", client_id="x", scopes=[], claims={"login": "person"}),
        AccessToken(token="t", client_id="x", scopes=[], claims={"sub": "123456"}),
        AccessToken(
            token="t",
            client_id="1",
            scopes=[],
            claims={"login": "person", "sub": True},
        ),
    ],
)
async def test_callback_rejects_unverifiable_or_incomplete_identity_and_cleans_token(
    verified: AccessToken | None,
) -> None:
    cleanup_calls = 0

    def cleanup(request: httpx.Request) -> httpx.Response:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return httpx.Response(204)

    proxy = _proxy(
        verifier=StubVerifier(verified),
        cleanup_transport=httpx.MockTransport(cleanup),
    )
    await _seed_transaction(proxy)
    _install_token_exchange(proxy, {"access_token": "temporary-github-token"})

    response = await proxy._handle_idp_callback(_callback_request())

    assert response.status_code == 403
    assert cleanup_calls == 1
    assert await proxy._client_storage.keys(collection="mcp-authorization-codes") == []


@pytest.mark.anyio
@pytest.mark.parametrize("cleanup_status", [404, 422, 503])
async def test_cleanup_already_gone_or_failed_never_retains_token_or_blocks_code(
    cleanup_status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def cleanup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(cleanup_status)

    proxy = _proxy(cleanup_transport=httpx.MockTransport(cleanup))
    await _seed_transaction(proxy)
    _install_token_exchange(proxy, {"access_token": "temporary-github-token"})

    response = await proxy._handle_idp_callback(_callback_request())

    assert response.status_code == 302
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    stored = await proxy._code_store.get(key=code)
    assert stored is not None and not _contains_credentials(stored.idp_tokens)
    assert "temporary-github-token" not in caplog.text
    if cleanup_status in {422, 503}:
        assert "cleanup" in caplog.text.lower()


@pytest.mark.anyio
async def test_cleanup_occurs_before_proof_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls = 0

    def cleanup(request: httpx.Request) -> httpx.Response:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return httpx.Response(204)

    proxy = _proxy(cleanup_transport=httpx.MockTransport(cleanup))
    await _seed_transaction(proxy)
    _install_token_exchange(proxy, {"access_token": "temporary-github-token"})

    async def fail_put(*args: Any, **kwargs: Any) -> None:
        raise OSError("code store unavailable")

    monkeypatch.setattr(proxy._code_store, "put", fail_put)

    response = await proxy._handle_idp_callback(_callback_request())

    assert response.status_code == 500
    assert cleanup_calls == 1


@pytest.mark.anyio
async def test_cleanup_already_happened_when_redirect_handling_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls = 0

    def cleanup(request: httpx.Request) -> httpx.Response:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return httpx.Response(204)

    proxy = _proxy(cleanup_transport=httpx.MockTransport(cleanup))
    await _seed_transaction(proxy)
    _install_token_exchange(proxy, {"access_token": "temporary-github-token"})

    def fail_redirect(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("redirect construction failed")

    monkeypatch.setattr("exomem.session_oauth.RedirectResponse", fail_redirect)

    response = await proxy._handle_idp_callback(_callback_request())

    assert response.status_code == 500
    assert cleanup_calls == 1
    keys = await proxy._client_storage.keys(collection="mcp-authorization-codes")
    assert len(keys) == 1
    stored = await proxy._code_store.get(key=keys[0])
    assert stored is not None and not _contains_credentials(stored.idp_tokens)


@pytest.mark.anyio
async def test_invalid_pkce_issues_no_session_and_retains_no_github_credential() -> None:
    authority = FakeAuthority()
    proxy = _proxy(authority=authority)
    code = "downstream-code"
    valid_verifier = "correct-verifier"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(valid_verifier.encode()).digest()
    ).decode().rstrip("=")
    await proxy._code_store.put(
        key=code,
        value=ClientCode(
            code=code,
            client_id="codex-client",
            redirect_uri="http://127.0.0.1:8765/callback",
            code_challenge=challenge,
            code_challenge_method="S256",
            scopes=["exomem:read"],
            idp_tokens={
                "exomem_identity": {
                    "github_user_id": 123456,
                    "github_login": "person",
                }
            },
            expires_at=int(time.time() + 300),
            created_at=time.time(),
        ),
        ttl=300,
    )

    response = await TokenHandler(proxy, _ClientAuthenticator()).handle(
        _token_request(code=code, code_verifier="incorrect-verifier")
    )

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_grant"
    assert authority.issue_calls == []
    stored = await proxy._code_store.get(key=code)
    assert stored is not None and not _contains_credentials(stored.idp_tokens)


@pytest.mark.anyio
async def test_token_endpoint_omits_expiry_and_refresh_fields() -> None:
    authority = FakeAuthority()
    proxy = _proxy(authority=authority)
    code = "successful-downstream-code"
    verifier = "correct-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    await proxy._code_store.put(
        key=code,
        value=ClientCode(
            code=code,
            client_id="codex-client",
            redirect_uri="http://127.0.0.1:8765/callback",
            code_challenge=challenge,
            code_challenge_method="S256",
            scopes=["exomem:read"],
            idp_tokens={
                "exomem_identity": {
                    "github_user_id": 123456,
                    "github_login": "person",
                }
            },
            expires_at=int(time.time() + 300),
            created_at=time.time(),
        ),
        ttl=300,
    )

    response = await TokenHandler(proxy, _ClientAuthenticator()).handle(
        _token_request(code=code, code_verifier=verifier)
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload == {
        "access_token": "local-session-0",
        "token_type": "Bearer",
        "scope": "exomem:read",
    }
    assert len(authority.issue_calls) == 1


@pytest.mark.anyio
async def test_offline_code_exchange_issues_expiring_access_and_refresh_token() -> None:
    authority = FakeAuthority()
    proxy = _proxy(authority=authority)
    code = "offline-downstream-code"
    await proxy._code_store.put(
        key=code,
        value=ClientCode(
            code=code,
            client_id="codex-client",
            redirect_uri="http://127.0.0.1:8765/callback",
            code_challenge="downstream-pkce-challenge",
            code_challenge_method="S256",
            scopes=["offline_access", "exomem:read"],
            idp_tokens={
                "exomem_identity": {
                    "github_user_id": 123456,
                    "github_login": "person",
                }
            },
            expires_at=int(time.time() + 300),
            created_at=time.time(),
        ),
        ttl=300,
    )

    token = await proxy.exchange_authorization_code(
        _client(), _authorization_code(code)
    )

    assert token.access_token == "local-access-0"
    assert token.refresh_token == "local-refresh-0"
    assert token.expires_in == ACCESS_TOKEN_TTL_SECONDS
    assert token.scope == "offline_access exomem:read"
    assert authority.issue_calls == []
    assert len(authority.offline_issue_calls) == 1


@pytest.mark.anyio
async def test_proxy_loads_rotates_and_revokes_exomem_refresh_without_legacy_store() -> None:
    authority = FakeAuthority()
    proxy = _proxy(authority=authority)
    _, _, refresh = await authority.issue_offline(
        client_id="codex-client",
        scopes=["offline_access", "exomem:read"],
        identity=SessionIdentity(github_user_id=123456, github_login="person"),
    )

    loaded = await proxy.load_refresh_token(_client(), refresh)
    assert loaded is not None
    assert loaded.client_id == "codex-client"
    assert loaded.scopes == ["offline_access", "exomem:read"]
    assert loaded.expires_at is None
    assert await proxy.load_refresh_token(_client("other-client"), refresh) is None

    expanded = await TokenHandler(proxy, _ClientAuthenticator()).handle(
        _refresh_request(refresh, scope="offline_access exomem:admin")
    )
    assert expanded.status_code == 400
    assert json.loads(expanded.body)["error"] == "invalid_scope"
    assert authority.rotate_calls == []

    response = await TokenHandler(proxy, _ClientAuthenticator()).handle(
        _refresh_request(
            refresh,
            scope="offline_access exomem:read exomem:read",
        )
    )
    assert response.status_code == 200
    rotated = json.loads(response.body)
    assert rotated["access_token"] == "rotated-access-0"
    assert rotated["refresh_token"] == "rotated-refresh-0"
    assert rotated["expires_in"] == ACCESS_TOKEN_TTL_SECONDS
    assert rotated["scope"] == "offline_access exomem:read"
    assert authority.rotate_calls == [
        {
            "token": refresh,
            "client_id": "codex-client",
            "scopes": ["offline_access", "exomem:read"],
        }
    ]

    await proxy.revoke_token(loaded)
    assert authority.revoked_bearers == [
        (refresh, "oauth-client-revocation")
    ]


@pytest.mark.anyio
async def test_exchange_rejects_corrupt_boolean_identity_proof() -> None:
    authority = FakeAuthority()
    proxy = _proxy(authority=authority)
    code = "corrupt-proof-code"
    await proxy._code_store.put(
        key=code,
        value=ClientCode(
            code=code,
            client_id="codex-client",
            redirect_uri="http://127.0.0.1:8765/callback",
            code_challenge="downstream-pkce-challenge",
            code_challenge_method="S256",
            scopes=["exomem:read"],
            idp_tokens={
                "exomem_identity": {
                    "github_user_id": True,
                    "github_login": "person",
                }
            },
            expires_at=int(time.time() + 300),
            created_at=time.time(),
        ),
        ttl=300,
    )

    with pytest.raises(TokenError, match="identity proof is invalid"):
        await proxy.exchange_authorization_code(_client(), _authorization_code(code))

    assert authority.issue_calls == []


@pytest.mark.anyio
async def test_load_access_token_uses_only_session_authority_for_repeated_requests() -> None:
    authority = FakeAuthority()
    verifier = StubVerifier(None)  # simulates GitHub revocation after issuance
    proxy = _proxy(authority=authority, verifier=verifier)
    for index in range(12):
        code = f"authorization-code-{index}"
        await proxy._code_store.put(
            key=code,
            value=ClientCode(
                code=code,
                client_id="codex-client",
                redirect_uri="http://127.0.0.1:8765/callback",
                code_challenge="downstream-pkce-challenge",
                code_challenge_method="S256",
                scopes=["exomem:read"],
                idp_tokens={
                    "exomem_identity": {
                        "github_user_id": 123456,
                        "github_login": "person",
                    }
                },
                expires_at=int(time.time() + 300),
                created_at=time.time(),
            ),
            ttl=300,
        )
        issued = await proxy.exchange_authorization_code(
            _client(), _authorization_code(code)
        )
        assert issued.access_token == f"local-session-{index}"

    for index in range(12):
        first = await proxy.load_access_token(f"local-session-{index}")
        second = await proxy.load_access_token(f"local-session-{index}")
        assert first is not None and second is not None
        assert first.client_id == "codex-client"
        assert first.scopes == ["exomem:read"]
        assert first.expires_at is None
        assert first.resource == "https://memory.example/mcp"
        assert first.claims["github_user_id"] == 123456

    assert verifier.calls == []
    assert await proxy.load_access_token("malformed-or-legacy-token") is None


@pytest.mark.anyio
async def test_load_access_token_propagates_authority_failure() -> None:
    authority = FakeAuthority()
    authority.validation_error = SessionStoreUnavailable("coordinator down")
    proxy = _proxy(authority=authority)

    with pytest.raises(SessionStoreUnavailable, match="coordinator down"):
        await proxy.load_access_token("session-token")


@pytest.mark.anyio
async def test_rfc7009_revocation_tombstones_shared_session_and_unknown_is_success() -> None:
    authority = FakeAuthority()
    authority.sessions["session-token"] = FakeRecord(
        session_id="session-id",
        client_id="codex-client",
        scopes=("exomem:read",),
    )
    proxy = _proxy(authority=authority)

    assert proxy.revocation_options is not None and proxy.revocation_options.enabled
    await proxy.revoke_token(
        AccessToken(token="session-token", client_id="codex-client", scopes=["exomem:read"])
    )

    assert authority.revoked_bearers == [
        ("session-token", "oauth-client-revocation")
    ]
    assert await proxy.load_access_token("session-token") is None
    await proxy.revoke_token(
        AccessToken(token="unknown", client_id="codex-client", scopes=["exomem:read"])
    )
    assert authority.revoked_bearers == [
        ("session-token", "oauth-client-revocation"),
        ("unknown", "oauth-client-revocation"),
    ]
