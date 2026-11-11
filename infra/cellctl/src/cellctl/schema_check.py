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

from .db import CELL_COLUMNS, EXPORT_COLUMNS, OBSERVED_COLUMNS, ROLLOUT_COLUMNS

# Every table cellctl touches, with the columns its SQL reads and writes.
# SELECT * statements take their columns from this list too: a column a
# `SELECT *` row is read by name under the same contract. Grants to
# exomem_cellctl are column-scoped on cells, rollout, imports and exports
# (Substrate's exomem-cloud-grants.sql), so a write needs
# has_column_privilege, not the table-level predicate.
REQUIRED_TABLES: dict[str, dict[str, frozenset[str]]] = {
    # MEDIA_KEY_COLUMNS and deletion_grant are deliberately absent: the
    # reconcile loop tolerates their absence (media edge off, or a schema
    # before the deletion-grant migration), so they are not gaps.
    "exomem_cloud_cells": {
        "SELECT": frozenset(CELL_COLUMNS),
        "UPDATE": frozenset({
            *OBSERVED_COLUMNS,
            "cell_root_key_wrapped", "cell_root_key_version", "content_keys_retired_at",
        }),
    },
    "exomem_cloud_rollout": {
        "SELECT": frozenset((*ROLLOUT_COLUMNS, "id")),
        "UPDATE": frozenset(ROLLOUT_COLUMNS),
    },
    "exomem_cloud_settings": {
        "SELECT": frozenset({"key", "value"}),
    },
    # Read-only by grant: the image history is the barrier map, so the SELECT
    # privilege itself is part of the check.
    "exomem_cloud_image_history": {
        "SELECT": frozenset({"image", "release_tag"}),
    },
    "exomem_cloud_imports": {
        "SELECT": frozenset({"import_id", "cell_id", "requested_state"}),
        "UPDATE": frozenset({
            "recipient", "upload_id", "part_urls", "urls_expire_at", "observed_state",
            "error_code", "staged_files", "staged_bytes",
        }),
    },
    "exomem_cloud_exports": {
        "SELECT": frozenset({"export_id", "cell_id", "requested_state", "created_at", "link_requested_at"}),
        "UPDATE": frozenset({*EXPORT_COLUMNS, "link_issued_at"}),
    },
    "exomem_cloud_capacity": {
        "SELECT": frozenset({"node", "cell_slots", "attachments_used", "observed_at"}),
        "INSERT": frozenset({"node", "cell_slots", "attachments_used", "observed_at"}),
        "UPDATE": frozenset({"cell_slots", "observed_at"}),
    },
}


# Tables cellctl only reads or writes while the feature that uses them is on.
# The reconcile loop gates each on ClusterConfig; an absent table or grant is
# a gap only while the matching feature runs. `access_record` is
# ClusterConfig.access_record_enabled: exomem_cloud_placements and the three
# access tables exist for the access-record loop alone.
FEATURE_TABLES: dict[str, dict[str, dict[str, frozenset[str]]]] = {
    "access_record": {
    "exomem_cloud_placements": {
        "SELECT": frozenset({"cell_id", "pod_uid", "node", "source_created_at",
                             "source_deletion_requested_at", "first_observed_at",
                             "last_observed_at", "workload_kind", "workload_uid", "phase"}),
        "INSERT": frozenset({"cell_id", "pod_uid", "node", "source_created_at",
                             "source_deletion_requested_at", "first_observed_at",
                             "last_observed_at", "workload_kind", "workload_uid", "phase"}),
        "UPDATE": frozenset({"last_observed_at"}),
    },
    "exomem_cloud_access_receipts": {
        "SELECT": frozenset({"id", "bucket_id", "object_key", "version_id", "provider_at",
                             "received_at", "processed_at", "next_attempt_at", "attempts", "error_code"}),
        "UPDATE": frozenset({"attempts", "next_attempt_at", "processed_at", "error_code"}),
    },
    "exomem_cloud_access_records": {
        "SELECT": frozenset({"id"}),
        "INSERT": frozenset({
            "receipt_id", "line_number", "provider_at", "kind", "source_at",
            "node", "boot_id", "event_id", "identity", "action", "target",
            "namespace", "resource", "subresource", "session_id",
            "authentication_method", "fingerprint", "fingerprint_kind", "result",
            "identity_tier",
        }),
    },
    "exomem_cloud_access_entries": {
        "SELECT": frozenset({"cell_id", "record_id"}),
        "INSERT": frozenset({"cell_id", "record_id"}),
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
        for privilege, write_columns in accesses.items():
            for column in sorted(write_columns & found):
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
