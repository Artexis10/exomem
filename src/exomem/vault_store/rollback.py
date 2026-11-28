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
from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .. import epistemic_graph, held_fs, lexstore, memory_refs, state_migration, vault
from ..cli_ops import OpError
from ..collection_store import admission, connection, custody, schema
from . import (
    marker,
    migrate,
    mode,
    ownership,
    projection,
    projector,
    read,
    rederive,
    relationship_files,
    relationships,
    write,
)
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
    #: or the file beside a held item. A file of Evermatter's that a queued intent replaces is not one.
    edited: list[str] = field(default_factory=list)
    #: Paths the store holds that the vault lacks: an outside delete.
    deleted: list[str] = field(default_factory=list)
    #: ``store_held`` rows: edits and deletes that an importer already set aside.
    held: list[tuple[str, str, bytes | None]] = field(default_factory=list)
    #: Files at paths whose page the store no longer holds, which a person changed after Evermatter
    #: removed or moved the page: held, or held once the queue is projected. The store's version
    #: of each is no file, so the rollback moves it into the trash.
    vacated: list[str] = field(default_factory=list)
    projected_blocks: list[str] = field(default_factory=list)
    edited_relations: list[str] = field(default_factory=list)
    deleted_relations: list[str] = field(default_factory=list)
    #: Every portable replacement destination, including a receipt of absence.
    portable_relations: dict[str, projector.OnDisk | None] = field(default_factory=dict)
    #: Manifest hashes for interrupted final replacements whose live sidecars are absent.
    resuming_relations: dict[str, str] = field(default_factory=dict)
    #: Managed sidecars whose canonical projection is absence.
    vacated_relations: dict[str, projector.OnDisk | None] = field(default_factory=dict)

    @property
    def unexported(self) -> int:
        return len(
            {*self.edited, *self.deleted, *self.vacated, *self.edited_relations, *self.deleted_relations,
             *(path for path, _, _ in self.held)}
        )


def inspect(vault_root: Path, conn: sqlite3.Connection) -> Findings:
    """Read the queue, the held rows and every stored page's file through ``conn``; it writes
    nothing.

    A queued path's file counts as a person's only when the projector would hold it, so a file
    that still has Evermatter's earlier bytes is what the projection replaces, not an edit. The queue is
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
    revision, changed_at = conn.execute("SELECT revision, changed_at FROM store_mode WHERE id = 1").fetchone()
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
    for rel, sha256, projected in conn.execute(
        "SELECT p.path,p.sha256,m.sha256 FROM store_pages p LEFT JOIN store_manifest m"
        " ON m.path = p.path AND m.revision = p.revision WHERE NOT EXISTS (SELECT 1 FROM "
        f"store_projection_queue q WHERE q.path = p.path AND {mode.UNHELD_INTENT}) ORDER BY p.path"
    ):
        on_disk = _disk_sha256(vault_root, rel)
        if on_disk != sha256:
            if on_disk is not None and on_disk == projected and _has_projected_block(vault_root, rel, sha256):
                found.projected_blocks.append(rel)
            else:
                (found.deleted if on_disk is None else found.edited).append(rel)
    _inspect_relations(vault_root, conn, found, revision, _activation_id(conn, changed_at))
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


def _inspect_relations(
    root: Path, conn: sqlite3.Connection, found: Findings, revision: int, activation_id: str,
) -> None:
    portable: dict[str, tuple[str, str | None]] = {}
    recorded: dict[str, str | None] = {
        rel: digest for rel, digest in conn.execute(
            "SELECT path,sha256 FROM store_manifest WHERE path LIKE '%' || ?",
            (relationship_files.RELATIONS_FILE_SUFFIX,),
        ) if ownership.relation_source(rel) is not None
    }
    # A dry run can still inspect a store whose writer has yet to install the relationship tables.
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'store_unit_ids'").fetchone():
        for source, witness in relationship_files.export_pages(conn):
            rel = relationship_files.path_for(source)
            if ownership.relation_source(rel) == source:
                portable[rel] = (source, witness)
                recorded.setdefault(rel, None)
    for rel, _, _ in found.held:
        if ownership.relation_source(rel) is not None:
            recorded.setdefault(rel, None)
    for owned in ownership.iter_owned(root, include_relations=True):
        if owned.ownership is ownership.Ownership.RELATIONS:
            recorded.setdefault(owned.rel, None)
    if not recorded:
        return
    recovery = _RollbackRelationProjection(None, activation_id, phase="export")
    rel = "the vault's relations folder"
    try:
        with held_fs.acquire(projector.anchor(root)).require() as filesystem:
            for rel, digest in sorted(recorded.items()):
                receipt = projector.observe(filesystem, rel)
                vacated = rel not in portable
                receipts = found.vacated_relations if vacated else found.portable_relations
                receipts[rel] = receipt
                on_disk = None if receipt is None else receipt.sha256
                if not vacated and on_disk != digest:
                    source, witness = portable[rel]
                    with relationship_files.prepare_file(conn, source, revision, canonical_sha256=witness) as artifact:
                        if artifact is not None and digest is None and on_disk == artifact.sha256:
                            continue  # The live bytes already match this export's canonical output.
                        if (artifact is not None and digest is not None
                                and recovery.owns_replacement(filesystem, rel, artifact, digest, on_disk)):
                            if receipt is None:
                                found.resuming_relations[rel] = digest
                            continue
                if on_disk != digest or (vacated and receipt is not None):
                    (found.deleted_relations if on_disk is None else found.edited_relations).append(rel)
    except held_fs.HeldFsError as error:
        raise _relation_changed(rel) from error


def _relation_changed(rel: str, custody: str | None = None) -> OpError:
    recovery = "Keep the current file and make it a readable regular file."
    if custody is not None:
        recovery += f" If rollback moved it, its bytes are in {json.dumps(custody)}."
    return OpError(
        "VAULT_STORE_RELATIONS_CHANGED",
        f"The inspected version of {json.dumps(rel)} could not be verified for rollback",
        f"{recovery} Retry with a new export directory and use --export-held to keep any outside edit.",
    )


def _read_portable_edit(root: Path, rel: str, receipt: projector.OnDisk) -> bytes:
    """Copy only the exact version the owner agreed to export."""
    try:
        with held_fs.acquire(projector.anchor(root)).require() as filesystem:
            def capture(file: held_fs.HeldFile) -> bytes:
                if file.version() != receipt.version:
                    raise _relation_changed(rel)
                body = filesystem.read(file).require()
                if file.version() != receipt.version or hashlib.sha256(body).hexdigest() != receipt.sha256:
                    raise _relation_changed(rel)
                return body

            body = projector._with_file(filesystem, rel, capture)
    except held_fs.HeldFsError as error:
        raise _relation_changed(rel) from error
    if body is None:
        raise _relation_changed(rel)
    return body


def _has_projected_block(root: Path, rel: str, canonical_sha: str) -> bool:
    try:
        raw = vault.read_disk_bytes_without_pinning(root / rel)
        canonical = relationship_files.strip_block(raw)
    except (OpError, FileNotFoundError):
        return False
    return canonical != raw and hashlib.sha256(canonical).hexdigest() == canonical_sha


def _page(rel: str) -> bool:
    """Whether ``rel`` names a page file: not a folder intent's folder, nor a binary."""
    return not write.is_folder_intent(rel) and ownership.classify(rel) is ownership.Ownership.PAGE


def _disk_sha256(vault_root: Path, rel: str) -> str | None:
    """The hash of the page file at ``rel``, or None when no file is there.

    On POSIX the file is streamed: a rename over an open file is always allowed, so the open
    blocks no writer. Windows refuses a replacement of a file that ``open`` holds, so there the
    page is read whole without pinning it (``vault.read_disk_bytes_without_pinning``); only pages
    come here.
    """
    path = vault_root / rel
    try:
        if os.name == "nt":  # the two platforms whose open differs, as in `vault`
            return hashlib.sha256(vault.read_disk_bytes_without_pinning(path)).hexdigest()
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def _drain(root: Path, writer, destination: Path, *, activation_id: str) -> dict[str, str]:
    """Project every queued intent as the service's projector does, under this step's lease.

    It first removes the temporaries that a killed publication left, a killed rollback's
    included, so a second run continues where a kill stopped the first; a sweep that fails stops
    no pass, as at a service's start. It ends when a pass settles nothing, and returns each intent
    left unsettled with the projector's reason.
    """
    loop = projector.Projector(
        root, run=lambda work: work(writer),
        relation_files=_RollbackRelationProjection(destination, activation_id),
    )
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
        "edited_on_disk": _named(sorted({*found.edited, *found.edited_relations})),
        "deleted_on_disk": _named(sorted({*found.deleted, *found.deleted_relations})),
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
    row = conn.execute("SELECT mode, revision, changed_at FROM store_mode WHERE id = 1").fetchone()
    if row is None or row[0] != mode.STORE_MODE:
        return _rerun(root, writer, authority_check, committed=row is not None)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise OpError("VAULT_STORE_EXPORT_DESTINATION", "the export directory must be new or empty")
    timings: dict[str, float] = {}
    started = time.perf_counter()
    activation_id = _activation_id(conn, row[2])
    unsettled = _drain(root, writer, destination, activation_id=activation_id)
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
            "Make each named file or folder writable by Evermatter, or move away what stands in its "
            "place, then run this command again. Nothing was rolled back.",
        )
    if found.unexported and not export_held:
        paths = sorted(
            {*found.edited, *found.deleted, *found.vacated, *found.edited_relations,
             *found.deleted_relations, *(p for p, _, _ in found.held)}
        )
        raise OpError(
            "VAULT_STORE_HELD_ITEMS",
            f"{found.unexported} file(s) hold a person's edit or delete that the store has not taken in, e.g. "
            f"{', '.join(paths[:_PATHS_REPORTED])}",
            "Run again with --export-held to keep those edits in the export and project the store's version.",
        )
    started = time.perf_counter()
    exported, relation_digests = _export(
        root, conn, destination, found, why=why, revision=row[1], activation_id=activation_id,
        authority_check=authority_check,
    )
    _verify_exported_relations(root, relation_digests)
    timings["export_s"] = time.perf_counter() - started
    started = time.perf_counter()
    with writer.handle.transaction() as tx:
        _drop_store_mode(tx, root, revision=row[1] + 1, why=why)
    timings["drop_s"] = time.perf_counter() - started
    result = _finish(root, conn, authority_check, timings)
    return {"status": "rolled-back", "revision": row[1] + 1, "export": exported, **result}


def _activation_id(conn: sqlite3.Connection, fallback: str) -> str:
    decision = marker.order(marker.read_meta(conn)).decision
    return decision.id if decision else fallback


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
    activation_id: str,
    authority_check: Callable[[], None],
) -> tuple[dict, dict[str, str]]:
    """Make the audit trail and each held edit durable in ``destination``, then project the store's version over them.

    A held edit's body goes to ``held/<path>``. A file whose bytes differ from the store's page and from its
    held body goes to ``on-disk/<path>`` (or to ``held/<path>`` when nothing is held for it), so each version
    that the projection overwrites stays in the export. A vacated file, whose page the store no
    longer holds, is exported the same way and then moved into the vault's trash, as file mode's
    delete moves a file: the store's version of it is no file. It moves by one rename, never a copy
    and a removal, so a write that lands after the export's copy is in the moved file; a move that
    is refused stops the rollback in store mode. ``export.json`` names where each one went.
    A present relation sidecar moves to a named sibling custody slot. The export records each slot
    before its move under ``relation-projections/``. File-mode migration skips those slots.
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
    for rel in (*found.edited, *found.vacated, *found.edited_relations):
        receipt = found.portable_relations.get(rel) or found.vacated_relations.get(rel)
        on_disk = (
            _read_portable_edit(root, rel, receipt) if receipt is not None
            else vault.read_disk_bytes_without_pinning(root / rel)
        )
        if rel not in kept:
            kept[rel] = on_disk
        elif kept[rel] != on_disk:
            beside[rel] = on_disk
    for rel, body in kept.items():
        projection.write_file(destination / "held", rel, body, durable=True)
    for rel, body in beside.items():
        projection.write_file(destination / "on-disk", rel, body, durable=True)
    relation_files = len(found.portable_relations)
    held_deletes = sorted(
        {*found.deleted, *found.deleted_relations,
         *(path for path, kind, _ in found.held if kind == "delete")}
    )
    absent_from_store = _trash_paths(root, found.vacated)
    recovery = _RollbackRelationProjection(destination, activation_id, phase="export")
    summary = {
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": why,
        "store_revision": revision,
        "audit_rows": len(audit),
        "relation_files": relation_files,
        "held_edits": sorted(kept),
        "on_disk_beside_held": sorted(beside),
        "held_deletes": held_deletes,
        # Where each file whose page the store no longer holds goes: a free path in the trash.
        "absent_from_store": absent_from_store,
        "relations_custody": recovery.custody,
    }

    def write_summary() -> None:
        projection.write_file(
            destination, "export.json", json.dumps(summary, indent=2, sort_keys=True).encode(), durable=True,
        )

    write_summary()
    if found.vacated_relations:
        with held_fs.acquire(projector.anchor(root)).require() as filesystem:
            for rel, receipt in found.vacated_relations.items():
                if receipt is None:
                    _vacate_portable_relation(filesystem, rel, None, None, authority_check)
                else:
                    recovery.remove(filesystem, rel, receipt, authority_check)
    with read.snapshot(root, join_request=False) as view:
        relation_digests = _export_relationships(
            root, view.connection, destination, revision, found, recovery, authority_check,
        )
    if recovery.custody:
        write_summary()
    # The rebuild starts from the store's state: each file that differs gets the store's bytes, and the edit stays in the export.
    for rel in (*found.edited, *found.deleted, *found.projected_blocks):
        (body,) = conn.execute("SELECT body FROM store_pages WHERE path = ?", (rel,)).fetchone()
        projection.write_file(root, rel, body, durable=True)
    for rel, trashed in absent_from_store.items():
        try:
            projector.move_into_custody(root, rel, trashed)
        except (projector.Deferred, held_fs.HeldFsError, OSError) as error:
            reason = getattr(error, "code", None) or f"{type(error).__name__}: {error}"
            raise OpError(
                "VAULT_STORE_TRASH_REFUSED",
                f"{rel} could not be moved to {trashed}: {reason}. Its page is no longer in the "
                "store, so file mode must not find it there; nothing was rolled back",
                f"Make {rel} a regular file that Evermatter can move, and its trash folder writable, "
                "or move the file out of the vault yourself. Then run this command again with a "
                "new export directory; the vault stays in store mode until then.",
            ) from error
    return {
        "directory": str(destination),
        "audit_rows": len(audit),
        "relation_files": relation_files,
        "held_edits": len(kept),
        "on_disk_beside_held": len(beside),
        "held_deletes": len(held_deletes),
        "moved_to_trash": len(absent_from_store),
    }, relation_digests


def _export_relationships(
    root: Path,
    conn: sqlite3.Connection,
    destination: Path,
    revision: int,
    found: Findings,
    recovery: _RollbackRelationProjection,
    authority_check: Callable[[], None],
) -> dict[str, str]:
    """Keep each identity and current edge on disk before dropping its canonical rows."""
    if conn.execute("SELECT 1 FROM store_unit_ids LIMIT 1").fetchone() is None:
        return {}
    factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        history = conn.execute(
            "SELECT h.*,r.source_unit_id,r.relation_type,r.target_unit_id,r.target_page_path,r.target_page_ref"
            " FROM store_relationship_history h JOIN store_relationships r ON r.id=h.relationship_id"
            " ORDER BY h.revision,h.relationship_id,h.action"
        )
        projection.write_file(
            destination, "relationship-history.jsonl",
            ((json.dumps(dict(row), sort_keys=True) + "\n").encode() for row in history),
            durable=True,
        )
    finally:
        conn.row_factory = factory
    reference_paths = []
    digests = {}
    inspected = found.portable_relations
    anchor = held_fs.acquire(projector.anchor(root)).require() if inspected else nullcontext()
    with anchor as filesystem:
        for source, witness in relationship_files.export_pages(conn):
            rel = relationship_files.path_for(source)
            if ownership.relation_source(rel) != source:
                if next(relationships.outgoing_on(conn, source), None) is not None:
                    raise OpError(
                        "INVALID_RELATIONS_FILE", "a stored relationship has an unsafe source path",
                    )
                # Owned source files already include active far-end locators. The explicit
                # export keeps the remaining reference identities without writing beside them.
                reference_paths.append(source)
                continue
            if rel not in inspected or filesystem is None:
                raise _relation_changed(rel)
            with relationship_files.prepare_file(conn, source, revision, canonical_sha256=witness) as artifact:
                assert artifact is not None
                projection.write_file(destination / "relationships", rel, artifact.data.open(), durable=True)
                disk = inspected[rel]
                if rel in found.resuming_relations:
                    if recovery.resume(filesystem, rel, artifact, found.resuming_relations[rel], authority_check) is None:
                        raise _relation_changed(rel)
                elif disk is not None and disk.sha256 == artifact.sha256:
                    authority_check()
                    if projector.witness(filesystem, rel) != disk.version:
                        raise _relation_changed(rel)
                else:
                    recovery.publish(filesystem, rel, artifact, disk, authority_check)
                digests[rel] = artifact.sha256
    if reference_paths:
        rows = conn.execute(
            "SELECT id,parent_path,anchor,parent_ref FROM store_unit_ids "
            "WHERE parent_path IN (SELECT value FROM json_each(?)) ORDER BY parent_path,anchor,id",
            (json.dumps(reference_paths),),
        )
        projection.write_file(
            destination, "reference-unit-ids.jsonl",
            ((json.dumps({"id": unit_id, "parent_path": path, "anchor": anchor,
                          "parent_ref": reference}) + "\n").encode()
             for unit_id, path, anchor, reference in rows),
            durable=True,
        )
    return digests


def _verify_exported_relations(root: Path, expected: dict[str, str]) -> None:
    """Observe the managed sidecars once more before committing file mode."""
    if expected:
        rel = "the vault's relations folder"
        try:
            with held_fs.acquire(projector.anchor(root)).require() as filesystem:
                for rel, digest in expected.items():
                    receipt = projector.observe(filesystem, rel)
                    if receipt is None or receipt.sha256 != digest:
                        raise _relation_changed(rel)
        except held_fs.HeldFsError as error:
            raise _relation_changed(rel) from error
    # List membership last so a sidecar created during hashing reaches the cutover check.
    paths = {
        owned.rel for owned in ownership.iter_owned(root, include_relations=True)
        if owned.ownership is ownership.Ownership.RELATIONS
    }
    if changed := paths.symmetric_difference(expected):
        raise _relation_changed(min(changed))


def _vacate_portable_relation(
    filesystem: held_fs.HeldFilesystem,
    rel: str,
    receipt: projector.OnDisk | None,
    custody: str | None,
    authority_check: Callable[[], None],
) -> None:
    expected = None if receipt is None else receipt.version

    def check() -> None:
        authority_check()
        if projector.witness(filesystem, rel) != expected:
            raise _relation_changed(rel, custody)

    try:
        if receipt is not None:
            assert custody is not None
            projector._rename(filesystem, rel, custody, receipt, check)
            displaced = projector.observe(filesystem, custody)
            if displaced is None or (displaced.identity, displaced.sha256) != (receipt.identity, receipt.sha256):
                raise _relation_changed(rel, custody)
        authority_check()
        if projector.witness(filesystem, rel) is not None:
            raise _relation_changed(rel, custody)
    except (projector.Deferred, held_fs.HeldFsError, OSError) as error:
        raise _relation_changed(rel, custody) from error


def _publish_new_relation(
    filesystem: held_fs.HeldFilesystem,
    rel: str,
    artifact: relationship_files.PreparedFile,
    check: Callable[[], None],
) -> projector.OnDisk:
    try:
        return projector._publish(
            filesystem, rel, artifact.data, None, check, sha256=artifact.sha256, rename_new=True,
        )
    except (projector.Deferred, held_fs.HeldFsError, OSError) as error:
        raise _relation_changed(rel) from error


class _RollbackRelationProjection:
    """Keep sidecar replacements recoverable if rollback stops between capture and install."""

    def __init__(self, destination: Path | None, activation_id: str, *, phase: str = "queue") -> None:
        self._destination = destination / "relation-projections" if destination is not None else None
        self._activation_id = activation_id
        self._phase = phase
        self.custody: dict[str, str] = {}

    def publish(
        self, filesystem: held_fs.HeldFilesystem, rel: str, artifact: relationship_files.PreparedFile,
        disk: projector.OnDisk | None, check: Callable[[], None],
    ) -> projector.OnDisk:
        if disk is None:
            return _publish_new_relation(filesystem, rel, artifact, check)
        key, prior, stage = self._slots(rel, artifact.sha256)
        staged = projector.observe(filesystem, stage)
        if staged is None:
            _publish_new_relation(filesystem, stage, artifact, check)
            staged = projector.observe(filesystem, stage)
        if staged is None or staged.sha256 != artifact.sha256:
            raise _relation_changed(stage)
        if projector.witness(filesystem, prior) is not None:
            # An earlier capture owns the stable slot. Preserve this newly present version separately.
            key, prior, _ = self._slots(rel, uuid4().hex)
        self._record(key, rel, prior, disk.sha256, stage=stage, intended_sha256=artifact.sha256)
        _vacate_portable_relation(filesystem, rel, disk, prior, check)
        return self._install(filesystem, rel, prior, stage, staged, check)

    def resume(
        self, filesystem: held_fs.HeldFilesystem, rel: str, artifact: relationship_files.PreparedFile,
        manifest_sha256: str, check: Callable[[], None],
    ) -> projector.OnDisk | None:
        key, prior, stage = self._slots(rel, artifact.sha256)
        receipts = self._receipts(filesystem, prior, stage, manifest_sha256, artifact.sha256)
        if receipts is None:
            return None
        saved, staged = receipts
        if staged is None:
            return None
        self._record(key, rel, prior, saved.sha256, stage=stage, intended_sha256=artifact.sha256)

        def validate_prior() -> None:
            check()
            if projector.witness(filesystem, prior) != saved.version:
                raise _relation_changed(rel, prior)

        return self._install(filesystem, rel, prior, stage, staged, validate_prior)

    def owns_replacement(
        self, filesystem: held_fs.HeldFilesystem, rel: str, artifact: relationship_files.PreparedFile,
        manifest_sha256: str, live_sha256: str | None,
    ) -> bool:
        _, prior, stage = self._slots(rel, artifact.sha256)
        receipts = self._receipts(filesystem, prior, stage, manifest_sha256, artifact.sha256)
        if receipts is None:
            return False
        _, staged = receipts
        return (live_sha256 is None and staged is not None) or (live_sha256 == artifact.sha256 and staged is None)

    @staticmethod
    def _receipts(
        filesystem: held_fs.HeldFilesystem, prior: str, stage: str, prior_sha256: str, intended_sha256: str,
    ) -> tuple[projector.OnDisk, projector.OnDisk | None] | None:
        saved = projector.observe(filesystem, prior)
        if saved is None or saved.sha256 != prior_sha256:
            return None
        staged = projector.observe(filesystem, stage)
        if staged is not None and staged.sha256 != intended_sha256:
            return None
        return saved, staged

    def remove(
        self, filesystem: held_fs.HeldFilesystem, rel: str, disk: projector.OnDisk, check: Callable[[], None],
    ) -> None:
        # An editor can recreate the same bytes after a removal. Give each capture its own slot.
        key, prior, _ = self._slots(rel, uuid4().hex)
        self._record(key, rel, prior, disk.sha256)
        _vacate_portable_relation(filesystem, rel, disk, prior, check)

    def _slots(self, rel: str, revision_witness: str) -> tuple[str, str, str]:
        # Migration can reuse a path and revision. Only this store activation may resume the slot.
        key = hashlib.sha256(f"{self._activation_id}\0{self._phase}\0{rel}\0{revision_witness}".encode()).hexdigest()
        # Sibling dot files stay on the source volume and outside the ownership walk.
        folder = rel.rpartition("/")[0]
        return key, f"{folder}/.{key}.prior.held", f"{folder}/.{key}.stage.held"

    def _record(
        self, key: str, rel: str, prior: str, prior_sha256: str, *,
        stage: str | None = None, intended_sha256: str | None = None,
    ) -> None:
        assert self._destination is not None
        receipt = {
            "phase": self._phase, "path": rel, "custody": prior, "prior_sha256": prior_sha256,
            "stage": stage, "intended_sha256": intended_sha256,
        }
        projection.write_file(
            self._destination, f"{key}.json", json.dumps(receipt, sort_keys=True).encode(), durable=True,
        )
        self.custody[rel] = prior

    @staticmethod
    def _install(
        filesystem: held_fs.HeldFilesystem, rel: str, prior: str, stage: str,
        staged: projector.OnDisk, check: Callable[[], None],
    ) -> projector.OnDisk:
        def validate() -> None:
            check()
            if projector.witness(filesystem, rel) is not None or projector.witness(filesystem, stage) != staged.version:
                raise _relation_changed(rel, prior)

        try:
            projector._rename(filesystem, stage, rel, staged, validate)
            installed = projector.observe(filesystem, rel)
            if installed is None or (installed.identity, installed.sha256) != (staged.identity, staged.sha256):
                raise _relation_changed(rel, prior)
            return installed
        except (projector.Deferred, held_fs.HeldFsError, OSError) as error:
            raise _relation_changed(rel, prior) from error


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
