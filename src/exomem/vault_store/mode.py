"""Whether a vault is in store mode: one answer, read from the records on every call."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from .. import state_migration
from ..collection_store import capability, connection, schema

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
    A manifest that is absent, a store that is absent and a store older than schema 15 are file mode:
    none of them holds pages. A manifest or a store that cannot be read raises, because an unknown
    mode must never pass for file mode.
    """
    if not state_migration.vault_store_recorded(vault_root):
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


def status(vault_root: Path) -> dict:
    """What the records say about this vault, read-only and without a lease.

    A file-mode vault reports only the mode and the release gate. A store-mode vault adds the figures that
    an operator checks before a rollback: stored pages and binaries, queued projections and held items.
    """
    report: dict = {"gate": "on" if capability.vault_store_enabled() else "off"}
    if not store_mode_active(vault_root):
        return {**report, "mode": FILE_MODE}
    reader, _ = connection._open_read_only(connection.store_path(vault_root), connection.BUSY_TIMEOUT_MS)
    with closing(reader):
        revision, release, changed_at = reader.execute(
            "SELECT revision, release, changed_at FROM store_mode WHERE id = 1"
        ).fetchone()
        counts = {
            table: reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("store_pages", "store_binaries", "store_projection_queue", "store_held", "store_audit")
        }
    return {
        **report,
        "mode": STORE_MODE,
        "revision": revision,
        "release": release,
        "changed_at": changed_at,
        "pages": counts["store_pages"],
        "binaries": counts["store_binaries"],
        "queued_projections": counts["store_projection_queue"],
        "held": counts["store_held"],
        "audit_rows": counts["store_audit"],
    }
