"""Session-bound escalation and grant primitives for governance schema v4.

The public bearer never reaches this module.  Callers must first resolve it to
an :class:`AuthorizationSessionContext`; every operation then rechecks that
context against the active v4 row before reading or changing dependent state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Final

from .authorization_session_lifecycle import (
    AuthorizationSessionContext,
    AuthorizationSessionUnavailable,
)

_TOKEN_VERSION: Final = "wh1"
_TOKEN_DOMAIN: Final = b"exomem.withhold-token.v4\0"
_GRANT_ID_DOMAIN: Final = b"exomem.session-grant.v4\0"
_MAX_SQLITE_INTEGER: Final = (1 << 63) - 1
_MAX_ITEMS: Final = 1_024
_MAX_TEXT_BYTES: Final = 4_096
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class SessionMembership:
    path: str
    fingerprint: str
    scope_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionGrant:
    grant_id: str
    authorization_session_id: str
    principal_id: str
    issuer_family: str
    audience: str
    purpose: str | None
    ceiling: int
    paths: tuple[str, ...]
    fingerprints: tuple[str, ...]
    scope_ids: tuple[str, ...]
    membership: tuple[SessionMembership, ...]
    policy_fingerprint: str
    token_jti: str
    created_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class EscalationReview:
    authorization_session_id: str
    principal_id: str
    issuer_family: str
    audience: str
    purpose: str | None
    max_level: int
    org_ceiling: int
    paths: tuple[str, ...]
    fingerprints: tuple[str, ...]
    scope_ids: tuple[str, ...]
    expires_at: int


@dataclass(frozen=True, slots=True)
class _TokenClaim:
    jti: str
    authorization_session_id: str
    principal_id: str
    issuer_family: str
    audience: str
    max_level: int
    paths: tuple[str, ...]
    fingerprints: tuple[str, ...]
    scope_ids: tuple[str, ...]
    purpose: str | None
    org_ceiling: int
    expires_at: int
    minted_at: int


def _unavailable() -> AuthorizationSessionUnavailable:
    return AuthorizationSessionUnavailable()


def _integer(value: object, *, minimum: int = 0, maximum: int = _MAX_SQLITE_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _unavailable()
    return value


def _text(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or value != value.strip():
        raise _unavailable()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _unavailable() from None
    if len(encoded) > _MAX_TEXT_BYTES or any(ord(character) < 0x20 for character in value):
        raise _unavailable()
    return value


def _digest(value: object) -> str:
    text = _text(value)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise _unavailable()
    return text


def _sequence(
    values: object, *, digests: bool = False, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_ITEMS:
        raise _unavailable()
    normalized = tuple(_digest(value) if digests else _text(value) for value in values)
    if (not normalized and not allow_empty) or normalized != tuple(sorted(set(normalized))):
        raise _unavailable()
    return normalized


def _path_fingerprint_pairs(
    paths: object,
    fingerprints: object,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    canonical_paths = _sequence(paths)
    if not isinstance(fingerprints, tuple) or len(fingerprints) != len(canonical_paths):
        raise _unavailable()
    canonical_fingerprints = tuple(_digest(value) for value in fingerprints)
    return canonical_paths, canonical_fingerprints


def _signing_key(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise _unavailable()
    return value


def _purpose(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _active_context(
    connection: sqlite3.Connection,
    context: object,
    *,
    now: int,
) -> AuthorizationSessionContext:
    moment = _integer(now, minimum=1)
    if not isinstance(context, AuthorizationSessionContext):
        raise _unavailable()
    try:
        row = connection.execute(
            "SELECT principal_id, issuer_family, cell_id, logical_vault_id, keyring_id, "
            "credential_generation, expires_at, status FROM governance_authorization_sessions "
            "WHERE session_id=?",
            (context.session_id,),
        ).fetchone()
    except sqlite3.Error:
        raise _unavailable() from None
    expected = (
        context.principal_id,
        context.issuer_family,
        context.cell_id,
        context.logical_vault_id,
        context.keyring_id,
        context.credential_generation,
        context.expires_at,
        "active",
    )
    if row is None or tuple(row) != expected or moment > context.expires_at:
        raise _unavailable()
    return context


def _claim_value(claim: _TokenClaim) -> dict[str, object]:
    return {
        "audience": claim.audience,
        "authorization_session_id": claim.authorization_session_id,
        "expires_at": claim.expires_at,
        "fingerprints": list(claim.fingerprints),
        "issuer_family": claim.issuer_family,
        "jti": claim.jti,
        "max_level": claim.max_level,
        "minted_at": claim.minted_at,
        "org_ceiling": claim.org_ceiling,
        "paths": list(claim.paths),
        "principal_id": claim.principal_id,
        "purpose": claim.purpose,
        "scope_ids": list(claim.scope_ids),
    }


def _signature(key: bytes, claim: _TokenClaim) -> str:
    return hmac.new(
        key, _TOKEN_DOMAIN + _canonical_json(_claim_value(claim)), hashlib.sha256
    ).hexdigest()


def _parse_token(value: object) -> tuple[str, int, str]:
    if not isinstance(value, str) or len(value) > 128 or value != value.strip():
        raise _unavailable()
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_VERSION:
        raise _unavailable()
    jti, raw_expiry, signature = parts[1:]
    if (
        len(jti) != 32
        or any(character not in _HEX for character in jti)
        or not raw_expiry.isascii()
        or not raw_expiry.isdecimal()
        or str(int(raw_expiry)) != raw_expiry
        or len(signature) != 64
        or any(character not in _HEX for character in signature)
    ):
        raise _unavailable()
    return jti, _integer(int(raw_expiry), minimum=1), signature


def _membership(value: object) -> tuple[SessionMembership, ...]:
    if not isinstance(value, tuple) or not value or len(value) > _MAX_ITEMS:
        raise _unavailable()
    rows: list[SessionMembership] = []
    for item in value:
        if not isinstance(item, SessionMembership):
            raise _unavailable()
        rows.append(
            SessionMembership(
                path=_text(item.path),
                fingerprint=_digest(item.fingerprint),
                scope_ids=_sequence(item.scope_ids, allow_empty=True),
            )
        )
    result = tuple(sorted(rows, key=lambda row: row.path))
    if len({row.path for row in result}) != len(result):
        raise _unavailable()
    return result


def _membership_json(value: tuple[SessionMembership, ...]) -> str:
    return _canonical_json(
        [
            {
                "fingerprint": row.fingerprint,
                "path": row.path,
                "scope_ids": list(row.scope_ids),
            }
            for row in value
        ]
    ).decode("utf-8")


def _load_membership(value: object) -> tuple[SessionMembership, ...]:
    if not isinstance(value, str):
        raise _unavailable()
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        raise _unavailable() from None
    if not isinstance(decoded, list):
        raise _unavailable()
    rows: list[SessionMembership] = []
    for item in decoded:
        if not isinstance(item, dict) or set(item) != {"path", "fingerprint", "scope_ids"}:
            raise _unavailable()
        raw_scopes = item["scope_ids"]
        if not isinstance(raw_scopes, list):
            raise _unavailable()
        rows.append(
            SessionMembership(
                path=item["path"],
                fingerprint=item["fingerprint"],
                scope_ids=tuple(raw_scopes),
            )
        )
    return _membership(tuple(rows))


def mint_escalation_token(
    connection: sqlite3.Connection,
    *,
    context: AuthorizationSessionContext,
    signing_key: bytes,
    audience: str,
    purpose: str | None,
    max_level: int,
    org_ceiling: int,
    paths: tuple[str, ...],
    fingerprints: tuple[str, ...],
    scope_ids: tuple[str, ...],
    now: int,
    expires_at: int,
) -> str:
    """Mint one bearer-free-at-rest escalation token for an active session."""

    if connection.in_transaction:
        raise _unavailable()
    current = _active_context(connection, context, now=now)
    key = _signing_key(signing_key)
    canonical_audience = _text(audience)
    if canonical_audience != current.principal_id:
        raise _unavailable()
    canonical_paths, canonical_fingerprints = _path_fingerprint_pairs(paths, fingerprints)
    scopes = _sequence(scope_ids)
    issued_at = _integer(now, minimum=1)
    expiry = _integer(expires_at, minimum=issued_at + 1)
    if expiry > current.expires_at:
        raise _unavailable()
    ceiling = _integer(max_level, maximum=6)
    cap = _integer(org_ceiling, maximum=6)
    if ceiling > cap:
        raise _unavailable()
    claim = _TokenClaim(
        jti=secrets.token_hex(16),
        authorization_session_id=current.session_id,
        principal_id=current.principal_id,
        issuer_family=current.issuer_family,
        audience=canonical_audience,
        max_level=ceiling,
        paths=canonical_paths,
        fingerprints=canonical_fingerprints,
        scope_ids=scopes,
        purpose=_purpose(purpose),
        org_ceiling=cap,
        expires_at=expiry,
        minted_at=issued_at,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        _active_context(connection, current, now=now)
        connection.execute(
            "INSERT INTO withhold_tokens "
            "(jti, authorization_session_id, principal_id, issuer_family, audience, "
            "max_level, fingerprints, paths, scope_ids, purpose, org_ceiling, status, "
            "prepared_event_id, expires_at, minted_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, NULL)",
            (
                claim.jti,
                claim.authorization_session_id,
                claim.principal_id,
                claim.issuer_family,
                claim.audience,
                claim.max_level,
                _canonical_json(list(claim.fingerprints)).decode(),
                _canonical_json(list(claim.paths)).decode(),
                _canonical_json(list(claim.scope_ids)).decode(),
                claim.purpose,
                claim.org_ceiling,
                claim.expires_at,
                claim.minted_at,
            ),
        )
        connection.commit()
    except (sqlite3.Error, AuthorizationSessionUnavailable):
        connection.rollback()
        raise _unavailable() from None
    return f"{_TOKEN_VERSION}.{claim.jti}.{claim.expires_at}.{_signature(key, claim)}"


def _load_claim(
    connection: sqlite3.Connection, jti: str
) -> tuple[_TokenClaim, str, str | None, int | None]:
    try:
        row = connection.execute(
            "SELECT authorization_session_id, principal_id, issuer_family, audience, "
            "max_level, fingerprints, paths, scope_ids, purpose, org_ceiling, status, "
            "prepared_event_id, expires_at, minted_at, consumed_at FROM withhold_tokens "
            "WHERE jti=?",
            (jti,),
        ).fetchone()
        if row is None:
            raise _unavailable()
        fingerprints = tuple(json.loads(str(row[5])))
        paths = tuple(json.loads(str(row[6])))
        canonical_paths, canonical_fingerprints = _path_fingerprint_pairs(paths, fingerprints)
        scopes = tuple(json.loads(str(row[7])))
        claim = _TokenClaim(
            jti=jti,
            authorization_session_id=_text(row[0]),
            principal_id=_text(row[1]),
            issuer_family=_text(row[2]),
            audience=_text(row[3]),
            max_level=_integer(row[4], maximum=6),
            fingerprints=canonical_fingerprints,
            paths=canonical_paths,
            scope_ids=_sequence(scopes),
            purpose=_purpose(row[8]),
            org_ceiling=_integer(row[9], maximum=6),
            expires_at=_integer(row[12], minimum=1),
            minted_at=_integer(row[13], minimum=1),
        )
        return claim, _text(row[10]), row[11], row[14]
    except (json.JSONDecodeError, TypeError, ValueError, sqlite3.Error):
        raise _unavailable() from None


def inspect_escalation_token(
    connection: sqlite3.Connection,
    *,
    token: object,
    context: AuthorizationSessionContext,
    signing_key: bytes,
    audience: str,
    purpose: str | None,
    now: int,
) -> EscalationReview:
    """Verify a live token and return only its reviewed authorization bounds."""

    current = _active_context(connection, context, now=now)
    key = _signing_key(signing_key)
    jti, wire_expiry, supplied_signature = _parse_token(token)
    claim, status, prepared_event_id, consumed_at = _load_claim(connection, jti)
    canonical_audience = _text(audience)
    canonical_purpose = _purpose(purpose)
    if (
        claim.authorization_session_id != current.session_id
        or claim.principal_id != current.principal_id
        or claim.issuer_family != current.issuer_family
        or claim.audience != canonical_audience
        or canonical_audience != current.principal_id
        or claim.purpose != canonical_purpose
        or claim.expires_at != wire_expiry
        or now > claim.expires_at
        or status != "active"
        or prepared_event_id is not None
        or consumed_at is not None
        or not hmac.compare_digest(_signature(key, claim), supplied_signature)
    ):
        raise _unavailable()
    return EscalationReview(
        authorization_session_id=claim.authorization_session_id,
        principal_id=claim.principal_id,
        issuer_family=claim.issuer_family,
        audience=claim.audience,
        purpose=claim.purpose,
        max_level=claim.max_level,
        org_ceiling=claim.org_ceiling,
        paths=claim.paths,
        fingerprints=claim.fingerprints,
        scope_ids=claim.scope_ids,
        expires_at=claim.expires_at,
    )


def redeem_escalation_token(
    connection: sqlite3.Connection,
    *,
    token: object,
    context: AuthorizationSessionContext,
    signing_key: bytes,
    audience: str,
    purpose: str | None,
    membership: tuple[SessionMembership, ...],
    policy_fingerprint: str,
    now: int,
    grant_expires_at: int | None = None,
) -> SessionGrant:
    """Consume a token once and atomically create its exact session grant."""

    current = _active_context(connection, context, now=now)
    review = inspect_escalation_token(
        connection,
        token=token,
        context=current,
        signing_key=signing_key,
        audience=audience,
        purpose=purpose,
        now=now,
    )
    jti, _wire_expiry, _supplied_signature = _parse_token(token)
    canonical_membership = _membership(membership)
    canonical_policy = _digest(policy_fingerprint)
    canonical_audience = review.audience
    canonical_purpose = review.purpose
    try:
        active_policy = connection.execute(
            "SELECT policy_fingerprint FROM active_governance_tuple WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error:
        raise _unavailable() from None
    if (
        active_policy != (canonical_policy,)
        or tuple(row.path for row in canonical_membership) != review.paths
        or tuple(row.fingerprint for row in canonical_membership) != review.fingerprints
        or not set(review.scope_ids).issubset(
            {scope for row in canonical_membership for scope in row.scope_ids}
        )
    ):
        raise _unavailable()
    membership_json = _membership_json(canonical_membership)
    grant_id = hashlib.sha256(
        _GRANT_ID_DOMAIN + current.session_id.encode() + b"\0" + jti.encode("ascii")
    ).hexdigest()
    grant = SessionGrant(
        grant_id=grant_id,
        authorization_session_id=current.session_id,
        principal_id=current.principal_id,
        issuer_family=current.issuer_family,
        audience=canonical_audience,
        purpose=canonical_purpose,
        ceiling=review.max_level,
        paths=review.paths,
        fingerprints=review.fingerprints,
        scope_ids=review.scope_ids,
        membership=canonical_membership,
        policy_fingerprint=canonical_policy,
        token_jti=jti,
        created_at=now,
        expires_at=min(
            review.expires_at,
            current.expires_at,
            review.expires_at
            if grant_expires_at is None
            else _integer(grant_expires_at, minimum=_integer(now, minimum=1) + 1),
        ),
    )
    if connection.in_transaction:
        raise _unavailable()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _active_context(connection, current, now=now)
        if inspect_escalation_token(
            connection,
            token=token,
            context=current,
            signing_key=signing_key,
            audience=canonical_audience,
            purpose=canonical_purpose,
            now=now,
        ) != review:
            raise _unavailable()
        active_policy = connection.execute(
            "SELECT policy_fingerprint FROM active_governance_tuple WHERE singleton=1"
        ).fetchone()
        if active_policy != (canonical_policy,):
            raise _unavailable()
        consumed = connection.execute(
            "UPDATE withhold_tokens SET consumed_at=?, status='consumed' "
            "WHERE jti=? AND authorization_session_id=? AND status='active' "
            "AND prepared_event_id IS NULL AND consumed_at IS NULL",
            (now, jti, current.session_id),
        )
        if consumed.rowcount != 1:
            raise _unavailable()
        connection.execute(
            "INSERT INTO governance_session_grants "
            "(grant_id, authorization_session_id, principal_id, issuer_family, audience, "
            "purpose, ceiling, paths, fingerprints, scope_ids, membership_manifest, "
            "policy_fingerprint, token_jti, status, prepared_event_id, created_at, "
            "expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'active', NULL, ?, ?, NULL)",
            (
                grant.grant_id,
                grant.authorization_session_id,
                grant.principal_id,
                grant.issuer_family,
                grant.audience,
                grant.purpose,
                grant.ceiling,
                _canonical_json(list(grant.paths)).decode(),
                _canonical_json(list(grant.fingerprints)).decode(),
                _canonical_json(list(grant.scope_ids)).decode(),
                membership_json,
                grant.policy_fingerprint,
                grant.token_jti,
                grant.created_at,
                grant.expires_at,
            ),
        )
        connection.commit()
    except (sqlite3.Error, AuthorizationSessionUnavailable):
        connection.rollback()
        raise _unavailable() from None
    except BaseException:
        connection.rollback()
        raise
    return grant


def active_session_grants(
    connection: sqlite3.Connection,
    *,
    context: AuthorizationSessionContext,
    audience: str,
    purpose: str | None,
    path: str,
    fingerprint: str,
    scope_ids: tuple[str, ...],
    policy_fingerprint: str,
    now: int,
) -> tuple[tuple[SessionGrant, ...], str]:
    """Return grants only when their reviewed policy and membership still match."""

    current = _active_context(connection, context, now=now)
    canonical_audience = _text(audience)
    canonical_purpose = _purpose(purpose)
    canonical_path = _text(path)
    canonical_fingerprint = _digest(fingerprint)
    current_scopes = _sequence(scope_ids, allow_empty=True)
    current_policy = _digest(policy_fingerprint)
    if canonical_audience != current.principal_id:
        raise _unavailable()
    try:
        active_policy = connection.execute(
            "SELECT policy_fingerprint FROM active_governance_tuple WHERE singleton=1"
        ).fetchone()
        rows = connection.execute(
            "SELECT grant_id, purpose, ceiling, paths, fingerprints, scope_ids, "
            "membership_manifest, policy_fingerprint, token_jti, created_at, expires_at "
            "FROM governance_session_grants WHERE authorization_session_id=? "
            "AND principal_id=? AND issuer_family=? AND audience=? AND status='active' "
            "AND expires_at>=? ORDER BY grant_id",
            (
                current.session_id,
                current.principal_id,
                current.issuer_family,
                canonical_audience,
                now,
            ),
        ).fetchall()
    except sqlite3.Error:
        raise _unavailable() from None
    if active_policy != (current_policy,):
        return (), "no-session-grants"
    active: list[SessionGrant] = []
    for row in rows:
        try:
            row_purpose = _purpose(row[1])
            paths, fingerprints = _path_fingerprint_pairs(
                tuple(json.loads(str(row[3]))),
                tuple(json.loads(str(row[4]))),
            )
            grant_scopes = _sequence(tuple(json.loads(str(row[5]))))
            reviewed = _load_membership(row[6])
            reviewed_item = next(
                (item for item in reviewed if item.path == canonical_path),
                None,
            )
            if (
                row_purpose != canonical_purpose
                or _digest(row[7]) != current_policy
                or reviewed_item is None
                or reviewed_item.fingerprint != canonical_fingerprint
                or reviewed_item.scope_ids != current_scopes
                or canonical_path not in paths
                or fingerprints[paths.index(canonical_path)] != canonical_fingerprint
            ):
                continue
            active.append(
                SessionGrant(
                    grant_id=_digest(row[0]),
                    authorization_session_id=current.session_id,
                    principal_id=current.principal_id,
                    issuer_family=current.issuer_family,
                    audience=canonical_audience,
                    purpose=row_purpose,
                    ceiling=_integer(row[2], maximum=6),
                    paths=paths,
                    fingerprints=fingerprints,
                    scope_ids=grant_scopes,
                    membership=reviewed,
                    policy_fingerprint=current_policy,
                    token_jti=_text(row[8]),
                    created_at=_integer(row[9], minimum=1),
                    expires_at=_integer(row[10], minimum=1),
                )
            )
        except (json.JSONDecodeError, ValueError, TypeError, AuthorizationSessionUnavailable):
            raise _unavailable() from None
    if not active:
        return (), "no-session-grants"
    identity = hashlib.sha256(
        b"exomem.active-session-grants.v4\0"
        + _canonical_json(
            [
                {
                    "grant_id": grant.grant_id,
                    "scope_ids": list(grant.scope_ids),
                    "policy_fingerprint": grant.policy_fingerprint,
                }
                for grant in active
            ]
        )
    ).hexdigest()
    return tuple(active), identity


def declare_purpose(
    connection: sqlite3.Connection,
    *,
    context: AuthorizationSessionContext,
    audience: str,
    purpose: str,
    now: int,
    expires_at: int,
) -> str:
    """Replace one session's active purpose without touching sibling sessions."""

    current = _active_context(connection, context, now=now)
    canonical_audience = _text(audience)
    canonical_purpose = _text(purpose)
    expiry = _integer(expires_at, minimum=_integer(now, minimum=1) + 1)
    if canonical_audience != current.principal_id or expiry > current.expires_at:
        raise _unavailable()
    if connection.in_transaction:
        raise _unavailable()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _active_context(connection, current, now=now)
        connection.execute(
            "INSERT INTO governance_session_purpose "
            "(authorization_session_id, principal_id, issuer_family, audience, purpose, "
            "status, prepared_event_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?) "
            "ON CONFLICT(authorization_session_id, audience) DO UPDATE SET "
            "principal_id=excluded.principal_id, issuer_family=excluded.issuer_family, "
            "purpose=excluded.purpose, status='active', prepared_event_id=NULL, "
            "created_at=excluded.created_at, expires_at=excluded.expires_at",
            (
                current.session_id,
                current.principal_id,
                current.issuer_family,
                canonical_audience,
                canonical_purpose,
                now,
                expiry,
            ),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise _unavailable() from None
    except BaseException:
        connection.rollback()
        raise
    return canonical_purpose


def active_session_purpose(
    connection: sqlite3.Connection,
    *,
    context: AuthorizationSessionContext,
    audience: str,
    now: int,
) -> str | None:
    """Read the purpose bound to this exact active session and issuer."""

    current = _active_context(connection, context, now=now)
    canonical_audience = _text(audience)
    if canonical_audience != current.principal_id:
        raise _unavailable()
    try:
        row = connection.execute(
            "SELECT purpose FROM governance_session_purpose "
            "WHERE authorization_session_id=? AND principal_id=? AND issuer_family=? "
            "AND audience=? AND status='active' AND expires_at>=?",
            (
                current.session_id,
                current.principal_id,
                current.issuer_family,
                canonical_audience,
                now,
            ),
        ).fetchone()
    except sqlite3.Error:
        raise _unavailable() from None
    return None if row is None else _text(row[0])


def revoke_session_grants(
    connection: sqlite3.Connection,
    *,
    context: AuthorizationSessionContext,
    audience: str,
    now: int,
) -> int:
    """Revoke only grants owned by one verified internal session."""

    current = _active_context(connection, context, now=now)
    canonical_audience = _text(audience)
    if canonical_audience != current.principal_id or connection.in_transaction:
        raise _unavailable()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _active_context(connection, current, now=now)
        revoked = connection.execute(
            "UPDATE governance_session_grants SET status='revoked', prepared_event_id=NULL, "
            "revoked_at=? WHERE authorization_session_id=? AND principal_id=? "
            "AND issuer_family=? AND audience=? AND status='active'",
            (
                now,
                current.session_id,
                current.principal_id,
                current.issuer_family,
                canonical_audience,
            ),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise _unavailable() from None
    except BaseException:
        connection.rollback()
        raise
    return int(revoked.rowcount or 0)
