"""Bearer-free authorization-session lifecycle over governance schema v4."""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from . import (
    authorization_custody,
    authorization_serving_membership,
    authorization_sessions,
    schema_v4,
)

MAX_SESSION_TTL_SECONDS: Final = 3_600
_MAX_SQLITE_INTEGER: Final = (1 << 63) - 1


class AuthorizationSessionUnavailable(RuntimeError):
    """Credential-independent refusal for every unavailable session state."""

    code = "AUTHORIZATION_SESSION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("authorization session is unavailable")


@dataclass(frozen=True, slots=True)
class AuthorizationSessionContext:
    session_id: str
    principal_id: str
    issuer_family: str
    cell_id: str
    logical_vault_id: str
    keyring_id: str
    credential_generation: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class IssuedAuthorizationSessionCredential:
    bearer: str = field(repr=False)
    expires_at: str
    kind: str = field(init=False, default="authorization-session-bearer")

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "bearer": self.bearer,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class AuthorizationSessionIssuance:
    issued_credential: IssuedAuthorizationSessionCredential
    context: AuthorizationSessionContext

    @property
    def bearer(self) -> str:
        return self.issued_credential.bearer

    def response(self) -> dict[str, object]:
        return {
            "status": "ok",
            "issued_credential": self.issued_credential.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class _ResolvedSession:
    context: AuthorizationSessionContext
    record: authorization_sessions.AuthorizationSessionVerifierRecord


def _bounded_time(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_SQLITE_INTEGER
    ):
        raise AuthorizationSessionUnavailable
    return value


def _bounded_ttl(value: object) -> int:
    ttl = _bounded_time(value)
    if ttl > MAX_SESSION_TTL_SECONDS:
        raise AuthorizationSessionUnavailable
    return ttl


def _bounded_identity(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AuthorizationSessionUnavailable
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise AuthorizationSessionUnavailable from None
    if len(encoded) > 512:
        raise AuthorizationSessionUnavailable
    return value


def _ready_custody(
    connection: sqlite3.Connection,
    custody: authorization_custody.AuthorizationCustody,
    *,
    now: int,
) -> authorization_custody.AuthorizationVerifierKey:
    try:
        current = _bounded_time(now)
        if not isinstance(custody, authorization_custody.AuthorizationCustody):
            raise AuthorizationSessionUnavailable
        authorization_custody.require_current_standalone_registry(
            custody,
            now=current,
            require_serving=True,
        )
        keyring = custody.keyring
        control = custody.control
        if (
            not control.governance_enrolled
            or control.activation_store_id is None
            or control.activation_epoch is None
            or control.activation_state_digest is None
            or keyring.keyring_id != control.keyring_id
            or keyring.cell_id != control.cell_id
            or keyring.logical_vault_id != control.logical_vault_id
            or not control.issued_at <= current < control.expires_at
        ):
            raise AuthorizationSessionUnavailable
        active_key = keyring.active_key
        if not active_key.not_before <= current < active_key.not_after:
            raise AuthorizationSessionUnavailable
        if not any(
            key.key_id == control.signing_key_id
            and key.not_before <= control.issued_at
            and control.expires_at <= key.not_after
            for key in keyring.accepted_keys
        ):
            raise AuthorizationSessionUnavailable
        schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=control.activation_store_id,
            expected_activation_epoch=control.activation_epoch,
            expected_activation_state_digest=control.activation_state_digest,
        )
        membership = custody.serving_membership
        local_replica_id = custody.local_replica_id
        if membership is None or local_replica_id is None:
            raise AuthorizationSessionUnavailable
        if membership.record_digest and (
            membership.record_digest != control.serving_membership_digest
        ):
            raise AuthorizationSessionUnavailable
        live_key_rows = connection.execute(
            "SELECT DISTINCT verifier_key_id FROM governance_authorization_sessions "
            "WHERE status='active' AND expires_at>?",
            (current,),
        ).fetchall()
        readiness = authorization_serving_membership.evaluate_serving_membership(
            membership,
            now=current,
            local_replica_id=local_replica_id,
            local_software_version=authorization_custody.runtime_software_version(),
            local_schema_version=schema_v4.SCHEMA_USER_VERSION,
            expected_cell_id=control.cell_id,
            expected_control_digest=authorization_custody.control_attestation_digest(
                control
            ),
            expected_keyring_digest=authorization_custody.keyring_attestation_digest(
                keyring
            ),
            local_active_key_id=keyring.active_key_id,
            local_accepted_key_ids=tuple(
                sorted(key.key_id for key in keyring.accepted_keys)
            ),
            valid_verifier_key_ids=tuple(
                sorted(
                    key.key_id
                    for key in keyring.accepted_keys
                    if key.not_before <= current < key.not_after
                )
            ),
            live_verifier_key_ids=tuple(sorted(str(row[0]) for row in live_key_rows)),
        )
        if not readiness.ready:
            raise AuthorizationSessionUnavailable
        return active_key
    except (
        AttributeError,
        TypeError,
        ValueError,
        sqlite3.Error,
        schema_v4.SchemaV4Error,
        authorization_custody.AuthorizationCustodyUnavailable,
        authorization_serving_membership.ServingMembershipUnavailable,
        AuthorizationSessionUnavailable,
    ):
        raise AuthorizationSessionUnavailable from None


def serving_membership_readiness(
    vault_root: Path,
    *,
    now: int | None = None,
) -> authorization_serving_membership.ServingMembershipReadiness:
    """Recheck exact-v4 fleet readiness for a content-free control surface."""

    connection: sqlite3.Connection | None = None
    try:
        current = int(time.time()) if now is None else _bounded_time(now)
        custody = authorization_custody.load_authorization_custody(
            Path(vault_root),
            now=current,
        )
        from . import store

        connection = store.open_authorization_session_connection(Path(vault_root))
        _ready_custody(connection, custody, now=current)
        membership = custody.serving_membership
        if membership is None:
            raise AuthorizationSessionUnavailable
        return authorization_serving_membership.ServingMembershipReadiness(
            ready=True,
            code="AUTHORIZATION_MEMBERSHIP_READY",
            epoch=membership.epoch,
            serving_replicas=sum(
                item.state == "SERVING" for item in membership.replicas
            ),
            draining_replicas=sum(
                item.state == "DRAINING" for item in membership.replicas
            ),
        )
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
        AuthorizationSessionUnavailable,
        authorization_custody.AuthorizationCustodyUnavailable,
    ):
        return authorization_serving_membership.unavailable_readiness()
    finally:
        if connection is not None:
            connection.close()


def _expiry(
    *,
    now: int,
    ttl_seconds: int,
    custody: authorization_custody.AuthorizationCustody,
    issuance_key: authorization_custody.AuthorizationVerifierKey,
) -> int:
    current = _bounded_time(now)
    ttl = _bounded_ttl(ttl_seconds)
    expires_at = current + ttl
    if (
        expires_at > _MAX_SQLITE_INTEGER
        or expires_at > custody.control.expires_at
        or expires_at > issuance_key.not_after
    ):
        raise AuthorizationSessionUnavailable
    return expires_at


def _rfc3339(timestamp: int) -> str:
    try:
        return (
            datetime.fromtimestamp(timestamp, tz=UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OSError, OverflowError, ValueError):
        raise AuthorizationSessionUnavailable from None


def _context(
    binding: authorization_sessions.AuthorizationSessionBinding,
) -> AuthorizationSessionContext:
    return AuthorizationSessionContext(
        session_id=binding.session_id,
        principal_id=binding.principal_id,
        issuer_family=binding.issuer_family,
        cell_id=binding.cell_id,
        logical_vault_id=binding.logical_vault_id,
        keyring_id=binding.keyring_id,
        credential_generation=binding.credential_generation,
        expires_at=binding.expires_at,
    )


def _new_binding(
    custody: authorization_custody.AuthorizationCustody,
    *,
    session_id: str,
    principal_id: str,
    issuer_family: str,
    credential_generation: int,
    expires_at: int,
) -> authorization_sessions.AuthorizationSessionBinding:
    try:
        return authorization_sessions.AuthorizationSessionBinding(
            session_id=session_id,
            principal_id=_bounded_identity(principal_id),
            issuer_family=_bounded_identity(issuer_family),
            cell_id=custody.control.cell_id,
            logical_vault_id=custody.control.logical_vault_id,
            keyring_id=custody.control.keyring_id,
            credential_generation=credential_generation,
            expires_at=expires_at,
        )
    except (AttributeError, TypeError, ValueError, AuthorizationSessionUnavailable):
        raise AuthorizationSessionUnavailable from None


def _begin_immediate(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise AuthorizationSessionUnavailable
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        raise AuthorizationSessionUnavailable from None


def open_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    principal_id: str,
    issuer_family: str,
    now: int,
    ttl_seconds: int,
) -> AuthorizationSessionIssuance:
    """Issue and persist one new principal-bound session capability."""

    issuance_key = _ready_custody(connection, custody, now=now)
    expires_at = _expiry(
        now=now,
        ttl_seconds=ttl_seconds,
        custody=custody,
        issuance_key=issuance_key,
    )
    expires_at_wire = _rfc3339(expires_at)
    binding = _new_binding(
        custody,
        session_id=f"authorization-session:{secrets.token_hex(16)}",
        principal_id=principal_id,
        issuer_family=issuer_family,
        credential_generation=1,
        expires_at=expires_at,
    )
    issued = authorization_sessions.issue_credential(
        verifier_key=issuance_key.key,
        verifier_key_id=issuance_key.key_id,
        binding=binding,
    )
    _begin_immediate(connection)
    try:
        _ready_custody(connection, custody, now=now)
        schema_v4.insert_authorization_session(
            connection,
            issued.record,
            created_at=now,
        )
        connection.commit()
    except (
        sqlite3.Error,
        schema_v4.SchemaV4Error,
        AuthorizationSessionUnavailable,
    ):
        connection.rollback()
        raise AuthorizationSessionUnavailable from None
    except BaseException:
        connection.rollback()
        raise
    return AuthorizationSessionIssuance(
        IssuedAuthorizationSessionCredential(
            issued.bearer,
            expires_at_wire,
        ),
        _context(binding),
    )


def _candidate_rows(
    connection: sqlite3.Connection,
    *,
    parsed: authorization_sessions.AuthorizationSessionCredential,
    custody: authorization_custody.AuthorizationCustody,
) -> list[tuple[object, ...]]:
    digests = tuple(
        {
            authorization_sessions._locator_digest(key.key, parsed.locator)
            for key in custody.keyring.accepted_keys
        }
    )
    if not digests:
        return []
    placeholders = ",".join("?" for _ in digests)
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT session_id, locator_digest, verifier, verifier_key_id, "
            "credential_generation, principal_id, issuer_family, cell_id, "
            "logical_vault_id, keyring_id, status, created_at, rotated_at, "
            "expires_at, closed_at FROM governance_authorization_sessions "
            f"WHERE locator_digest IN ({placeholders})",
            digests,
        ).fetchall()
    ]


def _resolve_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    bearer: object,
    principal_id: str,
    issuer_family: str,
    now: int,
) -> _ResolvedSession:
    try:
        _ready_custody(connection, custody, now=now)
        parsed = authorization_sessions.parse_credential(bearer)
        if parsed is None:
            raise AuthorizationSessionUnavailable
        rows = _candidate_rows(connection, parsed=parsed, custody=custody)
        if len(rows) != 1:
            raise AuthorizationSessionUnavailable
        row = rows[0]
        binding = authorization_sessions.AuthorizationSessionBinding(
            session_id=str(row[0]),
            principal_id=str(row[5]),
            issuer_family=str(row[6]),
            cell_id=str(row[7]),
            logical_vault_id=str(row[8]),
            keyring_id=str(row[9]),
            credential_generation=int(row[4]),
            expires_at=int(row[13]),
        )
        record = authorization_sessions.AuthorizationSessionVerifierRecord(
            binding=binding,
            verifier_key_id=str(row[3]),
            locator_digest=bytes(row[1]),
            verifier=bytes(row[2]),
            status=str(row[10]),  # type: ignore[arg-type]
        )
        key = next(
            (
                candidate
                for candidate in custody.keyring.accepted_keys
                if candidate.key_id == record.verifier_key_id
            ),
            None,
        )
        if (
            key is None
            or not key.not_before <= now < key.not_after
            or binding.expires_at > key.not_after
        ):
            raise AuthorizationSessionUnavailable
        expected = _new_binding(
            custody,
            session_id=binding.session_id,
            principal_id=principal_id,
            issuer_family=issuer_family,
            credential_generation=binding.credential_generation,
            expires_at=binding.expires_at,
        )
        if not authorization_sessions.verify_credential(
            bearer,
            record=record,
            verifier_key=key.key,
            verifier_key_id=key.key_id,
            expected_binding=expected,
            now=now,
        ):
            raise AuthorizationSessionUnavailable
        return _ResolvedSession(_context(binding), record)
    except (
        IndexError,
        TypeError,
        ValueError,
        sqlite3.Error,
        AuthorizationSessionUnavailable,
    ):
        raise AuthorizationSessionUnavailable from None


def _resolve_verified_context(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    context: object,
    now: int,
) -> _ResolvedSession:
    """Recheck dispatcher-verified context without receiving the raw bearer."""
    try:
        _ready_custody(connection, custody, now=now)
        if not isinstance(context, AuthorizationSessionContext):
            raise AuthorizationSessionUnavailable
        rows = connection.execute(
            "SELECT session_id, locator_digest, verifier, verifier_key_id, "
            "credential_generation, principal_id, issuer_family, cell_id, "
            "logical_vault_id, keyring_id, status, created_at, rotated_at, "
            "expires_at, closed_at FROM governance_authorization_sessions "
            "WHERE session_id=?",
            (context.session_id,),
        ).fetchall()
        if len(rows) != 1:
            raise AuthorizationSessionUnavailable
        row = tuple(rows[0])
        binding = authorization_sessions.AuthorizationSessionBinding(
            session_id=str(row[0]),
            principal_id=str(row[5]),
            issuer_family=str(row[6]),
            cell_id=str(row[7]),
            logical_vault_id=str(row[8]),
            keyring_id=str(row[9]),
            credential_generation=int(row[4]),
            expires_at=int(row[13]),
        )
        record = authorization_sessions.AuthorizationSessionVerifierRecord(
            binding=binding,
            verifier_key_id=str(row[3]),
            locator_digest=bytes(row[1]),
            verifier=bytes(row[2]),
            status=str(row[10]),  # type: ignore[arg-type]
        )
        expected = _new_binding(
            custody,
            session_id=context.session_id,
            principal_id=context.principal_id,
            issuer_family=context.issuer_family,
            credential_generation=context.credential_generation,
            expires_at=context.expires_at,
        )
        key = next(
            (
                candidate
                for candidate in custody.keyring.accepted_keys
                if candidate.key_id == record.verifier_key_id
            ),
            None,
        )
        if (
            key is None
            or not key.not_before <= now < key.not_after
            or binding.expires_at > key.not_after
            or record.status != "active"
            or binding != expected
            or _context(binding) != context
            or now >= context.expires_at
        ):
            raise AuthorizationSessionUnavailable
        return _ResolvedSession(context, record)
    except (
        IndexError,
        TypeError,
        ValueError,
        sqlite3.Error,
        AuthorizationSessionUnavailable,
    ):
        raise AuthorizationSessionUnavailable from None


def resume_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    bearer: object,
    principal_id: str,
    issuer_family: str,
    now: int,
) -> AuthorizationSessionContext:
    """Resolve one capability without creating or changing authority."""

    return _resolve_session(
        connection,
        custody=custody,
        bearer=bearer,
        principal_id=principal_id,
        issuer_family=issuer_family,
        now=now,
    ).context


def status_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    bearer: object,
    principal_id: str,
    issuer_family: str,
    now: int,
) -> AuthorizationSessionContext:
    """Return the verified active session context without exposing its bearer."""

    return resume_session(
        connection,
        custody=custody,
        bearer=bearer,
        principal_id=principal_id,
        issuer_family=issuer_family,
        now=now,
    )


def status_verified_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    context: AuthorizationSessionContext,
    now: int,
) -> AuthorizationSessionContext:
    """Recheck already-verified request context without forwarding its bearer."""
    return _resolve_verified_context(
        connection,
        custody=custody,
        context=context,
        now=now,
    ).context


def rotate_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    bearer: object,
    principal_id: str,
    issuer_family: str,
    now: int,
    ttl_seconds: int,
) -> AuthorizationSessionIssuance:
    """Atomically replace one locator/verifier while retaining session identity."""

    resolved = _resolve_session(
        connection,
        custody=custody,
        bearer=bearer,
        principal_id=principal_id,
        issuer_family=issuer_family,
        now=now,
    )
    return _rotate_resolved_session(
        connection,
        custody=custody,
        resolved=resolved,
        now=now,
        ttl_seconds=ttl_seconds,
    )


def rotate_verified_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    context: AuthorizationSessionContext,
    now: int,
    ttl_seconds: int,
) -> AuthorizationSessionIssuance:
    """Rotate from immutable dispatcher context, never from a leaf bearer."""
    resolved = _resolve_verified_context(
        connection,
        custody=custody,
        context=context,
        now=now,
    )
    return _rotate_resolved_session(
        connection,
        custody=custody,
        resolved=resolved,
        now=now,
        ttl_seconds=ttl_seconds,
    )


def _rotate_resolved_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    resolved: _ResolvedSession,
    now: int,
    ttl_seconds: int,
) -> AuthorizationSessionIssuance:
    issuance_key = _ready_custody(connection, custody, now=now)
    expires_at = _expiry(
        now=now,
        ttl_seconds=ttl_seconds,
        custody=custody,
        issuance_key=issuance_key,
    )
    expires_at_wire = _rfc3339(expires_at)
    binding = _new_binding(
        custody,
        session_id=resolved.context.session_id,
        principal_id=resolved.context.principal_id,
        issuer_family=resolved.context.issuer_family,
        credential_generation=resolved.context.credential_generation + 1,
        expires_at=expires_at,
    )
    issued = authorization_sessions.issue_credential(
        verifier_key=issuance_key.key,
        verifier_key_id=issuance_key.key_id,
        binding=binding,
    )
    _begin_immediate(connection)
    try:
        _ready_custody(connection, custody, now=now)
        updated = connection.execute(
            "UPDATE governance_authorization_sessions SET locator_digest=?, verifier=?, "
            "verifier_key_id=?, credential_generation=?, principal_id=?, issuer_family=?, "
            "cell_id=?, logical_vault_id=?, keyring_id=?, rotated_at=?, expires_at=? "
            "WHERE session_id=? AND locator_digest=? AND credential_generation=? "
            "AND status='active'",
            (
                issued.record.locator_digest,
                issued.record.verifier,
                issued.record.verifier_key_id,
                binding.credential_generation,
                binding.principal_id,
                binding.issuer_family,
                binding.cell_id,
                binding.logical_vault_id,
                binding.keyring_id,
                now,
                binding.expires_at,
                binding.session_id,
                resolved.record.locator_digest,
                resolved.context.credential_generation,
            ),
        )
        if updated.rowcount != 1:
            raise AuthorizationSessionUnavailable
        connection.commit()
    except (sqlite3.Error, AuthorizationSessionUnavailable):
        connection.rollback()
        raise AuthorizationSessionUnavailable from None
    except BaseException:
        connection.rollback()
        raise
    return AuthorizationSessionIssuance(
        IssuedAuthorizationSessionCredential(
            issued.bearer,
            expires_at_wire,
        ),
        _context(binding),
    )


def close_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    bearer: object,
    principal_id: str,
    issuer_family: str,
    now: int,
) -> AuthorizationSessionContext:
    """Atomically close one session and revoke only its dependent authority."""

    resolved = _resolve_session(
        connection,
        custody=custody,
        bearer=bearer,
        principal_id=principal_id,
        issuer_family=issuer_family,
        now=now,
    )
    return _close_resolved_session(connection, resolved=resolved, now=now)


def close_verified_session(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    context: AuthorizationSessionContext,
    now: int,
) -> AuthorizationSessionContext:
    """Close from immutable dispatcher context, never from a leaf bearer."""
    resolved = _resolve_verified_context(
        connection,
        custody=custody,
        context=context,
        now=now,
    )
    return _close_resolved_session(connection, resolved=resolved, now=now)


def _close_resolved_session(
    connection: sqlite3.Connection,
    *,
    resolved: _ResolvedSession,
    now: int,
) -> AuthorizationSessionContext:
    _begin_immediate(connection)
    try:
        closed = connection.execute(
            "UPDATE governance_authorization_sessions SET status='closed', closed_at=? "
            "WHERE session_id=? AND locator_digest=? AND credential_generation=? "
            "AND status='active'",
            (
                now,
                resolved.context.session_id,
                resolved.record.locator_digest,
                resolved.context.credential_generation,
            ),
        )
        if closed.rowcount != 1:
            raise AuthorizationSessionUnavailable
        connection.execute(
            "UPDATE governance_session_purpose SET status='closed', prepared_event_id=NULL "
            "WHERE authorization_session_id=?",
            (resolved.context.session_id,),
        )
        connection.execute(
            "DELETE FROM governance_session_purpose_staging WHERE authorization_session_id=?",
            (resolved.context.session_id,),
        )
        connection.execute(
            "UPDATE governance_session_grants SET status='revoked', prepared_event_id=NULL, "
            "revoked_at=? WHERE authorization_session_id=? AND status<>'revoked'",
            (now, resolved.context.session_id),
        )
        connection.execute(
            "UPDATE withhold_tokens SET status='expired', prepared_event_id=NULL "
            "WHERE authorization_session_id=? AND consumed_at IS NULL",
            (resolved.context.session_id,),
        )
        connection.commit()
    except (sqlite3.Error, AuthorizationSessionUnavailable):
        connection.rollback()
        raise AuthorizationSessionUnavailable from None
    except BaseException:
        connection.rollback()
        raise
    return resolved.context
