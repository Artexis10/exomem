"""Whether a vault is in store mode: one answer, read from the records on every call."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from .. import state_migration
from ..collection_store import connection, schema

#: The values of ``store_mode.mode``: a closed set that the CHECK of schema 15 fixes.
FILE_MODE = "file"
STORE_MODE = "store"


def store_mode_active(vault_root: Path) -> bool:
    """Whether this vault's pages live in the collection store.

    Two records must agree. The vault state manifest records ``vault-store-v1``, and the store's
    ``store_mode`` row says ``store``. Activation records the id before its store commit and rollback
    drops it after its own, so a crash between the two leaves file mode and both commands rerun cleanly.
    The manifest is read first, so a file-mode vault never opens the store.

    It reads the records each time and keeps no cache: it prevents a service from writing Markdown
    into a vault that the store owns, so a stale answer would cost more than the read.
    A store that is absent, or older than schema 15, is file mode: it holds no pages. A store
    that cannot be read raises, because an unknown mode must never pass for file mode.
    """
    recorded = state_migration.recorded_descriptor_ids(vault_root)
    if recorded is None or state_migration.VAULT_STORE_COMPATIBILITY_ID not in recorded:
        return False
    path = connection.store_path(vault_root)
    if not path.is_file():
        return False
    reader, found = connection._open_read_only(path, connection.BUSY_TIMEOUT_MS)
    with closing(reader):
        if found < schema.VAULT_STORE_SCHEMA_VERSION:
            return False
        row = reader.execute("SELECT mode FROM store_mode WHERE id = 1").fetchone()
    return row is not None and row[0] == STORE_MODE
