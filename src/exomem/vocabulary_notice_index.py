"""Machine-local exact deduplication for transient vocabulary advisories."""

from __future__ import annotations

import time
from pathlib import Path

from . import deferred_index

_CLEANUP_BATCH = 64


def _ensure_schema(connection) -> None:  # noqa: ANN001
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_notification_reservations (
            context TEXT NOT NULL,
            item_ref TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            reserved_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            PRIMARY KEY(context, item_ref, fingerprint)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_notification_expiry "
        "ON vocabulary_notification_reservations(expires_at)"
    )


def reserve(
    vault_root: Path,
    *,
    context: str,
    item_ref: str,
    fingerprint: str,
    expires_at: float,
    now: float | None = None,
    reserved_at: float | None = None,
) -> bool:
    """Atomically reserve one exact advisory after a fixed-size expiry cleanup."""
    moment = time.time() if now is None else now
    stamped = moment if reserved_at is None else reserved_at
    with deferred_index._connect(vault_root, create=True) as connection:
        _ensure_schema(connection)
        connection.execute(
            "DELETE FROM vocabulary_notification_reservations WHERE rowid IN "
            "(SELECT rowid FROM vocabulary_notification_reservations "
            "WHERE expires_at < ? ORDER BY expires_at LIMIT ?)",
            (moment, _CLEANUP_BATCH),
        )
        connection.execute(
            "DELETE FROM vocabulary_notification_reservations "
            "WHERE context = ? AND item_ref = ? AND fingerprint = ? AND expires_at < ?",
            (context, item_ref, fingerprint, moment),
        )
        existing = connection.execute(
            "SELECT 1 FROM vocabulary_notification_reservations "
            "WHERE context = ? AND item_ref = ? AND fingerprint = ?",
            (context, item_ref, fingerprint),
        ).fetchone()
        if existing is not None:
            connection.commit()
            return False
        cursor = connection.execute(
            "INSERT OR IGNORE INTO vocabulary_notification_reservations "
            "(context, item_ref, fingerprint, reserved_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (context, item_ref, fingerprint, stamped, expires_at),
        )
        connection.commit()
        return cursor.rowcount == 1
