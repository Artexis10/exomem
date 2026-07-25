"""Pure-ASGI access log: one structured record per HTTP request.

Modeled on `edge_ingress.py`'s pure-ASGI middleware style so it can be
installed in the same `middleware=` list FastMCP passes to uvicorn/Starlette,
without depending on FastMCP's own request lifecycle. It resolves one request
id per request — from the inbound `x-exomem-request-id` header when it is
UUIDv4-shaped, else a freshly minted one — injects it as a request header
(so downstream MCP tool code resolves the SAME id via
`command_surface.mcp_request_id()`) and as a response header (so a client or
operator can correlate their own logs), and logs one record after the
response completes.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .command_surface import canonical_request_id
from .log_events import log_event

ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]

logger = logging.getLogger("exomem.access")

_REQUEST_ID_HEADER = b"x-exomem-request-id"
_SESSION_ID_HEADER = b"mcp-session-id"
_CF_RAY_HEADER = b"cf-ray"


def access_log_disabled(env: dict[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("EXOMEM_DISABLE_ACCESS_LOG", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class AccessLogMiddleware:
    """One structured `event=http_request` record per HTTP request."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: ASGIMessage, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or access_log_disabled():
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers") or [])
        request_id = canonical_request_id(_header_value(headers, _REQUEST_ID_HEADER))
        if request_id is None:
            request_id = str(uuid.uuid4())
            headers = [*headers, (_REQUEST_ID_HEADER, request_id.encode("latin-1"))]
            scope = {**scope, "headers": headers}

        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        session_id = _header_value(headers, _SESSION_ID_HEADER)
        cf_ray = _header_value(headers, _CF_RAY_HEADER)
        client = scope.get("client")
        client_ip = client[0] if client else None

        response_status: dict[str, int] = {}

        async def send_with_correlation(message: ASGIMessage) -> None:
            if message.get("type") == "http.response.start":
                response_status["status"] = int(message.get("status", 0))
                response_headers = [
                    *(message.get("headers") or []),
                    (_REQUEST_ID_HEADER, request_id.encode("latin-1")),
                ]
                message = {**message, "headers": response_headers}
            await send(message)

        t0 = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_correlation)
        finally:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            fields: dict[str, Any] = {
                "method": method,
                "status": response_status.get("status"),
                "duration_ms": duration_ms,
                "request_id": request_id,
            }
            if session_id:
                fields["session_id"] = session_id
            if cf_ray:
                fields["cf_ray"] = cf_ray
            # The raw path is client-controlled text (any URL a client requests
            # is logged, including 404 probes), so it is content-classified:
            # kept locally, dropped by the hosted privacy boundary. Bounded so
            # an oversized URL cannot bloat the log line.
            content: dict[str, Any] = {"path": path[:512]}
            if client_ip:
                content["client_ip"] = client_ip
            log_event(
                logger,
                logging.INFO,
                "http_request",
                fields=fields,
                content=content,
            )
            try:
                from . import metrics

                metrics.inc_counter(
                    "exomem_http_requests_total",
                    {"status": str(response_status.get("status") or 0)},
                )
            except Exception:  # noqa: BLE001 - observability must never break a request
                pass
