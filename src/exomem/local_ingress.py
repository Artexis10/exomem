"""Worker half of authenticated local ingress (`add-authenticated-local-ingress`).

A managed worker only ever sees its supervisor's private socket, so it cannot
tell a same-machine client from a tunnelled one by address. The supervisor's
loopback listener says so instead: it forwards admitted requests with
`x-exomem-internal-ingress: local` and the manager's per-process proof, and
strips every inbound `x-exomem-internal-*` header on both listeners.

`LocalIngressMiddleware` honours that stamp only on a supervisor-owned worker
whose manager gave it the proof. A stamped request must then present a local
client session (the local issuer and audience below); nothing else, not an
OAuth session, the REST key or the upload token, is accepted there. A request
with no stamp is passed on untouched, so the public path is unchanged.

A verified local credential binds a request-local grant. The OAuth proxy's
`load_access_token` answers from it (and only for that exact bearer), principal
resolution turns it into the `owner-local` principal, and REST and `/upload`
accept it. Tokens give attribution and revocation, not isolation: a process
running as the same user can read the token file.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, ClassVar

from fastmcp.server.auth.auth import AccessToken

from .auth_sessions import SessionStoreUnavailable
from .service_ingress import (
    INGRESS_KEY_ENV,
    INGRESS_PROOF_HEADER,
    INGRESS_STAMP_HEADER,
    INTERNAL_HEADER_PREFIX,
    local_refusal,
)

logger = logging.getLogger(__name__)

#: The local door's issuer and audience. Neither can be a public base URL, so
#: the public authority refuses every local session and vice versa.
LOCAL_ISSUER = "urn:exomem:local-ingress"
LOCAL_AUDIENCE = "urn:exomem:local-ingress/mcp"
LOCAL_SCOPES = ("exomem:read", "exomem:write")


class LocalIngressAccessToken(AccessToken):
    """An access token only the local-ingress branch constructs.

    Deliberately not an `ExomemSessionAccessToken`: it can never satisfy the
    remote-owner provenance rule.
    """

    EXOMEM_LOCAL_INGRESS_PROVENANCE: ClassVar[bool] = True


@dataclass(frozen=True)
class LocalGrant:
    """One verified local credential, bound for the lifetime of its request."""

    bearer: str = field(repr=False)
    access_token: LocalIngressAccessToken = field(repr=False)
    client_id: str
    session_id: str


_GRANT: ContextVar[LocalGrant | None] = ContextVar("exomem_local_ingress_grant", default=None)

#: The manager's proof, taken out of the environment once at worker start.
_PROOF_KEY: str | None = None


def claim_proof_key() -> None:
    """Take the manager's proof out of this process's environment.

    Called once, first thing in a managed worker, so no descendant (the media
    worker, a converter subprocess) inherits a credential meant for this
    process alone.
    """
    global _PROOF_KEY
    _PROOF_KEY = os.environ.pop(INGRESS_KEY_ENV, "").strip() or None


def current_grant() -> LocalGrant | None:
    """The live request's verified local grant, or None off local ingress."""
    return _GRANT.get()


def access_token_for(bearer: str) -> LocalIngressAccessToken | None:
    """The grant's token for exactly this bearer; None for any other one."""
    grant = _GRANT.get()
    if grant is None:
        return None
    if not hmac.compare_digest(grant.bearer.encode("utf-8"), bearer.encode("utf-8")):
        return None
    return grant.access_token


def is_local_grant_token(token: object) -> bool:
    """Provenance: this request's live grant, and the object it created."""
    grant = _GRANT.get()
    return (
        grant is not None
        and token is grant.access_token
        and getattr(type(token), "EXOMEM_LOCAL_INGRESS_PROVENANCE", False) is True
    )


class LocalCredentialVerifier:
    """Validate a bearer against the local issuer and audience only."""

    def __init__(self, authority: Any = None) -> None:
        self._authority = authority
        self._reported_unavailable = False

    def _session_authority(self) -> Any:
        if self._authority is None:
            from .server_auth import build_local_session_authority

            self._authority = build_local_session_authority()
        return self._authority

    async def verify(self, bearer: str) -> LocalGrant | None:
        try:
            authority = self._session_authority()
        except (RuntimeError, ValueError):
            if not self._reported_unavailable:
                self._reported_unavailable = True
                logger.warning("event=local_ingress_unavailable reason=session_authority")
            return None
        record = await authority.validate(bearer)
        if record is None or record.issuer != LOCAL_ISSUER or record.audience != LOCAL_AUDIENCE:
            return None
        token = LocalIngressAccessToken(
            token=bearer,
            client_id=record.client_id,
            scopes=list(record.scopes),
            expires_at=None,
            resource=LOCAL_AUDIENCE,
            claims={
                "sub": f"local:{record.session_id}",
                "iss": LOCAL_ISSUER,
                "aud": LOCAL_AUDIENCE,
                "client_id": record.client_id,
                "ingress": "local",
            },
        )
        return LocalGrant(
            bearer=bearer,
            access_token=token,
            client_id=record.client_id,
            session_id=record.session_id,
        )


def note_owner_credential(credential: str, headers: Any) -> None:
    """Log owner static-credential use on a Cloudflare-transited request.

    Measurement only: whether any remote client still uses the owner REST key
    or the static upload token decides when they can be refused through the
    tunnel. Nothing is refused here, and nothing but the credential's kind is
    recorded. Log-only on purpose: the metrics registry is served
    unauthenticated at `/metrics.json`, which the tunnel reaches.
    """
    try:
        if headers.get("cf-ray") is None:
            return
        logger.info("event=owner_credential_transit credential=%s transit=cloudflare", credential)
    except Exception:  # noqa: BLE001 - observability must never break a request
        pass


def _values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for key, value in headers if key.lower() == name]


def _bearer(headers: list[tuple[bytes, bytes]]) -> str | None:
    values = _values(headers, b"authorization")
    if len(values) != 1:
        return None
    try:
        scheme, _, credential = values[0].decode("ascii").partition(" ")
    except UnicodeDecodeError:
        return None
    # The MCP SDK takes everything after `Bearer ` as the token, whitespace
    # included. Refusing padding here keeps the gate and the SDK reading the
    # same token, so no request passes one and fails the other.
    if credential != credential.strip():
        return None
    return credential if scheme.lower() == "bearer" and credential else None


async def _respond(
    send: Any, status: int, payload: dict[str, str], headers: tuple = ()
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _refused(reason: str) -> None:
    # Content-free: a reason code, never a header, token, path or body.
    logger.warning("event=local_ingress_refused where=worker reason=%s", reason)


class LocalIngressMiddleware:
    """Pure ASGI gate for stamped requests; inert for every other request.

    Installed first in the OAuth proxy's middleware (so it runs before FastMCP
    authentication) and at the front of a no-auth worker's own list. It
    removes the stamp from the scope in place, so a second instance, and
    everything downstream, never sees one.
    """

    def __init__(
        self,
        app: Any,
        *,
        verifier: LocalCredentialVerifier | None = None,
        proof_key: str | None = None,
    ) -> None:
        self.app = app
        self.verifier = verifier or LocalCredentialVerifier()
        key = (proof_key if proof_key is not None else _PROOF_KEY or "").strip()
        self._proof_key = key.encode("ascii", "replace") if key else None

    def _proven(self, stamps: list[bytes], proofs: list[bytes]) -> bool:
        if self._proof_key is None or stamps != [b"local"] or len(proofs) != 1:
            return False
        return hmac.compare_digest(proofs[0], self._proof_key)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers") or [])
        stamps = _values(headers, INGRESS_STAMP_HEADER)
        proofs = _values(headers, INGRESS_PROOF_HEADER)
        if not stamps and not proofs:
            await self.app(scope, receive, send)
            return

        # In place, so the request object FastMCP captured sees it too.
        scope["headers"] = [
            (name, value)
            for name, value in headers
            if not name.lower().startswith(INTERNAL_HEADER_PREFIX)
        ]
        if not self._proven(stamps, proofs):
            _refused("unproven_stamp")
            await _respond(send, 403, {"error": "local_ingress_refused", "reason": "stamp"})
            return
        reason = local_refusal(scope)
        if reason is not None:
            _refused(reason)
            await _respond(
                send,
                404 if reason == "path" else 403,
                {"error": "local_ingress_refused", "reason": reason},
            )
            return
        path = str(scope.get("path") or "")
        if path == "/health" or path.startswith("/health/"):
            await self.app(scope, receive, send)
            return

        bearer = _bearer(scope["headers"])
        if bearer is None:
            _refused("missing_credential")
            await _respond(
                send,
                401,
                {"error": "invalid_request", "error_description": "a local client token is required"},
                ((b"www-authenticate", b"Bearer"),),
            )
            return
        try:
            grant = await self.verifier.verify(bearer)
        except SessionStoreUnavailable:
            logger.warning("event=local_ingress_unavailable reason=session_store")
            await _respond(
                send,
                503,
                {"error": "temporarily_unavailable"},
                ((b"retry-after", b"5"),),
            )
            return
        if grant is None:
            _refused("invalid_credential")
            await _respond(
                send,
                401,
                {"error": "invalid_token", "error_description": "a local client token is required"},
                ((b"www-authenticate", b'Bearer error="invalid_token"'),),
            )
            return

        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        from starlette.authentication import AuthCredentials

        user = AuthenticatedUser(grant.access_token)
        scope["user"] = user
        scope["auth"] = AuthCredentials(list(grant.access_token.scopes))
        grant_token = _GRANT.set(grant)
        user_token = auth_context_var.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            auth_context_var.reset(user_token)
            _GRANT.reset(grant_token)
