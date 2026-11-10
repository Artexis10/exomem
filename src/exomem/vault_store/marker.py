"""The vault-side store-mode marker (OpenSpec make-vault-store-canonical D8).

The records that say a vault is in store mode are machine-local: the manifest sits under one state
root. A file-mode service on another state root over the same vault, such as Windows and WSL on one
folder, sees none of them and would write Markdown beside pages that the store owns. The store
therefore holds one ``store_meta`` key. Activation writes it in the transaction that records store
mode and rollback removes it in the transaction that records file mode. The lease handback copies
``store_meta`` into the replica under the vault, so the key travels with the vault. A file-mode
start reads it there. A takeover carries the replica's key forward (``carry``), so only a root that
holds the store, or that rolled back and has not yet published the clear replica, decides it.

Only a readable replica that says store refuses. An absent or unreadable replica starts the vault,
because the replica arrives by sync and a half-arrived copy is a normal state, not a sign that the
vault is in store mode.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ..collection_store import connection, replica, schema
from . import mode

log = logging.getLogger(__name__)

#: The ``store_meta`` key and its one value. The table is a generic key and value store, so no
#: schema carries this.
KEY = "vault_store_mode"
ACTIVE = "store"
#: Set by the rollback's transaction and removed by the publication that copies the clear replica:
#: while it stands, this root's file mode is not yet in the replica, so this root still decides.
CLEAR_UNPUBLISHED_KEY = "vault_store_clear_unpublished"

#: What a replica reading reports: the closed set of this module's own states, never a vault's
#: vocabulary.
STORE = "store"
CLEAR = "clear"
ABSENT = "absent"
UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Replica:
    """What the vault's replica says: ``state``, the store that published it, and why it failed."""

    state: str
    store_id: str | None = None
    instance_id: str | None = None
    reason: str | None = None


def record(tx: sqlite3.Connection) -> None:
    """Write the marker inside the transaction that activates store mode."""
    _put(tx, KEY, ACTIVE)
    tx.execute("DELETE FROM store_meta WHERE key = ?", (CLEAR_UNPUBLISHED_KEY,))


def clear(tx: sqlite3.Connection) -> None:
    """Remove the marker inside the transaction that returns the vault to file mode.

    The replica still says store until a publication copies this store, so the same transaction
    marks the clear as unpublished: until then this root decides the marker (``settle`` ends it).
    """
    tx.execute("DELETE FROM store_meta WHERE key = ?", (KEY,))
    _put(tx, CLEAR_UNPUBLISHED_KEY, "1")


def _put(tx: sqlite3.Connection, key: str, value: str) -> None:
    tx.execute(
        "INSERT INTO store_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def read_replica(vault_root: Path) -> Replica:
    """Read the marker from the vault's replica; an unreadable replica is reported, not guessed.

    ``store_meta`` is a generic key and value table at every schema, so it is read without the
    store's schema ceiling: a replica that a newer build wrote still says store when it says store.
    Only a file that is missing or half-arrived (a sync copy that is not a database yet) is
    unreadable.
    """
    path = replica.replica_path(vault_root)
    if not path.is_file():
        return Replica(ABSENT)
    try:
        with closing(
            sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=connection.BUSY_TIMEOUT_MS / 1000,
            )
        ) as reader:
            found = dict(
                reader.execute(
                    "SELECT key, value FROM store_meta WHERE key IN (?, ?, ?)",
                    (KEY, schema.META_STORE_ID, schema.META_INSTANCE_ID),
                )
            )
    except (sqlite3.Error, OSError) as error:
        return Replica(UNREADABLE, reason=f"{type(error).__name__}: {error}")
    return Replica(
        STORE if found.get(KEY) == ACTIVE else CLEAR,
        store_id=found.get(schema.META_STORE_ID),
        instance_id=found.get(schema.META_INSTANCE_ID),
    )


def carry(conn: sqlite3.Connection, replica_meta: Mapping[str, str]) -> None:
    """Make a takeover's store say about the marker what the replica it continues from says.

    A takeover starts a tenure on the replica's bytes (adopt) or on a store that carries the same
    head (continue). It never decides the marker: a root that does not decide (see
    ``_decided_mode``) copies the replica's key or its absence. A root that decides keeps its own
    key: the replica may be behind that decision, and a copy would drop a marker, or restore one,
    that was not published yet.
    """
    if _decided_mode(conn) is not None:
        return
    # Adopted bytes can carry the previous holder's unpublished-clear key; it means nothing here.
    conn.execute("DELETE FROM store_meta WHERE key = ?", (CLEAR_UNPUBLISHED_KEY,))
    if replica_meta.get(KEY) is None:
        conn.execute("DELETE FROM store_meta WHERE key = ?", (KEY,))
    else:
        _put(conn, KEY, replica_meta[KEY])


def _decided_mode(conn: sqlite3.Connection) -> str | None:
    """The mode this store decides the marker for, or None when the replica decides it.

    A store in store mode decides. A store in file mode decides only while its rollback's clear is
    unpublished (``CLEAR_UNPUBLISHED_KEY``): once the clear replica exists, a later root may migrate
    and this one must carry that root's marker like any root that never migrated. A store from a
    release before store schema 15 has no ``store_mode`` table, so it never decided.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_mode'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute("SELECT mode FROM store_mode WHERE id = 1").fetchone()
    if row is None:
        return None
    if row[0] == mode.STORE_MODE:
        return mode.STORE_MODE
    unpublished = conn.execute(
        "SELECT 1 FROM store_meta WHERE key = ?", (CLEAR_UNPUBLISHED_KEY,)
    ).fetchone()
    return mode.FILE_MODE if unpublished is not None else None


def settle(vault_root: Path, handle) -> None:
    """End this root's say over the marker once the clear replica is published.

    Call it after a publication succeeded. The replica then reads clear, so the key that kept this
    root deciding is spent. A replica that does not read clear keeps the key: the clear is owed.
    """
    pending = handle.connection.execute(
        "SELECT 1 FROM store_meta WHERE key = ?", (CLEAR_UNPUBLISHED_KEY,)
    ).fetchone()
    if pending is None or read_replica(vault_root).state != CLEAR:
        return
    with handle.transaction() as conn:
        conn.execute("DELETE FROM store_meta WHERE key = ?", (CLEAR_UNPUBLISHED_KEY,))


def replica_stale(vault_root: Path, store: sqlite3.Connection) -> bool:
    """Whether the hand-back must copy the replica again: it disagrees with this store's own mode.

    The replica is published by head, and store mode changes no head: activation and rollback
    commit the marker without a collection-chain commit. Without this answer the hand-back would
    find the head unchanged and keep the old replica, so a file-mode service elsewhere would never
    see the marker. Only a root that decides forces (``_decided_mode``): one that holds the store,
    or that rolled back and has not published the clear replica yet. Any other root carries the
    replica's marker through its takeovers (``carry``) and never forces. A store in store mode
    needs a replica that says store, and a store in file mode needs only that no replica still
    says store. An unreadable replica forces nothing: it is no evidence, and a copy would replace
    a replica that a newer build wrote.
    """
    decided = _decided_mode(store)
    if decided is None:
        return False
    found = read_replica(vault_root).state
    return found in (CLEAR, ABSENT) if decided == mode.STORE_MODE else found == STORE


def report(vault_root: Path) -> dict:
    """The replica's state for a status report: ``vault_marker``, and why when it cannot be read."""
    found = read_replica(vault_root)
    if found.reason is None:
        return {"vault_marker": found.state}
    return {"vault_marker": found.state, "vault_marker_unreadable": found.reason}


def conflict(vault_root: Path) -> Replica | None:
    """The replica when it says store and this state root does not hold that store, else None.

    This state root holds the published store when the replica carries its own tenure and its own
    store decides file: a rollback that committed here whose clear replica has not yet been
    published. That root must start, or no service could ever publish the replica that clears the
    marker. A rollback whose clear replica was published decides nothing, so a root that migrated
    elsewhere since is refused.
    """
    found = read_replica(vault_root)
    if found.state == UNREADABLE:
        log.warning(
            "The vault replica could not be read (%s), so its store-mode marker is not checked",
            found.reason,
        )
    if found.state != STORE or _rolled_back_here(vault_root, found):
        return None
    return found


def _rolled_back_here(vault_root: Path, found: Replica) -> bool:
    try:
        reader, _ = connection._open_read_only(
            connection.store_path(vault_root), connection.BUSY_TIMEOUT_MS
        )
    except Exception:  # noqa: BLE001 - no readable local store: the replica is all the evidence
        return False
    with closing(reader):
        try:
            ids = dict(
                reader.execute(
                    "SELECT key, value FROM store_meta WHERE key IN (?, ?)",
                    (schema.META_STORE_ID, schema.META_INSTANCE_ID),
                )
            )
            decided = _decided_mode(reader)
        except sqlite3.Error:
            return False
    same_tenure = (ids.get(schema.META_STORE_ID), ids.get(schema.META_INSTANCE_ID)) == (
        found.store_id,
        found.instance_id,
    )
    return same_tenure and decided == mode.FILE_MODE
