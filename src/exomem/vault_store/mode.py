"""Whether a vault is in store mode: read from the records, fresh for a write and by stat for a read."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

from .. import state_migration, state_paths
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
    none of them holds pages. A byte reader calls ``store_mode_for_reads`` instead.
    """
    return state(vault_root) == STORE_MODE


#: One entry per vault and placement environment: the manifest path, its stat signature when the answer
#: was read, and the answer. A small bound keeps a process that sees many vaults from holding them all.
_READ_MODES: dict[tuple[str, tuple[str, ...]], tuple[Path, tuple[int, ...] | None, bool]] = {}
_READ_MODES_MAX = 64


def _manifest_signature(manifest: Path) -> tuple[int, ...] | None:
    """The manifest's identity and change marks, or None when it is absent: one stat, no read.

    An atomic replace changes the inode and an in-place rewrite changes ``st_ctime_ns``, which no caller can
    set back, so an unchanged signature means the same bytes.
    """
    try:
        found = manifest.stat()
    except FileNotFoundError:
        return None
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)


def store_mode_for_reads(vault_root: Path) -> bool:
    """``store_mode_active`` for a byte reader: it re-reads the records only when the manifest changes.

    Activation and rollback run offline, and each changes the manifest, so a reader may keep its answer
    while the manifest's stat signature holds: one stat per read in place of a parse. A crash window is
    never kept, and a manifest or store that cannot be read raises as ``state`` does. The write refusal
    in ``availability`` does not use this: it asks ``store_mode_active`` every time.
    """
    key = (os.fspath(vault_root), state_paths._placement_environment())
    cached = _READ_MODES.get(key)
    if cached is not None and _manifest_signature(cached[0]) == cached[1]:
        return cached[2]
    manifest = state_migration._manifest_path(state_paths.vault_state_dir(vault_root))
    # The signature comes before the read, so a manifest replaced between them is read again next call.
    signature = _manifest_signature(manifest)
    current = state(vault_root)
    if current in (FILE_MODE, STORE_MODE):
        if len(_READ_MODES) >= _READ_MODES_MAX:
            _READ_MODES.clear()
        _READ_MODES[key] = (manifest, signature, current == STORE_MODE)
    return current == STORE_MODE


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


#: What ``status`` reports for a record it cannot read: never a zero, which would pass for an empty store.
UNKNOWN = "unknown"
#: The report's own field names for a store-mode vault: the figures that it adds to the mode and the gate.
STORE_FIGURES = (
    "revision",
    "release",
    "changed_at",
    "pages",
    "binaries",
    "queued_projections",
    "oldest_projection_age_seconds",
    "held",
    "held_by_kind",
    "audit_rows",
)


def status(vault_root: Path) -> dict:
    """What the records say about this vault, read-only and without a lease.

    A file-mode vault reports only the mode and the release gate. A crash window reports its own state. A
    store-mode vault adds the figures that an operator checks before a rollback: stored pages and binaries,
    queued projections with the age in seconds of the oldest one (None when nothing is queued), and held
    items in total and by kind. A store that cannot be read gives "unknown", for the mode or for each
    figure, with the error's name in ``unreadable``: an unreadable store never reports zeros. A damaged
    state manifest raises instead, so that the command refuses and names it.
    """
    report: dict = {"gate": "on" if capability.vault_store_enabled() else "off"}
    if not state_migration.vault_store_recorded(vault_root):  # a damaged manifest raises: the command refuses
        return {**report, "mode": FILE_MODE}
    try:
        current = state(vault_root)
    except Exception as error:  # noqa: BLE001 - an unreadable store is reported as unknown, never as a mode
        return {**report, "mode": UNKNOWN, "unreadable": _reason(error)}
    if current != STORE_MODE:
        return {**report, "mode": current}
    try:
        figures = _store_figures(vault_root)
    except Exception as error:  # noqa: BLE001 - an unreadable store is reported as unknown, never as zeros
        figures = {**dict.fromkeys(STORE_FIGURES, UNKNOWN), "unreadable": _reason(error)}
    return {**report, "mode": STORE_MODE, **figures}


def _reason(error: Exception) -> str:
    """The error's own code, or its type: a name that never carries a path or a page's content."""
    return str(getattr(error, "code", type(error).__name__))


def _store_figures(vault_root: Path) -> dict:
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
        # The queue stamps whole seconds in UTC, which SQLite's own clock reads, so no format lives here.
        (age,) = reader.execute(
            "SELECT CAST(strftime('%s', 'now') AS INTEGER) "
            "- CAST(strftime('%s', MIN(queued_at)) AS INTEGER) FROM store_projection_queue"
        ).fetchone()
        held_by_kind = dict(
            reader.execute("SELECT kind, COUNT(*) FROM store_held GROUP BY kind").fetchall()
        )
    return {
        "revision": revision,
        "release": release,
        "changed_at": changed_at,
        "pages": counts["store_pages"],
        "binaries": counts["store_binaries"],
        "queued_projections": counts["store_projection_queue"],
        "oldest_projection_age_seconds": None if age is None else max(age, 0),
        "held": counts["store_held"],
        "held_by_kind": held_by_kind,
        "audit_rows": counts["store_audit"],
    }
