"""Origin-side enforcement of the HA edge's per-request transit stamp.

design.md ("Decision 1 — Edge-transit stamp") describes the failure this
guards against: apex traffic reaching an origin without passing through the
HA edge worker (a stale DNS binding, a path-scoped worker route, a second
tunnel connector, or any future ingress drift). The worker stamps every
request it proxies — reads and mutations alike — with a request id and an
HMAC computed over that id, keyed by the same secret as
``EXOMEM_WRITER_LEASE_TOKEN``. This module is the origin-side half: pure ASGI
middleware that refuses a Cloudflare-transited unsafe-method request lacking a
valid stamp.

It is installed via the ``middleware=`` list passed to ``FastMCP.run()`` (see
``server.run``), which wraps the single Starlette app FastMCP builds for both
the streamable-http MCP endpoint and the REST facade (``server_rest`` and
``server_assets`` register their routes onto the same FastMCP instance via
``custom_route``) — one middleware insertion covers both surfaces.

Enforcement fires only when ALL of the following hold: writer-lease
coordination is enabled and a token is configured, the kill switch
(``EXOMEM_EDGE_STAMP_ENFORCE=0``) is not set, the request carries a
``cf-ray`` header (i.e. it transited Cloudflare — local CLI/REST/health
traffic never does), the method is unsafe (anything other than
GET/HEAD/OPTIONS — the worker classifies and stamps exactly that set,
including the capability-bound transfer PUT), and the ``x-exomem-edge-auth``
header is absent or does not match ``hex(HMAC_SHA256(token, request_id))``
for the presented ``x-exomem-request-id``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from . import cli_ops
from .cli_ops import OpError
from .writer_lease import LeaseConfig

ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]

logger = logging.getLogger(__name__)

INGRESS_BYPASSED_MESSAGE = "request reached the origin without transiting the HA edge"


def compute_edge_auth(token: str, request_id: str) -> str:
    """The worker's HMAC transit proof: hex(HMAC-SHA256(token, request_id))."""
    return hmac.new(
        token.encode("utf-8"), request_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def is_valid_stamp(token: str, request_id: str | None, presented_auth: str | None) -> bool:
    """Whether the presented transit proof verifies for the given request id.

    A missing request id or a missing auth header is invalid regardless of the
    other — an auth header without its paired request id can't be checked, so
    it is treated as invalid rather than silently ignored.
    """
    if not request_id or not presented_auth:
        return False
    # A malformed proof (wrong length, non-hex, non-ASCII bytes decoded via
    # latin-1) is invalid, not an error: compare_digest raises TypeError on
    # non-ASCII str input, which would turn a bad header into a 500 instead of
    # the INGRESS_BYPASSED refusal.
    if re.fullmatch(r"[0-9a-f]{64}", presented_auth) is None:
        return False
    return hmac.compare_digest(presented_auth, compute_edge_auth(token, request_id))


def is_ingress_violation(
    *,
    lease_enabled: bool,
    token: str | None,
    method: str,
    has_cf_ray: bool,
    request_id: str | None,
    presented_auth: str | None,
) -> bool:
    """Pure enforcement predicate (design.md Decision 1), independent of the kill switch.

    True when this request should have carried a valid edge-transit stamp but
    didn't. The kill switch decides what to DO about a violation (refuse vs.
    serve-and-log); it plays no part in whether one occurred.
    """
    if not lease_enabled or not token:
        return False
    if not has_cf_ray:
        return False
    # Exempt only truly-safe methods. The worker stamps every unsafe-method
    # request it proxies (its isMutationCapableRequest includes the transfer
    # PUT), so allow-listing POST here would leave that PUT unenforced.
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    return not is_valid_stamp(token, request_id, presented_auth)


def enforcement_disabled(env: Mapping[str, str] | None = None) -> bool:
    """``EXOMEM_EDGE_STAMP_ENFORCE=0`` (or ``false``) breaks glass without a redeploy."""
    values = os.environ if env is None else env
    return values.get("EXOMEM_EDGE_STAMP_ENFORCE", "").strip().lower() in {"0", "false"}


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _refusal_exception() -> OpError:
    return OpError("INGRESS_BYPASSED", INGRESS_BYPASSED_MESSAGE)


async def _send_refusal(send: Send) -> None:
    err = cli_ops.error_dict(_refusal_exception())
    body = json.dumps(
        cli_ops.envelope(False, error=err), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": cli_ops.http_status_for(err["code"]),
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class EdgeIngressMiddleware:
    """Pure ASGI middleware refusing Cloudflare-transited POSTs without a valid
    edge-transit stamp. See the module docstring for where this is installed.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._refused_count = 0
        self._bypassed_count = 0
        self._config: LeaseConfig | None = None

    async def __call__(self, scope: ASGIMessage, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Lease env is process-lifetime constant; read it once per instance
        # instead of re-parsing ~8 env vars on every request.
        config = self._config
        if config is None:
            config = self._config = LeaseConfig.from_env()
        headers = scope.get("headers") or []
        violation = is_ingress_violation(
            lease_enabled=config.enabled,
            token=config.token,
            method=str(scope.get("method") or ""),
            has_cf_ray=_header_value(headers, b"cf-ray") is not None,
            request_id=_header_value(headers, b"x-exomem-request-id"),
            presented_auth=_header_value(headers, b"x-exomem-edge-auth"),
        )
        if violation:
            # Content-free: only the path and a running counter are logged, never
            # headers or bodies.
            path = str(scope.get("path") or "")
            if enforcement_disabled():
                self._bypassed_count += 1
                logger.warning(
                    "event=edge_ingress_bypassed path=%s count=%s",
                    path,
                    self._bypassed_count,
                )
            else:
                self._refused_count += 1
                logger.warning(
                    "event=edge_ingress_refused path=%s count=%s",
                    path,
                    self._refused_count,
                )
                await _send_refusal(send)
                return

        await self.app(scope, receive, send)
