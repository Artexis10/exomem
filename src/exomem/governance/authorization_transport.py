"""Raw, bearer-redacting authorization-session carrier primitives."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Final

import anyio
import mcp.types
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from mcp.shared.exceptions import McpError
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from mcp.types import ErrorData

from . import authorization_sessions
from . import principal as principal_module
from .authorization_request import (
    ABSENT_CREDENTIAL,
    INVALID_CREDENTIAL,
    AuthorizationContextUnavailable,
    AuthorizationRouteUnclassified,
    credential_rule,
    enforce_credential_rule,
    verify_authorization_context,
)

AUTHORIZATION_SESSION_HEADER_NAME: Final = "X-Exomem-Authorization-Session"
AUTHORIZATION_SESSION_HEADER: Final = AUTHORIZATION_SESSION_HEADER_NAME.lower().encode()
MCP_CREDENTIAL_PARAMETER: Final = "authorization_session_credential"
_MAX_MCP_JSON_BYTES: Final = 64 * 1024 * 1024
_REQUEST_CARRIER: ContextVar[CredentialCarrier | None] = ContextVar(
    "exomem_authorization_credential_carrier", default=None
)

ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]


class AtomicMcpBatchRefusal(RuntimeError):
    """A JSON-RPC batch containing a tool call cannot dispatch partially."""

    code = "AUTHORIZATION_BATCH_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("authorization request is unavailable")


class AuthorizationEnvelopeUnavailable(RuntimeError):
    """The raw authorization envelope could not be safely classified."""

    code = "AUTHORIZATION_REQUEST_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("authorization request is unavailable")


class _ObjectPairs(list[tuple[str, object]]):
    """JSON object retaining duplicate keys until credential extraction."""


class CredentialCarrier:
    """One-shot, redacted storage for a bounded canonical bearer."""

    __slots__ = ("_payload", "_state")

    def __init__(self, payload: bytes | None, *, state: str) -> None:
        self._payload = bytearray(payload) if payload is not None else None
        self._state = state

    @classmethod
    def absent(cls) -> CredentialCarrier:
        return cls(None, state="absent")

    @classmethod
    def invalid(cls) -> CredentialCarrier:
        return cls(None, state="invalid")

    @classmethod
    def from_value(cls, value: object) -> CredentialCarrier:
        if not isinstance(value, str):
            return cls.invalid()
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            return cls.invalid()
        if (
            len(encoded) != authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES
            or authorization_sessions.parse_credential(value) is None
        ):
            return cls.invalid()
        return cls(encoded, state="present")

    def consume(self) -> object:
        state = self._state
        self._state = "consumed"
        if state == "absent":
            return ABSENT_CREDENTIAL
        if state != "present" or self._payload is None:
            return INVALID_CREDENTIAL
        try:
            return self._payload.decode("ascii")
        finally:
            for index in range(len(self._payload)):
                self._payload[index] = 0
            self._payload = None

    @property
    def is_absent(self) -> bool:
        """Whether no credential was presented at the protected boundary."""

        return self._state == "absent"

    @property
    def is_invalid(self) -> bool:
        """Whether a credential was presented but failed canonical parsing."""

        return self._state == "invalid"

    def discard(self) -> None:
        """Destroy any retained bytes without materializing an immutable string."""

        if self._payload is not None:
            for index in range(len(self._payload)):
                self._payload[index] = 0
            self._payload = None
        self._state = "consumed"

    def __repr__(self) -> str:
        return f"CredentialCarrier(state={self._state!r})"


class _StdioAuthorizationRequest:
    """FastMCP-compatible empty request carrying one protected stdio bearer."""

    __slots__ = ("authorization_carrier", "scope")

    def __init__(self, carrier: CredentialCarrier) -> None:
        self.authorization_carrier = carrier
        self.scope: dict[str, object] = {}

    @property
    def headers(self) -> dict[str, str]:
        return {}

    def __repr__(self) -> str:
        return "_StdioAuthorizationRequest(headers={})"


@contextmanager
def authorization_carrier_scope(carrier: CredentialCarrier) -> Iterator[None]:
    token = _REQUEST_CARRIER.set(carrier)
    try:
        yield
    finally:
        _REQUEST_CARRIER.reset(token)


def consume_request_authorization_carrier() -> object:
    carrier = _REQUEST_CARRIER.get()
    if carrier is None:
        return INVALID_CREDENTIAL
    return carrier.consume()


def current_request_authorization_carrier() -> CredentialCarrier | None:
    return _REQUEST_CARRIER.get()


@dataclass(frozen=True, slots=True)
class SanitizedMcpRequest:
    body: bytes
    carrier: CredentialCarrier
    tool_name: str | None
    arguments: dict[str, object]


def _pairs_to_value(value: object) -> object:
    if isinstance(value, _ObjectPairs):
        return {key: _pairs_to_value(item) for key, item in value}
    if isinstance(value, list):
        return [_pairs_to_value(item) for item in value]
    return value


def _pair_value(pairs: _ObjectPairs, key: str) -> object:
    matches = [value for candidate, value in pairs if candidate == key]
    return matches[-1] if matches else None


def _pair_values(pairs: _ObjectPairs, key: str) -> list[object]:
    return [value for candidate, value in pairs if candidate == key]


def _sanitize_call(value: _ObjectPairs) -> tuple[object, CredentialCarrier, str | None, dict[str, object]]:
    method_values = _pair_values(value, "method")
    if "tools/call" not in method_values:
        return _pairs_to_value(value), CredentialCarrier.absent(), None, {}

    params_values = _pair_values(value, "params")
    params = params_values[-1] if params_values else None
    if not isinstance(params, _ObjectPairs):
        return _pairs_to_value(value), CredentialCarrier.invalid(), None, {}
    name = _pair_value(params, "name")
    tool_name = name if isinstance(name, str) else None
    arguments_values = _pair_values(params, "arguments")
    arguments = arguments_values[-1] if arguments_values else None
    carrier = CredentialCarrier.absent()
    sanitized_arguments: object = _pairs_to_value(arguments)
    all_argument_objects = [
        argument_object
        for params_object in params_values
        if isinstance(params_object, _ObjectPairs)
        for argument_object in _pair_values(params_object, "arguments")
        if isinstance(argument_object, _ObjectPairs)
    ]
    carrier_values = [
        item
        for argument_object in all_argument_objects
        for key, item in argument_object
        if key == MCP_CREDENTIAL_PARAMETER
    ]
    ambiguous_envelope = (
        len(method_values) != 1
        or len(params_values) != 1
        or len(arguments_values) != 1
    )
    if ambiguous_envelope or len(carrier_values) > 1:
        carrier = CredentialCarrier.invalid()
    elif len(carrier_values) == 1:
        carrier = CredentialCarrier.from_value(carrier_values[0])
    if isinstance(arguments, _ObjectPairs):
        sanitized_arguments = {
            key: _pairs_to_value(item)
            for key, item in arguments
            if key != MCP_CREDENTIAL_PARAMETER
        }

    sanitized_params = {
        key: (
            sanitized_arguments
            if key == "arguments" and item is arguments
            else _pairs_to_value(item)
        )
        for key, item in params
    }
    sanitized = {
        key: sanitized_params if key == "params" and item is params else _pairs_to_value(item)
        for key, item in value
    }
    public_arguments = (
        dict(sanitized_arguments) if isinstance(sanitized_arguments, dict) else {}
    )
    return sanitized, carrier, tool_name, public_arguments


def sanitize_mcp_http_body(raw: bytes) -> SanitizedMcpRequest:
    """Remove the exact MCP bearer placeholder before FastMCP sees the body."""

    if not isinstance(raw, bytes) or len(raw) > _MAX_MCP_JSON_BYTES:
        raise AuthorizationEnvelopeUnavailable
    try:
        parsed = json.loads(raw, object_pairs_hook=_ObjectPairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise AuthorizationEnvelopeUnavailable from None

    if isinstance(parsed, list) and not isinstance(parsed, _ObjectPairs):
        has_tool_call = False
        sanitized_items: list[object] = []
        for item in parsed:
            if isinstance(item, _ObjectPairs):
                sanitized, _carrier, _tool_name, _arguments = _sanitize_call(item)
                sanitized_items.append(sanitized)
                has_tool_call = has_tool_call or "tools/call" in _pair_values(
                    item, "method"
                )
            else:
                sanitized_items.append(_pairs_to_value(item))
        if has_tool_call:
            raise AtomicMcpBatchRefusal
        body = json.dumps(sanitized_items, separators=(",", ":"), ensure_ascii=False).encode()
        return SanitizedMcpRequest(body, CredentialCarrier.absent(), None, {})

    if not isinstance(parsed, _ObjectPairs):
        raise AuthorizationEnvelopeUnavailable
    sanitized, carrier, tool_name, arguments = _sanitize_call(parsed)
    body = json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False).encode()
    return SanitizedMcpRequest(body, carrier, tool_name, arguments)


def sanitize_mcp_stdio_line(line: str) -> SessionMessage:
    """Parse one stdio request only after removing its bearer placeholder."""

    if not isinstance(line, str):
        raise AuthorizationEnvelopeUnavailable
    try:
        raw = line.encode("utf-8")
    except UnicodeEncodeError:
        raise AuthorizationEnvelopeUnavailable from None
    sanitized = sanitize_mcp_http_body(raw)
    try:
        message = mcp.types.JSONRPCMessage.model_validate_json(sanitized.body)
    except (ValueError, TypeError):
        sanitized.carrier.discard()
        raise AuthorizationEnvelopeUnavailable from None
    return SessionMessage(
        message=message,
        metadata=ServerMessageMetadata(
            request_context=_StdioAuthorizationRequest(sanitized.carrier)
        ),
    )


@asynccontextmanager
async def sanitized_stdio_server(
    stdin: anyio.AsyncFile[str] | None = None,
    stdout: anyio.AsyncFile[str] | None = None,
) -> AsyncIterator[tuple[Any, Any]]:
    """MCP stdio transport with bearer removal before SDK request logging."""

    if stdin is None:
        stdin = anyio.wrap_file(
            TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
        )
    if stdout is None:
        stdout = anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))

    read_send, read_receive = anyio.create_memory_object_stream(0)
    write_send, write_receive = anyio.create_memory_object_stream(0)

    async def stdin_reader() -> None:
        try:
            async with read_send:
                async for line in stdin:
                    try:
                        message: SessionMessage | Exception = sanitize_mcp_stdio_line(
                            line
                        )
                    except Exception as error:  # noqa: BLE001 - transport value
                        message = error
                    await read_send.send(message)
        except anyio.ClosedResourceError:  # pragma: no cover - transport shutdown
            await anyio.lowlevel.checkpoint()

    async def stdout_writer() -> None:
        try:
            async with write_receive:
                async for session_message in write_receive:
                    rendered = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await stdout.write(rendered + "\n")
                    await stdout.flush()
        except anyio.ClosedResourceError:  # pragma: no cover - transport shutdown
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdin_reader)
        task_group.start_soon(stdout_writer)
        yield read_receive, write_send


def strip_sensitive_authorization_header(
    headers: list[tuple[bytes, bytes]],
) -> tuple[list[tuple[bytes, bytes]], CredentialCarrier]:
    """Remove the sensitive session header while preserving service auth."""

    values: list[bytes] = []
    sanitized: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        if name.lower() == AUTHORIZATION_SESSION_HEADER:
            values.append(value)
        else:
            sanitized.append((name, value))
    if not values:
        carrier = CredentialCarrier.absent()
    elif len(values) != 1:
        carrier = CredentialCarrier.invalid()
    else:
        try:
            decoded: object = values[0].decode("ascii")
        except UnicodeDecodeError:
            decoded = INVALID_CREDENTIAL
        carrier = (
            CredentialCarrier.invalid()
            if decoded is INVALID_CREDENTIAL
            else CredentialCarrier.from_value(decoded)
        )
    return sanitized, carrier


def read_cli_authorization_fd(fd_spec: str) -> CredentialCarrier:
    """Read at most one canonical bearer from a caller-owned descriptor."""

    if fd_spec == "-":
        descriptor = 0
    elif isinstance(fd_spec, str) and fd_spec.isascii() and fd_spec.isdecimal():
        descriptor = int(fd_spec)
    else:
        return CredentialCarrier.invalid()
    if descriptor < 0:
        return CredentialCarrier.invalid()

    limit = authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES + 1
    payload = bytearray()
    try:
        while len(payload) < limit:
            chunk = os.read(descriptor, limit - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES:
            return CredentialCarrier.invalid()
        try:
            decoded: object = payload.decode("ascii")
        except UnicodeDecodeError:
            decoded = INVALID_CREDENTIAL
        return (
            CredentialCarrier.invalid()
            if decoded is INVALID_CREDENTIAL
            else CredentialCarrier.from_value(decoded)
        )
    except OSError:
        return CredentialCarrier.invalid()
    finally:
        for index in range(len(payload)):
            payload[index] = 0


async def _read_asgi_body(receive: Receive) -> bytes:
    body = bytearray()
    more = True
    while more:
        message = await receive()
        if message.get("type") != "http.request":
            raise AuthorizationEnvelopeUnavailable
        chunk = bytes(message.get("body") or b"")
        if len(body) + len(chunk) > _MAX_MCP_JSON_BYTES:
            raise AuthorizationEnvelopeUnavailable
        body.extend(chunk)
        more = bool(message.get("more_body"))
    return bytes(body)


def _replay_body(body: bytes) -> Receive:
    sent = False

    async def receive() -> ASGIMessage:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _send_refusal(send: Send, *, code: str) -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32000, "message": "authorization request is unavailable"},
        },
        separators=(",", ":"),
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"x-exomem-error-code", code.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class AuthorizationCarrierMiddleware:
    """Strip protected carriers before access logs and framework validation."""

    def __init__(self, app: Any, *, mcp_path: str | None = "/mcp") -> None:
        self.app = app
        self.mcp_path = None if mcp_path is None else (mcp_path.rstrip("/") or "/")

    async def __call__(self, scope: ASGIMessage, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers, header_carrier = strip_sensitive_authorization_header(
            list(scope.get("headers") or [])
        )
        sanitized_scope = {**scope, "headers": headers}
        carrier = header_carrier
        request_receive = receive
        request_path = str(scope.get("path") or "").rstrip("/") or "/"
        if (
            self.mcp_path is not None
            and scope.get("method") == "POST"
            and request_path == self.mcp_path
        ):
            try:
                raw = await _read_asgi_body(receive)
                sanitized = sanitize_mcp_http_body(raw)
            except AtomicMcpBatchRefusal as error:
                await _send_refusal(send, code=error.code)
                return
            except AuthorizationEnvelopeUnavailable as error:
                await _send_refusal(send, code=error.code)
                return
            if not header_carrier.is_absent:
                header_carrier.discard()
                sanitized.carrier.discard()
                await _send_refusal(
                    send, code="AUTHORIZATION_SESSION_UNAVAILABLE"
                )
                return
            carrier = sanitized.carrier
            request_receive = _replay_body(sanitized.body)
            headers = [
                (name, value)
                for name, value in headers
                if name.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(sanitized.body)).encode()))
            sanitized_scope = {**sanitized_scope, "headers": headers}

        with authorization_carrier_scope(carrier):
            await self.app(sanitized_scope, request_receive, send)


class AuthorizationSessionMiddleware(Middleware):
    """Verify MCP capability context before tracing and FunctionTool validation."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp.types.CallToolRequestParams],
        call_next,
    ):
        arguments = dict(context.message.arguments or {})
        request_context = context.fastmcp_context.request_context
        protected_carrier = None
        if request_context is not None:
            request = request_context.request
            candidate = getattr(request, "authorization_carrier", None)
            if isinstance(candidate, CredentialCarrier):
                protected_carrier = candidate
        if MCP_CREDENTIAL_PARAMETER in arguments:
            carrier = CredentialCarrier.from_value(
                arguments.pop(MCP_CREDENTIAL_PARAMETER)
            )
        elif protected_carrier is not None:
            carrier = protected_carrier
        else:
            carrier = current_request_authorization_carrier() or CredentialCarrier.absent()
        try:
            principal = principal_module.resolve_mcp_principal()
            admission = await anyio.to_thread.run_sync(
                partial(
                    verify_authorization_context,
                    self.vault_root,
                    principal=principal,
                    credential=carrier.consume(),
                    now=int(time.time()),
                )
            )
            rule = credential_rule(context.message.name, arguments)
            bound = enforce_credential_rule(admission, rule)
        except AuthorizationContextUnavailable as error:
            raise McpError(ErrorData(code=-32000, message=str(error))) from None
        except AuthorizationRouteUnclassified as error:
            raise McpError(ErrorData(code=-32000, message=str(error))) from None

        sanitized_message = context.message.model_copy(
            update={"arguments": arguments}
        )
        with principal_module.request_scope(bound):
            return await call_next(context.copy(message=sanitized_message))
