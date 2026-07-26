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

SCHEMA_USER_VERSION = 1
DATA_TABLE = "compiled_policy"


def sidecar_path(vault_root: Path) -> Path:
    return index_paths.governance_sidecar_path(Path(vault_root))


def open_connection(vault_root: Path) -> sqlite3.Connection:
    """Open (creating if absent) the governance sidecar with its schema in place."""
    path = sidecar_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    sidecar_store.apply_sidecar_pragmas(conn)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {DATA_TABLE} ("
        "fingerprint TEXT PRIMARY KEY, snapshot TEXT NOT NULL, compiled_at REAL NOT NULL)"
    )
    sidecar_store.ensure_meta_table(conn, DATA_TABLE, "governance")
    conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
    conn.commit()
    return conn
