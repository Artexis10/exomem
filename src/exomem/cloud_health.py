"""The cell's health-only listener: `GET /health/ready` on a port of its own.

The cell's main port carries MCP and every bearer-authenticated route, so a
network policy that admits a peer to it admits that peer to all of them. The
platform operator needs only the readiness answer, so a Cloud cell started
with `EXOMEM_CLOUD_HEALTH_PORT` also serves the same readiness handler on that
port and answers every other request there with 404. Local installs never
start it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Awaitable, Callable

import uvicorn
from starlette.requests import Request
from starlette.responses import Response

from .server_assets import READINESS_PATH

Readiness = Callable[[Request], Awaitable[Response]]


class _ReadinessOnly:
    """ASGI app that admits exactly `GET /health/ready` and refuses the rest.

    A routed Starlette app would also answer HEAD and redirect a trailing
    slash; this listener answers one method on one path.
    """

    def __init__(self, readiness: Readiness) -> None:
        self._readiness = readiness

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        if scope["method"] == "GET" and scope["path"] == READINESS_PATH:
            response = await self._readiness(Request(scope, receive))
        else:
            response = Response(status_code=404)
        await response(scope, receive, send)


class _HealthServer(uvicorn.Server):
    # The main server owns the process signals; this one stops with the lifespan.
    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


class HealthListener:
    """Serves the readiness handler on its own port for the life of the server.

    Built before the FastMCP app so its lifespan can wrap the app's, and given
    the readiness handler once the app has registered it.
    """

    def __init__(self, *, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._readiness: Readiness | None = None

    def serve(self, readiness: Readiness) -> None:
        self._readiness = readiness

    def lifespan(self, inner):
        @contextlib.asynccontextmanager
        async def _lifespan(server):
            if self._readiness is None:
                raise RuntimeError("the health-only listener has no readiness handler")
            # Bound here, so a taken port fails startup instead of exiting
            # from inside uvicorn's own bind.
            sock = socket.create_server((self._host, self._port))
            health = _HealthServer(
                uvicorn.Config(
                    _ReadinessOnly(self._readiness),
                    lifespan="off",
                    ws="none",
                    proxy_headers=False,
                    reset_contextvars=True,
                    timeout_graceful_shutdown=2,
                    # Logging is process-wide: a log config here would rerun
                    # dictConfig, and `access_log=False` would also silence
                    # the main server's access logger.
                    log_config=None,
                )
            )
            serving = asyncio.create_task(health.serve(sockets=[sock]))
            try:
                async with inner(server) as state:
                    yield state
            finally:
                health.should_exit = True
                try:
                    await serving
                finally:
                    sock.close()

        return _lifespan
