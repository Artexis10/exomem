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
    set back, so an unchanged signature means the same bytes. On Windows ``st_ctime_ns`` is the
    creation time, so only the file ID covers a rewrite there; Exomem writes the manifest by
    ``os.replace``, which gives it a new ID, and only a hand edit that restores the mtime would go
    unseen.
    """
    try:
        found = manifest.stat()
    except FileNotFoundError:
        return None
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)


def store_mode_for_reads(vault_root: Path) -> bool:
    """``store_mode_active`` for a byte reader: it re-reads the records only when the manifest changes.

    Activation and rollback run offline, and each changes the manifest, so a reader may keep its answer
    while the manifest's stat signature holds: one stat per read in place of a parse. A crash window
    is never kept, and a manifest or store that cannot be read raises as ``state`` does. A rollback
    commits the store's file mode before it changes the manifest; ``read.snapshot`` meets that
    window when it opens the store, and ``forget_read_mode`` drops the kept answer there. The write
    refusal in ``availability`` does not use this: it asks ``store_mode_active`` every time.
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


def forget_read_mode(vault_root: Path) -> None:
    """Drop the kept answer of ``store_mode_for_reads``, so the next read re-reads the records."""
    _READ_MODES.pop((os.fspath(vault_root), state_paths._placement_environment()), None)


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


#: The projector acts on a queued intent only while no hold sits on its path or on a move's old
#: path. The projector's listing and the status age both read this rule, over the queue as ``q``.
UNHELD_INTENT = (
    "NOT EXISTS (SELECT 1 FROM store_held h WHERE h.path = q.path OR h.path = q.from_path)"
)
#: What ``status`` reports for a record it cannot read: never a zero, which would pass for empty.
UNKNOWN = "unknown"
#: The report's own field names for a store-mode vault: the figures that it adds to mode and gate.
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
    "registry",
    "registry_revision",
    "registry_target_revision",
    "registry_waiting_on_paths",
)


def status(vault_root: Path) -> dict:
    """What the records say about this vault, read-only and without a lease.

    A file-mode vault reports only the mode, the release gate and the vault-side marker. A crash
    window reports its own state. A store-mode vault adds the figures that an operator checks before
    a rollback: stored pages and binaries, queued projections, the age in seconds of the oldest
    queued projection that no hold sits on (None when none waits, "unknown" when the clock says it
    is in the future), and held items in total and by kind. A projection that the projector
    deferred, such as one on a linked file, stays queued and keeps ageing. The registry figures say
    whether the derived rows reflect the schema overlay (``current``), are being derived again
    toward ``registry_target_revision`` (``rebuilding``) or have no record (``unknown``).
    ``registry_waiting_on_paths`` counts the overlay paths that the projector has not put on disk or
    that a person's edit holds: the job waits for each, so a held path keeps a vault ``rebuilding``.
    ``vault_marker`` is what the replica under the vault says: ``store`` makes a file-mode start on
    another state root refuse. A store that cannot be read gives "unknown", for the mode or for each
    figure, with the error's name in ``unreadable``: an unreadable store never reports zeros. A
    damaged state manifest raises instead, so that the command refuses and names it.
    """
    from . import marker

    report: dict = {"gate": "on" if capability.vault_store_enabled() else "off"}
    # A damaged manifest raises here, so the command refuses and names it.
    recorded = state_migration.vault_store_recorded(vault_root)
    report.update(marker.report(vault_root))
    if not recorded:
        return {**report, "mode": FILE_MODE}
    try:
        current = state(vault_root)
    except Exception as error:  # noqa: BLE001 - an unreadable store is unknown, never a mode
        return {**report, "mode": UNKNOWN, "unreadable": _reason(error)}
    if current != STORE_MODE:
        return {**report, "mode": current}
    try:
        figures = _store_figures(vault_root)
    except Exception as error:  # noqa: BLE001 - an unreadable store is unknown, never zeros
        figures = {**dict.fromkeys(STORE_FIGURES, UNKNOWN), "unreadable": _reason(error)}
    return {**report, "mode": STORE_MODE, **figures}


def _reason(error: Exception) -> str:
    """The error's own code, or its type: a name that never carries a path or a page's content."""
    return str(getattr(error, "code", type(error).__name__))


def _store_figures(vault_root: Path) -> dict:
    from . import rederive

    reader, _ = connection._open_read_only(
        connection.store_path(vault_root), connection.BUSY_TIMEOUT_MS
    )
    with closing(reader):
        registry = rederive.read(reader)
        waiting = rederive.waiting_on(reader, vault_root)
        revision, release, changed_at = reader.execute(
            "SELECT revision, release, changed_at FROM store_mode WHERE id = 1"
        ).fetchone()
        counts = {
            table: reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in schema.VAULT_STORE_TABLES
        }
        # The queue stamps whole seconds in UTC, which SQLite's own clock reads: no format here.
        # The age counts what the projector can still act on: a held path waits for a person. An
        # intent that the projector deferred is counted, so that it never hides as a quiet queue.
        (age,) = reader.execute(
            "SELECT CAST(strftime('%s', 'now') AS INTEGER) "
            "- CAST(strftime('%s', MIN(queued_at)) AS INTEGER) "
            f"FROM store_projection_queue q WHERE {UNHELD_INTENT}"
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
        # A negative age is a clock that went backwards: it is not known, and it is never a zero.
        "oldest_projection_age_seconds": UNKNOWN if age is not None and age < 0 else age,
        "held": counts["store_held"],
        "held_by_kind": held_by_kind,
        "audit_rows": counts["store_audit"],
        "registry": registry.state,
        "registry_revision": registry.revision if registry.revision is not None else UNKNOWN,
        "registry_target_revision": registry.target if registry.target is not None else UNKNOWN,
        "registry_waiting_on_paths": waiting,
    }
