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

from .capacity import NodeCapacity
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
    "cell_root_key_wrapped",
    "cell_root_key_version",
    "content_keys_retired_at",
    "last_live_key_destroyed_at",
    "key_retention_sources",
    "keys_expire_at",
    "custody_observation",
    "relocation_target_storage_class",
    "relocation_old_volume_id",
    "relocation_accepted_at",
    "b2_key_id",
    "b2_key_wrapped",
    "b2_key_version",
    "hold_kind",
    "hold_started_at",
    # move-cloud-cells-to-local-storage D10, Substrate migration 0059.
    "grown_storage_gib",
    "observed_storage_pvc_uid",
    "observed_storage_pvc_version",
    "observed_storage_capacity_bytes",
    "observed_storage_generation",
    "observed_storage_at",
    # Migration 0066: the disk use the last backup or export Job measured.
    "used_bytes",
    "filesystem_bytes",
    "usage_observed_at",
    "import_limits",
)

# add-cloud-media-store D5, Substrate migration 0081: the media key bundle, wrapped under
# the cell root. Kept apart from the columns above, so a cellctl whose schema predates the
# migration can still read and retire every cell: each call that reads or writes them says so.
MEDIA_KEY_COLUMNS = ("media_key_wrapped", "media_key_version")
MEDIA_KEY_MIGRATION = "0081_exomem_cloud_media_keys"

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
    "created_at",
)

# The columns cellctl writes. A media key write reaches the server only on a schema that has them.
WRITABLE_COLUMNS = (*OBSERVED_COLUMNS, *MEDIA_KEY_COLUMNS)

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


def _cell_columns(media_keys: bool) -> str:
    return ", ".join((*CELL_COLUMNS, *MEDIA_KEY_COLUMNS) if media_keys else CELL_COLUMNS)


def _row_from_record(record: asyncpg.Record) -> CellRow:
    values = {column: record[column] for column in CELL_COLUMNS}
    # A read without the media key columns leaves them at the row's default, None.
    values.update({column: record[column] for column in MEDIA_KEY_COLUMNS if column in record})
    if isinstance(values["import_limits"], str):
        values["import_limits"] = json.loads(values["import_limits"])
    return CellRow(**values)


async def has_media_key_columns(connection: asyncpg.Connection) -> bool:
    """Whether the schema cellctl runs against has the media key columns of migration 0081."""
    present = await connection.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'exomem_cloud_cells' AND column_name = ANY($1::text[])", list(MEDIA_KEY_COLUMNS))
    return present == len(MEDIA_KEY_COLUMNS)


def _rollout_from_record(record: asyncpg.Record) -> RolloutRow:
    return RolloutRow(**{column: record[column] for column in ROLLOUT_COLUMNS})


async def select_all_rows(connection: asyncpg.Connection, *, media_keys: bool = False) -> list[CellRow]:
    records = await connection.fetch(f"SELECT {_cell_columns(media_keys)} FROM exomem_cloud_cells")
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


async def lock_cell(connection: asyncpg.Connection, cell_id: str, *, media_keys: bool = False) -> CellRow:
    """Serialize key conversion with deletion and competing controller retries."""
    record = await connection.fetchrow(
        f"SELECT {_cell_columns(media_keys)} FROM exomem_cloud_cells WHERE cell_id=$1 FOR UPDATE", cell_id)
    if record is None:
        raise ValueError("cell no longer exists")
    return _row_from_record(record)


async def retire_content_keys(connection: asyncpg.Connection, now: datetime, *, media_keys: bool = False) -> None:
    """Retire decryption material before any provider cleanup can block. With `media_keys`,
    the media key bundle too; a schema without its columns holds no bundle to retire."""
    media_set = "media_key_wrapped=NULL, media_key_version=NULL, " if media_keys else ""
    media_held = " OR media_key_wrapped IS NOT NULL" if media_keys else ""
    await connection.execute(
        "UPDATE exomem_cloud_cells SET cell_root_key_wrapped=NULL, cell_root_key_version=NULL, "
        f"backup_key_wrapped=NULL, backup_key_version=NULL, {media_set}"
        "content_keys_retired_at=COALESCE(content_keys_retired_at,$1) "
        "WHERE desired_state='deleted' AND (content_keys_retired_at IS NULL "
        f"OR cell_root_key_wrapped IS NOT NULL OR backup_key_wrapped IS NOT NULL{media_held})", now)


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
    unknown = set(updates) - set(WRITABLE_COLUMNS)
    if unknown:
        raise ValueError(f"not an observed column: {sorted(unknown)}")
    updates = {key: json.dumps(value) if key == "import_limits" and value is not None else value
               for key, value in updates.items()}
    assignments, values = _assignments(updates, start=2)
    await connection.execute(
        f"UPDATE exomem_cloud_cells SET {assignments} WHERE cell_id = $1", cell_id, *values
    )


async def write_rollout(
    connection: asyncpg.Connection,
    updates: dict[str, object],
    *,
    only_if: dict[str, object] | None = None,
) -> bool:
    """Writes `updates` to the rollout row. With `only_if`, writes only while
    each named column still holds that value, so a pause the owner route
    wrote after cellctl read the row is never overwritten. Returns whether
    the row was written."""

    if not updates:
        return False
    only_if = only_if or {}
    unknown = (set(updates) | set(only_if)) - set(ROLLOUT_COLUMNS)
    if unknown:
        raise ValueError(f"not a rollout column: {sorted(unknown)}")
    assignments, values = _assignments(updates, start=1)
    conditions = ["id = 1"]
    for index, (column, value) in enumerate(only_if.items(), start=len(values) + 1):
        conditions.append(f"{column} IS NOT DISTINCT FROM ${index}")
        values.append(value)
    result = await connection.execute(
        f"UPDATE exomem_cloud_rollout SET {assignments} WHERE {' AND '.join(conditions)}", *values
    )
    return result == "UPDATE 1"


async def try_write_once(
    connection: asyncpg.Connection, cell_id: str, column: str, value: object
) -> bool:
    """D7 compare-and-set: sets `column` only while it is NULL.

    Returns whether this call won the race. The caller re-reads the row on a
    loss and uses the value that is already stored.
    """

    if column not in WRITABLE_COLUMNS:
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
    unknown = set(columns) - set(WRITABLE_COLUMNS)
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


async def write_capacity_snapshot(
    connection: asyncpg.Connection, *, capacities: dict[str, NodeCapacity], observed_at: datetime,
) -> None:
    """Publish one complete pass under the same lock as invite redemption."""
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext('exomem-cloud-capacity'))")
        for node, capacity in capacities.items():
            await write_capacity(connection, node=node, cell_slots=capacity.cell_slots,
                                 attachments_used=capacity.attachments_used, observed_at=observed_at)
        await zero_absent_capacity(connection, present_nodes=list(capacities), observed_at=observed_at)


async def read_import(connection: asyncpg.Connection, cell_id: str):
    from .imports import ImportRow

    # Migration 0061 fixes the open predicate; terminal history only repairs an orphan C1 hold.
    record = await connection.fetchrow(
        "SELECT * FROM (SELECT i.*, (observed_state IS NULL OR "
        "observed_state IN ('key_ready','importing','cleanup_running','cleanup_stopping','cleanup_discard_stopping','cleanup_pending') OR "
        "(requested_state='discard_requested' AND observed_state IS DISTINCT FROM 'discarded')) AS is_open "
        "FROM exomem_cloud_imports i WHERE cell_id=$1) requests WHERE is_open OR "
        "EXISTS (SELECT 1 FROM exomem_cloud_cells c WHERE c.cell_id=$1 AND c.hold_kind='import') "
        "ORDER BY is_open DESC, created_at DESC, import_id DESC LIMIT 1", cell_id,
    )
    if record is None:
        return None
    fields = {name: record[name] for name in ImportRow.__dataclass_fields__}
    fields["import_id"] = str(fields["import_id"])
    for name in ("part_etags", "part_urls"):
        if isinstance(fields[name], str):
            fields[name] = json.loads(fields[name])
    return ImportRow(**fields)


async def write_import(connection: asyncpg.Connection, import_id: str, updates: dict) -> None:
    import uuid

    # C5 column grants fix this controller-owned subset.
    allowed = {"recipient", "upload_id", "part_urls", "urls_expire_at", "observed_state", "error_code",
               "staged_files", "staged_bytes"}
    if not updates:
        return
    if set(updates) - allowed:
        raise ValueError("not an import observation column")
    values = {name: json.dumps(value) if name == "part_urls" and value is not None else value
              for name, value in updates.items()}
    assignments, parameters = _assignments(values, start=2)
    await connection.execute(f"UPDATE exomem_cloud_imports SET {assignments} WHERE import_id=$1",
                             uuid.UUID(import_id), *parameters)


async def finalize_import(
    connection: asyncpg.Connection, cell_id: str, import_id: str, *, media_keys: bool = False
) -> CellRow | None:
    """Publish cleanup completion and release admission under the API's C1-first row lock."""
    import uuid

    async with connection.transaction():
        record = await connection.fetchrow(
            f"SELECT {_cell_columns(media_keys)} FROM exomem_cloud_cells WHERE cell_id=$1 FOR UPDATE", cell_id,
        )
        if record is None or record["desired_state"] == "deleted" or record["hold_kind"] not in (None, "import"):
            return None
        item = await connection.fetchrow(
            "SELECT requested_state, observed_state, error_code FROM exomem_cloud_imports "
            "WHERE cell_id=$1 AND import_id=$2 FOR UPDATE", cell_id, uuid.UUID(import_id),
        )
        # C5 fixes these stopping observations as proof of a completed cleanup phase.
        if item is None or item["observed_state"] not in ("cleanup_stopping", "cleanup_discard_stopping"):
            return None
        # Keep-adoption completion cannot acknowledge newer full-discard intent.
        if item["observed_state"] == "cleanup_stopping" and item["requested_state"] in ("cancelled", "discard_requested"):
            await write_import(connection, import_id, {"observed_state": "cleanup_running"})
            started_at = await connection.fetchval("SELECT clock_timestamp()")
            await write_observed(connection, cell_id, {"hold_kind": "import", "hold_started_at": started_at})
            return None
        # C5 fixes intent, expiry codes and terminal observations.
        if item["requested_state"] in ("cancelled", "discard_requested"):
            terminal = "discarded"
        elif item["error_code"] in ("IMPORT_EXPIRED", "IMPORT_HOLD_EXPIRED"):
            terminal = "expired"
        else:
            terminal = "failed" if item["error_code"] else "done"
        await write_import(connection, import_id, {"observed_state": terminal})
        await write_observed(connection, cell_id, {"hold_kind": None, "hold_started_at": None,
                                                  "last_error_code": None})
        return _row_from_record(record)


async def read_exports(connection: asyncpg.Connection, cell_id: str):
    """C6 rows that still need cellctl, oldest first: the one being packed,
    every ready one (links, expiry, supersede), every ended one whose upload
    removal is still pending, and the newest one, whose ending releases an
    export hold."""
    from .exports import ExportRow

    records = await connection.fetch(
        "SELECT * FROM exomem_cloud_exports WHERE cell_id=$1 AND ("
        "observed_state IS NULL OR observed_state IN ('packing','ready') OR upload_id IS NOT NULL "
        "OR archive_version_id IS NOT NULL OR export_id = ("
        "SELECT export_id FROM exomem_cloud_exports WHERE cell_id=$1 ORDER BY created_at DESC, export_id DESC LIMIT 1)) "
        "ORDER BY created_at, export_id", cell_id,
    )
    rows = []
    for record in records:
        fields = {name: record[name] for name in ExportRow.__dataclass_fields__}
        fields["export_id"] = str(fields["export_id"])
        rows.append(ExportRow(**fields))
    return rows


# C6 column grants fix this controller-owned subset.
EXPORT_COLUMNS = frozenset({
    "observed_state", "error_code", "archive_bytes", "archive_sha256", "archive_version_id", "upload_id",
    "ready_at", "expires_at", "download_url", "download_url_expires_at", "packing_report",
})


async def write_export(connection: asyncpg.Connection, export_id: str, updates: dict, *, link_issued: bool = False) -> None:
    """Writes cellctl's C6 observation. `link_issued` records the link this
    write carries as answering every request up to now: never earlier than
    the request it answers, so a request is never answered twice."""
    import uuid

    if set(updates) - EXPORT_COLUMNS:
        raise ValueError("not an export observation column")
    if not updates and not link_issued:
        return
    assignments, parameters = _assignments(updates, start=2)
    if link_issued:
        assignments = ", ".join(filter(None, [
            assignments, "link_issued_at = GREATEST(clock_timestamp(), COALESCE(link_requested_at, '-infinity'))"]))
    if updates.get("observed_state") == "ready":
        # Lifecycle and export requests lock the cell before its export; no network work occurs here.
        async with connection.transaction():
            cell_id = await connection.fetchval("SELECT cell_id FROM exomem_cloud_exports WHERE export_id=$1",
                                                uuid.UUID(export_id))
            if cell_id is None:
                return
            desired = await connection.fetchval(
                "SELECT desired_state FROM exomem_cloud_cells WHERE cell_id=$1 FOR UPDATE", cell_id)
            if desired is None or desired == "deleted":
                return
            requested = await connection.fetchval(
                "SELECT requested_state FROM exomem_cloud_exports WHERE export_id=$1 AND cell_id=$2 FOR UPDATE",
                uuid.UUID(export_id), cell_id)
            if requested is None or requested == "superseded":
                return
            await connection.execute(f"UPDATE exomem_cloud_exports SET {assignments} WHERE export_id=$1",
                                     uuid.UUID(export_id), *parameters)
        return
    await connection.execute(f"UPDATE exomem_cloud_exports SET {assignments} WHERE export_id=$1",
                             uuid.UUID(export_id), *parameters)
