"""The projector of a store-mode vault (OpenSpec make-vault-store-canonical D4 and D5, task 4.1).

The store holds each page's exact bytes and the Markdown files are its projection. A commit queues
one intent for each path it changed, and the projector makes the file on disk say what the store
says: it writes a page, removes a deleted file, or renames a moved one. A file is durable before the
projection manifest records it, so a kill at any point leaves an intent for the next start.

The projector never overwrites a person's work. Before it replaces or removes a file it compares
the file's hash with the manifest. A file that matches is Exomem's own projection. A file with the
hash of bytes that the audit trail shows Exomem wrote for this path in its current life, which a
kill left unrecorded, is Exomem's own projection too. Any other file is held in ``store_held`` and
left alone, and so is a projected file that has gone missing, unless the audit trail shows that
Exomem removed it. A held path stays unprojected until something resolves the hold. This module
does not resolve holds.

Every manifest, queue and hold write runs as store-thread work, one short transaction each, inside
the vault mutation guard that the store thread takes for it (``store_thread_runner``). The
projector thread holds no mutation boundary while it waits for the store thread. A busy boundary
leaves the intent queued for the next pass.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from sqlite3 import Connection
from typing import TYPE_CHECKING, TypeVar

from .. import held_fs, mutation_lock, vault
from ..collection_store import preview, runtime
from . import mode, ownership, read, write

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
#: Intents listed per query. A listing carries no bodies, so this bounds only the rows in memory.
LISTING_PAGE = 128
STOP_TIMEOUT_SECONDS = 15.0
#: The most that a hold copies into the store. A held edit is one SQLite blob; a file above this
#: stays on disk, untouched, and is held by its hash alone. The hash, not the size, says whose.
HELD_BODY_LIMIT = 64 * 1024 * 1024
#: The two kinds of ``store_held`` row, which the table's own CHECK fixes: a person's changed bytes,
#: and a person's delete.
HELD_EDIT = "edit"
HELD_DELETE = "delete"

#: Runs bookkeeping where the store's writer lives: ``run(work)`` calls ``work(writer)`` there.
Runner = Callable[[Callable[["CollectionWriter"], None]], None]

_Found = TypeVar("_Found")


class StoreNotServed(RuntimeError):
    """No running service in this process serves the vault's store, so bookkeeping has no thread."""


class Deferred(Exception):
    """This intent cannot be judged or finished now; it stays queued and the next pass retries."""


class BoundaryBusy(Exception):
    """Another holder has the vault's mutation boundary, an interactive writer waits for it, or
    foreground requests kept the store thread for the mutation timeout, so the store thread did not
    run the bookkeeping. For background work this is ordinary contention: the caller leaves its
    intent queued, or its path to the next sweep, and logs no error."""


class HopUnderBoundary(RuntimeError):
    """A caller sent bookkeeping to the store thread while its own thread holds a mutation boundary.

    The store thread takes the vault boundary for the work, so it would wait for this caller, which
    waits for the store thread, until the mutation timeout. Take the boundary inside the hopped work
    instead (``media.on_store_thread`` runs a whole guarded unit there).
    """


def store_thread_runner(vault_root: Path, *, operation: str = "vault_store_bookkeeping") -> Runner:
    """The runner that sends bookkeeping to the collection store's thread, without a wake-up.

    The work runs there inside the vault mutation guard, which the store's lease check requires of
    the thread that writes (``CollectionStoreRuntime._authority``): a manifest record, an import or
    a media commit orders with every other store mutation. One call is one hold, so the caller
    bounds what one ``work`` does. ``operation`` names the hold in the boundary's holder record.
    """
    root = Path(vault_root).resolve()

    def run(work: Callable[[CollectionWriter], None]) -> None:
        if mutation_lock.current_thread_holds_boundary():
            raise HopUnderBoundary(
                f"{operation}: this thread holds a mutation boundary, which the store thread "
                "would wait for; take the boundary inside the hopped work"
            )
        # The one door to the store thread, as the collection store's own edit-back route uses it.
        server = runtime._server(root)
        if server is None:
            raise StoreNotServed("no running service serves this vault's store")

        def on_store_thread() -> None:
            writer = preview.bound_writer(root)
            if writer is None:
                raise StoreNotServed("the store thread holds no writer for this vault")
            with _holding_boundary(root, operation):
                work(writer)

        def not_taken(_completion_deadline: float) -> float | None:
            # Foreground requests go first, so under their load the store may not take this in the
            # mutation timeout. The queue drops a request it did not take: nothing of it ran.
            raise BoundaryBusy(f"{operation}: the store thread did not take it in time")

        server.call(on_store_thread, wake_maintenance=False, extend=not_taken)

    return run


#: How long one store-thread job of a batch settles units under one hold before it ends that hold,
#: so a writer that waits for the vault boundary waits for this and one unit at most. The batch's
#: other units take the next hold (``importer.Importer._in_holds``). The projector records one
#: intent per hold, so it needs no budget.
HOLD_BUDGET_SECONDS = 0.25


@contextmanager
def _holding_boundary(root: Path, operation: str) -> Iterator[None]:
    """Hold the vault mutation guard on the store thread for one unit of bookkeeping, or raise
    ``BoundaryBusy`` having waited for nothing.

    Bookkeeping is background work. It never waits here, because a wait on the store thread holds
    back every store request queued behind it, and it goes after an interactive writer that waits
    (``waiter_count``). The guard re-enters the zero-wait hold, so it adds writer authority and the
    commit generation bump that every store mutation takes.
    """
    from ..writer_lease import active_manager

    manager = active_manager()
    mutation = manager._mutation_coordinator_for(root)
    if mutation.waiter_count():
        raise BoundaryBusy(f"{operation}: a writer waits for the vault boundary")
    with mutation.try_hold(operation=operation, holder_kind="background") as held:
        if not held:
            raise BoundaryBusy(f"{operation}: another holder has the vault boundary")
        with manager.mutation_guard(root, operation=operation, holder_kind="background"):
            yield


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
    #: The hold at this path as its row reads, or None when nothing is held. A commit that settles
    #: the path names this row in ``StoreChange.resolves_held``.
    held_row: write.HeldRow | None
    #: Hashes of the bytes that the audit trail shows Exomem wrote or recorded here, which a kill
    #: may have put on disk before the manifest heard of them: those after the manifest's revision,
    #: or with no manifest, those of this path's current life. For a binary, whose file the store
    #: never writes, it is the last recorded hash alone.
    audited: frozenset[str]
    #: The audit trail shows Exomem removed this path after the manifest's revision, so a missing
    #: file is the removal that a kill left unrecorded and not an outside delete.
    removed: bool = False
    #: The record of a binary at this path; None for a page, or when the store records none.
    binary: read.BinaryMeta | None = None
    #: A queued move names this path as its source. The projector still has to remove the file
    #: here, so Exomem's link to the path lasts until that removal, as an intent's does.
    moves_away: bool = False

    @property
    def held(self) -> bool:
        return self.held_row is not None

    @property
    def held_sha256(self) -> str | None:
        """The hash of the bytes that the hold keeps, or None when nothing is held at this path."""
        return None if self.held_row is None else self.held_row.sha256

    @property
    def held_kind(self) -> str | None:
        """``HELD_EDIT`` or ``HELD_DELETE`` for the hold at this path, or None when none is held."""
        return None if self.held_row is None else self.held_row.kind

    @property
    def stored_sha256(self) -> str | None:
        """The hash of the page or binary that the store holds at this path, or None."""
        if self.page is not None:
            return self.page.sha256
        return None if self.binary is None else self.binary.sha256

    @property
    def own_hashes(self) -> frozenset[str]:
        """The hashes of a file that Exomem put at this path and that a person did not change.

        With no manifest row, no queued intent, no queued move away and nothing stored, Exomem's
        life at this path has ended: a file with bytes of that life is a person's restore, so none
        are Exomem's.
        """
        if (
            self.manifest is None
            and self.intent is None
            and not self.moves_away
            and self.stored_sha256 is None
        ):
            return frozenset()
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
    #: The held version (identity, size, mtime, change time) that this read saw; a later read that
    #: finds another version knows that the file changed in between.
    version: held_fs.FileVersion | None = None


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
    #: The pass stopped at a busy vault boundary (``BoundaryBusy``); what it had not recorded stays
    #: queued, and the next pass retries it.
    busy: bool = False

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
        self._run = run or store_thread_runner(self._root, operation="vault_store_projection")
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
                if report.settled and not report.busy:
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
            acquired = held_fs.acquire(anchor(self._root))
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
                        try:
                            self._project(filesystem, intent.path, report)
                        except BoundaryBusy:
                            # Every later record needs the same boundary, so the pass ends here.
                            report.busy = True
                            return report
                    cursor = (batch[-1].queued_at, batch[-1].path)
        return report

    def _list(self, after: tuple[str, str]) -> list[Intent]:
        """The next intents after ``after`` in queue order, without bodies or held paths."""
        with read.snapshot(self._root) as view:
            rows = view.connection.execute(
                "SELECT path, revision, operation, from_path, queued_at "
                "FROM store_projection_queue q WHERE (queued_at, path) > (?, ?) "
                f"AND {mode.UNHELD_INTENT} ORDER BY queued_at, path LIMIT ?",
                (*after, LISTING_PAGE),
            ).fetchall()
        return [Intent(*row) for row in rows]

    def _facts(self, rel: str) -> Facts:
        return read_facts(self._root, rel)

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
        if intent.operation == "write":
            # A write is text the store holds, so only a page path fits.
            allowed = owner is ownership.Ownership.PAGE
        else:
            allowed = owner is not ownership.Ownership.NONE
        if not allowed:
            raise Deferred("the store does not own this path")
        if intent.operation == "delete":
            self._project_delete(filesystem, facts, report)
        elif intent.operation == "move" and owner is ownership.Ownership.BINARY:
            self._project_binary_move(filesystem, facts, report)
        elif intent.operation == "move":
            self._project_move(filesystem, facts, report)
        else:
            self._project_write(filesystem, facts, report)

    def _project_write(
        self, filesystem: held_fs.HeldFilesystem, facts: Facts, report: PassReport
    ) -> None:
        intent, page = facts.intent, facts.page
        assert intent is not None
        if page is None:
            raise Deferred("a write intent names a path with no stored page")
        disk = observe(filesystem, intent.path)
        self._apply_write(filesystem, facts, disk, _judge_write(facts, disk), report)

    def _apply_write(
        self,
        filesystem: held_fs.HeldFilesystem,
        facts: Facts,
        disk: OnDisk | None,
        verdict: Judgement,
        report: PassReport,
    ) -> None:
        page, manifest = facts.page, facts.manifest
        assert page is not None
        if verdict is Judgement.HOLD_DELETE:
            assert manifest is not None
            self._hold(
                report,
                facts.path,
                HELD_DELETE,
                base_revision=manifest.revision,
                store_revision=page.revision,
                sha256=manifest.sha256,
                body=None,
            )
        elif verdict is Judgement.HOLD_EDIT:
            self._hold_edit(filesystem, report, facts, store_revision=page.revision)
        elif verdict is Judgement.IN_PLACE:
            assert disk is not None
            # A retry after a refused flush or install finds the file in place. It cannot know which
            # folders the first attempt made, so every folder up to the root is flushed first.
            _flush_through_root(filesystem, _split(facts.path)[0])
            self._record_projected(page, disk.size, disk.mtime_ns)
            report.projected += 1
        else:
            body = self._stored_body(page)
            if body is None:
                return  # a commit changed the page meanwhile; its intent comes up next pass
            installed = _publish(filesystem, facts.path, body, disk)
            self._record_projected(page, installed.size, installed.mtime_ns)
            report.projected += 1

    def _project_delete(
        self, filesystem: held_fs.HeldFilesystem, facts: Facts, report: PassReport
    ) -> None:
        intent = facts.intent
        assert intent is not None
        disk = observe(filesystem, intent.path)
        removed = report.removed
        if self._remove(filesystem, facts, disk, report, store_revision=None):
            self._record_removed(intent.path, intent.revision)
            if report.removed == removed:
                report.dropped += 1  # no file of Exomem's was there
            else:
                prune_emptied(filesystem, _split(intent.path)[0])

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
            # A retry after a refused flush finds the file gone; its removal is made durable first.
            _flush_folder(filesystem, _split(facts.path)[0])
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
        old_disk = observe(filesystem, old.path)
        new_disk = observe(filesystem, intent.path)
        if (
            old_disk is not None
            and old.manifest is not None
            and old.manifest.sha256 == page.sha256 == old_disk.sha256
            and new_disk is None
        ):
            renamed = _rename(filesystem, old.path, intent.path, old_disk)
            self._record_moved(intent, page, old.path, renamed)
            report.projected += 1
            _move_parts(filesystem, old.path, intent.path)
            prune_emptied(filesystem, _split(old.path)[0])
            return
        verdict = _judge_write(facts, new_disk)
        if verdict in (Judgement.HOLD_DELETE, Judgement.HOLD_EDIT):
            # A person's file at the new path is held before the old file goes: the old one stays.
            self._apply_write(filesystem, facts, new_disk, verdict, report)
            return
        if not self._remove(filesystem, old, old_disk, report, store_revision=None):
            return
        self._record_move_split(intent, old.path)
        _move_parts(filesystem, old.path, intent.path)
        prune_emptied(filesystem, _split(old.path)[0])
        self._project(filesystem, intent.path, report)

    def _project_binary_move(
        self, filesystem: held_fs.HeldFilesystem, facts: Facts, report: PassReport
    ) -> None:
        """Finish a binary's move: rename its file when the old path still holds the recorded bytes
        and the new path is free, or find that the writer already did.

        The store holds no bytes of a binary, so the projector never unlinks or replaces one. In
        any other state a person has changed, removed or put a file at one of the paths: the intent
        is dropped, and the importer judges both paths as the outside changes that they are.
        """
        intent, binary = facts.intent, facts.binary
        assert intent is not None and intent.from_path is not None
        old_disk = observe(filesystem, intent.from_path)
        new_disk = observe(filesystem, intent.path)
        if (
            binary is not None
            and old_disk is not None
            and old_disk.sha256 == binary.sha256
            and new_disk is None
        ):
            try:
                _rename(filesystem, intent.from_path, intent.path, old_disk)
            except (Deferred, held_fs.HeldFsError, OSError):
                # The writer renames the same file right after its commit and may have won.
                old_disk = observe(filesystem, intent.from_path)
                new_disk = observe(filesystem, intent.path)
                if old_disk is not None:
                    raise
            else:
                old_disk, new_disk = None, observe(filesystem, intent.path)
        moved = (
            binary is not None
            and new_disk is not None
            and new_disk.sha256 == binary.sha256
            and old_disk is None
        )
        self._run(lambda writer: _settle_binary_move(writer, intent))
        if moved:
            report.projected += 1
            prune_emptied(filesystem, _split(intent.from_path)[0])
        else:
            report.dropped += 1

    # -- holds ------------------------------------------------------------------------------------

    def _hold_edit(
        self,
        filesystem: held_fs.HeldFilesystem,
        report: PassReport,
        facts: Facts,
        *,
        store_revision: int | None,
    ) -> None:
        """Keep a person's file and its bytes: it is neither the projection nor Exomem's own.

        A file above ``HELD_BODY_LIMIT`` is held by its hash alone; its bytes stay in the file.
        """
        kept = observe(filesystem, facts.path, keep_body=True)
        if kept is None:
            raise Deferred("the file vanished while it was being held")
        self._hold(
            report,
            facts.path,
            HELD_EDIT,
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
        def work(writer: CollectionWriter) -> None:
            with writer.handle.transaction() as tx:
                record_held(tx, rel, kind, base_revision, store_revision, sha256, body)

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
                record_file(tx, page.path, page.sha256, size, mtime_ns, page.revision)
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
                record_file(
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
        acquired = held_fs.acquire(anchor(self._root))
        if not acquired.ok:
            raise Deferred(f"the held filesystem route is unavailable ({acquired.error.code})")
        with acquired.require() as filesystem:
            for folder in sorted(folders):
                removed += self._sweep_folder(filesystem, folder)
        return removed

    def _sweep_folder(self, filesystem: held_fs.HeldFilesystem, folder: str) -> int:
        opened = _parent(filesystem, folder, access="mutate")
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


class Judgement(enum.Enum):
    """What a write intent does with the file at its path: the closed set it acts on."""

    IN_PLACE = enum.auto()
    PUBLISH = enum.auto()
    HOLD_EDIT = enum.auto()
    HOLD_DELETE = enum.auto()


def _judge_write(facts: Facts, disk: OnDisk | None) -> Judgement:
    """Whether the file at a path may be left, replaced or written, or is a person's, held."""
    page, manifest = facts.page, facts.manifest
    assert page is not None
    if disk is None:
        # A projected file that is gone is an outside delete, unless Exomem removed it and a kill
        # came before the manifest heard of it. A person's delete while a store delete or move-away
        # is queued, followed by a store put, is therefore not held; no bytes are lost, because the
        # store holds the page and the removal was already queued.
        if manifest is not None and not facts.removed:
            return Judgement.HOLD_DELETE
        return Judgement.PUBLISH
    if disk.sha256 == page.sha256:
        return Judgement.IN_PLACE
    if disk.sha256 not in facts.own_hashes:
        return Judgement.HOLD_EDIT
    return Judgement.PUBLISH


# -- store reads ----------------------------------------------------------------------------------


def read_facts(vault_root: Path, rel: str) -> Facts:
    """What the store records about ``rel``, read in one snapshot; the importer reads it too."""
    with read.snapshot(vault_root) as view:
        database = view.connection
        queued = database.execute(
            "SELECT path, revision, operation, from_path, queued_at "
            "FROM store_projection_queue WHERE path = ?",
            (rel,),
        ).fetchone()
        recorded = database.execute(
            "SELECT sha256, size, mtime_ns, revision FROM store_manifest WHERE path = ?", (rel,)
        ).fetchone()
        held = database.execute(
            "SELECT kind, detected_at, sha256 FROM store_held WHERE path = ?", (rel,)
        ).fetchone()
        manifest_revision = None if recorded is None else recorded[3]
        moving = database.execute(
            "SELECT 1 FROM store_projection_queue WHERE operation = 'move' AND from_path = ? "
            "LIMIT 1",
            (rel,),
        ).fetchone()
        return Facts(
            path=rel,
            intent=None if queued is None else Intent(*queued),
            page=view.page_meta(rel),
            manifest=None if recorded is None else Manifest(*recorded),
            held_row=None if held is None else write.HeldRow(rel, *held),
            audited=_audited(database, rel, manifest_revision),
            removed=_removed_since(database, rel, manifest_revision),
            binary=view.binary(rel),
            moves_away=moving is not None,
        )


def record_held(
    tx: Connection,
    rel: str,
    kind: str,
    base_revision: int | None,
    store_revision: int | None,
    sha256: str,
    body: bytes | None,
) -> None:
    """Hold ``rel`` for a person to resolve, inside the caller's transaction.

    One row for each path: a second outside change replaces the bytes and keeps the first
    detection time, so a person's repeated saves are one held item.
    """
    tx.execute(
        "INSERT INTO store_held(path, kind, base_revision, store_revision, sha256, "
        "body, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET "
        "kind = excluded.kind, base_revision = excluded.base_revision, "
        "store_revision = excluded.store_revision, sha256 = excluded.sha256, "
        "body = excluded.body",
        (rel, kind, base_revision, store_revision, sha256, body, write.timestamp()),
    )


def _audited(database: Connection, rel: str, manifest_revision: int | None) -> frozenset[str]:
    """The hashes that ``Facts.audited`` names.

    A page with a manifest row takes the rows after the manifest's revision. A page with none has no
    file that the projector recorded, so only its latest life counts: the writes after the last
    removal that came before the latest write, and after the latest migration or rollback of the
    vault. A removal after that write may still be queued, and the file that it removes is this
    life's. The bytes of an earlier life never count, because a person may have restored them. A
    binary takes its last row.
    """
    if ownership.classify(rel) is not ownership.Ownership.PAGE:
        rows = database.execute(
            "SELECT sha256 FROM store_audit WHERE path = ? AND sha256 IS NOT NULL "
            "ORDER BY seq DESC LIMIT 1",
            (rel,),
        )
    elif manifest_revision is not None:
        rows = database.execute(
            "SELECT DISTINCT sha256 FROM store_audit "
            "WHERE path = ? AND revision > ? AND sha256 IS NOT NULL",
            (rel, manifest_revision),
        )
    else:
        latest_write = _last_audit(database, f"path = ? AND {write.BEGINS_A_LIFE}", rel)
        last_end = _last_audit(
            database, f"path = ? AND {write.ENDS_A_LIFE} AND seq < ?", rel, latest_write
        )
        life_starts_after = max(last_end, _last_audit(database, write.RESETS_EVERY_LIFE))
        rows = database.execute(
            "SELECT DISTINCT sha256 FROM store_audit "
            f"WHERE path = ? AND {write.BEGINS_A_LIFE} AND sha256 IS NOT NULL AND seq > ?",
            (rel, life_starts_after),
        )
    return frozenset(row[0] for row in rows)


def _last_audit(database: Connection, condition: str, *arguments: object) -> int:
    """The sequence of the newest audit row that ``condition`` selects, or 0 when none."""
    # ``condition`` is built from the closed audit rules in ``write``, never from a path.
    (seq,) = database.execute(
        f"SELECT COALESCE(MAX(seq), 0) FROM store_audit WHERE {condition}", arguments
    ).fetchone()
    return seq


def _removed_since(database: Connection, rel: str, manifest_revision: int | None) -> bool:
    """Whether Exomem removed or moved away ``rel`` after the manifest's revision."""
    if manifest_revision is None:
        return False
    row = database.execute(
        "SELECT 1 FROM store_audit WHERE path = ? AND revision > ? "
        f"AND {write.ENDS_A_LIFE} LIMIT 1",
        (rel, manifest_revision),
    ).fetchone()
    return row is not None


def record_file(
    tx: Connection, rel: str, sha256: str, size: int, mtime_ns: int, revision: int
) -> None:
    """Record what the file at ``rel`` holds; the page's mtime then says what a stat does."""
    write.record_manifest(tx, rel, sha256, size, mtime_ns, revision)
    tx.execute(
        "UPDATE store_pages SET mtime_ns = ? WHERE path = ? AND revision = ? AND sha256 = ?",
        (mtime_ns, rel, revision, sha256),
    )


def drop_binary_move(tx: Connection, dst: str, src: str) -> None:
    """The file of a binary's move is at ``dst``, or the move was given up: drop its intent.

    The writer calls it after its own rename, and the projector after it finishes a move. An intent
    that a later commit replaced names another source, or another operation, and stays.
    """
    tx.execute(
        "DELETE FROM store_projection_queue WHERE path = ? AND operation = 'move' "
        "AND from_path = ?",
        (dst, src),
    )


def _settle_binary_move(writer: CollectionWriter, intent: Intent) -> None:
    assert intent.from_path is not None
    with writer.handle.transaction() as tx:
        drop_binary_move(tx, intent.path, intent.from_path)


def _turn_into_write(tx: Connection, intent: Intent) -> None:
    """A move whose bytes need a rewrite is a write of its new path, unless a commit changed it."""
    tx.execute(
        "UPDATE store_projection_queue SET operation = 'write', from_path = NULL "
        "WHERE path = ? AND revision = ? AND operation = 'move'",
        (intent.path, intent.revision),
    )


# -- disk, through the held filesystem ------------------------------------------------------------


def anchor(root: Path) -> Path:
    """The vault root as the held filesystem anchors it: a symlinked root, and on Windows a root
    junction, resolves first, as the store's own routes do."""
    return root.resolve() if os.name == "nt" or root.is_symlink() else root


def _missing(result: held_fs.HeldResult) -> bool:
    return not result.ok and result.error is not None and result.error.code == "MISSING"


def _split(rel: str) -> tuple[str, str]:
    folder, _, leaf = rel.rpartition("/")
    return folder, leaf


def _parent(
    filesystem: held_fs.HeldFilesystem, folder: str, *, create: bool = False, access: str = "read"
) -> held_fs.HeldResult[held_fs.HeldDirectory]:
    """Hold ``folder``, where "" is the vault root: the held filesystem names it "." and refuses "".

    ``_split`` gives "" for a file at the vault root, so every hold of a folder goes through here.
    """
    return filesystem.parent(folder or ".", create=create, access=access)


def observe(
    filesystem: held_fs.HeldFilesystem,
    rel: str,
    *,
    keep_body: bool = False,
    trusted: tuple[held_fs.FileVersion, str] | None = None,
) -> OnDisk | None:
    """The file at ``rel`` with its hash, or None when no file is there.

    The hash is streamed, so a file of any size costs no memory. With ``keep_body`` the bytes come
    too, unless the file is above ``HELD_BODY_LIMIT``. A caller that hashed a version before passes
    it as ``trusted`` with that hash, and a file that still shows that exact version is not read
    again. A refusal other than absence raises ``HeldFsError``: a symlink, an unreadable folder or
    a name that differs only by case is not something to judge, and the intent stays queued.
    """

    def read_file(file: held_fs.HeldFile) -> OnDisk:
        version = file.version()
        body = None
        if trusted is not None and trusted[0] == version and not keep_body:
            digest = trusted[1]
        elif keep_body and version.size <= HELD_BODY_LIMIT:
            body = filesystem.read(file, max_bytes=HELD_BODY_LIMIT).require()
            digest = hashlib.sha256(body).hexdigest()
        else:
            digest = _stream_sha256(file)
        return OnDisk(
            sha256=digest,
            size=version.size,
            mtime_ns=version.mtime_ns,
            identity=file.identity,
            permissions=_permissions(file),
            body=body,
            version=version,
        )

    return _with_file(filesystem, rel, read_file)


def witness(filesystem: held_fs.HeldFilesystem, rel: str) -> held_fs.FileVersion | None:
    """The file's held version without reading its bytes, or None when no file is there."""
    return _with_file(filesystem, rel, lambda file: file.version())


def _with_file(
    filesystem: held_fs.HeldFilesystem, rel: str, use: Callable[[held_fs.HeldFile], _Found]
) -> _Found | None:
    """``use`` of the held file at ``rel``, or None when it is missing; other refusals raise."""
    folder, leaf = _split(rel)
    # A look changes nothing, so it holds the folder for reading: "read" is the only access that the
    # Windows backend grants on the vault root, where a root-level Markdown file lives.
    opened = _parent(filesystem, folder)
    if _missing(opened):
        return None
    with opened.require() as parent:
        found = filesystem.file(parent, leaf)
        if _missing(found):
            return None
        with found.require() as file:
            return use(file)


def _stream_sha256(file: held_fs.HeldFile) -> str:
    """The file's hash, read in chunks from a duplicate of the held descriptor."""
    with os.fdopen(os.dup(file.descriptor), "rb") as stream:
        stream.seek(0)
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
    rename and makes the temporary durable before it. A create-only install is a hard link, and a
    new folder is a bare mkdir, and neither flushes a folder: so this flushes the file's folder
    after the install, and the folder above each folder that it made, before it returns.
    """
    folder, leaf = _split(rel)
    opened, created = _open_creating(filesystem, folder)
    with opened.require() as parent:
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
        _flush(filesystem, parent)
        _flush_above(filesystem, created)
        return _installed(filesystem, parent, leaf, published[-1], hashlib.sha256(body).hexdigest())


def _open_creating(
    filesystem: held_fs.HeldFilesystem, folder: str
) -> tuple[held_fs.HeldResult[held_fs.HeldDirectory], list[str]]:
    """Open ``folder`` for a change, making it if need be, with the folders that did not exist."""
    opened = _parent(filesystem, folder, access="mutate")
    if not _missing(opened):
        return opened, []
    absent = [folder]
    above = _split(folder)[0]
    while above:
        probe = _parent(filesystem, above)
        if probe.ok:
            probe.require().close()
            break
        if not _missing(probe):
            probe.require()
        absent.append(above)
        above = _split(above)[0]
    return _parent(filesystem, folder, create=True, access="mutate"), absent


def _flush(filesystem: held_fs.HeldFilesystem, directory: held_fs.HeldDirectory) -> None:
    """Make the name changes in ``directory`` durable."""
    # Windows cannot open a directory for a flush: a rename is durable once the file's flush
    # returned.
    if os.name != "nt":
        filesystem.flush_directory(directory).require()


def _flush_above(filesystem: held_fs.HeldFilesystem, created: list[str]) -> None:
    """Make durable the entries that a ``create`` made: each new folder sits in a folder above."""
    for folder in created:
        _flush_folder(filesystem, _split(folder)[0])


def _flush_through_root(filesystem: held_fs.HeldFilesystem, folder: str) -> None:
    """Make the name changes in ``folder`` and in every folder above it, up to the root, durable."""
    while True:
        _flush_folder(filesystem, folder)
        if not folder:
            return
        folder = _split(folder)[0]


def _flush_folder(filesystem: held_fs.HeldFilesystem, folder: str) -> None:
    """Make the name changes in ``folder`` durable; a folder that does not exist has none."""
    if os.name == "nt":
        return
    opened = _parent(filesystem, folder, access="flush")
    if _missing(opened):
        return
    with opened.require() as directory:
        filesystem.flush_directory(directory).require()


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
    with _parent(filesystem, folder, access="mutate").require() as parent:
        with filesystem.file(parent, leaf, access="mutate").require() as file:
            if file.identity != expected.identity or _stream_sha256(file) != expected.sha256:
                raise Deferred("the file changed before it was removed")
            _retrying(lambda: filesystem.unlink(file))
        _flush(filesystem, parent)


def prune_emptied(filesystem: held_fs.HeldFilesystem, folder: str) -> None:
    """Remove ``folder`` and each folder above it that is left empty, the deepest first.

    The projector calls it on the folder that its own move or removal just took a file or folder
    from. Removing a folder refuses while it holds anything, a person's file included, so the walk
    stops at the first folder that is not empty, and it never reaches the Knowledge Base folder.
    """
    from ..kbdir import kb_dirname

    kb = kb_dirname()
    while folder.startswith(f"{kb}/"):
        opened = _parent(filesystem, folder, access="mutate")
        if not opened.ok:
            return
        with opened.require() as directory:
            if not filesystem.unlink_directory(directory).ok:
                return
        folder = _split(folder)[0]


def _move_parts(filesystem: held_fs.HeldFilesystem, old_page: str, new_page: str) -> None:
    """Move a page's dot folder of parts beside the page's new path, as the page's move left it.

    The writer renames the folder right after its commit; a kill before that leaves it here. The
    store owns nothing in a dot folder, so the folder moves only to a free name, and a folder that
    a person put there stays. A raw-marked page's parts are pages and move by their own intents.
    """
    from ..extraction_parts import folder_for

    old_parts = folder_for(Path(old_page)).as_posix()
    new_parts = folder_for(Path(new_page)).as_posix()
    if ownership.owns_folder(old_parts):
        return
    new_folder, new_leaf = _split(new_parts)
    source = _parent(filesystem, old_parts, access="mutate")
    if _missing(source):
        return
    with source.require() as held:
        opened, created = _open_creating(filesystem, new_folder)
        with opened.require() as new_parent:
            # A name that is taken refuses the rename: a person's folder there stays, and so does
            # this one.
            if not filesystem.rename_directory(held, new_parent, new_leaf).ok:
                return
            _flush_above(filesystem, created)
    _flush_folder(filesystem, _split(old_parts)[0])


def _rename(filesystem: held_fs.HeldFilesystem, old: str, new: str, expected: OnDisk) -> OnDisk:
    """Rename the old file to a free path, only while it is still the exact file that was judged.

    The source is checked before the destination folder is made, so a refused rename leaves no new
    folder. On POSIX the rename flushes both folders; this flushes the folders above any new one.
    """
    old_folder, old_leaf = _split(old)
    new_folder, new_leaf = _split(new)
    with _parent(filesystem, old_folder, access="mutate").require() as old_parent:
        with filesystem.file(old_parent, old_leaf, access="mutate").require() as file:
            if file.identity != expected.identity or _stream_sha256(file) != expected.sha256:
                raise Deferred("the file changed before it was renamed")
            opened, created = _open_creating(filesystem, new_folder)
            with opened.require() as new_parent:
                _retrying(lambda: filesystem.rename(file, new_parent, new_leaf, replace=False))
                _flush_above(filesystem, created)
                return _installed(
                    filesystem, new_parent, new_leaf, expected.identity, expected.sha256
                )
