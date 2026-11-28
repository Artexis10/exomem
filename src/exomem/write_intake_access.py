"""Receipt ownership and bounded lookups for the durable write intake."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from . import auth_sessions, cloud_cell
from .governance.principal import RequestPrincipal

# These paths belong to the intake's stored principal record, not user vocabulary.
_BINDING_FIELDS = (
    "audience_id", "issuer_family", "client_binding.issuer", "client_binding.client_id",
    "origin_session.generation", "origin_session.audience",
)
_LEGACY_SCOPE = ""
_INVALID_SCOPE = "invalid"
_FAMILY_PREFIX = "family:"
Scopes = tuple[str, ...] | None


def _path_value(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _scope_key(record: Mapping[str, Any], login: str | None) -> str:
    return json.dumps([*(_path_value(record, field) for field in _BINDING_FIELDS), login],
                      ensure_ascii=False, separators=(",", ":"))


def recorded_scope(record: Mapping[str, Any]) -> str:
    """The scope of a stored receipt, including rows from before the scope index."""
    if record.get("client_binding") is None and record.get("origin_session") is None:
        return _LEGACY_SCOPE
    family = record.get("receipt_family")
    login = _FAMILY_PREFIX + family if isinstance(family, str) else _path_value(record, "origin_session.session_id")
    return _scope_key(record, login)


def scopes(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Seek the current login and the earlier format's current access identifier."""
    stable = recorded_scope(record)
    if record.get("receipt_family") is None:
        return (stable,)
    return stable, _scope_key(record, _path_value(record, "origin_session.session_id"))


def can_release(principal: RequestPrincipal) -> bool:
    """Recheck a signed login without accepting stale personal connector evidence."""
    if cloud_cell.is_cloud_issuer_family(principal.issuer_family):
        # The cell relies on the gateway's request proof. Personal issuer checks cannot verify it.
        return True
    if principal.client_binding is None and principal.origin_session is None:
        return True
    if principal.client_binding is None or principal.origin_session is None:
        return False
    return auth_sessions.origin_login_state_sync(principal, require_owner=True) is auth_sessions.OriginLogin.VALID


def row_matches(row: sqlite3.Row, allowed: Scopes, *, prior_login=None) -> bool:
    if allowed is None:
        return True
    try:
        record = json.loads(row["principal"])
        return isinstance(record, Mapping) and (
            recorded_scope(record) in allowed or (prior_login is not None and prior_login(record))
        )
    except (KeyError, TypeError, ValueError):
        return False


def same_legacy_family(current: Mapping[str, Any], stored: Mapping[str, Any], *, restore) -> bool:
    """Confirm one older access identifier belongs to the currently verified refresh family."""
    family = current.get("receipt_family")
    if not family or stored.get("receipt_family") is not None or not isinstance(stored.get("origin_session"), Mapping):
        return False
    if any(_path_value(current, field) != _path_value(stored, field) for field in _BINDING_FIELDS):
        return False
    return auth_sessions.legacy_receipt_family(restore(stored)) == family


def indexed(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'intake_receipt_counts'").fetchone() is not None


def ensure_index(conn: sqlite3.Connection) -> None:
    """Install derived scope indexes inside a governed intake write transaction."""
    if indexed(conn):
        return
    if "named_at" not in {row[1] for row in conn.execute("PRAGMA table_info(intake)")}:
        conn.execute("ALTER TABLE intake ADD COLUMN named_at REAL")
    fields = ", ".join(f"json_extract(principal, '$.{field}')" for field in _BINDING_FIELDS)
    conn.execute(f"""
        ALTER TABLE intake ADD COLUMN receipt_scope TEXT GENERATED ALWAYS AS (
            CASE WHEN NOT json_valid(principal) THEN '{_INVALID_SCOPE}'
                 WHEN json_extract(principal, '$.client_binding') IS NULL
                  AND json_extract(principal, '$.origin_session') IS NULL THEN '{_LEGACY_SCOPE}'
                 ELSE json_array({fields}, COALESCE('{_FAMILY_PREFIX}' || json_extract(principal, '$.receipt_family'),
                                                   json_extract(principal, '$.origin_session.session_id'))) END
        ) VIRTUAL
    """)
    conn.execute("CREATE INDEX intake_receipt_scope ON intake(receipt_scope, state, seq)")
    conn.execute("CREATE INDEX intake_receipt_notice ON intake(receipt_scope) "
                 "WHERE state = 'failed' AND named_at IS NULL")
    conn.execute("CREATE TABLE intake_receipt_counts (scope TEXT, state TEXT, n INTEGER, PRIMARY KEY(scope, state))")
    conn.execute("INSERT INTO intake_receipt_counts SELECT receipt_scope, state, COUNT(*) FROM intake "
                 "GROUP BY receipt_scope, state")
    add = ("INSERT INTO intake_receipt_counts VALUES (NEW.receipt_scope, NEW.state, 1) "
           "ON CONFLICT(scope, state) DO UPDATE SET n = n + 1;")
    remove = ("UPDATE intake_receipt_counts SET n = n - 1 WHERE scope = OLD.receipt_scope AND state = OLD.state; "
              "DELETE FROM intake_receipt_counts WHERE scope = OLD.receipt_scope AND state = OLD.state AND n = 0;")
    conn.execute(f"CREATE TRIGGER intake_receipt_insert AFTER INSERT ON intake BEGIN {add} END")
    conn.execute(f"CREATE TRIGGER intake_receipt_delete AFTER DELETE ON intake BEGIN {remove} END")
    conn.execute(f"CREATE TRIGGER intake_receipt_transition AFTER UPDATE OF state ON intake "
                 f"WHEN NEW.state != OLD.state BEGIN {remove} {add} END")
