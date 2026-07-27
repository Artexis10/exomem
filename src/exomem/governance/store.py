"""Per-machine sidecar for the compiled-policy snapshot — inspection only.

`.governance.sqlite` is a derived convenience, never the enforcement
authority (design decision D6): rebuildable at any time from
`_Governance/**.yaml`, never synced, and never consulted by
`policy.load`/`membership.evaluate`/`decisions.decide` — those run entirely
in-process off the parsed YAML. Only an explicit `compile.write_snapshot`
call opens this file.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

from .. import index_paths, sidecar_store

SCHEMA_USER_VERSION = 2
DATA_TABLE = "compiled_policy"
_INITIALIZED_SIDECARS_MAX = 64
_INITIALIZED_SIDECARS: OrderedDict[
    Path, tuple[int, int, int, int, int, str]
] = OrderedDict()
_INITIALIZED_SIDECARS_LOCK = threading.Lock()


def _reset_initialized_sidecars_after_fork() -> None:
    global _INITIALIZED_SIDECARS, _INITIALIZED_SIDECARS_LOCK
    _INITIALIZED_SIDECARS = OrderedDict()
    _INITIALIZED_SIDECARS_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_initialized_sidecars_after_fork)


def sidecar_path(vault_root: Path) -> Path:
    return index_paths.governance_sidecar_path(Path(vault_root))


def open_connection(
    vault_root: Path, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open (creating if absent) the governance sidecar with its schema in place."""
    path = sidecar_path(vault_root).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    try:
        with _INITIALIZED_SIDECARS_LOCK:
            state = _connection_state(conn, path)
            cached = _INITIALIZED_SIDECARS.get(path)
            if cached == state:
                # WAL mode persists in the database. These two pragmas are
                # connection-local and cheap, so every new handle still gets
                # the production timeout/durability settings without rerunning
                # journal negotiation and idempotent DDL on every receipt.
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                _INITIALIZED_SIDECARS.move_to_end(path)
            else:
                sidecar_store.apply_sidecar_pragmas(conn)
                _migrate(conn)
                sidecar_store.ensure_meta_table(conn, DATA_TABLE, "governance")
                conn.commit()
                _INITIALIZED_SIDECARS[path] = _connection_state(conn, path)
                _INITIALIZED_SIDECARS.move_to_end(path)
                while len(_INITIALIZED_SIDECARS) > _INITIALIZED_SIDECARS_MAX:
                    _INITIALIZED_SIDECARS.popitem(last=False)
        return conn
    except BaseException:
        conn.close()
        raise


def _connection_state(
    conn: sqlite3.Connection, path: Path
) -> tuple[int, int, int, int, int, str]:
    """Identity + live schema state; DML does not invalidate this fast path."""
    stat_result = path.stat()
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    return (
        os.getpid(),
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        user_version,
        schema_version,
        journal_mode,
    )


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
            "observed_seq INTEGER NOT NULL, observed_hash TEXT NOT NULL, "
            "path TEXT NOT NULL DEFAULT '', byte_offset INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_secrets ("
            "name TEXT PRIMARY KEY, value BLOB NOT NULL)"
        )
        version = 2
    if version == 2:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(receipts_head)")}
        if "path" not in columns:
            conn.execute("ALTER TABLE receipts_head ADD COLUMN path TEXT NOT NULL DEFAULT ''")
        if "byte_offset" not in columns:
            conn.execute(
                "ALTER TABLE receipts_head ADD COLUMN byte_offset INTEGER NOT NULL DEFAULT 0"
            )
    if version <= SCHEMA_USER_VERSION:
        conn.execute(f"PRAGMA user_version = {version}")
