from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from exomem import logging_config, server
from exomem.server_assets import register_oauth_metadata_route
from exomem.server_transport import PrimeMcpSSEMiddleware


async def _receive() -> dict:
    return {"type": "http.disconnect"}


def test_mcp_get_sse_is_primed_immediately() -> None:
    async def scenario() -> list[dict]:
        sent: list[dict] = []

        async def capture(message: dict) -> None:
            sent.append(message)

        async def app(scope, receive, send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"data: later\r\n\r\n"})

        middleware = PrimeMcpSSEMiddleware(app)
        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [(b"accept", b"text/event-stream")],
            },
            _receive,
            capture,
        )
        return sent

    sent = asyncio.run(scenario())

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]
    assert sent[1] == {
        "type": "http.response.body",
        "body": b": stream-ready\r\n\r\n",
        "more_body": True,
    }


def test_non_sse_or_failed_response_is_not_primed() -> None:
    async def run(scope: dict, *, status: int, content_type: bytes) -> list[dict]:
        sent: list[dict] = []

        async def capture(message: dict) -> None:
            sent.append(message)

        async def app(inner_scope, receive, send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", content_type)],
                }
            )
            await send({"type": "http.response.body", "body": b"original"})

        await PrimeMcpSSEMiddleware(app)(scope, _receive, capture)
        return sent

    base = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": [(b"accept", b"text/event-stream")],
    }
    assert len(asyncio.run(run(base, status=401, content_type=b"text/event-stream"))) == 2
    assert len(asyncio.run(run(base, status=200, content_type=b"application/json"))) == 2
    assert (
        len(
            asyncio.run(
                run(
                    {**base, "method": "POST"},
                    status=200,
                    content_type=b"text/event-stream",
                )
            )
        )
        == 2
    )


class _FakeMcp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> _FakeMcp:
    fake = _FakeMcp()
    monkeypatch.setattr(server, "build_server", lambda **_kwargs: fake)
    monkeypatch.setattr(logging_config, "configure_logging", lambda *_args, **_kwargs: None)
    return fake


@pytest.mark.parametrize("transport", ["http", "streamable-http"])
def test_remote_http_runs_stateless(
    fake_mcp: _FakeMcp,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    monkeypatch.delenv("EXOMEM_HOST", raising=False)

    server.run(transport=transport, host="127.0.0.1", port=9876)

    assert len(fake_mcp.calls) == 1
    call = fake_mcp.calls[0]
    middleware = call.pop("middleware")
    assert call == {
        "transport": transport,
        "host": "127.0.0.1",
        "port": 9876,
        "stateless_http": True,
    }
    assert len(middleware) == 3
    assert middleware[0].cls is server.EdgeIngressMiddleware
    assert middleware[1].cls is server.AccessLogMiddleware
    assert middleware[2].cls is server.PrimeMcpSSEMiddleware


def test_stdio_does_not_apply_http_stateless_configuration(fake_mcp: _FakeMcp) -> None:
    server.run(transport="stdio")

    assert fake_mcp.calls == [{"transport": "stdio"}]


def test_stateless_http_keeps_get_sse_compatibility() -> None:
    mcp = server.ExomemFastMCP("transport-test")

    app = mcp.http_app(transport="streamable-http", stateless_http=True)

    endpoint = next(route for route in app.routes if getattr(route, "path", None) == "/mcp")
    assert endpoint.methods == {"GET", "POST", "DELETE"}


@pytest.mark.parametrize("value", [True, False, "1", 1.0])
def test_mcp_preserves_declared_strict_integer_validation(value: object) -> None:
    from pydantic import StrictInt

    mcp = server.ExomemFastMCP("strict-input-test")
    calls: list[int] = []

    def bounded(limit: StrictInt) -> int:
        calls.append(limit)
        return limit

    # Resolve the local annotation for the same callable reflection used by
    # public command registration.
    bounded.__annotations__["limit"] = StrictInt
    mcp.tool(bounded)
    with pytest.raises(Exception, match="limit"):
        asyncio.run(mcp.call_tool("bounded", {"limit": value}))
    assert calls == []
    asyncio.run(mcp.call_tool("bounded", {"limit": 1}))
    assert calls == [1]


def test_openid_discovery_alias_returns_oauth_metadata() -> None:
    mcp = server.ExomemFastMCP("discovery-test")
    register_oauth_metadata_route(
        mcp,
        base_url="https://memory.example",
        auth_enabled=True,
    )
    app = mcp.http_app(transport="streamable-http", stateless_http=True)
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/.well-known/openid-configuration"
    )

    response = asyncio.run(route.endpoint(None))
    metadata = json.loads(response.body)

    assert metadata["issuer"] == "https://memory.example/"
    assert metadata["authorization_endpoint"] == "https://memory.example/authorize"
    assert metadata["token_endpoint"] == "https://memory.example/token"
    assert metadata["registration_endpoint"] == "https://memory.example/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["scopes_supported"] == [
        "offline_access",
        "exomem:read",
        "exomem:write",
    ]
    assert "openid" not in metadata["scopes_supported"]
    for oidc_only_field in (
        "userinfo_endpoint",
        "jwks_uri",
        "subject_types_supported",
        "id_token_signing_alg_values_supported",
    ):
        assert oidc_only_field not in metadata


def test_bare_protected_resource_alias_omits_protocol_scope() -> None:
    mcp = server.ExomemFastMCP("discovery-test")
    register_oauth_metadata_route(
        mcp,
        base_url="https://memory.example",
        auth_enabled=True,
    )
    app = mcp.http_app(transport="streamable-http", stateless_http=True)
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/.well-known/oauth-protected-resource"
    )

    response = asyncio.run(route.endpoint(None))
    metadata = json.loads(response.body)

    assert metadata["scopes_supported"] == ["exomem:read", "exomem:write"]
    assert "offline_access" not in metadata["scopes_supported"]


def test_managed_worker_socket_preserves_public_auth_intent(fake_mcp, monkeypatch, tmp_path):
    requested = []
    monkeypatch.delenv("EXOMEM_HOST", raising=False)
    monkeypatch.delenv("EXOMEM_BASE_URL", raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-only-key")
    monkeypatch.setattr(
        server, "build_server", lambda **kwargs: requested.append(kwargs) or fake_mcp
    )
    socket_path = tmp_path / "worker.sock"
    server.run(transport="streamable-http", host="0.0.0.0", worker_socket=socket_path)
    assert requested == [{"require_auth": True}]
    call = fake_mcp.calls[0]
    assert call["stateless_http"] is True
    assert call["host"] == "0.0.0.0"
    assert call["uvicorn_config"]["uds"] == str(socket_path)
