"""Bearer-free, request-scoped owner admission for consolidation adapters.

Trusted transports call this after verifying their protected session carrier,
before normal argument validation. Neither an ordinary owner principal nor an
unbound library invocation is sufficient. This capability does not acknowledge
a rendered page or provide the separate human confirmation for approval.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn, cast

from . import authorization_custody, authorization_session_lifecycle, consolidation_identity, store
from .authorization_session_lifecycle import AuthorizationSessionContext
from .principal import OWNER_AUDIENCE, RequestPrincipal

_SEAL = object()
_CONTEXT_TTL_SECONDS = 60
_LOCAL_SURFACES = {
    "cli": "cli-local-owner",
    "mcp": "mcp-local-stdio",
    "rest": "rest-api-key",
}


class ConsolidationOwnerUnavailable(RuntimeError):
    """Equivalent refusal for all missing, expired, or cross-bound owners."""

    code = "CONSOLIDATION_OWNER_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation owner is unavailable")

    def as_public_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": str(self), "remediation": None}


def _fail() -> NoReturn:
    raise ConsolidationOwnerUnavailable from None


@dataclass(frozen=True, slots=True, repr=False)
class _OwnerFacts:
    schema: str
    vault_id: str
    cell_id: str
    installation_id: str
    installation_generation: int
    active_fence_digest: str
    root_binding_digest: str
    principal_id: str
    authorization_session_id: str
    credential_generation: int
    purpose: str
    action: str
    issuer_family: str
    surface: str
    issued_at: int
    expires_at: int
    nonce: str
    verifier_fingerprint: str
    human_confirmation: bool = False


class ConsolidationOwnerContext:
    """Opaque internal capability, never a product argument or serialized value."""

    __slots__ = ("__facts", "__seal")
    __facts: _OwnerFacts
    __seal: object

    def __init__(self, facts: _OwnerFacts, *, seal: object) -> None:
        if seal is not _SEAL or type(facts) is not _OwnerFacts:
            _fail()
        object.__setattr__(self, "_ConsolidationOwnerContext__facts", facts)
        object.__setattr__(self, "_ConsolidationOwnerContext__seal", seal)

    def __repr__(self) -> str:
        return "<ConsolidationOwnerContext process-local>"

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("consolidation owner context is immutable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("consolidation owner context is process-local")

    def __reduce_ex__(self, _protocol: object) -> NoReturn:
        raise TypeError("consolidation owner context is process-local")

    def __copy__(self) -> NoReturn:
        raise TypeError("consolidation owner context is process-local")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("consolidation owner context is process-local")

    def _verified_facts(self) -> _OwnerFacts:
        try:
            if self.__seal is _SEAL and type(self.__facts) is _OwnerFacts:
                return self.__facts
        except AttributeError:
            pass
        _fail()


def _session(who: object, *, now: int) -> AuthorizationSessionContext:
    if type(now) is not int or now < 1 or type(who) is not RequestPrincipal:
        _fail()
    principal = cast(RequestPrincipal, who)
    if (
        not principal.resolved
        or principal.audience_id != OWNER_AUDIENCE
        or principal.surface not in _LOCAL_SURFACES
        or principal.issuer_family != _LOCAL_SURFACES[principal.surface]
    ):
        _fail()
    session = principal.verified_authorization_session
    if (
        type(session) is not AuthorizationSessionContext
        or session.principal_id != principal.audience_id
        or session.issuer_family != principal.issuer_family
        or not session.session_id
        or session.session_id != principal.authorization_session_id
        or type(session.expires_at) is not int
        or now >= session.expires_at
        or type(session.credential_generation) is not int
        or session.credential_generation < 1
    ):
        _fail()
    return session


def _action(arguments: object) -> str:
    # No iteration, copying, coercion, hashing, or validation of action fields.
    # A dict subclass is not a transport-decoded envelope.
    from .consolidation_request import ACTIONS, REQUEST_SCHEMA_NAME

    if type(arguments) is not dict:
        _fail()
    schema = arguments.get("schema")
    action = arguments.get("action")
    if (
        type(schema) is not str
        or schema != REQUEST_SCHEMA_NAME
        or type(action) is not str
        or action not in ACTIONS
    ):
        _fail()
    return action


def _durable_session(
    vault_root: Path,
    *,
    principal: RequestPrincipal,
    now: int,
) -> AuthorizationSessionContext:
    """Recheck issuance and current custody; public context fields are not proof."""
    session = _session(principal, now=now)
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(vault_root, now=now)
        connection = store.open_authorization_session_connection(vault_root)
        return authorization_session_lifecycle.status_verified_session(
            connection,
            custody=custody,
            context=session,
            now=now,
        )
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        store.UnsupportedGovernanceSchema,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        _fail()
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _check_identity(
    identity: object,
    session: AuthorizationSessionContext,
) -> consolidation_identity.ConsolidationCellIdentity:
    if type(identity) is not consolidation_identity.ConsolidationCellIdentity:
        _fail()
    checked = cast(consolidation_identity.ConsolidationCellIdentity, identity)
    if (
        checked.schema != consolidation_identity.IDENTITY_SCHEMA
        or checked.vault_id != session.logical_vault_id
        or checked.cell_id != session.cell_id
        or checked.cell_id == checked.vault_id
        or checked.installation_id in {checked.vault_id, checked.cell_id}
        or type(checked.installation_generation) is not int
        or checked.installation_generation < 1
    ):
        _fail()
    return checked


def admit_local_owner(
    vault_root: Path,
    *,
    principal: RequestPrincipal,
    arguments: object,
    now: int,
) -> ConsolidationOwnerContext:
    """Bind a transport-verified local owner session to one exact request action.

    CLI's session must originate in the protected descriptor adapter; the normal
    local-owner default has no verified session and is refused. Loading the
    authenticated identity also verifies the held root's actual OS owner and
    current registration. No adoption, enrollment, or session issuance occurs.
    Hosted entitlement is a separate trust source and cannot enter this adapter.
    """
    session = _durable_session(vault_root, principal=principal, now=now)
    action = _action(arguments)
    try:
        identity = _check_identity(
            consolidation_identity.load_local_identity(Path(vault_root), now=now),
            session,
        )
    except (
        consolidation_identity.ConsolidationIdentityUnavailable,
        authorization_custody.AuthorizationCustodyUnavailable,
        OSError,
        TypeError,
        ValueError,
    ):
        _fail()
    facts = _OwnerFacts(
        schema="ConsolidationOwnerContext/v1",
        vault_id=identity.vault_id,
        cell_id=identity.cell_id,
        installation_id=identity.installation_id,
        installation_generation=identity.installation_generation,
        active_fence_digest=identity.active_fence_digest,
        root_binding_digest=identity.root_binding_digest,
        principal_id=principal.audience_id,
        authorization_session_id=session.session_id,
        credential_generation=session.credential_generation,
        purpose="vault-consolidation",
        action=action,
        issuer_family=session.issuer_family,
        surface=principal.surface,
        issued_at=now,
        expires_at=min(now + _CONTEXT_TTL_SECONDS, session.expires_at),
        nonce=secrets.token_hex(16),
        verifier_fingerprint=hashlib.sha256(identity.machine_key_id.encode("utf-8")).hexdigest(),
    )
    return ConsolidationOwnerContext(facts, seal=_SEAL)


def require_owner_context(
    context: object,
    *,
    principal: RequestPrincipal,
    identity: consolidation_identity.ConsolidationCellIdentity,
    action: str,
    now: int,
) -> _OwnerFacts:
    """Recheck an injected capability against the current trusted binding."""
    session = _session(principal, now=now)
    identity = _check_identity(identity, session)
    if type(context) is not ConsolidationOwnerContext:
        _fail()
    facts = context._verified_facts()
    if (
        not facts.issued_at <= now < facts.expires_at
        or facts.action != action
        or facts.vault_id != identity.vault_id
        or facts.cell_id != identity.cell_id
        or facts.installation_id != identity.installation_id
        or facts.installation_generation != identity.installation_generation
        or facts.active_fence_digest != identity.active_fence_digest
        or facts.root_binding_digest != identity.root_binding_digest
        or facts.principal_id != principal.audience_id
        or facts.authorization_session_id != session.session_id
        or facts.credential_generation != session.credential_generation
        or facts.issuer_family != principal.issuer_family
        or facts.surface != principal.surface
        or facts.verifier_fingerprint
        != hashlib.sha256(identity.machine_key_id.encode("utf-8")).hexdigest()
    ):
        _fail()
    return facts


def bind_local_owner(
    vault_root: Path,
    *,
    principal: RequestPrincipal,
    arguments: object,
    now: int,
) -> RequestPrincipal:
    """Inject the exact owner capability into an already-authenticated principal."""
    context = admit_local_owner(
        vault_root,
        principal=principal,
        arguments=arguments,
        now=now,
    )
    _validate_admitted_request(vault_root, arguments, context._verified_facts())
    return replace(principal, consolidation_owner_context=context)


def require_injected_owner(principal: RequestPrincipal, *, now: int) -> None:
    """Refuse an unbound library call before resolving its selected vault."""
    _session(principal, now=now)
    context = principal.consolidation_owner_context
    if type(context) is not ConsolidationOwnerContext:
        _fail()
    facts = context._verified_facts()
    if not facts.issued_at <= now < facts.expires_at:
        _fail()


def require_local_owner_session(principal: RequestPrincipal, *, now: int) -> None:
    """Check a verified carrier before CLI parsing can inspect action fields."""
    _session(principal, now=now)


def require_bound_request(
    vault_root: Path,
    *,
    principal: RequestPrincipal,
    arguments: object,
    now: int,
) -> None:
    """Revalidate the selected installation before dispatch or writer admission."""
    require_injected_owner(principal, now=now)
    _durable_session(vault_root, principal=principal, now=now)
    action = _action(arguments)
    try:
        identity = consolidation_identity.load_local_identity(vault_root, now=now)
    except (consolidation_identity.ConsolidationIdentityUnavailable, OSError):
        _fail()
    facts = require_owner_context(
        principal.consolidation_owner_context,
        principal=principal,
        identity=identity,
        action=action,
        now=now,
    )
    _validate_admitted_request(vault_root, arguments, facts)


def _validate_admitted_request(vault_root: Path, arguments: object, facts: _OwnerFacts) -> None:
    """Apply the shared semantic contract only after current owner admission."""
    from . import consolidation_request, consolidation_run_state

    request = consolidation_request._validate_request_fields(arguments)  # noqa: SLF001
    mode = None
    if (
        request["action"] == "plan"
        and request["operation"] == "materialize"
        and request["plan_kind"] == "cutover"
    ):
        try:
            record = consolidation_run_state.ConsolidationRunStore(vault_root).load(
                request["run_id"]
            )
        except consolidation_run_state.ConsolidationRunUnavailable:
            raise consolidation_request.ConsolidationRequestUnavailable from None
        identity = record.identity
        if (
            identity.destination_vault_id != facts.vault_id
            or identity.destination_installation_id != facts.installation_id
            or identity.destination_generation != facts.installation_generation
            or identity.destination_fence_digest != facts.active_fence_digest
        ):
            raise consolidation_request.ConsolidationRequestUnavailable
        mode = identity.run_mode
    consolidation_request.validate_request(request, trusted_run_mode=mode)
