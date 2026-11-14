"""Return a store-mode vault to file mode (OpenSpec make-vault-store-canonical D8, task 1.5).

Rollback runs offline, as the owner. The store holds data that no file has: the audit trail, queued
projections and held corrections. Rollback therefore projects what is queued first, as the
service's projector does, refuses while a person's edit would be overwritten unless the owner
exports it, and exports the audit trail before it drops anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import epistemic_graph, held_fs, lexstore, memory_refs, state_migration, vault
from ..cli_ops import OpError
from ..collection_store import admission, connection, custody, schema
from . import marker, migrate, mode, ownership, projection, projector, read, rederive, write
from .write import Origin

#: The store-mode tables a rollback keeps: the mode row that it sets back to file, and the audit trail, which is append-only history.
_KEPT_TABLES = ("store_mode", "store_audit")  # nosemgrep: ep-word-set -- Table names that schema 15 fixes.
_EMPTIED_TABLES = tuple(table for table in schema.VAULT_STORE_TABLES if table not in _KEPT_TABLES)
#: How many paths a refusal names.
_PATHS_REPORTED = 5


@dataclass(slots=True)
class Findings:
    """What stands between a store-mode vault and file mode."""

    #: How many queued intents are at paths that nothing holds: what a projection writes, removes
    #: or moves before the rollback goes on.
    queued: int = 0
    #: The oldest of them, which a report names.
    queued_first: list[str] = field(default_factory=list)
    #: Paths whose bytes on disk differ from the store's page: a person's edit that no import took in,
    #: or the file beside a held item. A file of Exomem's that a queued intent replaces is not one.
    edited: list[str] = field(default_factory=list)
    #: Paths the store holds that the vault lacks: an outside delete.
    deleted: list[str] = field(default_factory=list)
    #: ``store_held`` rows: edits and deletes that an importer already set aside.
    held: list[tuple[str, str, bytes | None]] = field(default_factory=list)
    #: Files at paths whose page the store no longer holds, which a person changed after Exomem
    #: removed or moved the page: held, or held once the queue is projected. The store's version
    #: of each is no file, so the rollback moves it into the trash.
    vacated: list[str] = field(default_factory=list)

    @property
    def unexported(self) -> int:
        return len(
            {*self.edited, *self.deleted, *self.vacated, *(path for path, _, _ in self.held)}
        )


def inspect(vault_root: Path, conn: sqlite3.Connection) -> Findings:
    """Read the queue, the held rows and every stored page's file through ``conn``; it writes
    nothing.

    A queued path's file counts as a person's only when the projector would hold it, so a file
    that still has Exomem's earlier bytes is what the projection replaces, not an edit. The queue is
    read in batches, with one set of statements for each batch, and a path is classified before its
    file is read: a folder intent's folder and a binary are never read as a page.
    """
    found = Findings()
    found.held = [
        (path, kind, body)
        for path, kind, body in conn.execute(
            "SELECT path, kind, body FROM store_held ORDER BY path"
        )
    ]
    (revision,) = conn.execute("SELECT revision FROM store_mode WHERE id = 1").fetchone()
    view = read.StoreSnapshot(revision, conn)
    vacated: set[str] = set()
    after = ("", "")
    while batch := conn.execute(
        "SELECT path, operation, from_path, queued_at FROM store_projection_queue q "
        f"WHERE (queued_at, path) > (?, ?) AND {mode.UNHELD_INTENT} "
        "ORDER BY queued_at, path LIMIT ?",
        (*after, read.KEYS_PER_QUERY),
    ).fetchall():
        after = (batch[-1][3], batch[-1][0])
        found.queued += len(batch)
        named = _PATHS_REPORTED - len(found.queued_first)
        found.queued_first.extend(row[0] for row in batch[:named])
        written = [path for path, operation, _, _ in batch if operation != "delete" and _page(path)]
        # A removal's path, or a move's old one: a page file there goes unless the pass holds it.
        leaving = [
            rel
            for path, operation, from_path, _ in batch
            if (rel := path if operation == "delete" else from_path) is not None and _page(rel)
        ]
        stored = view.page_metas(written)
        differs = {
            rel: on_disk
            for rel in written
            if rel in stored and (on_disk := _disk_sha256(vault_root, rel)) != stored[rel].sha256
        }
        present = {
            rel: on_disk
            for rel in leaving
            if (on_disk := _disk_sha256(vault_root, rel)) is not None
        }
        held = projector.preview_holds(view, differs, present)
        for rel, on_disk in differs.items():
            if rel in held:
                (found.deleted if on_disk is None else found.edited).append(rel)
        vacated |= held & present.keys()
    # Every stored page that no unheld intent is about to write: its file has the store's bytes.
    for rel, sha256 in conn.execute(
        "SELECT p.path, p.sha256 FROM store_pages p WHERE NOT EXISTS (SELECT 1 FROM "
        f"store_projection_queue q WHERE q.path = p.path AND {mode.UNHELD_INTENT}) ORDER BY p.path"
    ):
        on_disk = _disk_sha256(vault_root, rel)
        if on_disk != sha256:
            (found.deleted if on_disk is None else found.edited).append(rel)
    # A held file whose page the store no longer holds.
    for (rel,) in conn.execute(
        "SELECT h.path FROM store_held h "
        "WHERE NOT EXISTS (SELECT 1 FROM store_pages p WHERE p.path = h.path)"
    ):
        if _page(rel) and _disk_sha256(vault_root, rel) is not None:
            vacated.add(rel)
    found.edited.sort()
    found.deleted.sort()
    found.vacated = sorted(vacated)
    return found


def _page(rel: str) -> bool:
    """Whether ``rel`` names a page file: not a folder intent's folder, nor a binary."""
    return not write.is_folder_intent(rel) and ownership.classify(rel) is ownership.Ownership.PAGE


def _disk_sha256(vault_root: Path, rel: str) -> str | None:
    """The hash of the file at ``rel``, streamed, or None when no file is there."""
    try:
        with vault.open_disk_file_without_pinning(vault_root / rel) as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def _drain(root: Path, writer) -> dict[str, str]:
    """Project every queued intent as the service's projector does, under this step's lease.

    It first removes the temporaries that a killed publication left, a killed rollback's
    included, so a second run continues where a kill stopped the first; a sweep that fails stops
    no pass, as at a service's start. It ends when a pass settles nothing, and returns each intent
    left unsettled with the projector's reason.
    """
    loop = projector.Projector(root, run=lambda work: work(writer))
    loop.sweep_at_start()
    try:
        while (report := loop.run_pass()).settled:
            pass
    except projector.Deferred as refusal:
        return {"*": str(refusal)}
    return report.deferred


def dry_run(vault_root: Path) -> dict:
    """What a rollback would find, read-only and without a lease."""
    root = Path(vault_root).resolve()
    admission.require_owner(root, "store-mode rollback")
    current = mode.state(root)
    if current == mode.FILE_MODE:
        raise _not_active()
    if current == mode.INTERRUPTED_ACTIVATION:
        return {
            "status": "dry-run",
            "interrupted_activation": True,
            "note": "the vault is in file mode; the apply step finishes what an earlier run left, such as the recorded id",
        }
    if current == mode.INTERRUPTED_ROLLBACK:
        return {
            "status": "dry-run",
            "interrupted_rollback": True,
            "note": "the store is already in file mode; the apply step drops the recorded id and rebuilds the sidecars",
        }
    reader, _ = connection._open_read_only(connection.store_path(root), connection.BUSY_TIMEOUT_MS)
    with closing(reader):
        found = inspect(root, reader)
    return {
        "status": "dry-run",
        # The apply projects these first; a file system that refuses one stops it there.
        "queued_projections": found.queued,
        "would_project": {"count": found.queued, "first": found.queued_first},
        "held": len(found.held),
        "edited_on_disk": _named(found.edited),
        "deleted_on_disk": _named(found.deleted),
        "changed_after_removal": _named(found.vacated),
        "refusal": "VAULT_STORE_HELD_ITEMS" if found.unexported else None,
        "needs_export_held": bool(found.unexported),
    }


def _not_active() -> OpError:
    return OpError("VAULT_STORE_NOT_ACTIVE", "this vault is not in store mode")


def _named(paths: list[str]) -> dict:
    return {"count": len(paths), "first": paths[:_PATHS_REPORTED]}


def apply_route(vault_root: Path, *, export_to: Path, why: str, export_held: bool = False) -> dict:
    """Return the vault to file mode as the offline owner, exporting the audit trail and any held edits first."""
    if not isinstance(why, str) or not why.strip():
        raise ValueError("a store-mode rollback needs a reason")
    root = Path(vault_root).resolve()
    admission.require_owner(root, "store-mode rollback")
    if mode.state(root) == mode.FILE_MODE:
        raise _not_active()
    destination = Path(export_to)
    placement = custody.verify_backup_destination(root, destination)
    if not placement.verified:
        raise OpError(
            "VAULT_STORE_EXPORT_DESTINATION", f"the export cannot go there: {placement.reason}"
        )
    result = admission.store_mode_route(
        root,
        "store-mode rollback",
        lambda session, token, writer: _rollback(
            root, session, token, writer, destination=destination, why=why, export_held=export_held
        ),
    )
    migrate.refresh_review_advice(root, result)
    # What the replica says once the hand-back ran: "store" here means the republish is still owed.
    return {**result, "vault_marker": marker.read_replica(root).state}


def _rollback(
    root: Path, session, token, writer, *, destination: Path, why: str, export_held: bool
) -> dict:
    conn = writer.connection
    authority_check = lambda: session.require(token)  # noqa: E731 - the one closure both id calls share
    migrate.remove_stale_staging(root)
    row = conn.execute("SELECT mode, revision FROM store_mode WHERE id = 1").fetchone()
    if row is None or row[0] != mode.STORE_MODE:
        return _rerun(root, writer, authority_check, committed=row is not None)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise OpError("VAULT_STORE_EXPORT_DESTINATION", "the export directory must be new or empty")
    timings: dict[str, float] = {}
    started = time.perf_counter()
    unsettled = _drain(root, writer)
    timings["project_s"] = time.perf_counter() - started
    started = time.perf_counter()
    found = inspect(root, conn)
    timings["inspect_s"] = time.perf_counter() - started
    if found.queued:
        named = [f"{path}: {unsettled.get(path, unsettled.get('*', 'not projected'))}"
                 for path in found.queued_first]
        raise OpError(
            "VAULT_STORE_PROJECTION_PENDING",
            f"{found.queued} queued file(s) could not be projected, so their files lack the "
            f"store's bytes: {'; '.join(named)}",
            "Make each named file or folder writable by Exomem, or move away what stands in its "
            "place, then run this command again. Nothing was rolled back.",
        )
    if found.unexported and not export_held:
        paths = sorted(
            {*found.edited, *found.deleted, *found.vacated, *(p for p, _, _ in found.held)}
        )
        raise OpError(
            "VAULT_STORE_HELD_ITEMS",
            f"{found.unexported} file(s) hold a person's edit or delete that the store has not taken in, e.g. "
            f"{', '.join(paths[:_PATHS_REPORTED])}",
            "Run again with --export-held to keep those edits in the export and project the store's version.",
        )
    started = time.perf_counter()
    exported = _export(root, conn, destination, found, why=why, revision=row[1])
    timings["export_s"] = time.perf_counter() - started
    started = time.perf_counter()
    with writer.handle.transaction() as tx:
        _drop_store_mode(tx, root, revision=row[1] + 1, why=why)
    timings["drop_s"] = time.perf_counter() - started
    result = _finish(root, conn, authority_check, timings)
    return {"status": "rolled-back", "revision": row[1] + 1, "export": exported, **result}


def _rerun(root: Path, writer, authority_check, *, committed: bool) -> dict:
    """Finish what an earlier run left: the id alone, or a committed rollback that stopped before its last steps.

    ``committed`` says the store's mode row exists, so a rollback committed and only the id, the vacuum or the
    sidecar rebuild remain. Without a row, an activation died before its commit, the store holds nothing of
    store mode, and only the id remains. Without the id either, the vault is plainly in file mode.
    An activation that died after it claimed the store, but before its commit, leaves the claim:
    this run decides file over it, so the next hand-back publishes that and the other roots start.
    """
    conn = writer.connection
    recorded = state_migration.recorded_descriptor_ids(root) or ()
    if state_migration.VAULT_STORE_COMPATIBILITY_ID not in recorded:
        raise _not_active()
    with writer.handle.transaction() as tx:
        marker.clear(tx, root)
    if committed:
        return {"status": "rolled-back", **_finish(root, conn, authority_check, {})}
    _drop_id(root, authority_check)
    return {"status": "interrupted-activation-cleared"}


def _drop_id(root: Path, authority_check) -> None:
    try:
        state_migration.record_vault_store_compatibility(
            root, authority_check=authority_check, active=False
        )
    except (OSError, RuntimeError) as error:
        raise OpError(
            "VAULT_STORE_ID_RETAINED",
            f"the vault state still records vault-store-v1: {error}",
            "Run this command again; an earlier release refuses the vault while the id stays.",
        ) from error


def _export(
    root: Path,
    conn: sqlite3.Connection,
    destination: Path,
    found: Findings,
    *,
    why: str,
    revision: int,
) -> dict:
    """Make the audit trail and each held edit durable in ``destination``, then project the store's version over them.

    A held edit's body goes to ``held/<path>``. A file whose bytes differ from the store's page and from its
    held body goes to ``on-disk/<path>`` (or to ``held/<path>`` when nothing is held for it), so each version
    that the projection overwrites stays in the export. A vacated file, whose page the store no
    longer holds, is exported the same way and then moved into the vault's trash, as file mode's
    delete moves a file: the store's version of it is no file. It moves by one rename, never a copy
    and a removal, so a write that lands after the export's copy is in the moved file; a move that
    is refused stops the rollback in store mode. ``export.json`` names where each one went.
    ``export.json`` is written before the first file is projected, so it lists every edit about to be overwritten.
    A crash during the projection leaves those bytes listed only in that first export directory: a rerun finds
    the files already projected, so its own export names none of them.
    """
    audit = [
        {
            "seq": seq,
            "revision": rev,
            "path": path,
            "operation": operation,
            "origin": origin,
            "sha256": sha256,
            "reason": reason,
            "at": at,
            "from_path": from_path,
        }
        for seq, rev, path, operation, origin, sha256, reason, at, from_path in conn.execute(
            "SELECT seq, revision, path, operation, origin, sha256, reason, at, from_path "
            "FROM store_audit ORDER BY seq"
        )
    ]
    projection.write_file(
        destination,
        "audit.jsonl",
        "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in audit).encode(),
        durable=True,
    )
    kept: dict[str, bytes] = {path: body for path, _, body in found.held if body is not None}
    beside: dict[str, bytes] = {}
    for rel in (*found.edited, *found.vacated):
        on_disk = vault.read_disk_bytes_without_pinning(root / rel)
        if rel not in kept:
            kept[rel] = on_disk
        elif kept[rel] != on_disk:
            beside[rel] = on_disk
    for rel, body in kept.items():
        projection.write_file(destination / "held", rel, body, durable=True)
    for rel, body in beside.items():
        projection.write_file(destination / "on-disk", rel, body, durable=True)
    summary = {
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": why,
        "store_revision": revision,
        "audit_rows": len(audit),
        "held_edits": sorted(kept),
        "on_disk_beside_held": sorted(beside),
        "held_deletes": sorted(
            {*found.deleted, *(path for path, kind, _ in found.held if kind == "delete")}
        ),
        # Where each file whose page the store no longer holds goes: a free path in the trash.
        "absent_from_store": _trash_paths(root, found.vacated),
    }
    projection.write_file(
        destination,
        "export.json",
        json.dumps(summary, indent=2, sort_keys=True).encode(),
        durable=True,
    )
    # The rebuild starts from the store's state: each file that differs gets the store's bytes, and the edit stays in the export.
    for rel in (*found.edited, *found.deleted):
        (body,) = conn.execute("SELECT body FROM store_pages WHERE path = ?", (rel,)).fetchone()
        projection.write_file(root, rel, body, durable=True)
    for rel, trashed in summary["absent_from_store"].items():
        try:
            projector.move_into_custody(root, rel, trashed)
        except (projector.Deferred, held_fs.HeldFsError, OSError) as error:
            reason = getattr(error, "code", None) or f"{type(error).__name__}: {error}"
            raise OpError(
                "VAULT_STORE_TRASH_REFUSED",
                f"{rel} could not be moved to {trashed}: {reason}. Its page is no longer in the "
                "store, so file mode must not find it there; nothing was rolled back",
                f"Make {rel} a regular file that Exomem can move, and its trash folder writable, "
                "or move the file out of the vault yourself. Then run this command again with a "
                "new export directory; the vault stays in store mode until then.",
            ) from error
    return {
        "directory": str(destination),
        "audit_rows": len(audit),
        "held_edits": len(kept),
        "on_disk_beside_held": len(beside),
        "held_deletes": len(summary["held_deletes"]),
        "moved_to_trash": len(summary["absent_from_store"]),
    }


def _trash_paths(root: Path, rels: list[str]) -> dict[str, str]:
    """A free trash path for each of ``rels``, named as file mode's delete names one."""
    from ..delete_file import free_trash_path, planned_trash_path

    now = datetime.now()
    chosen: dict[str, str] = {}
    taken: set[Path] = set()
    for rel in rels:
        planned = root / planned_trash_path(root, rel, now)
        free = free_trash_path(planned, lambda slot: slot in taken or os.path.lexists(slot))
        taken.add(free)
        chosen[rel] = free.relative_to(root).as_posix()
    return chosen


def _drop_store_mode(tx: sqlite3.Connection, root: Path, *, revision: int, why: str) -> None:
    objects = tx.execute("SELECT name, type FROM store_mode_objects").fetchall()
    # Triggers go first, then indexes, then tables; dropping a virtual table drops its shadow tables with it.
    for kind, statement in (
        ("trigger", "DROP TRIGGER"),
        ("index", "DROP INDEX"),
        ("virtual", "DROP TABLE"),
        ("table", "DROP TABLE"),
    ):
        for name, recorded in objects:
            if recorded == kind:
                tx.execute(f'{statement} IF EXISTS "{name}"')
    for table in _EMPTIED_TABLES:
        tx.execute(f"DELETE FROM {table}")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tx.execute(
        "INSERT INTO store_audit(revision, path, operation, origin, sha256, reason, at) "
        "VALUES (?, NULL, 'rollback', ?, NULL, ?, ?)",
        (revision, Origin.ROLLBACK.value, why, now),
    )
    tx.execute(
        "UPDATE store_mode SET mode = ?, revision = ?, changed_at = ? WHERE id = 1",
        (mode.FILE_MODE, revision, now),
    )
    marker.clear(tx, root)
    rederive.clear(tx)


def _finish(
    root: Path, conn: sqlite3.Connection, authority_check, timings: dict[str, float]
) -> dict:
    """After the commit: drop the id, return the freed pages to the disk, and bring the sidecars current with their owners.

    Each step's outcome is reported. A failure does not undo the rollback, which already committed; a rerun
    repeats the id, the vacuum and the rebuilds.
    """
    report: dict = {}
    started = time.perf_counter()
    try:
        _drop_id(root, authority_check)
    except OpError as error:
        report["compatibility_id"] = f"retained: {error.code}"
    try:
        conn.execute("VACUUM")
        report["vacuum"] = "done"
    except sqlite3.Error as error:
        report["vacuum"] = f"failed: {type(error).__name__}"
    timings["vacuum_s"] = time.perf_counter() - started
    started = time.perf_counter()
    report["sidecars"] = {
        "lexical": _rebuild_lexical(root),
        "graph": _rebuild_graph(root),
        "references": _rebuild_references(root),
    }
    timings["rebuild_s"] = time.perf_counter() - started
    return {**report, "timings": {name: round(seconds, 2) for name, seconds in timings.items()}}


def _rebuild_lexical(root: Path) -> str:
    """Reconcile the lexical sidecar with the files that the projection rewrote, then ask its owner whether it serves them.

    The owner's atomic rebuild trusts the freshness registry, which saw none of the projection's writes, so it
    declines until something reconciles: its reconcile seam is the one that heals an edit made around the system.
    """
    try:
        lexstore.ensure_fresh(root)
        proof = lexstore.runtime_retrieval_catalog_proof(
            root, require_live_projection=False, schedule_repair=False
        )
    except Exception as error:  # noqa: BLE001 - the service rebuilds a sidecar that this could not
        return f"pending: {type(error).__name__}"
    return "current" if proof else "pending: the index does not yet prove current"


def _rebuild_graph(root: Path) -> str:
    try:
        built = epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    except Exception as error:  # noqa: BLE001 - the service rebuilds a sidecar that this could not
        return f"pending: {type(error).__name__}"
    return "disabled" if built.get("disabled") else "rebuilt"


def _rebuild_references(root: Path) -> str:
    try:
        memory_refs.ReferenceIndex(root).rebuild_all()
    except Exception as error:  # noqa: BLE001 - the service rebuilds a sidecar that this could not
        return f"pending: {type(error).__name__}"
    return "rebuilt"
