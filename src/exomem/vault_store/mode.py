"""Whether a vault is in store mode: one answer, read from the records on every call."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from .. import state_migration
from ..collection_store import capability, connection, schema

#: The values of ``store_mode.mode``: a closed set that the CHECK of schema 15 fixes.
FILE_MODE = "file"
STORE_MODE = "store"
#: The id sits in the vault state while the store says otherwise: a crash between the two records of an
#: activation or of a rollback. The vault serves from its files, and the interrupted command runs again.
INTERRUPTED_ACTIVATION = "interrupted-activation"
INTERRUPTED_ROLLBACK = "interrupted-rollback"


def state(vault_root: Path) -> str:
    """The mode, or the interrupted command that the two records show: one of the four constants above.

    The vault state manifest records ``vault-store-v1`` and the store's ``store_mode`` row says ``store`` or
    ``file``. Activation records the id before its store commit and rollback drops it after its own, so
    the records disagree only in a crash window: the id with no mode row is an activation that died before
    its commit, and the id with a ``file`` row is a rollback that committed and died before dropping the id.
    The manifest is read first, so a file-mode vault never opens the store. A manifest or a store that
    cannot be read raises, because an unknown mode must never pass for file mode.
    """
    if not state_migration.vault_store_recorded(vault_root):
        return FILE_MODE
    recorded = _stored_mode(vault_root)
    if recorded is None:
        return INTERRUPTED_ACTIVATION
    return STORE_MODE if recorded == STORE_MODE else INTERRUPTED_ROLLBACK


def store_mode_active(vault_root: Path) -> bool:
    """Whether this vault's pages live in the collection store, so a service reads and writes the store.

    Both records must agree: the manifest records the id and the store's mode row says ``store``. Any other
    state, a crash window included, is file mode for a service.

    It reads the records each time and keeps no cache: it prevents a service from writing Markdown
    into a vault that the store owns, so a stale answer would cost more than the read.
    A manifest that is absent, a store that is absent and a store older than schema 15 are file mode:
    none of them holds pages.
    """
    return state(vault_root) == STORE_MODE


def _stored_mode(vault_root: Path) -> str | None:
    """The store's ``store_mode.mode``, or None when the store, its schema 15 or its mode row is absent."""
    path = connection.store_path(vault_root)
    if not path.is_file():
        return None
    reader, found = connection._open_read_only(path, connection.BUSY_TIMEOUT_MS)
    with closing(reader):
        if found < schema.VAULT_STORE_SCHEMA_VERSION:
            return None
        row = reader.execute("SELECT mode FROM store_mode WHERE id = 1").fetchone()
    return None if row is None else row[0]


def status(vault_root: Path) -> dict:
    """What the records say about this vault, read-only and without a lease.

    A file-mode vault reports only the mode and the release gate. A crash window reports its own state. A
    store-mode vault adds the figures that an operator checks before a rollback: stored pages and binaries,
    queued projections and held items.
    """
    report: dict = {"gate": "on" if capability.vault_store_enabled() else "off"}
    current = state(vault_root)
    if current != STORE_MODE:
        return {**report, "mode": current}
    reader, _ = connection._open_read_only(
        connection.store_path(vault_root), connection.BUSY_TIMEOUT_MS
    )
    with closing(reader):
        revision, release, changed_at = reader.execute(
            "SELECT revision, release, changed_at FROM store_mode WHERE id = 1"
        ).fetchone()
        counts = {
            table: reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in schema.VAULT_STORE_TABLES
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
