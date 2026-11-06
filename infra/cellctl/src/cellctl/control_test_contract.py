"""Shared SQL setup for disposable controller and infrastructure checks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _committed_sql(fixtures: Path, key: str) -> str:
    """One file of the pinned Substrate contract, read from its committed blob."""
    contract = json.loads((fixtures / "substrate-contract.json").read_text(encoding="utf-8"))
    checkout = os.environ["CELLCTL_SUBSTRATE_CONTRACT_ROOT"]
    # Committed blobs keep dirty or newer checkout files outside the fixture contract.
    return subprocess.run(
        ["git", "-C", checkout, "show", f"{contract.get('artifact_refs', {}).get(key, contract['ref'])}:{contract[key]}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def control_test_sql(fixtures: Path) -> tuple[str, str]:
    """Load the baseline and pinned companion migration/grants without fetching."""
    schema = (fixtures / "exomem_cloud_schema.sql").read_text(encoding="utf-8")
    # Earlier migrations own entitlements; the companion migration extends this minimal stand-in.
    schema += "\nCREATE TABLE exomem_entitlements (tenant_id uuid PRIMARY KEY);\n"
    return schema + _committed_sql(fixtures, "migration"), _committed_sql(fixtures, "grants")


def import_test_sql(fixtures: Path) -> str:
    """C5's import tables, run after `control_test_sql`'s schema and before its grants."""
    return "\n".join(
        [
            "CREATE EXTENSION IF NOT EXISTS pgcrypto;",
            # C5's canonical migration also seeds the CLI client. Earlier OAuth migrations own this table.
            "CREATE TABLE exomem_oauth_clients (client_id text PRIMARY KEY, admission_mode text, enabled boolean, "
            "redirect_uris jsonb, redirect_uris_digest bytea);",
            (fixtures / "exomem_cloud_imports.sql").read_text(encoding="utf-8"),
            (fixtures / "exomem_cloud_import_cleanup.sql").read_text(encoding="utf-8"),
            _committed_sql(fixtures, "imports_migration"),
        ]
    )


def export_test_sql(fixtures: Path) -> str:
    """C6's export table, disk-use columns and export hold, run after `import_test_sql` and before the grants.

    The fixture is the Exomem half of the pinned Substrate migration 0066. Its other half alters
    OAuth tables this disposable database only stubs, so the fixture must stay a prefix of it.
    """
    local = (fixtures / "exomem_cloud_exports.sql").read_text(encoding="utf-8")
    if not _committed_sql(fixtures, "exports_migration").startswith(local):
        raise RuntimeError("exomem_cloud_exports.sql is not the Exomem half of the pinned migration 0066")
    return local + "\n" + _committed_sql(fixtures, "custody_migration")


def media_key_test_sql(fixtures: Path) -> str:
    """The media key columns and cellctl's grant on them, run after the pinned grants.

    The pinned grants script revokes every earlier grant on the cell table, so this must follow it.
    It mirrors Substrate migration 0081 and its grants stanza until the pin reaches a commit with both.
    """
    return (fixtures / "exomem_cloud_media_keys.sql").read_text(encoding="utf-8")


def operation_grant_test_sql(fixtures: Path) -> str:
    """The grant columns of an export and an import, and Substrate's grants on them, run after the pinned grants.

    It mirrors Substrate migration 0082 and its grants stanzas until the pin reaches a commit with both.
    """
    return (fixtures / "exomem_cloud_operation_grants.sql").read_text(encoding="utf-8")


def access_record_test_sql(fixtures: Path) -> str:
    """The canonical access migration and additive grants, after the existing grant fixtures."""
    return _committed_sql(fixtures, "access_migration") + "\n" + _committed_sql(fixtures, "access_grants")


def control_database_sql(fixtures: Path) -> list[str]:
    """Every script that builds a disposable control database, in the order they must run.

    Each caller that builds one runs this list, so a new fixture reaches every caller at once.
    The later fixtures follow the pinned grants, which revoke earlier grants on the cell table.
    """
    schema_sql, grants_sql = control_test_sql(fixtures)
    return [
        schema_sql,
        import_test_sql(fixtures),
        export_test_sql(fixtures),
        grants_sql,
        media_key_test_sql(fixtures),
        operation_grant_test_sql(fixtures),
        access_record_test_sql(fixtures),
    ]
