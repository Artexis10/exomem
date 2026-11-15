"""automate-cloud-releases D7: cellctl refuses to start on a schema it cannot
fully read or write.

`check_schema` names every gap -- a missing table, a missing column, or a
missing SELECT/INSERT/UPDATE privilege on a column cellctl reads or writes.
It asks `to_regclass`, `pg_catalog` and `has_column_privilege` only, so it
never writes; `pg_catalog`, not `information_schema`, because the information
schema hides objects a revoked caller may not see, which would report a
privilege gap as a missing table. `python -m cellctl.schema_check --dsn-env
VAR` runs the same check for the platform upgrade command, printing each gap
and exiting non-zero while one remains.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import access_record as access
from . import db

# Required columns come from the row shapes and SQL contracts at their call sites.
# Media keys, deletion grants and operation grants remain backward-compatible.
REQUIRED_TABLES: dict[str, dict[str, frozenset[str]]] = {
    "exomem_cloud_cells": {
        "SELECT": frozenset(db.CELL_COLUMNS),
        "UPDATE": frozenset(db.OBSERVED_COLUMNS),
    },
    "exomem_cloud_rollout": {
        "SELECT": frozenset(db.ROLLOUT_READ_COLUMNS),
        "UPDATE": frozenset(db.ROLLOUT_COLUMNS),
    },
    "exomem_cloud_settings": {"SELECT": frozenset(db.SETTINGS_COLUMNS)},
    db.IMAGE_HISTORY_TABLE: {"SELECT": frozenset(db.IMAGE_HISTORY_COLUMNS)},
    "exomem_cloud_imports": {
        "SELECT": db.IMPORT_READ_COLUMNS,
        "UPDATE": db.IMPORT_COLUMNS,
    },
    "exomem_cloud_exports": {
        "SELECT": db.EXPORT_READ_COLUMNS,
        "UPDATE": db.EXPORT_UPDATE_COLUMNS,
    },
    "exomem_cloud_capacity": {
        "SELECT": frozenset(db.CAPACITY_COLUMNS),
        "INSERT": frozenset(db.CAPACITY_COLUMNS),
        "UPDATE": frozenset(db.CAPACITY_UPDATE_COLUMNS),
    },
}

FEATURE_TABLES: dict[str, dict[str, dict[str, frozenset[str]]]] = {
    "access_record": {
        "exomem_cloud_placements": {
            "SELECT": frozenset(access.PLACEMENT_COLUMNS),
            "INSERT": frozenset(access.PLACEMENT_COLUMNS),
            "UPDATE": frozenset(access.PLACEMENT_UPDATE_COLUMNS),
        },
        "exomem_cloud_access_receipts": {
            "SELECT": frozenset(access.RECEIPT_COLUMNS),
            "UPDATE": frozenset(access.RECEIPT_UPDATE_COLUMNS),
        },
        "exomem_cloud_access_records": {
            "SELECT": frozenset(access.RECORD_READ_COLUMNS),
            "INSERT": frozenset(access.RECORD_INSERT_COLUMNS),
        },
        "exomem_cloud_access_entries": {
            "SELECT": frozenset(access.ENTRY_COLUMNS),
            "INSERT": frozenset(access.ENTRY_COLUMNS),
        },
    },
}


class SchemaGap(RuntimeError):
    """A table or column cellctl reads or writes is missing or unreadable."""


async def check_schema(connection, *, access_record: bool = False) -> list[str]:
    """The name of every missing table, column or privilege, empty on a whole schema.

    `access_record` matches ClusterConfig.access_record_enabled: the four
    access-record tables are required only while that loop runs."""

    tables = dict(REQUIRED_TABLES)
    if access_record:
        tables.update(FEATURE_TABLES["access_record"])
    gaps: list[str] = []
    for table, accesses in tables.items():
        # pg_catalog, not information_schema: the information schema hides
        # objects the caller holds no privilege on, which would report a
        # revoked table as missing and never reach the privilege check.
        present = await connection.fetchval(
            "SELECT to_regclass(format('%I.%I', current_schema(), $1::text)) IS NOT NULL",
            table,
        )
        if not present:
            gaps.append(f"table {table} is missing")
            continue
        found = {
            record["attname"]
            for record in await connection.fetch(
                "SELECT a.attname FROM pg_catalog.pg_attribute a"
                " JOIN pg_catalog.pg_class c ON c.oid = a.attrelid"
                " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
                " WHERE c.relname = $1 AND n.nspname = current_schema()"
                " AND a.attnum > 0 AND NOT a.attisdropped",
                table,
            )
        }
        columns = frozenset().union(*accesses.values())
        for column in sorted(columns - found):
            gaps.append(f"column {table}.{column} is missing")
        for privilege, required_columns in accesses.items():
            # SELECT * needs grants on all present columns, including optional or newly added ones.
            selected_columns = (found if privilege == "SELECT" and table in
                                db.SELECT_ALL_TABLES | access.SELECT_ALL_TABLES else required_columns & found)
            for column in sorted(selected_columns):
                if not await connection.fetchval(
                    "SELECT has_column_privilege($1, $2, $3)", table, column, privilege,
                ):
                    gaps.append(f"{privilege} privilege on {table}.{column} is missing")
    return gaps


async def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m cellctl.schema_check")
    parser.add_argument("--dsn-env", required=True,
                        help="environment variable holding the cellctl database DSN")
    parser.add_argument("--access-record", action="store_true",
                        help="also require the access-record tables, as cellctl does when "
                             "cellctl.accessRecord.enabled renders CELLCTL_ACCESS_RECORD_ENABLED")
    args = parser.parse_args()
    dsn = os.environ.get(args.dsn_env)
    if dsn is None:
        print(f"{args.dsn_env} is not set", file=sys.stderr)
        return 2
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        gaps = await check_schema(connection, access_record=args.access_record)
    finally:
        await connection.close()
    for gap in gaps:
        print(gap)
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
