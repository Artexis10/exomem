"""The credentials of remote media workers (D6): owner-issued, one per worker, narrow and expiring.

A credential lives in the media job ledger, in machine-local service state beside the leases
it admits. The ledger stores the SHA-256 of the token, never the token. A credential admits the
`/media-worker/v1/` routes and nothing else, and its `id` is the worker identity of its leases.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .media_jobs import MediaJobStore

SCHEMA = """
    CREATE TABLE IF NOT EXISTS media_worker_credentials (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        token_sha256 TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        max_leases INTEGER NOT NULL,
        revoked_at REAL,
        last_seen_at REAL
    )
"""

DEFAULT_DAYS = 90
DEFAULT_MAX_LEASES = 1
MAX_DAYS = 3650
MAX_LEASES = 64
# A format check on a label, not a vocabulary: it keeps a name printable and free of separators.
NAME_FORMAT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_TOKEN_PREFIX = "mw1_"
_MAX_TOKEN_CHARS = 256


class CredentialError(ValueError):
    """The request for a credential is invalid."""


@dataclass(frozen=True)
class WorkerCredential:
    id: str
    name: str
    created_at: float
    expires_at: float
    max_leases: int
    revoked_at: float | None = None
    last_seen_at: float | None = None

    def status(self, now: float) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "expired" if self.expires_at <= now else "active"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _record(row) -> WorkerCredential:
    return WorkerCredential(
        id=str(row["id"]), name=str(row["name"]), created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]), max_leases=int(row["max_leases"]),
        revoked_at=None if row["revoked_at"] is None else float(row["revoked_at"]),
        last_seen_at=None if row["last_seen_at"] is None else float(row["last_seen_at"]),
    )


def new_token() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


def issue(
    store: MediaJobStore, token: str, *, name: str, days: int = DEFAULT_DAYS, max_leases: int = DEFAULT_MAX_LEASES
) -> WorkerCredential:
    """Record `token`'s hash as a new credential. The caller keeps the token, which this never stores."""
    if NAME_FORMAT.fullmatch(name) is None:
        raise CredentialError("the name must be 1-64 of a-z, 0-9, '.', '_' or '-', starting alphanumeric")
    if not 1 <= days <= MAX_DAYS:
        raise CredentialError(f"days must be between 1 and {MAX_DAYS}")
    if not 1 <= max_leases <= MAX_LEASES:
        raise CredentialError(f"max leases must be between 1 and {MAX_LEASES}")
    now = time.time()
    credential = WorkerCredential(
        id="mwc_" + secrets.token_hex(6), name=name, created_at=now,
        expires_at=now + days * 86400, max_leases=max_leases,
    )
    conn = store._connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO media_worker_credentials(id, name, token_sha256, created_at, expires_at, max_leases) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (credential.id, name, _digest(token), now, credential.expires_at, max_leases),
            )
    finally:
        conn.close()
    return credential


def authenticate(store: MediaJobStore, token: str) -> WorkerCredential | None:
    """The credential that `token` belongs to, or None for an unknown, expired or revoked one.

    Hashes are compared in constant time against every credential, so the time taken does not
    tell a caller which credential its token came closest to.
    """
    if not token or len(token) > _MAX_TOKEN_CHARS:
        return None
    presented = _digest(token)
    conn = store._connect(readonly=True)
    try:
        rows = conn.execute("SELECT * FROM media_worker_credentials").fetchall()
    finally:
        conn.close()
    found = None
    for row in rows:
        if hmac.compare_digest(str(row["token_sha256"]), presented):
            found = _record(row)
    if found is None or found.status(time.time()) != "active":
        return None
    return found


def touch(store: MediaJobStore, credential_id: str) -> None:
    """Record that the worker was heard from now."""
    conn = store._connect()
    try:
        with conn:
            conn.execute(
                "UPDATE media_worker_credentials SET last_seen_at = ? WHERE id = ?", (time.time(), credential_id)
            )
    finally:
        conn.close()


def list_credentials(store: MediaJobStore) -> list[WorkerCredential]:
    conn = store._connect(readonly=True)
    try:
        return [_record(row) for row in conn.execute("SELECT * FROM media_worker_credentials ORDER BY created_at, id")]
    finally:
        conn.close()


def revoke(store: MediaJobStore, name: str) -> int:
    """Revoke every live credential of this name and return how many. A revoked credential stays listed."""
    conn = store._connect()
    try:
        with conn:
            return conn.execute(
                "UPDATE media_worker_credentials SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (time.time(), name),
            ).rowcount
    finally:
        conn.close()
