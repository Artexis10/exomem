"""Transport-level ASGI behavior for the public MCP endpoint."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]

_SSE_CONTENT_TYPE = b"text/event-stream"
_INITIAL_SSE_COMMENT = b": stream-ready\r\n\r\n"


class PrimeMcpSSEMiddleware:
    """Flush the standalone MCP SSE stream before its first keepalive.

    Some reverse proxies buffer an SSE response until body bytes arrive. The
    SDK's standalone GET stream is initially idle and its first ping is 15
    seconds later, which can serialize an authenticated client's first request
    behind that wait. An SSE comment is protocol-neutral and forces an
    immediate flush without becoming an MCP message.
    """

    def __init__(self, app: Any, *, path: str = "/mcp") -> None:
        self.app = app
        self.path = path.rstrip("/") or "/"

    async def __call__(self, scope: ASGIMessage, receive: Receive, send: Send) -> None:
        if not self._is_standalone_stream(scope):
            await self.app(scope, receive, send)
            return

        async def send_with_prime(message: ASGIMessage) -> None:
            await send(message)
            if message.get("type") != "http.response.start":
                return
            if int(message.get("status", 0)) != 200:
                return
            headers = message.get("headers") or []
            content_type = next(
                (value for name, value in headers if name.lower() == b"content-type"),
                b"",
            )
            if _SSE_CONTENT_TYPE not in content_type.lower():
                return
            await send(
                {
                    "type": "http.response.body",
                    "body": _INITIAL_SSE_COMMENT,
                    "more_body": True,
                }
            )

        await self.app(scope, receive, send_with_prime)

    def _is_standalone_stream(self, scope: ASGIMessage) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "GET":
            return False
        request_path = str(scope.get("path") or "").rstrip("/") or "/"
        if request_path != self.path:
            return False
        headers = scope.get("headers") or []
        accept = next(
            (value for name, value in headers if name.lower() == b"accept"),
            b"",
        )
        return _SSE_CONTENT_TYPE in accept.lower()
