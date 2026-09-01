"""Raw authorization-session carriers are consumed before framework work."""

from __future__ import annotations

import json
import os
from asyncio import run

import pytest

BEARER = (
    "as1.AQEBAQEBAQEBAQEBAQEBAQ."
    "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
)


def test_mcp_single_call_consumes_only_placeholder_and_sanitizes_wire_body() -> None:
    from exomem.governance.authorization_transport import sanitize_mcp_http_body

    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "ask_memory",
                "arguments": {
                    "query": "governance",
                    "authorization_session_credential": BEARER,
                },
            },
        }
    ).encode()

    sanitized = sanitize_mcp_http_body(raw)

    decoded = json.loads(sanitized.body)
    assert decoded["params"]["arguments"] == {"query": "governance"}
    assert sanitized.tool_name == "ask_memory"
    assert sanitized.arguments == {"query": "governance"}
    assert sanitized.carrier.consume() == BEARER
    assert BEARER not in sanitized.body.decode()
    assert BEARER not in repr(sanitized)
    assert sanitized.carrier.consume() is not BEARER


def test_stdio_session_message_is_sanitized_before_low_level_logging() -> None:
    from exomem.governance.authorization_transport import (
        CredentialCarrier,
        sanitize_mcp_stdio_line,
    )

    line = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "ask_memory",
                "arguments": {
                    "query": "governance",
                    "authorization_session_credential": BEARER,
                },
            },
        }
    )

    message = sanitize_mcp_stdio_line(line)

    assert BEARER not in repr(message)
    request = message.message.root
    assert request.params["arguments"] == {"query": "governance"}
    request_context = message.metadata.request_context
    assert request_context.headers == {}
    assert request_context.scope == {}
    carrier = request_context.authorization_carrier
    assert isinstance(carrier, CredentialCarrier)
    assert carrier.consume() == BEARER


@pytest.mark.parametrize(
    "carrier_fragment",
    [
        '"authorization_session_credential": 7',
        (
            f'"authorization_session_credential":"{BEARER}",'
            f'"authorization_session_credential":"{BEARER}"'
        ),
    ],
)
def test_mcp_invalid_or_duplicate_carrier_is_sanitized_and_marked_invalid(
    carrier_fragment: str,
) -> None:
    from exomem.governance import authorization_request
    from exomem.governance.authorization_transport import sanitize_mcp_http_body

    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"ask_memory","arguments":{'
        + carrier_fragment
        + ',"limit":"malformed"}}}'
    ).encode()

    sanitized = sanitize_mcp_http_body(raw)

    assert sanitized.carrier.consume() is authorization_request.INVALID_CREDENTIAL
    assert BEARER not in sanitized.body.decode()
    assert BEARER not in repr(sanitized)


def test_mcp_duplicate_arguments_objects_cannot_hide_a_carrier() -> None:
    from exomem.governance import authorization_request
    from exomem.governance.authorization_transport import sanitize_mcp_http_body

    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{'
        '"name":"ask_memory","arguments":{'
        f'"authorization_session_credential":"{BEARER}"'
        '},"arguments":{"limit":"malformed"}}}'
    ).encode()

    sanitized = sanitize_mcp_http_body(raw)

    assert sanitized.carrier.consume() is authorization_request.INVALID_CREDENTIAL
    assert BEARER not in sanitized.body.decode()


def test_mcp_batch_with_tool_call_is_atomically_refused_without_secret_copy() -> None:
    from exomem.governance.authorization_transport import (
        AtomicMcpBatchRefusal,
        sanitize_mcp_http_body,
    )

    raw = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "ask_memory",
                    "arguments": {"authorization_session_credential": BEARER},
                },
            },
        ]
    ).encode()

    with pytest.raises(AtomicMcpBatchRefusal) as error:
        sanitize_mcp_http_body(raw)

    assert BEARER not in repr(error.value)
    assert str(error.value) == "authorization request is unavailable"


def test_sensitive_header_is_removed_before_downstream_scope() -> None:
    from exomem.governance.authorization_transport import (
        AUTHORIZATION_SESSION_HEADER,
        strip_sensitive_authorization_header,
    )

    headers, carrier = strip_sensitive_authorization_header(
        [
            (b"authorization", b"Bearer service-key"),
            (AUTHORIZATION_SESSION_HEADER, BEARER.encode()),
            (b"content-type", b"application/json"),
        ]
    )

    assert headers == [
        (b"authorization", b"Bearer service-key"),
        (b"content-type", b"application/json"),
    ]
    assert carrier.consume() == BEARER
    assert BEARER not in repr(carrier)


def test_duplicate_sensitive_header_is_removed_and_marked_invalid() -> None:
    from exomem.governance import authorization_request
    from exomem.governance.authorization_transport import (
        AUTHORIZATION_SESSION_HEADER,
        strip_sensitive_authorization_header,
    )

    headers, carrier = strip_sensitive_authorization_header(
        [
            (AUTHORIZATION_SESSION_HEADER, BEARER.encode()),
            (AUTHORIZATION_SESSION_HEADER.upper(), BEARER.encode()),
        ]
    )

    assert headers == []
    assert carrier.consume() is authorization_request.INVALID_CREDENTIAL
    assert BEARER not in repr(carrier)


def test_cli_fd_reader_is_bounded_one_shot_and_does_not_close_caller_fd() -> None:
    from exomem.governance.authorization_transport import read_cli_authorization_fd

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, BEARER.encode())
        os.close(write_fd)
        write_fd = -1

        carrier = read_cli_authorization_fd(str(read_fd))

        assert carrier.consume() == BEARER
        assert BEARER not in repr(carrier)
        assert carrier.consume() is not BEARER
        os.fstat(read_fd)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


@pytest.mark.parametrize("payload", [BEARER.encode() + b"x", b"not-a-bearer"])
def test_cli_fd_reader_marks_oversized_or_malformed_input_invalid(payload: bytes) -> None:
    from exomem.governance import authorization_request
    from exomem.governance.authorization_transport import read_cli_authorization_fd

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = -1

        carrier = read_cli_authorization_fd(str(read_fd))

        assert carrier.consume() is authorization_request.INVALID_CREDENTIAL
        assert payload.decode() not in repr(carrier)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


@pytest.mark.parametrize(
    ("path", "query_string", "body"),
    [
        (
            "/api/ask_memory",
            f"authorization_session_credential={BEARER}".encode(),
            b'{"query":"governance"}',
        ),
        (
            "/private/exomem/v1/command/ask_memory",
            b"",
            json.dumps(
                {
                    "query": "governance",
                    "authorization_session_credential": BEARER,
                }
            ).encode(),
        ),
        (
            "/private/exomem/v1/agent/hosted-alpha-agent-v2/command/ask_memory",
            b"",
            json.dumps({"query": f"show inert text {BEARER}"}).encode(),
        ),
    ],
)
def test_forbidden_http_carrier_is_redacted_before_downstream_request_copies(
    path: str,
    query_string: bytes,
    body: bytes,
) -> None:
    from exomem.governance.authorization_transport import AuthorizationCarrierMiddleware

    downstream: list[object] = []
    sent: list[dict[str, object]] = []

    async def app(scope, receive, send) -> None:
        downstream.append(scope)
        downstream.append(await receive())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    run(
        AuthorizationCarrierMiddleware(app)(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "query_string": query_string,
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )
    )

    rendered = repr((downstream, sent))
    assert "authorization_session_credential" in rendered
    assert BEARER not in rendered


def test_mcp_bearer_outside_protected_placeholder_is_redacted_and_invalid() -> None:
    from exomem.governance import authorization_request
    from exomem.governance.authorization_transport import sanitize_mcp_http_body

    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "ask_memory",
                "arguments": {"query": f"retrieved text {BEARER}"},
            },
        }
    ).encode()

    sanitized = sanitize_mcp_http_body(raw)

    assert sanitized.carrier.consume() is authorization_request.INVALID_CREDENTIAL
    assert BEARER not in sanitized.body.decode()
    assert BEARER not in repr(sanitized.arguments)
