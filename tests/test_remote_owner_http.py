"""The remote owner binding, proven through real HTTP requests.

The unit tests in `test_remote_owner_binding.py` pin the rule; these prove
the installed FastMCP carries the session proxy's own token type through to
the tool leaf, so the provenance check sees what production sees. All ids,
hosts and tokens are synthetic.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from key_value.aio.stores.memory import MemoryStore

from exomem import command_surface, server
from exomem.auth_sessions import SessionAuthority, SessionIdentity
from exomem.governance import principal as principal_module
from exomem.governance.authorization_transport import AuthorizationSessionMiddleware
from exomem.session_oauth import ExomemSessionOAuthProxy

ISSUER = "https://memory.example"
AUDIENCE = f"{ISSUER}/mcp"
PROTOCOL_VERSION = "2026-07-28"
TOOL_NAME = "ask_memory"
BOUND_ID = 4242
OTHER_ID = 7171
ISSUER_FAMILY = "mcp-oauth:" + hashlib.sha256(ISSUER.encode()).hexdigest()


class _NeverExternalVerifier:
    required_scopes: list[str] = []

    async def verify_token(self, token: str) -> None:
        raise AssertionError("durable Exomem access tokens must not call the provider")


@dataclass
class _Harness:
    app: Any
    authority: SessionAuthority | None


def _probe_app(vault: Path, *, auth: Any) -> Any:
    mcp = server.ExomemFastMCP("remote-owner-probe", auth=auth)
    mcp.add_middleware(AuthorizationSessionMiddleware(vault))

    @mcp.tool(name=TOOL_NAME)
    async def principal_probe(marker: str = "") -> dict[str, object]:
        principal = principal_module.effective_principal()
        return {
            "audience_id": principal.audience_id,
            "principal_kind": principal.principal_kind,
            "remote_owner": principal.remote_owner,
            "issuer_family": principal.issuer_family,
            "retry_scope": command_surface.mcp_retry_scope(),
        }

    return mcp.http_app(stateless_http=True, json_response=True)


@pytest.fixture
def bound_host(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("EXOMEM_BASE_URL", ISSUER)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", str(BOUND_ID))
    monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    return monkeypatch


@pytest.fixture
def oauth_harness(vault: Path, tmp_path: Path) -> _Harness:
    authority = SessionAuthority.local(
        directory=tmp_path / "oauth-sessions",
        signing_root="remote-owner-signing-root",
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    oauth = ExomemSessionOAuthProxy(
        session_authority=authority,
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id="github-client",
        upstream_client_secret="github-secret",
        upstream_revocation_endpoint=None,
        token_verifier=_NeverExternalVerifier(),
        base_url=ISSUER,
        client_storage=MemoryStore(),
        jwt_signing_key="remote-owner-jwt-root",
        require_authorization_consent=False,
    )
    return _Harness(app=_probe_app(vault, auth=oauth), authority=authority)


@pytest.fixture
def loopback_harness(vault: Path) -> _Harness:
    """The loopback HTTP server runs without OAuth at all."""
    return _Harness(app=_probe_app(vault, auth=None), authority=None)


@asynccontextmanager
async def _client(harness: _Harness) -> AsyncIterator[httpx.AsyncClient]:
    async with harness.app.router.lifespan_context(harness.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app),
            base_url=ISSUER,
        ) as client:
            yield client


async def _issue(harness: _Harness, user_id: int) -> str:
    assert harness.authority is not None
    token, _record = await harness.authority.issue(
        client_id="remote-client",
        scopes=["exomem:read", "exomem:write"],
        identity=SessionIdentity(github_user_id=user_id, github_login="example-owner"),
    )
    return token


async def _call(client: httpx.AsyncClient, bearer: str, request_id: int) -> dict[str, Any]:
    response = await client.post(
        "/mcp",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {bearer}",
            "mcp-protocol-version": PROTOCOL_VERSION,
            "mcp-method": "tools/call",
            "mcp-name": TOOL_NAME,
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": {},
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "remote-client",
                        "version": "1",
                    },
                },
                "name": TOOL_NAME,
                "arguments": {"marker": str(request_id)},
            },
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]


def _remote_scope(user_id: int) -> str:
    return "principal:" + hashlib.sha256(f"{ISSUER}\0{user_id}".encode()).hexdigest()


@pytest.mark.anyio
async def test_bound_session_is_the_owner_inside_the_tool_leaf(
    oauth_harness: _Harness, bound_host: pytest.MonkeyPatch
) -> None:
    token = await _issue(oauth_harness, BOUND_ID)
    async with _client(oauth_harness) as client:
        seen = await _call(client, token, 1)

    assert seen == {
        "audience_id": principal_module.OWNER_AUDIENCE,
        "principal_kind": "owner-oauth",
        "remote_owner": True,
        # Labelled remote: the issuer family and the retry scope stay the
        # remote identity's, never the local owner's.
        "issuer_family": ISSUER_FAMILY,
        "retry_scope": _remote_scope(BOUND_ID),
    }


@pytest.mark.anyio
async def test_session_for_another_id_stays_a_separate_principal(
    oauth_harness: _Harness, bound_host: pytest.MonkeyPatch
) -> None:
    token = await _issue(oauth_harness, OTHER_ID)
    async with _client(oauth_harness) as client:
        seen = await _call(client, token, 2)

    assert seen["audience_id"] == _remote_scope(OTHER_ID)
    assert seen["principal_kind"] == "principal"
    assert seen["remote_owner"] is False


@pytest.mark.anyio
async def test_unset_binding_keeps_the_bound_account_a_separate_principal(
    oauth_harness: _Harness, bound_host: pytest.MonkeyPatch
) -> None:
    bound_host.delenv("EXOMEM_OWNER_OAUTH_SUBJECT")
    token = await _issue(oauth_harness, BOUND_ID)
    async with _client(oauth_harness) as client:
        seen = await _call(client, token, 3)

    assert seen["audience_id"] == _remote_scope(BOUND_ID)
    assert seen["principal_kind"] == "principal"


@pytest.mark.anyio
async def test_removing_the_binding_applies_to_the_next_request_of_a_live_session(
    oauth_harness: _Harness, bound_host: pytest.MonkeyPatch
) -> None:
    token = await _issue(oauth_harness, BOUND_ID)
    async with _client(oauth_harness) as client:
        before = await _call(client, token, 4)
        bound_host.delenv("EXOMEM_OWNER_OAUTH_SUBJECT")
        after = await _call(client, token, 5)

    assert before["principal_kind"] == "owner-oauth"
    # The same session, not revoked, is now its separate remote principal.
    assert after["audience_id"] == _remote_scope(BOUND_ID)
    assert after["principal_kind"] == "principal"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "credential",
    [str(BOUND_ID), f"github:{BOUND_ID}", '{"sub":"4242","github_user_id":4242}'],
)
async def test_forged_bearer_on_loopback_http_is_never_the_owner(
    loopback_harness: _Harness, bound_host: pytest.MonkeyPatch, credential: str
) -> None:
    async with _client(loopback_harness) as client:
        seen = await _call(client, credential, 6)

    assert seen["audience_id"] != principal_module.OWNER_AUDIENCE
    assert seen["principal_kind"] == "principal"
    assert seen["remote_owner"] is False
