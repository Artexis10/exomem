"""Encrypted, replica-local cache for successful session validations."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedSessionValidation:
    claims: dict[str, Any]
    validated_at: float


class SessionValidationCache:
    """Persist successful validations without ever storing the raw bearer."""

    def __init__(self, path: Path, *, encryption_key: bytes) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(encryption_key)
        self._lock = threading.RLock()
        self._blocked_digests: set[str] = set()
        self._stale_disabled = False
        self._initialize()

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_validations (
                    token_digest TEXT PRIMARY KEY,
                    encrypted_value BLOB NOT NULL
                )
                """
            )

    def upsert(
        self,
        token: str,
        claims: Mapping[str, Any],
        *,
        validated_at: float,
    ) -> None:
        timestamp = float(validated_at)
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError("validated_at must be a positive finite timestamp")
        plaintext = json.dumps(
            {"claims": dict(claims), "validated_at": timestamp},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)
        digest = self.token_digest(token)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_validations(token_digest, encrypted_value)
                VALUES (?, ?)
                ON CONFLICT(token_digest) DO UPDATE SET
                    encrypted_value = excluded.encrypted_value
                """,
                (digest, encrypted),
            )
            self._blocked_digests.discard(digest)

    def get(self, token: str) -> CachedSessionValidation | None:
        digest = self.token_digest(token)
        try:
            with self._lock:
                if self._stale_disabled or digest in self._blocked_digests:
                    return None
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT encrypted_value FROM session_validations WHERE token_digest = ?",
                        (digest,),
                    ).fetchone()
                if row is None:
                    return None
                return self._decode(bytes(row[0]))
        except (InvalidToken, KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
            logger.warning("event=session_validation_cache_read_failed")
            return None

    def _decode(self, encrypted: bytes) -> CachedSessionValidation:
        plaintext = self._fernet.decrypt(encrypted)
        payload = json.loads(plaintext)
        claims = payload["claims"]
        validated_at = float(payload["validated_at"])
        if (
            not isinstance(claims, dict)
            or not math.isfinite(validated_at)
            or validated_at <= 0
        ):
            raise ValueError("invalid cached validation")
        return CachedSessionValidation(claims=dict(claims), validated_at=validated_at)

    def delete(self, token: str) -> None:
        digest = self.token_digest(token)
        with self._lock:
            self._blocked_digests.add(digest)
        try:
            with self._lock, self._connect() as connection:
                removed = connection.execute(
                    "DELETE FROM session_validations WHERE token_digest = ?",
                    (digest,),
                ).rowcount
                if removed == 0:
                    # No row existed, so there is nothing for a block to guard.
                    # Dropping the digest keeps unauthenticated garbage bearers
                    # (every failed validation lands here) from growing this
                    # set without bound; the exception path below keeps the
                    # block so a failed DELETE still cannot lose a revocation.
                    self._blocked_digests.discard(digest)
        except sqlite3.Error:
            logger.warning("event=session_validation_cache_delete_failed")

    def _delete_matching(self, field: str, value: str) -> None:
        with self._lock:
            was_disabled = self._stale_disabled
            self._stale_disabled = True
            try:
                with self._connect() as connection:
                    rows = connection.execute(
                        "SELECT token_digest, encrypted_value FROM session_validations"
                    ).fetchall()
                    digests: list[str] = []
                    for digest, encrypted in rows:
                        try:
                            cached = self._decode(bytes(encrypted))
                        except (
                            InvalidToken,
                            KeyError,
                            TypeError,
                            ValueError,
                            json.JSONDecodeError,
                        ):
                            continue
                        if cached.claims.get(field) == value:
                            digests.append(str(digest))
                    connection.executemany(
                        "DELETE FROM session_validations WHERE token_digest = ?",
                        ((digest,) for digest in digests),
                    )
                    self._blocked_digests.update(digests)
            except sqlite3.Error:
                logger.warning("event=session_validation_cache_delete_failed")
                return
            self._stale_disabled = was_disabled

    def delete_session(self, session_id: str) -> None:
        self._delete_matching("session_id", session_id)

    def delete_family(self, family_id: str) -> None:
        """Invalidate cached access sessions belonging to a revoked refresh family."""
        self._delete_matching("family_id", family_id)

    def clear(self) -> None:
        with self._lock:
            self._stale_disabled = True
            try:
                with self._connect() as connection:
                    connection.execute("DELETE FROM session_validations")
            except sqlite3.Error:
                logger.warning("event=session_validation_cache_delete_failed")
                return
            self._blocked_digests.clear()
            self._stale_disabled = False


class SessionStoreTelemetry:
    """Process-local, content-free session-store readiness counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._degraded = False
        self._stale_served_count = 0

    def remote_ok(self) -> None:
        with self._lock:
            self._degraded = False

    def remote_unavailable(self, *, stale_active: bool) -> None:
        with self._lock:
            self._degraded = stale_active

    def stale_served(self) -> int:
        with self._lock:
            self._stale_served_count += 1
            return self._stale_served_count

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": "degraded" if self._degraded else "ok",
                "stale_served_count": self._stale_served_count,
            }


session_store_telemetry = SessionStoreTelemetry()


def session_store_readiness() -> dict[str, Any]:
    return session_store_telemetry.snapshot()
