from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.middleware import RequireAuthMiddleware
from key_value.aio.stores.memory import MemoryStore
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse

from exomem.auth_sessions import SessionAuthority, SessionIdentity, SessionStoreUnavailable
from exomem.session_oauth import ExomemSessionOAuthProxy, SessionStoreUnavailableMiddleware


class Authority:
    def __init__(self, result: Any = None) -> None:
        self.result = result

    async def validate(self, token: str) -> Any:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Verifier:
    required_scopes: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        raise AssertionError("ordinary session requests must not call GitHub")


def _proxy(authority: Authority) -> ExomemSessionOAuthProxy:
    return ExomemSessionOAuthProxy(
        session_authority=authority,
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id="github-client",
        upstream_client_secret="github-secret",
        token_verifier=Verifier(),
        base_url="https://memory.example",
        client_storage=MemoryStore(),
        jwt_signing_key="stable-signing-root",
        require_authorization_consent=False,
    )


def _protected_app(proxy: ExomemSessionOAuthProxy) -> Any:
    async def endpoint(scope: Any, receive: Any, send: Any) -> None:
        await JSONResponse({"ok": True})(scope, receive, send)

    app: Any = RequireAuthMiddleware(
        endpoint,
        required_scopes=[],
        resource_metadata_url=AnyHttpUrl(
            "https://memory.example/.well-known/oauth-protected-resource/mcp"
        ),
    )
    for middleware in reversed(proxy.get_middleware()):
        app = middleware.cls(app, *middleware.args, **middleware.kwargs)
    return app


@pytest.mark.anyio
async def test_invalid_revoked_and_prior_format_tokens_keep_normal_401_challenge(
    tmp_path: Path,
) -> None:
    current = SessionAuthority.local(
        directory=tmp_path / "current",
        signing_root="current-signing-root",
        issuer="https://memory.example",
        audience="https://memory.example/mcp",
    )
    revoked, revoked_record = await current.issue(
        client_id="codex-client",
        scopes=["exomem:read"],
        identity=SessionIdentity(github_user_id=123456, github_login="person"),
    )
    await current.tombstone(revoked_record.session_id, reason="test")

    prior = SessionAuthority.local(
        directory=tmp_path / "current",
        signing_root="prior-signing-root",
        issuer="https://memory.example",
        audience="https://memory.example/mcp",
    )
    prior_key_token, _ = await prior.issue(
        client_id="codex-client",
        scopes=["exomem:read"],
        identity=SessionIdentity(github_user_id=123456, github_login="person"),
    )
    app = _protected_app(_proxy(current))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://memory.example"
    ) as client:
        for presented in ("malformed", "legacy.jwt.token", revoked, prior_key_token):
            response = await client.get(
                "/mcp", headers={"Authorization": f"Bearer {presented}"}
            )

            assert response.status_code == 401
            assert "invalid_token" in response.headers["www-authenticate"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        SessionStoreUnavailable("coordinator unavailable"),
        SessionStoreUnavailable("session record decrypt failed"),
    ],
)
async def test_authority_outage_or_corruption_returns_503_without_oauth_challenge(
    failure: SessionStoreUnavailable,
) -> None:
    app = _protected_app(_proxy(Authority(failure)))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://memory.example"
    ) as client:
        response = await client.get(
            "/mcp", headers={"Authorization": "Bearer session-token"}
        )

    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"
    assert "retry" in response.json()["error_description"].lower()
    assert "www-authenticate" not in response.headers
    assert response.headers["retry-after"] == "5"


@pytest.mark.anyio
async def test_boundary_does_not_reclassify_unrelated_exceptions() -> None:
    app = _protected_app(_proxy(Authority(RuntimeError("programming error"))))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="https://memory.example",
    ) as client:
        with pytest.raises(RuntimeError, match="programming error"):
            await client.get("/mcp", headers={"Authorization": "Bearer session-token"})


def test_store_unavailable_middleware_is_outermost() -> None:
    middleware = _proxy(Authority(None)).get_middleware()

    assert middleware[0].cls is SessionStoreUnavailableMiddleware
