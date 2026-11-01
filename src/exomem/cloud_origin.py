"""Gateway-signed login origin for Cloud upload handoffs (bridge-cloud-agent-uploads) and original downloads.

A Cloud cell has no session store and no route to the control plane. The
gateway therefore signs, on every forwarded request, which OAuth login sent
it: tenant, cell, client, grant, login family and resource. A cell configured
with the gateway's public key verifies that proof once, when the request
starts, and binds it to the request. The principal then carries a stable
client binding and origin session, so the existing upload ticket, held file and
capture checks apply unchanged. Without the key, the cell keeps its fixed
owner principal and publishes no upload handoff.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import cloud_cell, cloud_containment

# Version 1 of the protocol; the Substrate gateway signs the same names and formats.
PUBLIC_KEY_ENV = "EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY"
ORIGIN_HEADER = "x-exomem-cloud-origin"
ROUTE_HEADER = "x-exomem-cloud-upload-route"
TICKET_HEADER = "x-exomem-cloud-upload-ticket"
TOKEN_PREFIX = "exomem-cloud-upload:v1:"
MAX_TOKEN_CHARS = 8 * 1024
_ORIGIN_AUDIENCE = "exomem-cloud-origin"
_ROUTE_AUDIENCE = "exomem-cloud-upload-route"
# A proof covers one forwarded request; a route covers one handoff's 900 seconds.
_PROOF_LIFETIME = 60
_ROUTE_LIFETIME = 900
# Gateway and cell clocks are separate hosts in one cluster.
_LEEWAY_SECONDS = 5


@dataclass(frozen=True, slots=True)
class Origin:
    """One OAuth login, as the gateway resolved it for this request."""

    tenant_id: str
    cell_id: str
    client_id: str
    grant_id: str
    family_id: str
    resource: str

    def client_binding(self):
        from .governance.principal import ClientBinding

        return ClientBinding(issuer=self.resource, client_id=self.client_id)

    def session(self):
        from .governance.principal import OriginSessionBinding

        # Refresh keeps a login's family, so this binding is stable across its access tokens.
        return OriginSessionBinding(
            session_id=self.grant_id, generation=self.family_id, audience=self.resource
        )


# Each signed object carries exactly the login's fields, the version and the RFC 7519 claims.
_ORIGIN_FIELDS = tuple(field.name for field in fields(Origin))
_SIGNED_FIELDS = ("v", "iss", "aud", "iat", "exp", "jti")  # nosemgrep: ep-word-set -- RFC 7519 claims and the wire version.
_PROOF_FIELDS = frozenset((*_ORIGIN_FIELDS, *_SIGNED_FIELDS, "purpose"))
_ROUTE_FIELDS = frozenset((*_ORIGIN_FIELDS, *_SIGNED_FIELDS, "upload_url"))


@dataclass(frozen=True, slots=True)
class Route:
    """The routing capsule an upload handoff carries back to the gateway."""

    capsule: str
    upload_url: str
    expires: int


@dataclass(frozen=True, slots=True)
class RequestOrigin:
    origin: Origin
    route: Route | None


_current: ContextVar[RequestOrigin | None] = ContextVar("exomem_cloud_origin", default=None)


@lru_cache(maxsize=8)
def load_public_key(
    pem: str, env_name: str = PUBLIC_KEY_ENV, code: str = "CLOUD_UPLOAD_KEY_INVALID",
) -> Ed25519PublicKey:
    """The Ed25519 verifying key a variable holds, for every signed object a cell checks.

    `code` names the variable's own error, so a bad grant key does not read as a bad upload key.
    """
    try:
        key = serialization.load_pem_public_key(pem.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        key = None
    if not isinstance(key, Ed25519PublicKey):
        # Never echo key material; name only the variable.
        raise cloud_cell.CloudConfigError(code, f"{env_name} must be an Ed25519 public key")
    return key


def _public_key() -> Ed25519PublicKey | None:
    pem = os.environ.get(PUBLIC_KEY_ENV, "").strip()
    return load_public_key(pem) if pem else None


def enabled() -> bool:
    """Whether this Cloud cell is configured to accept gateway upload handoffs."""
    return cloud_cell.cloud_mode_enabled() and _public_key() is not None


def validate_configuration() -> None:
    """Fail startup on a malformed key instead of on the first upload."""
    if cloud_cell.cloud_mode_enabled():
        _public_key()


def signed_claims(
    token: object, *, key: Ed25519PublicKey | None, cell_id: str | None, audience: str,
    fields: frozenset[str], lifetime: int,
) -> dict | None:
    """The claims of an Ed25519 JWT that this cell may act on, or None.

    Every signed object the cell verifies passes the same checks: the `JWT`
    type, the key, the audience, exactly these fields, integer `iat` and `exp`
    within `lifetime`, and the cell's own `cell_id`. A caller adds the checks
    that belong to its own object. A Job has no bearer, so the caller names
    the cell ID instead of this function reading the serving credentials.
    """
    if key is None or cell_id is None or not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        return None
    try:
        if jwt.get_unverified_header(token).get("typ") != "JWT":
            return None
        claims = jwt.decode(
            token, key, algorithms=["EdDSA"], audience=audience,
            options={"require": sorted(fields)}, leeway=_LEEWAY_SECONDS,
        )
    except (jwt.PyJWTError, ValueError, TypeError):
        return None
    if (set(claims) != fields or claims["aud"] != audience
            or type(claims["iat"]) is not int or type(claims["exp"]) is not int
            or not 0 < claims["exp"] - claims["iat"] <= lifetime
            or claims["cell_id"] != cell_id):
        return None
    return claims


def _serving_cell_id() -> str | None:
    try:
        return cloud_cell.CloudCellCredentials.from_env().cell_id
    except cloud_cell.CloudConfigError:
        return None


def _claims(token: object, *, audience: str, fields: frozenset[str], lifetime: int) -> dict | None:
    claims = signed_claims(token, key=_public_key(), cell_id=_serving_cell_id(), audience=audience,
                           fields=fields, lifetime=lifetime)
    if (claims is None or claims["v"] != 1 or claims["iss"] != claims["resource"]
            or not all(cloud_cell.canonical_app_id(claims[name]) for name in ("tenant_id", "grant_id", "family_id"))
            or not all(isinstance(claims[name], str) and claims[name] for name in ("client_id", "resource"))):
        return None
    return claims


def _origin(claims: Mapping[str, Any]) -> Origin:
    return Origin(**{name: claims[name] for name in _ORIGIN_FIELDS})


def verify_request(
    headers: Mapping[str, str], *, ticket: str | None = None, download_path: str | None = None
) -> RequestOrigin | None:
    """Verify the gateway's proof for this request, or None.

    An MCP request needs a proof for MCP and the route capsule for the same
    login. An upload needs a proof that names the exact ticket it carries. A
    download needs a proof that names the exact path it requests; there is no
    ticket, so each GET carries its own proof.
    """
    extra = {"ticket_sha256"} if ticket is not None else {"target_sha256"} if download_path is not None else set()
    proof = _claims(headers.get(ORIGIN_HEADER), audience=_ORIGIN_AUDIENCE,
                    fields=_PROOF_FIELDS | extra, lifetime=_PROOF_LIFETIME)
    if proof is None:
        return None
    if download_path is not None:
        try:
            digest = hashlib.sha256(download_path.encode("utf-8")).hexdigest()
        except UnicodeError:
            return None
        if proof["purpose"] != "download" or proof["target_sha256"] != digest:
            return None
        return RequestOrigin(_origin(proof), None)
    if ticket is not None:
        digest = hashlib.sha256(ticket.encode("ascii")).hexdigest()
        if proof["purpose"] != "upload" or proof["ticket_sha256"] != digest:
            return None
        return RequestOrigin(_origin(proof), None)
    if proof["purpose"] != "mcp":
        return None
    capsule = headers.get(ROUTE_HEADER)
    route = _claims(capsule, audience=_ROUTE_AUDIENCE, fields=_ROUTE_FIELDS, lifetime=_ROUTE_LIFETIME)
    if route is None or _origin(route) != _origin(proof) or not isinstance(route["upload_url"], str):
        return None
    return RequestOrigin(_origin(proof), Route(capsule, route["upload_url"], route["exp"]))


@contextmanager
def bound(request_origin: RequestOrigin | None) -> Iterator[None]:
    token = _current.set(request_origin)
    try:
        yield
    finally:
        _current.reset(token)


def current() -> RequestOrigin | None:
    return _current.get()


def with_origin(who):
    """The Cloud owner principal, bound to this request's verified login when there is one."""
    request_origin = _current.get()
    if request_origin is None or who.issuer_family != cloud_cell.issuer_family(request_origin.origin.cell_id):
        return who
    return replace(
        who,
        client_binding=request_origin.origin.client_binding(),
        origin_session=request_origin.origin.session(),
    )


def matches(who) -> bool:
    """Whether this request's verified login is the one bound to `who`.

    The proof was checked when the request started; the cell cannot observe a
    revocation during a request, so the gateway rechecks around each upload.
    """
    request_origin = _current.get()
    if request_origin is None:
        return False
    origin = request_origin.origin
    return (who.issuer_family == cloud_cell.issuer_family(origin.cell_id)
            and who.client_binding == origin.client_binding()
            and who.origin_session == origin.session())


def handoff_token(route: Route, nonce: str) -> str:
    """The additive Cloud token; lowercase hex keeps the capsule intact through output scrubbing."""
    return TOKEN_PREFIX + route.capsule.encode("ascii").hex() + ":" + nonce


def envelope_nonce(value: object) -> str | None:
    """The ticket nonce of a well-formed Cloud token, or None; the gateway verifies the capsule."""
    if not isinstance(value, str) or len(value) > MAX_TOKEN_CHARS or not value.startswith(TOKEN_PREFIX):
        return None
    parts = value[len(TOKEN_PREFIX):].split(":")
    if len(parts) != 2 or not parts[0] or len(parts[0]) % 2 or len(parts[1]) != 64:
        return None
    try:
        capsule = bytes.fromhex(parts[0])
        nonce = bytes.fromhex(parts[1])
    except ValueError:
        return None
    if capsule.hex() != parts[0] or nonce.hex() != parts[1] or not capsule.isascii():
        return None
    return parts[1]


class CloudOriginMiddleware:
    """Bind each HTTP request's verified gateway origin for the request's duration.

    An MCP request also reports which credential it used (contain-cloud-controller-compromise D1).
    Report mode serves it as before; `enforce` refuses one without a valid gateway proof.
    """

    def __init__(self, app, *, path: str = "/mcp", enforce: bool = cloud_containment.ENFORCE) -> None:
        self.app = app
        self.path = path.rstrip("/") or "/"
        self.enforce = enforce

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import Headers

        headers = Headers(scope=scope)
        path = scope.get("path", "")
        configured = enabled()
        # The upload and download receivers verify their own request-bound proofs.
        request_origin = None if not configured or path in ("/upload", "/download") else verify_request(headers)
        if (path.rstrip("/") or "/") == self.path and not await self._admits(scope, headers, configured, request_origin):
            from starlette.responses import JSONResponse

            await JSONResponse(
                {"code": "CLOUD_PROOF_REQUIRED", "reason": "this request carries no valid gateway proof"},
                status_code=401,
            )(scope, receive, send)
            return
        with bound(request_origin):
            await self.app(scope, receive, send)

    async def _admits(self, scope, headers, configured: bool, request_origin: RequestOrigin | None) -> bool:
        """Report the credential this MCP request used; False only when the proof gate refuses it."""
        from .server_auth import CloudCellAccessToken

        if not configured:
            proof = cloud_containment.Proof.UNCONFIGURED
        elif request_origin is not None:
            proof = cloud_containment.Proof.VALID
        else:
            proof = cloud_containment.Proof.INVALID if ORIGIN_HEADER in headers else cloud_containment.Proof.ABSENT
        # The server's own authentication runs outside this middleware; the user it set is its verdict on the bearer.
        bearer_valid = type(getattr(scope.get("user"), "access_token", None)) is CloudCellAccessToken
        verdict = cloud_containment.judge_request(
            bearer_valid=bearer_valid, proof=proof, cell_id=_serving_cell_id(), enforce=self.enforce)
        return verdict is not cloud_containment.Verdict.REFUSED
