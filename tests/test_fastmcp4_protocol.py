"""FastMCP 4 wire compatibility at Exomem's authenticated HTTP boundary."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from key_value.aio.stores.memory import MemoryStore
from test_authorization_session_wire_reconnect import _configure_v4_authority

from exomem import command_surface, server
from exomem.auth_sessions import SessionAuthority, SessionIdentity
from exomem.governance import (
    authorization_custody,
    authorization_session_lifecycle,
    store,
)
from exomem.governance import principal as principal_module
from exomem.governance.authorization_transport import AuthorizationSessionMiddleware
from exomem.session_oauth import ExomemSessionOAuthProxy

LEGACY_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
ISSUER = "https://memory.example"
AUDIENCE = "https://memory.example/mcp"
TOOL_NAME = "ask_memory"


class _NeverExternalVerifier:
    required_scopes: list[str] = []

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def verify_token(self, token: str) -> None:
        self.calls.append(token)
        raise AssertionError("durable Exomem access tokens must not call the provider")


@dataclass
class _Harness:
    app: Any
    authority: SessionAuthority
    verifier: _NeverExternalVerifier
    leaf_calls: list[str]
    vault: Path


@pytest.fixture
def protocol_harness(vault: Path, tmp_path: Path) -> _Harness:
    authority = SessionAuthority.local(
        directory=tmp_path / "oauth-sessions",
        signing_root="protocol-regression-signing-root",
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    verifier = _NeverExternalVerifier()
    oauth = ExomemSessionOAuthProxy(
        session_authority=authority,
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id="github-client",
        upstream_client_secret="github-secret",
        upstream_revocation_endpoint=None,
        token_verifier=verifier,
        base_url=ISSUER,
        client_storage=MemoryStore(),
        jwt_signing_key="protocol-regression-jwt-root",
        require_authorization_consent=False,
    )
    mcp = server.ExomemFastMCP("protocol-regression", auth=oauth)
    mcp.add_middleware(AuthorizationSessionMiddleware(vault))
    leaf_calls: list[str] = []

    @mcp.tool(name=TOOL_NAME)
    async def principal_probe(marker: str = "") -> dict[str, object]:
        leaf_calls.append(marker)
        if marker.startswith("concurrent-"):
            await anyio.sleep(0.02)
        principal = principal_module.effective_principal()
        caller = command_surface.mcp_caller_identity()
        verified = principal.verified_authorization_session
        return {
            "marker": marker,
            "audience_id": principal.audience_id,
            "resolved": principal.resolved,
            "issuer_family": principal.issuer_family,
            "authorization_session_id": principal.authorization_session_id,
            "verified_session_id": (None if verified is None else verified.session_id),
            "client_name": caller["client_name"],
            "client_version": caller["client_version"],
        }

    return _Harness(
        app=mcp.http_app(stateless_http=True, json_response=True),
        authority=authority,
        verifier=verifier,
        leaf_calls=leaf_calls,
        vault=vault,
    )


@asynccontextmanager
async def _client(harness: _Harness) -> AsyncIterator[httpx.AsyncClient]:
    async with harness.app.router.lifespan_context(harness.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app),
            base_url=ISSUER,
        ) as client:
            yield client


async def _issue_oauth_token(
    harness: _Harness,
    *,
    user_id: int,
    login: str,
) -> tuple[str, str, str]:
    token, _record = await harness.authority.issue(
        client_id="protocol-client",
        scopes=["exomem:read", "exomem:write"],
        identity=SessionIdentity(github_user_id=user_id, github_login=login),
    )
    audience = principal_module.normalize_audience(
        subject=str(user_id),
        issuer=ISSUER,
    )
    issuer_family = "mcp-oauth:" + hashlib.sha256(ISSUER.encode()).hexdigest()
    return token, audience, issuer_family


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _modern_meta(*, client_name: str, client_version: str = "1") -> dict[str, object]:
    return {
        "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": client_name,
            "version": client_version,
        },
    }


async def _modern_request(
    client: httpx.AsyncClient,
    *,
    token: str | None,
    request_id: int,
    method: str,
    params: dict[str, object],
) -> httpx.Response:
    headers = {
        **_headers(token),
        "accept": "application/json",
        "mcp-protocol-version": MODERN_VERSION,
        "mcp-method": method,
    }
    if method == "tools/call":
        headers["mcp-name"] = str(params["name"])
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
    )


def _structured(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    structured = result["structuredContent"]
    assert isinstance(structured, dict)
    return structured


def _assert_tool_refusal(response: httpx.Response, *secrets: str) -> None:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is True, result
    assert result["content"] == [{"type": "text", "text": "authorization session is unavailable"}]
    for secret in secrets:
        assert secret not in response.text


@pytest.mark.anyio
async def test_legacy_initialize_list_and_call_use_the_oauth_principal(
    protocol_harness: _Harness,
) -> None:
    token, audience, issuer_family = await _issue_oauth_token(
        protocol_harness,
        user_id=101,
        login="legacy-person",
    )
    headers = _headers(token)

    async with _client(protocol_harness) as client:
        initialized = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LEGACY_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-client", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["protocolVersion"] == LEGACY_VERSION

        legacy_headers = {**headers, "mcp-protocol-version": LEGACY_VERSION}
        listed = await client.post(
            "/mcp",
            headers=legacy_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert listed.status_code == 200, listed.text
        assert [tool["name"] for tool in listed.json()["result"]["tools"]] == [TOOL_NAME]

        called = await client.post(
            "/mcp",
            headers=legacy_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": {"marker": "legacy"},
                },
            },
        )

    structured = _structured(called)
    assert {
        key: structured[key]
        for key in (
            "marker",
            "audience_id",
            "resolved",
            "issuer_family",
            "authorization_session_id",
            "verified_session_id",
        )
    } == {
        "marker": "legacy",
        "audience_id": audience,
        "resolved": True,
        "issuer_family": issuer_family,
        "authorization_session_id": None,
        "verified_session_id": None,
    }
    assert protocol_harness.leaf_calls == ["legacy"]
    assert protocol_harness.verifier.calls == []


@pytest.mark.anyio
async def test_modern_discover_and_direct_tool_call_use_request_metadata_and_oauth(
    protocol_harness: _Harness,
) -> None:
    token, audience, issuer_family = await _issue_oauth_token(
        protocol_harness,
        user_id=202,
        login="modern-person",
    )
    meta = _modern_meta(client_name="modern-client", client_version="2")

    async with _client(protocol_harness) as client:
        discovered = await _modern_request(
            client,
            token=token,
            request_id=10,
            method="server/discover",
            params={"_meta": meta},
        )
        assert discovered.status_code == 200, discovered.text
        discovery = discovered.json()["result"]
        assert discovery["supportedVersions"] == [MODERN_VERSION]
        assert discovery["resultType"] == "complete"
        assert "mcp-session-id" not in discovered.headers

        called = await _modern_request(
            client,
            token=token,
            request_id=11,
            method="tools/call",
            params={
                "_meta": meta,
                "name": TOOL_NAME,
                "arguments": {"marker": "modern"},
            },
        )

    assert _structured(called) == {
        "marker": "modern",
        "audience_id": audience,
        "resolved": True,
        "issuer_family": issuer_family,
        "authorization_session_id": None,
        "verified_session_id": None,
        "client_name": "modern-client",
        "client_version": "2",
    }
    assert called.json()["result"]["resultType"] == "complete"
    assert "mcp-session-id" not in called.headers
    assert protocol_harness.leaf_calls == ["modern"]
    assert protocol_harness.verifier.calls == []


@pytest.mark.anyio
async def test_missing_invalid_and_revoked_oauth_tokens_never_reach_the_tool(
    protocol_harness: _Harness,
) -> None:
    revoked, record = await protocol_harness.authority.issue(
        client_id="protocol-client",
        scopes=["exomem:read"],
        identity=SessionIdentity(github_user_id=303, github_login="revoked-person"),
    )
    await protocol_harness.authority.tombstone(record.session_id, reason="test")

    async with _client(protocol_harness) as client:
        for request_id, token in enumerate((None, "invalid-oauth-secret", revoked), 20):
            response = await _modern_request(
                client,
                token=token,
                request_id=request_id,
                method="tools/call",
                params={
                    "_meta": _modern_meta(client_name="denied-client"),
                    "name": TOOL_NAME,
                    "arguments": {"marker": "must-not-run"},
                },
            )
            assert response.status_code == 401
            assert "bearer" in response.headers["www-authenticate"].lower()
            if token is not None:
                assert token not in response.text

    assert protocol_harness.leaf_calls == []
    assert protocol_harness.verifier.calls == []


@pytest.mark.anyio
async def test_get_and_delete_remain_behind_oauth(
    protocol_harness: _Harness,
) -> None:
    async with _client(protocol_harness) as client:
        for method in ("GET", "DELETE"):
            response = await client.request(method, "/mcp")
            assert response.status_code == 401
            assert response.headers["www-authenticate"].startswith("Bearer")

    assert protocol_harness.leaf_calls == []


@pytest.mark.anyio
async def test_concurrent_modern_calls_keep_principals_request_local(
    protocol_harness: _Harness,
) -> None:
    first = await _issue_oauth_token(
        protocol_harness,
        user_id=401,
        login="first-person",
    )
    second = await _issue_oauth_token(
        protocol_harness,
        user_id=402,
        login="second-person",
    )
    results: dict[str, dict[str, object]] = {}

    async with _client(protocol_harness) as client:

        async def invoke(label: str, issued: tuple[str, str, str]) -> None:
            token, _audience, _issuer_family = issued
            response = await _modern_request(
                client,
                token=token,
                request_id=40 if label == "first" else 41,
                method="tools/call",
                params={
                    "_meta": _modern_meta(client_name=f"{label}-client"),
                    "name": TOOL_NAME,
                    "arguments": {"marker": f"concurrent-{label}"},
                },
            )
            results[label] = _structured(response)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(invoke, "first", first)
            tasks.start_soon(invoke, "second", second)

    for label, (_token, audience, issuer_family) in {
        "first": first,
        "second": second,
    }.items():
        assert results[label]["audience_id"] == audience
        assert results[label]["issuer_family"] == issuer_family
        assert results[label]["client_name"] == f"{label}-client"
    assert sorted(protocol_harness.leaf_calls) == [
        "concurrent-first",
        "concurrent-second",
    ]
    assert protocol_harness.verifier.calls == []


@pytest.mark.anyio
async def test_governance_carrier_is_verified_and_malformed_or_duplicate_is_safe(
    protocol_harness: _Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_v4_authority(
        protocol_harness.vault,
        tmp_path / "authorization-custody",
        monkeypatch,
    )
    oauth_token, audience, issuer_family = await _issue_oauth_token(
        protocol_harness,
        user_id=501,
        login="governed-person",
    )
    now = int(time.time())
    custody = authorization_custody.load_authorization_custody(
        protocol_harness.vault,
        now=now,
    )
    connection = store.open_authorization_session_connection(protocol_harness.vault)
    try:
        issued = authorization_session_lifecycle.open_session(
            connection,
            custody=custody,
            principal_id=audience,
            issuer_family=issuer_family,
            now=now,
            ttl_seconds=600,
        )
    finally:
        connection.close()

    meta = _modern_meta(client_name="governed-client")
    async with _client(protocol_harness) as client:
        valid = await _modern_request(
            client,
            token=oauth_token,
            request_id=50,
            method="tools/call",
            params={
                "_meta": meta,
                "name": TOOL_NAME,
                "arguments": {
                    "marker": "valid-governance",
                    "authorization_session_credential": issued.bearer,
                },
            },
        )
        valid_result = _structured(valid)
        assert valid_result["authorization_session_id"] == issued.context.session_id
        assert valid_result["verified_session_id"] == issued.context.session_id
        assert issued.bearer not in valid.text

        malformed = issued.bearer + "x"
        malformed_response = await _modern_request(
            client,
            token=oauth_token,
            request_id=51,
            method="tools/call",
            params={
                "_meta": meta,
                "name": TOOL_NAME,
                "arguments": {
                    "marker": "malformed-governance",
                    "authorization_session_credential": malformed,
                },
            },
        )
        _assert_tool_refusal(malformed_response, issued.bearer, malformed)

        duplicate_body = (
            '{"jsonrpc":"2.0","id":52,"method":"tools/call","params":{'
            f'"_meta":{json.dumps(meta, separators=(",", ":"))},'
            f'"name":"{TOOL_NAME}","arguments":{{"marker":"duplicate-governance",'
            f'"authorization_session_credential":"{issued.bearer}",'
            f'"authorization_session_credential":"{issued.bearer}"}}}}}}'
        )
        duplicate_response = await client.post(
            "/mcp",
            content=duplicate_body,
            headers={
                **_headers(oauth_token),
                "accept": "application/json",
                "mcp-protocol-version": MODERN_VERSION,
                "mcp-method": "tools/call",
                "mcp-name": TOOL_NAME,
            },
        )
        _assert_tool_refusal(duplicate_response, issued.bearer)

    assert protocol_harness.leaf_calls == ["valid-governance"]
    assert protocol_harness.verifier.calls == []
