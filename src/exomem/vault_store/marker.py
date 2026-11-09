"""The vault-side store-mode marker (OpenSpec make-vault-store-canonical D8).

The records that say a vault is in store mode are machine-local: the manifest sits under one state
root. A file-mode service on another state root over the same vault, such as Windows and WSL on one
folder, sees none of them and would write Markdown beside pages that the store owns. The store
therefore holds one ``store_meta`` key. Activation writes it in the transaction that records store
mode and rollback removes it in the transaction that records file mode. The lease handback copies
``store_meta`` into the replica under the vault, so the key travels with the vault. A file-mode
start reads it there.

Only a readable replica that says store refuses. An absent or unreadable replica starts the vault,
because the replica arrives by sync and a half-arrived copy is a normal state, not a sign that the
vault is in store mode.
"""

from __future__ import annotations

import logging
import sqlite3
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
    tx.execute(
        "INSERT INTO store_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KEY, ACTIVE),
    )


def clear(tx: sqlite3.Connection) -> None:
    """Remove the marker inside the transaction that returns the vault to file mode."""
    tx.execute("DELETE FROM store_meta WHERE key = ?", (KEY,))


def read_replica(vault_root: Path) -> Replica:
    """Read the marker from the vault's replica; an unreadable replica is reported, not guessed."""
    path = replica.replica_path(vault_root)
    if not path.is_file():
        return Replica(ABSENT)
    try:
        reader, _ = connection._open_read_only(path, connection.BUSY_TIMEOUT_MS)
        with closing(reader):
            found = dict(
                reader.execute(
                    "SELECT key, value FROM store_meta WHERE key IN (?, ?, ?)",
                    (KEY, schema.META_STORE_ID, schema.META_INSTANCE_ID),
                )
            )
    except Exception as error:  # noqa: BLE001 - a replica mid-sync or damaged is no verdict
        return Replica(UNREADABLE, reason=str(getattr(error, "code", type(error).__name__)))
    return Replica(
        STORE if found.get(KEY) == ACTIVE else CLEAR,
        store_id=found.get(schema.META_STORE_ID),
        instance_id=found.get(schema.META_INSTANCE_ID),
    )


def replica_stale(vault_root: Path, store: sqlite3.Connection) -> bool:
    """Whether the hand-back must copy the replica again: it disagrees with the store on the marker.

    The replica is published by head, and store mode changes no head: activation and rollback
    commit the marker without a collection-chain commit. Without this answer the hand-back would
    find the head unchanged and keep the old replica, so a file-mode service elsewhere would never
    see the marker. A store in store mode needs a replica that says store, and an unreadable
    replica is copied fresh. A store in file mode needs only that no replica still says store.
    """
    wanted = store.execute("SELECT value FROM store_meta WHERE key = ?", (KEY,)).fetchone()
    says_store = wanted is not None and wanted[0] == ACTIVE
    found = read_replica(vault_root).state
    return (found != STORE) if says_store else (found == STORE)


def report(vault_root: Path) -> dict:
    """The replica's state for a status report: ``vault_marker``, and why when it cannot be read."""
    found = read_replica(vault_root)
    if found.reason is None:
        return {"vault_marker": found.state}
    return {"vault_marker": found.state, "vault_marker_unreadable": found.reason}


def conflict(vault_root: Path) -> Replica | None:
    """The replica when it says store and this state root does not hold that store, else None.

    This state root holds the published store when the replica carries its own tenure and the
    store's own record says file: a rollback that committed here whose replica has not yet been
    republished. That root must start, or no service could ever publish the replica that clears the
    marker.
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
            row = reader.execute("SELECT mode FROM store_mode WHERE id = 1").fetchone()
        except sqlite3.Error:
            return False
    same_tenure = (ids.get(schema.META_STORE_ID), ids.get(schema.META_INSTANCE_ID)) == (
        found.store_id,
        found.instance_id,
    )
    return same_tenure and row is not None and row[0] == mode.FILE_MODE
