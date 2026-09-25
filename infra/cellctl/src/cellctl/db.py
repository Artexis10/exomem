"""asyncpg persistence for cellctl.

cellctl holds no database of its own: this module only reads C1-C1d rows and
writes the columns D4/D7 give it (observed columns, C1c, and the C1d
columns listed in the C1 privilege table for exomem_cellctl). state.py's
dataclasses are the shape; decide.py is the only decision logic.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import asyncpg

from .state import CellRow, RolloutRow

logger = logging.getLogger("cellctl")

# D4: checked when a row is read, before cell_id can reach a name, a URL, an
# object-storage prefix or a shell string. Migration 0056 also enforces this
# with a CHECK; cellctl does not rely on it (SR-M3). Always used with
# fullmatch: `$` also matches just before a trailing newline.
CELL_ID_RE = re.compile(r"[a-z2-7]{16}")

OBSERVED_COLUMNS = (
    "observed_generation",
    "observed_state",
    "observed_image",
    "ready",
    "last_error_code",
    "observed_at",
    "node",
    "volume_id",
    "last_backup_at",
    "last_backup_snapshot",
    "backup_key_wrapped",
    "backup_key_version",
    "b2_key_id",
    "b2_key_wrapped",
    "b2_key_version",
    "hold_kind",
    "hold_started_at",
)

ROLLOUT_COLUMNS = ("paused", "error_code", "held_cell_id", "last_good_image")

CELL_COLUMNS = (
    "cell_id",
    "tenant_id",
    "storage_gib",
    "rollout_priority",
    "desired_state",
    "desired_image",
    "generation",
    *OBSERVED_COLUMNS,
)

NOTIFY_CHANNEL = "exomem_cloud_cells"

# D4: the direct session survives a partition. TCP keepalives (idle 30 s,
# then 3 probes 10 s apart) make the server reap a dead session, and release
# its advisory lock, about a minute after the partition instead of after the
# two-hour server default. The 30 s statement timeout, applied both by the
# server and as asyncpg's client-side command timeout, bounds every call
# cellctl makes: each is a single-row read or write or one fleet SELECT that
# takes milliseconds on a healthy server, so 30 s is two orders of magnitude
# of headroom, and it is well inside the 120 s liveness bound, so a call
# stuck on a half-open socket surfaces as a reconnect rather than as a
# wedged loop that liveness restarts.
STATEMENT_TIMEOUT_SECONDS = 30
SESSION_SETTINGS = {
    "tcp_keepalives_idle": "30",
    "tcp_keepalives_interval": "10",
    "tcp_keepalives_count": "3",
    "statement_timeout": str(STATEMENT_TIMEOUT_SECONDS * 1000),
}


async def connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        dsn, command_timeout=STATEMENT_TIMEOUT_SECONDS, server_settings=SESSION_SETTINGS
    )


def _row_from_record(record: asyncpg.Record) -> CellRow:
    return CellRow(**{column: record[column] for column in CELL_COLUMNS})


def _rollout_from_record(record: asyncpg.Record) -> RolloutRow:
    return RolloutRow(**{column: record[column] for column in ROLLOUT_COLUMNS})


async def select_all_rows(connection: asyncpg.Connection) -> list[CellRow]:
    records = await connection.fetch(f"SELECT {', '.join(CELL_COLUMNS)} FROM exomem_cloud_cells")
    rows = []
    for record in records:
        row = _row_from_record(record)
        if not CELL_ID_RE.fullmatch(row.cell_id):
            logger.error("skipping row with malformed cell_id: %r", row.cell_id)
            continue
        rows.append(row)
    return rows


async def read_rollout(connection: asyncpg.Connection) -> RolloutRow:
    record = await connection.fetchrow(
        f"SELECT {', '.join(ROLLOUT_COLUMNS)} FROM exomem_cloud_rollout WHERE id = 1"
    )
    if record is None:
        raise RuntimeError("exomem_cloud_rollout has no row with id = 1")
    return _rollout_from_record(record)


async def read_cell_image(connection: asyncpg.Connection) -> str | None:
    value = await connection.fetchval(
        "SELECT value FROM exomem_cloud_settings WHERE key = 'cell_image'"
    )
    if value is None:
        return None
    decoded = json.loads(value) if isinstance(value, str) else value
    return decoded if isinstance(decoded, str) else None


def _assignments(columns: dict[str, object], *, start: int) -> tuple[str, list[object]]:
    clauses = []
    values: list[object] = []
    for index, (column, value) in enumerate(columns.items()):
        clauses.append(f"{column} = ${start + index}")
        values.append(value)
    return ", ".join(clauses), values


async def write_observed(
    connection: asyncpg.Connection, cell_id: str, updates: dict[str, object]
) -> None:
    if not updates:
        return
    unknown = set(updates) - set(OBSERVED_COLUMNS)
    if unknown:
        raise ValueError(f"not an observed column: {sorted(unknown)}")
    assignments, values = _assignments(updates, start=2)
    await connection.execute(
        f"UPDATE exomem_cloud_cells SET {assignments} WHERE cell_id = $1", cell_id, *values
    )


async def write_rollout(connection: asyncpg.Connection, updates: dict[str, object]) -> None:
    if not updates:
        return
    unknown = set(updates) - set(ROLLOUT_COLUMNS)
    if unknown:
        raise ValueError(f"not a rollout column: {sorted(unknown)}")
    assignments, values = _assignments(updates, start=1)
    await connection.execute(f"UPDATE exomem_cloud_rollout SET {assignments} WHERE id = 1", *values)


async def try_write_once(
    connection: asyncpg.Connection, cell_id: str, column: str, value: object
) -> bool:
    """D7 compare-and-set: sets `column` only while it is NULL.

    Returns whether this call won the race. The caller re-reads the row on a
    loss and uses the value that is already stored.
    """

    if column not in OBSERVED_COLUMNS:
        raise ValueError(f"not an observed column: {column}")
    result = await connection.execute(
        f"UPDATE exomem_cloud_cells SET {column} = $2 WHERE cell_id = $1 AND {column} IS NULL",
        cell_id,
        value,
    )
    return result == "UPDATE 1"


async def try_write_once_group(
    connection: asyncpg.Connection, cell_id: str, columns: dict[str, object], *, first_column: str
) -> bool:
    """D7: like `try_write_once`, but writes every column in `columns`
    together, in one statement, gated on `first_column` alone being NULL.

    D7 requires this for `backup_key_wrapped`+`backup_key_version` and for
    `b2_key_id`+`b2_key_wrapped`+`b2_key_version`: writing the group as two
    separate statements left a crash between them able to wedge every later
    pass on that row (H5).
    """

    if first_column not in columns:
        raise ValueError(f"first_column {first_column!r} must be one of {sorted(columns)}")
    unknown = set(columns) - set(OBSERVED_COLUMNS)
    if unknown:
        raise ValueError(f"not an observed column: {sorted(unknown)}")
    assignments, values = _assignments(columns, start=2)
    result = await connection.execute(
        f"UPDATE exomem_cloud_cells SET {assignments} WHERE cell_id = $1 AND {first_column} IS NULL",
        cell_id,
        *values,
    )
    return result == "UPDATE 1"


# D4: single-writer guard for the direct LISTEN session. `Recreate` alone
# does not stop two pods overlapping during an eviction; the lock does.
# Fixed, arbitrary 63-bit key: there is exactly one lock cellctl ever takes.
ADVISORY_LOCK_KEY = 0x63656C6C6374_6C  # "cellctl" mod 2^63, picked once and never reused


async def try_advisory_lock(connection: asyncpg.Connection) -> bool:
    return bool(await connection.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY))


async def write_capacity(
    connection: asyncpg.Connection,
    *,
    node: str,
    cell_slots: int,
    attachments_used: int,
    observed_at: datetime,
) -> None:
    await connection.execute(
        "INSERT INTO exomem_cloud_capacity (node, cell_slots, attachments_used, observed_at) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (node) DO UPDATE SET "
        "cell_slots = EXCLUDED.cell_slots, "
        "attachments_used = EXCLUDED.attachments_used, "
        "observed_at = EXCLUDED.observed_at",
        node,
        cell_slots,
        attachments_used,
        observed_at,
    )


async def zero_absent_capacity(connection: asyncpg.Connection, *, present_nodes: list[str], observed_at: datetime) -> None:
    """D9: a node no longer in the cluster has its row's cell_slots set to 0.
    cellctl holds only INSERT and UPDATE on C1c, and admission sums
    cell_slots, so a zero row offers nothing. A node that rejoins is
    republished with its real count by write_capacity."""

    await connection.execute(
        "UPDATE exomem_cloud_capacity SET cell_slots = 0, observed_at = $2 "
        "WHERE NOT (node = ANY($1::text[])) AND cell_slots <> 0",
        present_nodes,
        observed_at,
    )
