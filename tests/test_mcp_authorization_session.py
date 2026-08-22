"""MCP consumes the public bearer placeholder before FastMCP validation."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.testclient import TestClient

from exomem import server, writer_lease
from exomem.access_log import AccessLogMiddleware
from exomem.governance import authorization_transport
from exomem.governance.authorization_transport import AuthorizationCarrierMiddleware

CANONICAL_UNKNOWN_BEARER = (
    "as1.AQEBAQEBAQEBAQEBAQEBAQ."
    "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
)


@pytest.fixture(autouse=True)
def _reset_writer_state() -> None:
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _server(vault, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "writer-lease-state")
    )
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    return server.build_server(require_auth=False)


def test_generated_mcp_schema_exposes_only_optional_consumed_placeholder(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = {
        tool.name: tool.to_mcp_tool().model_dump(mode="json")
        for tool in asyncio.run(_server(vault, monkeypatch).list_tools())
    }

    for name in ("ask_memory", "govern_memory"):
        schema = tools[name]["inputSchema"]
        assert "authorization_session_credential" in schema["properties"]
        assert "authorization_session_credential" not in schema.get("required", [])
        assert "principal" not in schema["properties"]
        assert "principal_scope" not in schema["properties"]
        assert "issuer" not in schema["properties"]
        assert "authorization_session_id" not in schema["properties"]


def test_sse_transport_sanitizes_the_message_post_route(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _server(vault, monkeypatch).http_app(transport="sse")
    [carrier_middleware] = [
        item
        for item in app.user_middleware
        if item.cls is AuthorizationCarrierMiddleware
    ]

    assert carrier_middleware.kwargs["mcp_path"] == "/messages"


def test_raw_carrier_middleware_precedes_access_logging(
    vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _server(vault, monkeypatch).http_app(
        stateless_http=True,
        middleware=[ASGIMiddleware(AccessLogMiddleware)],
    )
    middleware = [item.cls for item in app.user_middleware]

    assert middleware.index(AuthorizationCarrierMiddleware) < middleware.index(
        AccessLogMiddleware
    )


def test_sanitized_http_body_replays_once_then_preserves_disconnect() -> None:
    calls = 0

    async def original_receive() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"type": "http.disconnect"}

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        receive = authorization_transport._replay_body(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
            original_receive,
        )
        return await receive(), await receive()

    replayed, disconnected = asyncio.run(exercise())

    assert replayed == {
        "type": "http.request",
        "body": b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
        "more_body": False,
    }
    assert disconnected == {"type": "http.disconnect"}
    assert calls == 1


def test_invalid_mcp_credential_wins_before_fastmcp_argument_validation(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    presented = "not-a-session-bearer"

    with TestClient(
        _server(vault, monkeypatch).http_app(stateless_http=True, json_response=True)
    ) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {
                    "name": "ask_memory",
                    "arguments": {
                        "authorization_session_credential": presented,
                        "limit": "malformed",
                    },
                },
            },
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        )

    assert response.status_code in {200, 400}
    assert "authorization session is unavailable" in response.text
    assert "limit" not in response.text
    assert presented not in response.text


def test_mcp_verifies_present_credential_before_route_selector_validation(
    vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def refuse_verification(*_args, **_kwargs):
        calls.append("verify")
        raise authorization_transport.AuthorizationContextUnavailable

    def reject_early_selector(*_args, **_kwargs):
        calls.append("selector")
        raise authorization_transport.AuthorizationRouteUnclassified

    monkeypatch.setattr(
        authorization_transport,
        "verify_authorization_context",
        refuse_verification,
        raising=False,
    )
    monkeypatch.setattr(
        authorization_transport,
        "credential_rule",
        reject_early_selector,
    )

    with TestClient(
        _server(vault, monkeypatch).http_app(stateless_http=True, json_response=True)
    ) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 44,
                "method": "tools/call",
                "params": {
                    "name": "govern_memory",
                    "arguments": {
                        "authorization_session_credential": CANONICAL_UNKNOWN_BEARER,
                    },
                },
            },
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        )

    assert calls == ["verify"]
    assert "authorization session is unavailable" in response.text
    assert CANONICAL_UNKNOWN_BEARER not in response.text


def test_mcp_tool_call_batch_is_refused_before_any_element_dispatch(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(
        _server(vault, monkeypatch).http_app(stateless_http=True, json_response=True)
    ) as client:
        response = client.post(
            "/mcp",
            content=json.dumps(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "browse_memory", "arguments": {}},
                    },
                ]
            ),
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        )

    assert response.status_code == 400
    assert response.headers["x-exomem-error-code"] == "AUTHORIZATION_BATCH_UNAVAILABLE"
    assert response.json()["error"]["message"] == "authorization request is unavailable"


def test_mcp_rejects_protected_header_as_wrong_surface_carrier(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(
        _server(vault, monkeypatch).http_app(stateless_http=True, json_response=True)
    ) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {"name": "browse_memory", "arguments": {"mode": "list"}},
            },
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "x-exomem-authorization-session": "not-a-session-bearer",
            },
        )

    assert response.status_code == 400
    assert response.headers["x-exomem-error-code"] == (
        "AUTHORIZATION_SESSION_UNAVAILABLE"
    )
    assert response.json()["error"]["message"] == "authorization request is unavailable"
    assert "not-a-session-bearer" not in response.text


def test_mcp_duplicate_arguments_objects_cannot_hide_a_bearer(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    presented = "not-a-session-bearer"
    body = (
        '{"jsonrpc":"2.0","id":43,"method":"tools/call","params":{'
        '"name":"ask_memory","arguments":{'
        f'"authorization_session_credential":"{presented}"'
        '},"arguments":{"limit":"malformed"}}}'
    )
    with TestClient(
        _server(vault, monkeypatch).http_app(stateless_http=True, json_response=True)
    ) as client:
        response = client.post(
            "/mcp",
            content=body,
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        )

    assert response.status_code in {200, 400}
    assert "authorization session is unavailable" in response.text
    assert "limit" not in response.text
    assert presented not in response.text


def test_actual_stdio_refuses_before_validation_without_logging_bearer(
    vault, tmp_path: Path
) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    presented = (
        "as1.AQEBAQEBAQEBAQEBAQEBAQ."
        "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
    )
    transport_log = tmp_path / "stdio.log"
    exomem_log_dir = tmp_path / "logs"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EXOMEM_") and key != "PYTHONPATH"
    }
    env.update(
        {
            "EXOMEM_VAULT_PATH": str(vault),
            "EXOMEM_LOG_DIR": str(exomem_log_dir),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "writer-state"),
            "EXOMEM_CONFIG_PATH": str(tmp_path / "config.json"),
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_DISABLE_CORPUS_CACHE": "1",
            "EXOMEM_DISABLE_RESOLVER_WARM": "1",
            "EXOMEM_LOG_LEVEL": "DEBUG",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )

    async def exercise() -> object:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "exomem", "--transport", "stdio"],
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            keep_alive=False,
            log_file=transport_log,
        )
        async with Client(transport, timeout=30, init_timeout=30) as client:
            return await client.call_tool(
                "ask_memory",
                {
                    "authorization_session_credential": presented,
                    "limit": "malformed",
                },
                raise_on_error=False,
            )

    result = asyncio.run(exercise())
    rendered_result = repr(result)
    rendered_logs = transport_log.read_text(encoding="utf-8", errors="replace")
    rendered_logs += (exomem_log_dir / "exomem.log").read_text(
        encoding="utf-8", errors="replace"
    )

    assert result.is_error is True
    assert "authorization session is unavailable" in rendered_result
    assert "limit" not in rendered_result
    assert presented not in rendered_result
    assert presented not in rendered_logs
