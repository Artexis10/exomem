from __future__ import annotations

import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from test_session_oauth import StubVerifier, _proxy


def request(cookie="", origin=None):
    headers = [(b"cookie", cookie.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/owner",
            "query_string": b"",
            "headers": headers,
            "server": ("memory.example", 443),
        }
    )


@pytest.fixture
async def owner(tmp_path):
    from exomem.server_auth import SingleUserGitHubVerifier

    cleanup = []
    verifier = SingleUserGitHubVerifier(allowed_login="person", allowed_user_id=123456)
    verifier._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"id": 123456, "login": "person"})
        )
    )
    proxy = _proxy(
        verifier=verifier,
        cleanup_transport=httpx.MockTransport(
            lambda req: (cleanup.append(req), httpx.Response(204))[1]
        ),
    )
    auth = proxy.enable_owner_auth(tmp_path)
    exchanges = []

    class Client:
        async def fetch_token(self, **params):
            exchanges.append(params)
            return {"access_token": "temporary-github-token", "refresh_token": "discard"}

    @asynccontextmanager
    async def factory():
        yield Client()

    proxy._upstream_oauth_client = factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Starlette(routes=proxy.get_routes())),
        base_url="https://memory.example",
    ) as client:
        yield proxy, auth, client, exchanges, cleanup
    await verifier._http_client.aclose()


async def login(client):
    response = await client.get("/owner/login?redirect_uri=https://evil.example&scope=owner")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://memory.example/auth/callback"]
    assert "scope" not in query
    return query["state"][0]


@pytest.mark.anyio
async def test_owner_identity_is_separate_and_submission_protected(owner):
    proxy, auth, client, exchanges, cleanup = owner
    state = await login(client)
    response = await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    assert response.status_code == 302
    assert response.headers["location"] == "/owner"
    assert len(exchanges) == len(cleanup) == 1
    assert exchanges[0]["code_verifier"]
    assert await proxy._client_storage.keys(collection="mcp-authorization-codes") == []
    assert not proxy._session_authority.issue_calls
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    session = await auth.require_session(request(cookie))
    assert session.owner_id == "github:123456"
    assert session.expires_at <= time.time() + 600
    assert session.vault_root == str(auth.vault_root)
    assert (
        await auth.require_submission(request(cookie, auth.origin), session.csrf_token) == session
    )
    for origin, csrf in [
        (None, session.csrf_token),
        ("https://evil.example", session.csrf_token),
        (auth.origin, ""),
        (auth.origin, "wrong"),
    ]:
        with pytest.raises(HTTPException):
            await auth.require_submission(request(cookie, origin), csrf)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    replay = await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    assert replay.status_code == 400
    assert len(exchanges) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["absent", "mismatch", "expired", "error", "identity"])
async def test_owner_callback_refuses_and_consumes(owner, failure, monkeypatch):
    proxy, auth, client, exchanges, cleanup = owner
    state = await login(client)
    if failure == "absent":
        client.cookies.clear()
    if failure == "mismatch":
        client.cookies.clear()
        client.cookies.set(auth.binding_cookie_name, "wrong")
    if failure == "expired":
        monkeypatch.setattr("exomem.native_owner_auth.time.time", lambda: 10**12)
    if failure == "identity":
        proxy._token_validator = StubVerifier(None)
    params = {"state": state, "code": "github-code"}
    if failure == "error":
        params = {"state": state, "error": "access_denied"}
    response = await client.get("/auth/callback", params=params)
    assert response.status_code in {400, 403}
    assert await auth._login_store.get(key=state) is None
    assert await proxy._client_storage.keys(collection="exomem-owner-sessions") == []
    assert len(exchanges) == len(cleanup) == (1 if failure == "identity" else 0)


@pytest.mark.anyio
async def test_bearer_expired_session_and_changed_root_are_refused(owner, monkeypatch):
    _, auth, client, _, _ = owner
    with pytest.raises(HTTPException):
        await auth.require_session(request("Authorization=Bearer ordinary-token"))
    state = await login(client)
    await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    session = await auth.require_session(request(cookie))
    with monkeypatch.context() as patch:
        patch.setattr("exomem.native_owner_auth.time.time", lambda: session.expires_at + 1)
        with pytest.raises(HTTPException):
            await auth.require_session(request(cookie))
    auth.vault_root.rename(auth.vault_root.with_name(auth.vault_root.name + "-old"))
    auth.vault_root.mkdir()
    with pytest.raises(HTTPException):
        await auth.require_session(request(cookie))


@pytest.mark.anyio
async def test_callbacks_use_server_namespace_even_with_normal_transaction_collision(owner):
    from test_session_oauth import _seed_transaction

    proxy, auth, client, exchanges, _ = owner
    state = await login(client)
    ordinary = (await _seed_transaction(proxy)).model_copy(update={"txn_id": state})
    await proxy._transaction_store.put(key=state, value=ordinary, ttl=600)
    response = await client.get("/auth/callback", params={"state": state, "error": "access_denied"})
    assert response.status_code == 400
    assert "location" not in response.headers
    assert not exchanges
    assert await proxy._transaction_store.get(key=state) == ordinary
    assert await auth._login_store.get(key=state) is None


@pytest.mark.anyio
async def test_expiry_is_rechecked_after_exchange_and_not_delegated_to_storage(owner, monkeypatch):
    proxy, auth, client, _, _ = owner
    state = await login(client)
    transaction = await auth._login_store.get(key=state)
    exchange = proxy._exchange_identity

    async def delayed_exchange(*args):
        result = await exchange(*args)
        monkeypatch.setattr(
            "exomem.native_owner_auth.time.time", lambda: transaction.expires_at + 1
        )
        return result

    monkeypatch.setattr(proxy, "_exchange_identity", delayed_exchange)
    response = await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    assert response.status_code == 403
    assert await proxy._client_storage.keys(collection="exomem-owner-sessions") == []


@pytest.mark.anyio
async def test_get_and_duplicate_origin_are_not_submissions(owner):
    _, auth, client, _, _ = owner
    state = await login(client)
    await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    session = await auth.require_session(request(cookie))
    get = request(cookie, auth.origin)
    get.scope["method"] = "GET"
    with pytest.raises(HTTPException):
        await auth.require_submission(get, session.csrf_token)
    duplicate = request(cookie, auth.origin)
    duplicate.scope["headers"].append((b"origin", b"https://evil.example"))
    with pytest.raises(HTTPException):
        await auth.require_submission(duplicate, session.csrf_token)


@pytest.mark.parametrize(
    "origin,allowed",
    [
        ("http://memory.example", False),
        ("http://127.0.0.1:8123", False),
        ("http://127.0.0.1:8123", True),
        ("https://memory.example/path", False),
    ],
)
def test_origin_is_fixed_and_http_requires_explicit_loopback(tmp_path, origin, allowed):
    from pydantic import AnyHttpUrl

    proxy = _proxy()
    proxy.base_url = AnyHttpUrl(origin)
    if allowed:
        auth = proxy.enable_owner_auth(tmp_path, allow_insecure_loopback=True)
        assert not auth.session_cookie_name.startswith("__Host-")
    else:
        with pytest.raises(ValueError):
            proxy.enable_owner_auth(tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["../../other", "x" * 10000, "a"], ids=["path", "long", "short"])
async def test_untrusted_handles_are_rejected_before_storage(owner, suffix, monkeypatch):
    _, auth, client, _, _ = owner

    async def forbidden_get(**kwargs):
        raise AssertionError("Malformed handle reached storage")

    monkeypatch.setattr(auth._login_store, "get", forbidden_get)
    monkeypatch.setattr(auth._session_store, "get", forbidden_get)
    response = await client.get(
        "/auth/callback", params={"state": "owner-login-" + suffix, "code": "github-code"}
    )
    assert response.status_code == 400
    with pytest.raises(HTTPException) as error:
        await auth.require_session(request(auth.session_cookie_name + "=owner-session-" + suffix))
    assert error.value.status_code == 401


@pytest.mark.anyio
async def test_ordinary_oauth_remains_ordinary_when_owner_login_enabled(owner):
    from test_session_oauth import _install_token_exchange, _seed_transaction

    proxy, auth, client, _, _ = owner
    await _seed_transaction(proxy)
    _install_token_exchange(proxy, {"access_token": "temporary-github-token"})
    response = await client.get(
        "/auth/callback",
        params={"state": "transaction-id", "code": "github-code", "purpose": "owner"},
    )
    assert response.status_code == 302
    assert response.headers["location"] != "/owner"
    assert len(await proxy._client_storage.keys(collection="mcp-authorization-codes")) == 1
    assert await proxy._client_storage.keys(collection="exomem-owner-sessions") == []
    req = request()
    req.scope["headers"].append((b"authorization", b"Bearer ordinary-token"))
    with pytest.raises(HTTPException):
        await auth.require_session(req)


@pytest.mark.anyio
async def test_owner_expiry_is_enforced_when_storage_ignores_ttl(owner, monkeypatch):
    _, auth, client, _, _ = owner
    state = await login(client)
    transaction = await auth._login_store.get(key=state)

    async def expired_login(**kwargs):
        return transaction.model_copy(update={"expires_at": time.time() - 1})

    with monkeypatch.context() as patch:
        patch.setattr(auth._login_store, "get", expired_login)
        response = await client.get(
            "/auth/callback", params={"state": state, "code": "github-code"}
        )
        assert response.status_code == 403
    state = await login(client)
    await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    session = await auth.require_session(request(cookie))

    async def expired_session(**kwargs):
        return session.model_copy(update={"expires_at": time.time() - 1})

    monkeypatch.setattr(auth._session_store, "get", expired_session)
    with pytest.raises(HTTPException):
        await auth.require_session(request(cookie))


@pytest.mark.anyio
@pytest.mark.parametrize("configured_owner", [123456, 987654])
async def test_persisted_owner_session_rechecks_restarted_proxy_owner(owner, configured_owner):
    from exomem.server_auth import SingleUserGitHubVerifier

    proxy, auth, client, _, _ = owner
    state = await login(client)
    await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    original = await auth.require_session(request(cookie))
    restarted = _proxy(
        verifier=SingleUserGitHubVerifier(allowed_login="person", allowed_user_id=configured_owner)
    )
    restarted._client_storage = proxy._client_storage
    restarted_auth = restarted.enable_owner_auth(auth.vault_root)
    if configured_owner == 123456:
        assert (
            await restarted_auth.require_submission(
                request(cookie, restarted_auth.origin), original.csrf_token
            )
            == original
        )
    else:
        with pytest.raises(HTTPException) as error:
            await restarted_auth.require_session(request(cookie))
        assert error.value.status_code == 401
        with pytest.raises(HTTPException):
            await restarted_auth.require_submission(
                request(cookie, restarted_auth.origin), original.csrf_token
            )


@pytest.mark.anyio
async def test_callback_rechecks_current_pinned_owner_after_exchange(owner, monkeypatch):
    from exomem.server_auth import SingleUserGitHubVerifier

    proxy, _, client, _, _ = owner
    state = await login(client)
    exchange = proxy._exchange_identity

    async def changed_owner(*args):
        proof = await exchange(*args)
        proxy._token_validator = SingleUserGitHubVerifier(
            allowed_login="person", allowed_user_id=987654
        )
        return proof

    monkeypatch.setattr(proxy, "_exchange_identity", changed_owner)
    response = await client.get("/auth/callback", params={"state": state, "code": "github-code"})
    assert response.status_code == 403
    assert await proxy._client_storage.keys(collection="exomem-owner-sessions") == []
