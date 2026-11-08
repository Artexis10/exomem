"""The projector of a store-mode vault (OpenSpec make-vault-store-canonical D4 and D5, task 4.1).

The store holds each page's exact bytes and the Markdown files are its projection. A commit queues
one intent for each path it changed, and the projector makes the file on disk say what the store
says: it writes a page, removes a deleted file, or renames a moved one. A file is durable before the
projection manifest records it, so a kill at any point leaves an intent for the next start.

The projector never overwrites a person's work. Before it replaces or removes a file it compares
the file's hash with the manifest. A file that matches is Exomem's own projection. A file with the
hash of bytes that the audit trail shows Exomem wrote after the manifest's revision is Exomem's own
projection that a kill left unrecorded. Any other file is held in ``store_held`` and left alone, and
so is a projected file that has gone missing. A held path stays unprojected until something
resolves the hold. This module does not resolve holds.

Every manifest, queue and hold write runs as store-thread work, one short transaction each, and
the projector thread holds no mutation boundary while it waits for the store thread.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from sqlite3 import Connection
from typing import TYPE_CHECKING

from .. import held_fs, vault
from ..collection_store import preview, runtime
from . import ownership, read, write

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
#: Intents listed per query. A listing carries no bodies, so this bounds only the rows in memory.
LISTING_PAGE = 128
STOP_TIMEOUT_SECONDS = 15.0

#: Runs bookkeeping where the store's writer lives: ``run(work)`` calls ``work(writer)`` there.
Runner = Callable[[Callable[["CollectionWriter"], None]], None]


class StoreNotServed(RuntimeError):
    """No running service in this process serves the vault's store, so bookkeeping has no thread."""


class Deferred(Exception):
    """This intent cannot be judged or finished now; it stays queued and the next pass retries."""


def store_thread_runner(vault_root: Path) -> Runner:
    """The runner that sends bookkeeping to the collection store's thread, without a wake-up."""
    root = Path(vault_root).resolve()

    def run(work: Callable[[CollectionWriter], None]) -> None:
        # The one door to the store thread, as the collection store's own edit-back route uses it.
        server = runtime._server(root)
        if server is None:
            raise StoreNotServed("no running service serves this vault's store")

        def on_store_thread() -> None:
            writer = preview.bound_writer(root)
            if writer is None:
                raise StoreNotServed("the store thread holds no writer for this vault")
            work(writer)

        server.call(on_store_thread, wake_maintenance=False)

    return run


@dataclass(frozen=True, slots=True)
class Intent:
    """One queued projection: what to do at ``path``; ``from_path`` is a move's old path."""

    path: str
    revision: int
    operation: str
    from_path: str | None
    queued_at: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """What the projection manifest says the file at a path held when it was last projected."""

    sha256: str
    size: int
    mtime_ns: int
    revision: int


@dataclass(frozen=True, slots=True)
class Facts:
    """What the store records about one path, read in one snapshot and without any body."""

    path: str
    intent: Intent | None
    page: read.PageMeta | None
    manifest: Manifest | None
    held: bool
    #: Hashes of the bytes that the audit trail shows Exomem wrote or recorded here after the
    #: manifest's revision, which a kill may have put on disk before the manifest heard of them. For
    #: a binary, whose file the store never writes, it is the last recorded hash alone.
    audited: frozenset[str]

    @property
    def own_hashes(self) -> frozenset[str]:
        """The hashes of a file that Exomem put at this path and that a person did not change."""
        recorded = frozenset() if self.manifest is None else frozenset({self.manifest.sha256})
        return recorded | self.audited


@dataclass(frozen=True, slots=True)
class OnDisk:
    """The file at a path, as one held read found it."""

    sha256: str
    size: int
    mtime_ns: int
    identity: held_fs.StableIdentity
    permissions: int | None
    #: The file's bytes, read only for a hold, which keeps them.
    body: bytes | None = None


@dataclass(slots=True)
class PassReport:
    """What one pass did. An intent that a refusal left queued is in ``deferred``, with why."""

    #: Files that were written, renamed, or found already in place.
    projected: int = 0
    removed: int = 0
    #: Paths newly held in this pass, whether for an edit or a deleted file.
    held: int = 0
    #: Delete intents that removed nothing, because no file of Exomem's was at the path.
    dropped: int = 0
    deferred: dict[str, str] = field(default_factory=dict)

    @property
    def settled(self) -> int:
        return self.projected + self.removed + self.held + self.dropped


class Projector:
    """Projects a store-mode vault's queued intents onto its files.

    ``run_pass`` is the whole job and is what tests drive. ``start`` runs it on a thread, once
    after the startup sweep of unfinished temporaries and then each ``poll_seconds``.
    """

    def __init__(
        self, vault_root: Path, *, run: Runner | None = None, poll_seconds: float | None = None
    ) -> None:
        self._root = Path(vault_root)
        self._run = run or store_thread_runner(self._root)
        self._poll_seconds = POLL_SECONDS if poll_seconds is None else poll_seconds
        #: A temporary older than this process belongs to a publication that died with its owner.
        self._born_ns = time.time_ns()
        self._stop = threading.Event()
        self._pass_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_failure: str | None = None
        self._last_deferred: dict[str, str] = {}

    # -- thread ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="exomem-vault-projector", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Ask the thread to stop after its current intent and wait for it, bounded."""
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout)
        if thread.is_alive():
            log.warning("the vault projector did not stop within %s seconds", timeout)

    def _loop(self) -> None:
        try:
            self.sweep_temporaries()
        except Exception:  # noqa: BLE001 - a failed sweep must not stop projection; the next start sweeps again
            log.warning("the vault projector's startup sweep failed", exc_info=True)
        while not self._stop.is_set():
            report = None
            try:
                report = self.run_pass()
                self._last_failure = None
            except Exception as error:  # noqa: BLE001 - a store that cannot answer now is asked again next pass
                failure = f"{type(error).__name__}: {error}"
                if failure != self._last_failure:
                    log.warning("a vault projection pass failed: %s", failure, exc_info=True)
                    self._last_failure = failure
            if report is not None:
                self._note_deferred(report)
                if report.settled:
                    continue  # more may have queued while this pass ran
            self._stop.wait(self._poll_seconds)

    def _note_deferred(self, report: PassReport) -> None:
        if report.deferred != self._last_deferred:
            for path, reason in report.deferred.items():
                log.warning("the vault projector left %s queued: %s", path, reason)
            self._last_deferred = report.deferred

    # -- one pass ---------------------------------------------------------------------------------

    def run_pass(self) -> PassReport:
        """Project every intent that is queued now and not held, oldest first, and report it."""
        report = PassReport()
        with self._pass_lock:
            acquired = held_fs.acquire(self._anchor())
            if not acquired.ok:
                raise Deferred(f"the held filesystem route is unavailable ({acquired.error.code})")
            with acquired.require() as filesystem:
                cursor = ("", "")
                while not self._stop.is_set():
                    batch = self._list(cursor)
                    if not batch:
                        break
                    for intent in batch:
                        if self._stop.is_set():
                            break
                        self._project(filesystem, intent.path, report)
                    cursor = (batch[-1].queued_at, batch[-1].path)
        return report

    def _anchor(self) -> Path:
        # Windows resolves a root junction before no-follow anchoring, as the store's own routes do.
        return self._root.resolve() if os.name == "nt" or self._root.is_symlink() else self._root

    def _list(self, after: tuple[str, str]) -> list[Intent]:
        """The next intents after ``after`` in queue order, without bodies or held paths."""
        with read.snapshot(self._root) as view:
            rows = view.connection.execute(
                "SELECT path, revision, operation, from_path, queued_at "
                "FROM store_projection_queue q "
                "WHERE (queued_at, path) > (?, ?) AND NOT EXISTS ("
                "SELECT 1 FROM store_held h WHERE h.path = q.path OR h.path = q.from_path) "
                "ORDER BY queued_at, path LIMIT ?",
                (*after, LISTING_PAGE),
            ).fetchall()
        return [Intent(*row) for row in rows]

    def _facts(self, rel: str) -> Facts:
        with read.snapshot(self._root) as view:
            database = view.connection
            queued = database.execute(
                "SELECT path, revision, operation, from_path, queued_at "
                "FROM store_projection_queue WHERE path = ?",
                (rel,),
            ).fetchone()
            recorded = database.execute(
                "SELECT sha256, size, mtime_ns, revision FROM store_manifest WHERE path = ?", (rel,)
            ).fetchone()
            held = database.execute("SELECT 1 FROM store_held WHERE path = ?", (rel,)).fetchone()
            return Facts(
                path=rel,
                intent=None if queued is None else Intent(*queued),
                page=view.page_meta(rel),
                manifest=None if recorded is None else Manifest(*recorded),
                held=held is not None,
                audited=_audited(database, rel, None if recorded is None else recorded[3]),
            )

    def _stored_body(self, expected: read.PageMeta) -> bytes | None:
        """The body of the page that ``expected`` describes, or None when a commit changed it."""
        with read.snapshot(self._root) as view:
            page = view.page(expected.path)
        if page is None or (page.meta.sha256, page.meta.revision) != (
            expected.sha256,
            expected.revision,
        ):
            return None
        if hashlib.sha256(page.body).hexdigest() != expected.sha256:
            # The manifest would record a hash that the file does not have, so nothing is projected.
            raise Deferred("the stored page does not match its recorded hash")
        return page.body

    # -- one intent -------------------------------------------------------------------------------

    def _project(self, filesystem: held_fs.HeldFilesystem, rel: str, report: PassReport) -> None:
        try:
            facts = self._facts(rel)
            if facts.intent is None or facts.held:
                return  # a commit or a hold settled the path after the listing
            self._dispatch(filesystem, facts, report)
        except Deferred as refusal:
            report.deferred[rel] = str(refusal)
        except held_fs.HeldFsError as refusal:
            report.deferred[rel] = refusal.code
        except OSError as refusal:
            report.deferred[rel] = f"{type(refusal).__name__}: the file system refused"

    def _dispatch(
        self, filesystem: held_fs.HeldFilesystem, facts: Facts, report: PassReport
    ) -> None:
        intent = facts.intent
        assert intent is not None
        owner = ownership.classify(intent.path)
        if intent.operation == "delete":
            allowed = owner is not ownership.Ownership.NONE
        else:
            # A move's destination and a write are text the store holds, so only a page path fits.
            allowed = owner is ownership.Ownership.PAGE
        if not allowed:
            raise Deferred("the store does not own this path")
        if intent.operation == "delete":
            self._project_delete(filesystem, facts, report)
        elif intent.operation == "move":
            self._project_move(filesystem, facts, report)
        else:
            self._project_write(filesystem, facts, report)

    def _project_write(
        self, filesystem: held_fs.HeldFilesystem, facts: Facts, report: PassReport
    ) -> None:
        intent, page, manifest = facts.intent, facts.page, facts.manifest
        assert intent is not None
        if page is None:
            raise Deferred("a write intent names a path with no stored page")
        disk = _observe(filesystem, intent.path)
        if disk is None:
            if manifest is not None:
                # The file was projected, is gone, and Exomem did not remove it: an outside delete.
                self._hold(
                    report,
                    intent.path,
                    "delete",
                    base_revision=manifest.revision,
                    store_revision=page.revision,
                    sha256=manifest.sha256,
                    body=None,
                )
                return
        elif disk.sha256 == page.sha256:
            self._record_projected(page, disk.size, disk.mtime_ns)
            report.projected += 1
            return
        elif disk.sha256 not in facts.own_hashes:
            self._hold_edit(filesystem, report, facts, store_revision=page.revision)
            return
        body = self._stored_body(page)
        if body is None:
            return  # a commit changed the page meanwhile, and its intent comes up on the next pass
        installed = _publish(filesystem, intent.path, body, disk)
        self._record_projected(page, installed.size, installed.mtime_ns)
        report.projected += 1

    def _project_delete(
        self, filesystem: held_fs.HeldFilesystem, facts: Facts, report: PassReport
    ) -> None:
        intent = facts.intent
        assert intent is not None
        disk = _observe(filesystem, intent.path)
        removed = report.removed
        if self._remove(filesystem, facts, disk, report, store_revision=None):
            self._record_removed(intent.path, intent.revision)
            if report.removed == removed:
                report.dropped += 1  # no file of Exomem's was there

    def _remove(
        self,
        filesystem: held_fs.HeldFilesystem,
        facts: Facts,
        disk: OnDisk | None,
        report: PassReport,
        *,
        store_revision: int | None,
    ) -> bool:
        """Take away the file at ``facts.path`` if it is Exomem's own, and hold it if a person's.

        A path with no manifest row was never projected, so a file there that Exomem did not write
        is not this removal's to touch: it stays as it is, unheld and unlinked. Returns False only
        when the file was held, and True otherwise, meaning that the removal is settled.
        """
        if disk is None:
            return True
        if disk.sha256 in facts.own_hashes:
            _unlink(filesystem, facts.path, disk)
            report.removed += 1
            return True
        if facts.manifest is None:
            return True
        self._hold_edit(filesystem, report, facts, store_revision=store_revision)
        return False

    def _project_move(
        self, filesystem: held_fs.HeldFilesystem, facts: Facts, report: PassReport
    ) -> None:
        """Rename the old file when it is the page's projection and the new path is free.

        Any other state is the move's two halves, which are the removal of the old path and the
        write of the new one from the store, and each half then keeps its own rule about a
        person's file.
        """
        intent, page = facts.intent, facts.page
        assert intent is not None and intent.from_path is not None
        if page is None:
            raise Deferred("a move intent names a path with no stored page")
        old = self._facts(intent.from_path)
        if old.held:
            return
        old_disk = _observe(filesystem, old.path)
        if (
            old_disk is not None
            and old.manifest is not None
            and old.manifest.sha256 == page.sha256 == old_disk.sha256
            and _observe(filesystem, intent.path) is None
        ):
            renamed = _rename(filesystem, old.path, intent.path, old_disk)
            self._record_moved(intent, page, old.path, renamed)
            report.projected += 1
            return
        if not self._remove(filesystem, old, old_disk, report, store_revision=None):
            return
        self._record_move_split(intent, old.path)
        self._project(filesystem, intent.path, report)

    # -- holds ------------------------------------------------------------------------------------

    def _hold_edit(
        self,
        filesystem: held_fs.HeldFilesystem,
        report: PassReport,
        facts: Facts,
        *,
        store_revision: int | None,
    ) -> None:
        """Keep a person's file and its bytes: it is neither the projection nor Exomem's own."""
        kept = _observe(filesystem, facts.path, keep_body=True)
        if kept is None or kept.body is None:
            raise Deferred("the file vanished while it was being held")
        self._hold(
            report,
            facts.path,
            "edit",
            base_revision=None if facts.manifest is None else facts.manifest.revision,
            store_revision=store_revision,
            sha256=kept.sha256,
            body=kept.body,
        )

    def _hold(
        self,
        report: PassReport,
        rel: str,
        kind: str,
        *,
        base_revision: int | None,
        store_revision: int | None,
        sha256: str,
        body: bytes | None,
    ) -> None:
        # One row for each path: a second outside change replaces the bytes and keeps the first
        # detection time, so a person's repeated saves are one held item.
        # The store's one timestamp format is the commit's, so held rows and queue rows read alike.
        detected = write._timestamp()

        def work(writer: CollectionWriter) -> None:
            with writer.handle.transaction() as tx:
                tx.execute(
                    "INSERT INTO store_held(path, kind, base_revision, store_revision, sha256, "
                    "body, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "kind = excluded.kind, base_revision = excluded.base_revision, "
                    "store_revision = excluded.store_revision, sha256 = excluded.sha256, "
                    "body = excluded.body",
                    (rel, kind, base_revision, store_revision, sha256, body, detected),
                )

        self._run(work)
        report.held += 1
        log.warning(
            "the vault projector held %s: the file on disk is not Exomem's projection (%s)",
            rel,
            kind,
        )

    # -- bookkeeping, each one short transaction on the store thread -----------------------------

    def _record_projected(self, page: read.PageMeta, size: int, mtime_ns: int) -> None:
        """The file holds ``page``'s bytes: record it, and drop the intent unless a newer waits."""

        def work(writer: CollectionWriter) -> None:
            with writer.handle.transaction() as tx:
                _record_file(tx, page.path, page.sha256, size, mtime_ns, page.revision)
                tx.execute(
                    "DELETE FROM store_projection_queue WHERE path = ? AND revision <= ?",
                    (page.path, page.revision),
                )

        self._run(work)

    def _record_removed(self, rel: str, revision: int) -> None:
        def work(writer: CollectionWriter) -> None:
            with writer.handle.transaction() as tx:
                tx.execute("DELETE FROM store_manifest WHERE path = ?", (rel,))
                tx.execute(
                    "DELETE FROM store_projection_queue WHERE path = ? AND revision <= ?",
                    (rel, revision),
                )

        self._run(work)

    def _record_moved(
        self, intent: Intent, page: read.PageMeta, old_path: str, renamed: OnDisk
    ) -> None:
        """The old file now sits at the new path and holds the page's bytes."""

        def work(writer: CollectionWriter) -> None:
            with writer.handle.transaction() as tx:
                tx.execute("DELETE FROM store_manifest WHERE path = ?", (old_path,))
                _record_file(
                    tx, page.path, page.sha256, renamed.size, renamed.mtime_ns, page.revision
                )
                tx.execute(
                    "DELETE FROM store_projection_queue WHERE path = ? AND revision <= ?",
                    (page.path, page.revision),
                )

        self._run(work)

    def _record_move_split(self, intent: Intent, old_path: str) -> None:
        """The old path has no file left: the rest of the move is a write of the new path."""

        def work(writer: CollectionWriter) -> None:
            with writer.handle.transaction() as tx:
                tx.execute("DELETE FROM store_manifest WHERE path = ?", (old_path,))
                _turn_into_write(tx, intent)

        self._run(work)

    # -- startup sweep ----------------------------------------------------------------------------

    def sweep_temporaries(self) -> int:
        """Remove the temporaries that a killed publication left, and return how many.

        A kill inside a publication leaves its temporary beside the target, and the target's intent
        is still queued, so the folders of the queued paths are the only places to look. A temporary
        is removed only when it is older than this process, because the name format is shared with
        every held publication and a live one is never this process's leftover.
        """
        with read.snapshot(self._root) as view:
            folders = {
                path.rpartition("/")[0]
                for row in view.connection.execute(
                    "SELECT path, from_path FROM store_projection_queue"
                )
                for path in row
                if path
            }
        removed = 0
        acquired = held_fs.acquire(self._anchor())
        if not acquired.ok:
            raise Deferred(f"the held filesystem route is unavailable ({acquired.error.code})")
        with acquired.require() as filesystem:
            for folder in sorted(folders):
                removed += self._sweep_folder(filesystem, folder)
        return removed

    def _sweep_folder(self, filesystem: held_fs.HeldFilesystem, folder: str) -> int:
        opened = filesystem.parent(folder, access="mutate")
        if _missing(opened):
            return 0
        removed = 0
        with opened.require() as parent:
            leftovers = [
                name for name in filesystem.iter_names(parent) if held_fs.is_publish_temporary(name)
            ]
            for name in leftovers:
                held = filesystem.file(parent, name, access="mutate")
                if not held.ok:
                    continue
                with held.require() as leftover:
                    if leftover.version().mtime_ns >= self._born_ns:
                        continue
                    _retrying(partial(filesystem.unlink, leftover))
                    removed += 1
        return removed


# -- store reads ----------------------------------------------------------------------------------


def _audited(database: Connection, rel: str, manifest_revision: int | None) -> frozenset[str]:
    """The hashes that ``Facts.audited`` names: rows after the manifest, or a binary's last row."""
    if ownership.classify(rel) is ownership.Ownership.PAGE:
        rows = database.execute(
            "SELECT DISTINCT sha256 FROM store_audit "
            "WHERE path = ? AND revision > ? AND sha256 IS NOT NULL",
            (rel, manifest_revision or 0),
        )
    else:
        rows = database.execute(
            "SELECT sha256 FROM store_audit WHERE path = ? AND sha256 IS NOT NULL "
            "ORDER BY seq DESC LIMIT 1",
            (rel,),
        )
    return frozenset(row[0] for row in rows)


def _record_file(
    tx: Connection, rel: str, sha256: str, size: int, mtime_ns: int, revision: int
) -> None:
    """Record what the file at ``rel`` holds; the page's mtime then says what a stat does."""
    tx.execute(
        "INSERT INTO store_manifest(path, sha256, size, mtime_ns, revision) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, size = excluded.size, "
        "mtime_ns = excluded.mtime_ns, revision = excluded.revision",
        (rel, sha256, size, mtime_ns, revision),
    )
    tx.execute(
        "UPDATE store_pages SET mtime_ns = ? WHERE path = ? AND revision = ? AND sha256 = ?",
        (mtime_ns, rel, revision, sha256),
    )


def _turn_into_write(tx: Connection, intent: Intent) -> None:
    """A move whose bytes need a rewrite is a write of its new path, unless a commit changed it."""
    tx.execute(
        "UPDATE store_projection_queue SET operation = 'write', from_path = NULL "
        "WHERE path = ? AND revision = ? AND operation = 'move'",
        (intent.path, intent.revision),
    )


# -- disk, through the held filesystem ------------------------------------------------------------


def _missing(result: held_fs.HeldResult) -> bool:
    return not result.ok and result.error is not None and result.error.code == "MISSING"


def _split(rel: str) -> tuple[str, str]:
    folder, _, leaf = rel.rpartition("/")
    return folder, leaf


def _observe(
    filesystem: held_fs.HeldFilesystem, rel: str, *, keep_body: bool = False
) -> OnDisk | None:
    """The file at ``rel`` with its hash, or None when no file is there.

    A refusal other than absence raises ``HeldFsError``: a symlink, an unreadable folder or a name
    that differs only by case is not something to judge, and the intent stays queued.
    """
    folder, leaf = _split(rel)
    opened = filesystem.parent(folder, access="mutate")
    if _missing(opened):
        return None
    with opened.require() as parent:
        found = filesystem.file(parent, leaf)
        if _missing(found):
            return None
        with found.require() as file:
            data = filesystem.read(file).require()
            version = file.version()
            return OnDisk(
                sha256=hashlib.sha256(data).hexdigest(),
                size=version.size,
                mtime_ns=version.mtime_ns,
                identity=file.identity,
                permissions=_permissions(file),
                body=data if keep_body else None,
            )


def _permissions(file: held_fs.HeldFile) -> int | None:
    descriptor = getattr(file, "descriptor", None)
    return stat.S_IMODE(os.fstat(descriptor).st_mode) if isinstance(descriptor, int) else None


def _keep_permissions(permissions: int | None) -> Callable[[held_fs.HeldFile], None] | None:
    """A replacement keeps the mode of the file it replaces; a new file takes the default mode."""
    # Windows has no mode bits to carry; its attributes stay with the held filesystem's own rules.
    if permissions is None or os.name == "nt":
        return None

    def prepare(staged: held_fs.HeldFile) -> None:
        descriptor = getattr(staged, "descriptor", None)
        if isinstance(descriptor, int):
            os.fchmod(descriptor, permissions)

    return prepare


def _retrying(operation: Callable[[], held_fs.HeldResult]) -> None:
    """Run a held operation, waiting out a transient Windows sharing refusal; other refusals raise.

    The Windows backend does not retry, and a replacement or an unlink that a scanner holds open
    fails for a moment. Only a ``PermissionError`` cause reaches the waiting wrapper.
    """

    def once() -> None:
        result = operation()
        error = result.error
        if (
            error is not None
            and error.code == "IO_REFUSED"
            and isinstance(error.cause, PermissionError)
        ):
            raise error.cause
        result.require()

    vault.replace_tolerating_transient_sharing(once)


def _publish(
    filesystem: held_fs.HeldFilesystem, rel: str, body: bytes, existing: OnDisk | None
) -> OnDisk:
    """Write ``body`` to a new file, or replace ``existing`` only while it is still that exact file.

    The held filesystem compares the file's identity and hash again just before the replacing
    rename, and makes the temporary durable before it and the folder durable after (on POSIX).
    """
    folder, leaf = _split(rel)
    with filesystem.parent(folder, create=True, access="mutate").require() as parent:
        published: list[held_fs.StableIdentity] = []

        def publish():
            result = held_fs.publish_bytes(
                filesystem,
                parent,
                leaf,
                body,
                expected_identity=None if existing is None else existing.identity,
                expected_sha256=None if existing is None else existing.sha256,
                prepare=_keep_permissions(None if existing is None else existing.permissions),
            )
            if result.ok:
                published.append(result.require())
            return result

        _retrying(publish)
        return _installed(filesystem, parent, leaf, published[-1], hashlib.sha256(body).hexdigest())


def _installed(
    filesystem: held_fs.HeldFilesystem,
    parent: held_fs.HeldDirectory,
    leaf: str,
    identity: held_fs.StableIdentity,
    sha256: str,
) -> OnDisk:
    """What the installed file shows, after a check that it is still the file that was installed."""
    with filesystem.file(parent, leaf).require() as file:
        version = file.version()
        if file.identity != identity:
            raise Deferred("the file changed as soon as it was installed")
        return OnDisk(sha256, version.size, version.mtime_ns, file.identity, _permissions(file))


def _unlink(filesystem: held_fs.HeldFilesystem, rel: str, expected: OnDisk) -> None:
    """Remove the file only while it is still the exact file, with the bytes, that was judged."""
    folder, leaf = _split(rel)
    with filesystem.parent(folder, access="mutate").require() as parent:
        with filesystem.file(parent, leaf, access="mutate").require() as file:
            if (
                file.identity != expected.identity
                or hashlib.sha256(filesystem.read(file).require()).hexdigest() != expected.sha256
            ):
                raise Deferred("the file changed before it was removed")
            _retrying(lambda: filesystem.unlink(file))


def _rename(filesystem: held_fs.HeldFilesystem, old: str, new: str, expected: OnDisk) -> OnDisk:
    """Rename the old file to a free path, only while it is still the exact file that was judged."""
    old_folder, old_leaf = _split(old)
    new_folder, new_leaf = _split(new)
    with (
        filesystem.parent(old_folder, access="mutate").require() as old_parent,
        filesystem.parent(new_folder, create=True, access="mutate").require() as new_parent,
    ):
        with filesystem.file(old_parent, old_leaf, access="mutate").require() as file:
            if (
                file.identity != expected.identity
                or hashlib.sha256(filesystem.read(file).require()).hexdigest() != expected.sha256
            ):
                raise Deferred("the file changed before it was renamed")
            _retrying(lambda: filesystem.rename(file, new_parent, new_leaf, replace=False))
        return _installed(filesystem, new_parent, new_leaf, expected.identity, expected.sha256)
