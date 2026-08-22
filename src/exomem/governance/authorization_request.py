"""Closed credential routing and bearer-free trusted request context."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from . import authorization_custody, authorization_session_lifecycle, store
from .principal import RequestPrincipal


class CredentialRule(StrEnum):
    """How one exact route variant admits authorization-session credentials."""

    FORBIDDEN = "forbidden"
    REQUIRED = "required"
    OPTIONAL = "optional"
    NON_AUTHORIZING = "non_authorizing"


class AuthorizationRouteUnclassified(RuntimeError):
    """A command or finite selector has no credential-matrix row."""

    code = "AUTHORIZATION_ROUTE_UNCLASSIFIED"

    def __init__(self) -> None:
        super().__init__("authorization route is unavailable")


class AuthorizationContextUnavailable(RuntimeError):
    """Content-free refusal for absent, invalid, or unavailable session context."""

    code = "AUTHORIZATION_SESSION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("authorization session is unavailable")

    def as_public_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": str(self), "remediation": None}


ABSENT_CREDENTIAL: Final = object()
INVALID_CREDENTIAL: Final = object()

_SELF_INSPECTION_OPERATIONS = frozenset({"list", "explain", "simulate"})
_OWNER_OPERATIONS = frozenset({"propose", "commit", "suspend", "resume", "undo"})
_SESSION_ACTION_RULES: Mapping[str, CredentialRule] = {
    "open": CredentialRule.FORBIDDEN,
    "status": CredentialRule.REQUIRED,
    "rotate": CredentialRule.REQUIRED,
    "close": CredentialRule.REQUIRED,
}
_OPTIONAL_COMMANDS = frozenset(
    {
        "add",
        "adopt",
        "adopt_vault",
        "adoption_studio",
        "append_to_file",
        "ask_memory",
        "attention",
        "audit",
        "audit_fix",
        "bootstrap",
        "browse_memory",
        "capture_source",
        "compile_source",
        "connect_memory",
        "coordination_status",
        "create_file",
        "delete",
        "edit",
        "edit_memory",
        "evolution",
        "fetch",
        "find",
        "get",
        "get_video_frames",
        "graph_context",
        "link",
        "list_directory",
        "list_inbound_links",
        "list_trash",
        "maintain_memory",
        "manage_memory_file",
        "move_file",
        "note",
        "observe_memory",
        "overview",
        "plan_memory",
        "preserve",
        "preserve_artifacts",
        "preserve_evidence",
        "process_media",
        "propose_compilation",
        "provenance_report",
        "query_data",
        "query_dataset",
        "read_media",
        "read_memory",
        "reconcile",
        "record_memory",
        "recover_from_trash",
        "remember",
        "replace",
        "replace_memory",
        "review_item_context",
        "review_memory",
        "schema_memory",
        "search",
        "suggest_links",
        "suggest_relations",
        "transfer_artifact",
        "triage_memory",
    }
)
_GOVERNANCE_OPERATIONS = frozenset(
    _SELF_INSPECTION_OPERATIONS
    | _OWNER_OPERATIONS
    | {"grant", "revoke", "declare", "backfill_companion", "session"}
)


@dataclass(frozen=True, slots=True)
class AuthorizationAdmission:
    """Bearer-free result of transport verification before selector validation."""

    principal: RequestPrincipal
    credential_present: bool


def credential_rule(
    command_name: str,
    arguments: Mapping[str, object],
) -> CredentialRule:
    """Return the one closed credential rule for a command selector variant."""

    if command_name != "govern_memory":
        if command_name in _OPTIONAL_COMMANDS:
            return CredentialRule.OPTIONAL
        raise AuthorizationRouteUnclassified

    operation = arguments.get("operation")
    if not isinstance(operation, str):
        raise AuthorizationRouteUnclassified
    if operation == "session":
        action = arguments.get("session_action")
        if not isinstance(action, str):
            raise AuthorizationRouteUnclassified
        try:
            return _SESSION_ACTION_RULES[action]
        except KeyError:
            raise AuthorizationRouteUnclassified from None
    if operation in {"grant", "revoke"}:
        scope = arguments.get("scope")
        if scope in (None, "session"):
            return CredentialRule.REQUIRED
        if scope == "standing":
            return CredentialRule.NON_AUTHORIZING
        raise AuthorizationRouteUnclassified
    if operation == "declare":
        return CredentialRule.REQUIRED
    if operation in _SELF_INSPECTION_OPERATIONS:
        return CredentialRule.OPTIONAL
    if operation in _OWNER_OPERATIONS:
        return CredentialRule.NON_AUTHORIZING
    if operation == "backfill_companion":
        if arguments.get("backfill_action") in {"preview", "commit"}:
            return CredentialRule.NON_AUTHORIZING
        raise AuthorizationRouteUnclassified
    raise AuthorizationRouteUnclassified


def validate_credential_registry(
    command_names: Iterable[str] | None = None,
) -> None:
    """Fail startup when the generated/legacy registry drifts from closed rows."""

    from .. import commands

    if command_names is None:
        names = frozenset(
            command.name
            for command in (*commands.PRODUCT_COMMANDS, *commands.COMMANDS)
        )
    else:
        names = frozenset(command_names)
    if names != _OPTIONAL_COMMANDS | {"govern_memory"}:
        raise AuthorizationRouteUnclassified

    govern = next(
        (command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory"),
        None,
    )
    if govern is None:
        raise AuthorizationRouteUnclassified
    choices = {
        parameter.name: frozenset(parameter.choices)
        for parameter in govern.params
        if parameter.choices
    }
    if (
        choices.get("operation") != _GOVERNANCE_OPERATIONS
        or choices.get("session_action") != frozenset(_SESSION_ACTION_RULES)
        or choices.get("backfill_action") != frozenset({"preview", "commit"})
    ):
        raise AuthorizationRouteUnclassified


def _without_session(principal: RequestPrincipal) -> RequestPrincipal:
    issuer_family = principal.issuer_family
    if not principal.resolved or not isinstance(issuer_family, str) or not issuer_family:
        raise AuthorizationContextUnavailable
    return principal.with_verified_authorization_session(
        None,
        issuer_family=issuer_family,
    )


def verify_authorization_context(
    vault_root: Path,
    *,
    principal: RequestPrincipal,
    credential: object,
    now: int,
) -> AuthorizationAdmission:
    """Verify a consumed bearer before ordinary body or argument validation."""

    has_credential = credential is not ABSENT_CREDENTIAL
    if credential is INVALID_CREDENTIAL:
        raise AuthorizationContextUnavailable

    clean = _without_session(principal)
    if not has_credential:
        return AuthorizationAdmission(clean, False)

    connection: object | None = None
    try:
        custody = authorization_custody.load_authorization_custody(
            Path(vault_root),
            now=now,
        )
        connection = store.open_authorization_session_connection(Path(vault_root))
        context = authorization_session_lifecycle.resume_session(
            connection,  # type: ignore[arg-type]
            custody=custody,
            bearer=credential,
            principal_id=clean.audience_id,
            issuer_family=clean.issuer_family or "",
            now=now,
        )
        return AuthorizationAdmission(
            clean.with_verified_authorization_session(
                context,
                issuer_family=context.issuer_family,
            ),
            True,
        )
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
        TypeError,
        ValueError,
    ):
        raise AuthorizationContextUnavailable from None
    finally:
        if connection is not None:
            try:
                connection.close()  # type: ignore[attr-defined]
            except (AttributeError, sqlite3.Error):
                pass


def enforce_credential_rule(
    admission: AuthorizationAdmission,
    rule: CredentialRule,
) -> RequestPrincipal:
    """Apply one route's presence rule after transport verification."""

    if rule is CredentialRule.REQUIRED and not admission.credential_present:
        raise AuthorizationContextUnavailable
    if rule is CredentialRule.FORBIDDEN and admission.credential_present:
        raise AuthorizationContextUnavailable
    return admission.principal


def bind_authorization_context(
    vault_root: Path,
    *,
    principal: RequestPrincipal,
    credential: object,
    rule: CredentialRule,
    now: int,
) -> RequestPrincipal:
    """Verify one bearer and enforce one already-classified route."""

    has_credential = credential is not ABSENT_CREDENTIAL
    if rule is CredentialRule.REQUIRED and not has_credential:
        raise AuthorizationContextUnavailable
    if rule is CredentialRule.FORBIDDEN and has_credential:
        raise AuthorizationContextUnavailable

    admission = verify_authorization_context(
        vault_root,
        principal=principal,
        credential=credential,
        now=now,
    )
    return enforce_credential_rule(admission, rule)
