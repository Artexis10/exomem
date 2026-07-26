"""Per-machine sidecar for the compiled-policy snapshot — inspection only.

`.governance.sqlite` is a derived convenience, never the enforcement
authority (design decision D6): rebuildable at any time from
`_Governance/**.yaml`, never synced, and never consulted by
`policy.load`/`membership.evaluate`/`decisions.decide` — those run entirely
in-process off the parsed YAML. Only an explicit `compile.write_snapshot`
call opens this file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import index_paths, sidecar_store

SCHEMA_USER_VERSION = 2
DATA_TABLE = "compiled_policy"


def sidecar_path(vault_root: Path) -> Path:
    return index_paths.governance_sidecar_path(Path(vault_root))


def open_connection(vault_root: Path) -> sqlite3.Connection:
    """Open (creating if absent) the governance sidecar with its schema in place."""
    path = sidecar_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    sidecar_store.apply_sidecar_pragmas(conn)
    _migrate(conn)
    sidecar_store.ensure_meta_table(conn, DATA_TABLE, "governance")
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply known sidecar migrations without ever lowering a newer version."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {DATA_TABLE} ("
            "fingerprint TEXT PRIMARY KEY, snapshot TEXT NOT NULL, compiled_at REAL NOT NULL)"
        )
        version = 1
    if version < 2:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_instance "
            "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1), instance_id TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipts_head ("
            "instance_id TEXT PRIMARY KEY, durable_seq INTEGER NOT NULL, durable_hash TEXT NOT NULL, "
            "observed_seq INTEGER NOT NULL, observed_hash TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_secrets ("
            "name TEXT PRIMARY KEY, value BLOB NOT NULL)"
        )
        version = 2
    if version <= SCHEMA_USER_VERSION:
        conn.execute(f"PRAGMA user_version = {version}")
