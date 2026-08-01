"""Canonical per-request audience resolution — one comparable identity space.

Design decision D5. Every content-returning read resolves a canonical audience
at its *surface boundary* and threads it to the release decision, so a grant
authored against one surface matches the same human arriving over another.
The binding is a contextvar + `request_scope()` — a deliberate clone of
`capabilities.active_surface` / `command_surface.mcp_request_context`, which
already prove the set/reset shape works through the synchronous tool wrapper
and `run_in_threadpool`.

Two invariants this module exists to hold:

- **stdio/CLI is the owner.** A local process talking to its own vault has no
  identity to resolve; that is `OWNER_AUDIENCE`, not a failed resolution. It
  is stated explicitly at the entry point (`owner_principal` / `library_scope`),
  never inferred from an absent binding.
- **unresolved-but-expected fails closed.** When a surface *should* produce an
  identity (a remote HTTP transport, a hosted cell) and cannot, the audience is
  `MOST_RESTRICTIVE_AUDIENCE` — never the owner, and never OPEN. Callers pair
  this with the `Policy.empty` → `Policy.blocked` check: an unresolved
  principal must not be able to reach the open fast path.
- **an ABSENT binding fails closed too.** `effective_principal()` treats "no
  principal bound" as unresolved rather than as the owner, so forgetting a
  `request_scope` costs disclosure rather than granting it.

`purpose` is NOT a surface property — it arrives as a per-call leaf parameter,
so it layers onto a bound principal via `with_purpose()` rather than being
resolved here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

# The vault's own operator: stdio MCP, the CLI, the shared REST key, and any
# in-process/library call with no surface bound.
OWNER_AUDIENCE = "owner"

# The fail-closed floor. Deliberately a reserved id no authored rule can name
# as a grant target (a grant is keyed on `audience`, and this value can never
# be produced by `normalize_audience`), so "deny" cannot be widened by policy.
MOST_RESTRICTIVE_AUDIENCE = "\x00unresolved"

# A stand-in for "whoever the policy does not name" — never bound to a request
# and never resolvable, on the same reserved `\x00` prefix so no document and
# no credential can mint it. Policy review enumerates audiences from the rules
# and grants a proposal touches, which by construction cannot see a change to
# the DEFAULT that applies where no rule matches; this id puts that default in
# the compared lattice so removing a `default_deny` declaration reports as the
# widening it is.
UNNAMED_AUDIENCE_PROBE = "\x00unnamed"

# Surface-scope prefixes whose payload is ALREADY a sha256 over `iss\0sub` —
# the same formula `normalize_audience` uses, so they fold into the shared id
# space by relabelling rather than by re-hashing. `cf-access:` is minted in
# `server_rest._rest_principal`; `principal:` in `command_surface`.
_OAUTH_EQUIVALENT_PREFIXES = ("principal:", "cf-access:")

_REQUEST_PRINCIPAL: ContextVar[RequestPrincipal | None] = ContextVar(
    "exomem_request_principal", default=None
)


@dataclass(frozen=True, slots=True)
class RequestPrincipal:
    """Who is asking, in the one id space the release decision compares against."""

    audience_id: str
    surface: str = "library"
    session_id: str | None = None
    authorization_session_id: str | None = None
    purpose: str | None = None
    resolved: bool = True

    def with_purpose(self, purpose: str | None) -> RequestPrincipal:
        """Layer a per-call declared purpose on without mutating the binding."""
        if purpose is None:
            return self
        return replace(self, purpose=purpose)

    def with_authorization_session(self, handle: str | None) -> RequestPrincipal:
        """Bind the explicit client-conversation authorization identity."""
        return replace(self, authorization_session_id=handle)


def normalize_audience(*, subject: Any, issuer: Any = None) -> str:
    """Map an identity claim pair into the single comparable audience id space.

    An empty subject is not an identity: it returns the fail-closed floor so a
    caller that forgets to check `resolved` still cannot land on the owner.
    """
    clean_subject = str(subject or "").strip()
    if not clean_subject:
        return MOST_RESTRICTIVE_AUDIENCE
    clean_issuer = str(issuer or "").strip() or "verified-principal"
    digest = hashlib.sha256(f"{clean_issuer}\0{clean_subject}".encode()).hexdigest()
    return f"principal:{digest}"


def _canonical_scope(scope: str) -> str:
    """Fold an already-minted per-surface principal scope into the id space."""
    clean = scope.strip()
    if not clean:
        return MOST_RESTRICTIVE_AUDIENCE
    for prefix in _OAUTH_EQUIVALENT_PREFIXES:
        if clean.startswith(prefix):
            payload = clean[len(prefix) :].strip()
            return f"principal:{payload}" if payload else MOST_RESTRICTIVE_AUDIENCE
    # An opaque surface scope (a hosted gateway scope, an MCP bearer digest):
    # stable and comparable to itself, distinct from every other scope.
    return normalize_audience(subject=clean, issuer="surface-scope")


def owner_principal(*, surface: str = "cli", purpose: str | None = None) -> RequestPrincipal:
    """The local operator: stdio MCP, the CLI, the vault's own REST key."""
    return RequestPrincipal(
        audience_id=OWNER_AUDIENCE, surface=surface, purpose=purpose, resolved=True
    )


def most_restrictive_principal(*, surface: str) -> RequestPrincipal:
    """Identity was expected and did not resolve — deny, do not open."""
    return RequestPrincipal(
        audience_id=MOST_RESTRICTIVE_AUDIENCE, surface=surface, resolved=False
    )


def _mcp_identity_claims() -> tuple[dict[str, Any] | None, str | None]:
    """`(claims, expectation)` for the live FastMCP request.

    `expectation` is `"authenticated"` when the transport says an identity
    SHOULD be resolvable (a remote HTTP request) but none was — the
    fail-closed trigger. A plain stdio invocation yields `(None, None)`,
    which is the owner. Separated from `resolve_mcp_principal` so tests pin
    the resolution contract without pinning FastMCP internals.
    """
    try:
        from fastmcp.server.dependencies import get_access_token, get_http_headers
    except ImportError:  # pragma: no cover - fastmcp is a hard dependency
        return None, None

    try:
        access_token = get_access_token()
    except (LookupError, RuntimeError):
        access_token = None
    if access_token is not None:
        claims = getattr(access_token, "claims", None) or {}
        if str(claims.get("sub") or "").strip():
            return dict(claims), None
        # An auth provider is in play but produced no subject.
        return None, "authenticated"

    try:
        headers = get_http_headers(include={"authorization"})
    except (LookupError, RuntimeError):
        return None, None
    if not headers:
        # No HTTP request bound at all -> local stdio transport -> owner.
        return None, None
    authorization = str(headers.get("authorization", "")).strip()
    scheme, separator, credential = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and credential.strip():
        return {"sub": credential.strip(), "iss": "bearer"}, None
    return None, "authenticated"


def resolve_mcp_principal() -> RequestPrincipal:
    """MCP boundary: OAuth principal, bearer credential, or local-stdio owner."""
    claims, expectation = _mcp_identity_claims()
    if claims is not None:
        audience = normalize_audience(subject=claims.get("sub"), issuer=claims.get("iss"))
        if audience != MOST_RESTRICTIVE_AUDIENCE:
            return RequestPrincipal(audience_id=audience, surface="mcp", resolved=True)
        return most_restrictive_principal(surface="mcp")
    if expectation is not None:
        return most_restrictive_principal(surface="mcp")
    return owner_principal(surface="mcp")


def resolve_rest_principal(principal_scope: str | None) -> RequestPrincipal:
    """REST boundary: `None` is the vault's own shared key, i.e. the owner.

    `server_rest._rest_principal` returns `(True, None)` only after a
    constant-time match against `EXOMEM_REST_API_KEY` (or an upload token
    minted from it) — that credential *is* the owner's. A CF-Access scope
    carries a real third-party identity and folds into the OAuth id space.
    """
    if principal_scope is None:
        return owner_principal(surface="rest")
    audience = _canonical_scope(principal_scope)
    if audience == MOST_RESTRICTIVE_AUDIENCE:
        return most_restrictive_principal(surface="rest")
    return RequestPrincipal(audience_id=audience, surface="rest", resolved=True)


def resolve_hosted_principal(principal_scope: str | None) -> RequestPrincipal:
    """Hosted cell boundary: the gateway always supplies a validated scope.

    Unlike REST, a missing scope here is never the owner — a hosted cell is
    reached only through the gateway, so absence means the expected identity
    did not resolve.
    """
    if principal_scope is None or not str(principal_scope).strip():
        return most_restrictive_principal(surface="hosted")
    audience = _canonical_scope(str(principal_scope))
    if audience == MOST_RESTRICTIVE_AUDIENCE:
        return most_restrictive_principal(surface="hosted")
    return RequestPrincipal(audience_id=audience, surface="hosted", resolved=True)


def current_principal() -> RequestPrincipal | None:
    """The principal bound by this invocation's surface, if any."""
    return _REQUEST_PRINCIPAL.get()


def effective_principal() -> RequestPrincipal:
    """The bound principal, or the fail-closed floor when nothing bound one.

    This default is the single most load-bearing line in the module, and it
    used to read `owner_principal(...)`. From inside this function, "no
    surface adapter ran" and "a surface adapter ran and forgot to bind" are
    the *same observation* — and the second one is a total bypass of the
    release plane, because every consumer that reaches here is deciding what
    to disclose. An implicit owner default means any new code path, any
    hand-registered handler, any helper invoked off the dispatcher gets full
    disclosure by omission. That is fail-open by construction.

    So the unbound reading is `most_restrictive_principal`. Genuine
    owner-local, in-process callers — the CLI's own entry point, an embedding
    library caller inside the owner's process — declare themselves with
    `library_scope()`. Declaring is cheap; the failure mode of not declaring
    is now an empty result instead of a leak.
    """
    bound = _REQUEST_PRINCIPAL.get()
    if bound is not None:
        return bound
    return most_restrictive_principal(surface="unbound")


@contextmanager
def request_scope(principal: RequestPrincipal) -> Iterator[None]:
    """Bind one principal for a nested, task-local surface invocation."""
    token = _REQUEST_PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _REQUEST_PRINCIPAL.reset(token)


@contextmanager
def library_scope() -> Iterator[None]:
    """Declare an owner-local, in-process caller — the explicit form of what
    `effective_principal()` used to assume.

    Deliberately a no-op when a surface principal is already bound: a library
    helper running *underneath* a remote request must never escalate that
    request back to the owner, which would reintroduce the same fail-open
    hole one frame deeper.
    """
    if _REQUEST_PRINCIPAL.get() is not None:
        yield
        return
    with request_scope(owner_principal(surface="library")):
        yield
