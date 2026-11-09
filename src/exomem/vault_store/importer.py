"""The importer of a store-mode vault (OpenSpec make-vault-store-canonical D5, tasks 4.2 and 4.3).

The store holds each page's exact bytes and the Markdown files are its projection (``projector``).
A person can still change a file outside Exomem, in an editor, with git or by restoring a backup.
The importer reads those changes from the disk and settles each one for its path:

* A file that holds bytes Exomem put there is not a change. Whose bytes a file holds is judged by
  hash alone: the projection manifest, the stored page, and the bytes that the audit trail shows
  Exomem wrote after the manifest. This is the projector's own rule (``Facts.own_hashes``), so
  Exomem's projections cause no import and no hold.
* A change that the store has not moved past is an audited import. "Not moved past" is one test,
  the ``Expect(revision, projected=True)`` of the commit that adopts the bytes: the manifest
  names the stored revision and bytes, no projection is queued, and nothing is held.
* A change that races an Exomem change, or lands on a held path, is held with the person's latest
  bytes. One row stands for each path and keeps its first detection time. A person's delete of a
  page is held too, and the hold clears when the file returns with Exomem's own bytes.
* A binary is disk-canonical: the store records its path, hash and size and holds no bytes, so a
  hold would keep nothing. An outside change, delete or rename of a binary imports as an audited
  record change. A binary is held only when an Exomem delete of it is queued and the file differs.

The importer never writes, replaces or removes a file. A path that it cannot judge stays as it is.
It reads the disk on the caller's thread and runs each decision on the store thread, one commit
for each path, so a kill between two paths leaves the earlier ones settled and the later ones to
the next sweep, which repeats nothing: every decision is rederived from the hashes. One lock lets
one ``import_paths`` or ``sweep`` run at a time, and the store thread never takes it. The store
thread also looks at each file again just before it commits what an earlier read saw, and leaves
a path whose file changed in between to the next event or sweep.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .. import held_fs, lexstore, media_processing, vault
from ..cli_ops import OpError
from . import ownership, projector, read, write
from .projector import HELD_DELETE, HELD_EDIT, Facts, OnDisk, Runner

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

_Item = TypeVar("_Item")

#: What ``ImportReport.unjudged`` says of a file that changed between a read and its commit.
CHANGED_WHILE_READ = "the file changed after it was read"

#: The state of an imported media binary: store mode derives no preview or transcript for it yet.
MEDIA_WAITS = "media waits for the store-mode media writer"

#: Paths settled by one store-thread job. A job holds the store thread, so a large change (a git
#: pull) is cut into jobs that let a governed write run between them; each path is its own commit.
APPLY_BATCH = 32


class Verdict(enum.Enum):
    """What one path's disk state means for the store: the closed set that ``judge`` returns."""

    #: Nothing to settle: the file holds bytes of Exomem's, or the store expects it missing.
    OWN = enum.auto()
    #: A person's bytes are at the path: import them, or hold them.
    ADOPT = enum.auto()
    #: A projected file is missing and Exomem did not remove it: a person deleted it.
    GONE = enum.auto()
    #: A held delete whose file is back with Exomem's own bytes: nothing is left to review.
    CLEAR = enum.auto()


def judge(facts: Facts, disk: OnDisk | None) -> Verdict:
    """Whether the file at a path is a person's change, from the store's facts and one disk read.

    A held page is already settled for a person: bytes that the hold already keeps are nothing new,
    a later change replaces the held bytes, and a later delete leaves the held row as it is. A hold
    of a delete is the exception that clears, because the file is back and nothing was lost. A
    binary's hold keeps no bytes, so it settles nothing.
    """
    page = ownership.classify(facts.path) is ownership.Ownership.PAGE
    if disk is None:
        # A removal that Exomem queued or made, whose manifest write a kill lost, is not an outside
        # delete: ``removed`` is the audit trail's own word for it. A page that the store dropped
        # is the same, and a path that was never projected has no file to lose.
        if (facts.held and page) or facts.removed or facts.stored_sha256 is None:
            return Verdict.OWN
        return Verdict.GONE if _was_projected(facts) else Verdict.OWN
    if facts.held_kind == HELD_DELETE and disk.sha256 in facts.own_hashes:
        return Verdict.CLEAR
    if disk.sha256 in facts.own_hashes or disk.sha256 in (facts.stored_sha256, facts.held_sha256):
        return Verdict.OWN
    return Verdict.ADOPT


def _was_projected(facts: Facts) -> bool:
    # A binary's file is never written by the store, so a recorded binary is on disk by definition.
    return facts.manifest is not None or (facts.binary is not None and facts.intent is None)


@dataclass(slots=True)
class ImportReport:
    """What one import or sweep settled, by path; ``unjudged`` names paths it left alone."""

    #: Paths whose bytes or record the store now holds from the disk, a rename's new path included.
    imported: list[str] = field(default_factory=list)
    #: Each renamed path as ``new path -> old path``.
    moved: dict[str, str] = field(default_factory=dict)
    #: Paths held for a person to resolve: a racing edit, a delete, or a file too large to copy.
    held: list[str] = field(default_factory=list)
    #: Binaries whose outside delete the store now records.
    removed: list[str] = field(default_factory=list)
    #: Held deletes that cleared because the file came back with Exomem's own bytes.
    cleared: list[str] = field(default_factory=list)
    #: Markdown files that the store does not own, whose derived rows were rederived from the file.
    refreshed: list[str] = field(default_factory=list)
    #: Imported or renamed media binaries, each in the state ``MEDIA_WAITS`` names: the store-mode
    #: media writer does not exist yet, so no derived media row follows the change.
    media_waiting: list[str] = field(default_factory=list)
    #: Every path that was examined and carried no change, whatever its kind.
    unchanged: int = 0
    #: What a sweep's walk of the lexical corpus cost: the Markdown files it reached, and the
    #: seconds it took, which include the walk itself. Zero for an import, which walks nothing.
    corpus_files: int = 0
    corpus_seconds: float = 0.0
    #: A path the importer left alone, with the reason: a refusal of the file system or the store,
    #: or a file that changed after it was read.
    unjudged: dict[str, str] = field(default_factory=dict)

    @property
    def settled(self) -> int:
        return (
            len(self.imported)
            + len(self.moved)
            + len(self.held)
            + len(self.removed)
            + len(self.cleared)
            + len(self.refreshed)
        )


@dataclass(frozen=True, slots=True)
class _Look:
    """One path as the importer judged it: the facts, the disk read, and the verdict."""

    rel: str
    facts: Facts
    disk: OnDisk | None
    verdict: Verdict


@dataclass(frozen=True, slots=True)
class _Rename:
    src: str
    dst: str
    disk: OnDisk
    page: bool


class Importer:
    """Settles a store-mode vault's outside changes: ``import_paths`` and ``sweep`` are the job."""

    def __init__(self, vault_root: Path, *, run: Runner | None = None) -> None:
        self._root = Path(vault_root)
        self._run = run or projector.store_thread_runner(self._root)
        # One settle at a time: two that overlap could each commit bytes that the other has since
        # replaced. The store thread never takes this lock, so a settle that waits on it cannot
        # deadlock with the job that it waits for.
        self._exclusive = threading.Lock()
        self._refresh_lock = threading.Lock()
        #: Unowned Markdown paths to refresh, each with the hash of its bytes when it had no row to
        #: compare them with (None otherwise).
        self._refresh: dict[str, str | None] = {}
        #: The hash of each unowned Markdown file whose refresh left it with no row, because recall
        #: policy withholds it. Set only after that refresh committed.
        self._rowless: dict[str, str] = {}
        #: The version and hash of each binary that the last sweep found equal to its record.
        self._clean: dict[str, tuple[held_fs.FileVersion, str]] = {}

    @staticmethod
    def takes(rel: str) -> bool:
        """Whether the store owns this path: the importer settles it, not the file-mode watcher."""
        return ownership.classify(rel) is not ownership.Ownership.NONE

    # -- the two entry points ---------------------------------------------------------------------

    def import_paths(
        self, rels: Iterable[str], moves: Mapping[str, str] | None = None
    ) -> ImportReport:
        """Settle each path that an event named; ``moves`` maps a new path to its old path.

        A move event is a hint that pairs two paths; it never decides alone. A rename is imported as
        a rename only when the old file is gone and the new file holds the old file's exact bytes.
        An event always hashes the file: only a sweep trusts a binary's unchanged version.
        """
        report = ImportReport()
        with self._exclusive, self._filesystem() as filesystem:
            self._settle(filesystem, sorted(set(rels)), dict(moves or {}), report)
            self._refresh_noted(report)
            self._note_media(report)
        return report

    def sweep(self) -> ImportReport:
        """Hash every file that the store owns and settle what differs from the store's records.

        The hash is the only test for a page, so an edit that keeps a file's size and modification
        time is caught as any other. A binary that still shows the version that the last sweep
        hashed (identity, size, modification and change time) is not read again: a binary can be
        large, and its change time moves with every write. A stored path with no file is judged
        missing only when the walk that looked for it was whole: a folder or file that the walk
        could not read leaves its paths as they are.
        """
        report = ImportReport()
        skipped: list[ownership.Skipped] = []
        with self._exclusive, self._filesystem() as filesystem:
            known = self._known_hashes()
            held_deletes = self._held_deletes()
            seen: set[str] = set()
            clean: dict[str, tuple[held_fs.FileVersion, str]] = {}
            differing: list[str] = []
            for owned in ownership.iter_owned(self._root, on_skip=skipped.append):
                rel = owned.rel
                seen.add(rel)
                binary = ownership.classify(rel) is ownership.Ownership.BINARY
                disk = self._observe(
                    filesystem, rel, report, trusted=self._clean.get(rel) if binary else None
                )
                if disk is None:
                    continue
                # A held delete is settled even when the file is back with the recorded bytes: only
                # the settle can tell a person's restore from a file that was never missing.
                if disk.sha256 in known.get(rel, ()) and rel not in held_deletes:
                    report.unchanged += 1
                    if binary and disk.version is not None:
                        clean[rel] = (disk.version, disk.sha256)
                else:
                    differing.append(rel)
            self._clean = clean
            missing = [rel for rel in known if rel not in seen and not _below_any(rel, skipped)]
            self._settle(filesystem, sorted({*differing, *missing}), {}, report)
            self._sweep_unowned(filesystem, report)
            self._refresh_noted(report)
            self._note_media(report)
        return report

    def _take_refresh(self) -> dict[str, str | None]:
        """The unowned Markdown paths whose derived rows are stale, in order; clears the list."""
        with self._refresh_lock:
            taken, self._refresh = dict(sorted(self._refresh.items())), {}
        return taken

    @staticmethod
    def _note_media(report: ImportReport) -> None:
        """Name the state of each imported media binary: its media rows wait for the media writer.

        ``media_processing.reconcile_media`` writes the media sidecar from the watcher thread, and
        store mode refuses that write off the store thread, so nothing derives these rows yet.
        """
        for rel in (*report.imported, *report.moved):
            if (
                ownership.classify(rel) is ownership.Ownership.BINARY
                and media_processing.classify_media(rel) is not None
            ):
                report.media_waiting.append(rel)

    # -- reading the disk -------------------------------------------------------------------------

    @contextmanager
    def _filesystem(self) -> Iterator[held_fs.HeldFilesystem]:
        with held_fs.acquire(projector.anchor(self._root)).require() as filesystem:
            yield filesystem

    @staticmethod
    def _observe(
        filesystem: held_fs.HeldFilesystem,
        rel: str,
        report: ImportReport,
        *,
        keep_body: bool = False,
        trusted: tuple[held_fs.FileVersion, str] | None = None,
    ) -> OnDisk | None:
        """The file at ``rel``, or None when it is missing or the file system will not show it.

        A refusal (a symlink, an unreadable folder) is recorded and reads as no change, never as a
        deleted file: ``unjudged`` keeps it visible, and the path is looked at again next time.
        """
        try:
            return projector.observe(filesystem, rel, keep_body=keep_body, trusted=trusted)
        except (held_fs.HeldFsError, OSError) as refusal:
            report.unjudged[rel] = getattr(refusal, "code", type(refusal).__name__)
            return None

    def _known_hashes(self) -> dict[str, frozenset[str]]:
        """The hashes that the store expects a file to hold at each path: manifest, page, binary."""
        known: dict[str, set[str]] = defaultdict(set)
        with read.snapshot(self._root) as view:
            for meta in view.pages():
                known[meta.path].add(meta.sha256)
            for table in ("store_manifest", "store_binaries"):
                for path, sha256 in view.connection.execute(f"SELECT path, sha256 FROM {table}"):
                    known[path].add(sha256)
        return {path: frozenset(hashes) for path, hashes in known.items()}

    def _held_deletes(self) -> frozenset[str]:
        """The paths with a held delete, which a sweep settles whatever bytes the file holds."""
        with read.snapshot(self._root) as view:
            rows = view.connection.execute(
                "SELECT path FROM store_held WHERE kind = ?", (HELD_DELETE,)
            )
            return frozenset(path for (path,) in rows)

    # -- judging and settling ---------------------------------------------------------------------

    def _settle(
        self,
        filesystem: held_fs.HeldFilesystem,
        rels: list[str],
        moves: dict[str, str],
        report: ImportReport,
    ) -> None:
        looks: dict[str, _Look] = {}
        for rel in rels:
            if ownership.classify(rel) is ownership.Ownership.NONE:
                self._judge_unowned(filesystem, rel, report)
                continue
            disk = self._observe(filesystem, rel, report)
            if rel in report.unjudged:
                continue
            facts = projector.read_facts(self._root, rel)
            verdict = judge(facts, disk)
            if verdict is Verdict.OWN:
                report.unchanged += 1
            else:
                looks[rel] = _Look(rel, facts, disk, verdict)
        renames = _pair_renames(looks, moves)
        renamed = {path for rename in renames for path in (rename.src, rename.dst)}
        singles = [rel for rel in looks if rel not in renamed]
        # A rename that the store refused is two changes, each held to its own path's rule.
        for rename in self._apply_renames(filesystem, renames, report):
            singles.extend((rename.src, rename.dst))
        for chunk in _chunks(sorted(singles), APPLY_BATCH):
            units: list[tuple[str, OnDisk | None]] = []
            for rel in chunk:
                disk = self._bytes_to_keep(filesystem, looks[rel], report)
                if rel not in report.unjudged:
                    units.append((rel, disk))
            self._apply(filesystem, units, report)

    def _bytes_to_keep(
        self, filesystem: held_fs.HeldFilesystem, look: _Look, report: ImportReport
    ) -> OnDisk | None:
        """The disk read that a decision commits: a page's changed bytes are read for the commit.

        A binary is recorded by hash and size, and a delete or a clearing needs no bytes, so those
        keep the read that judged them. The bytes of a page come from one read with their hash.
        """
        page = ownership.classify(look.rel) is ownership.Ownership.PAGE
        if look.verdict is not Verdict.ADOPT or not page:
            return look.disk
        return self._observe(filesystem, look.rel, report, keep_body=True)

    def _apply_renames(
        self, filesystem: held_fs.HeldFilesystem, renames: list[_Rename], report: ImportReport
    ) -> list[_Rename]:
        """Commit each rename; return the ones that the store refused."""
        refused: list[_Rename] = []
        for chunk in _chunks(renames, APPLY_BATCH):

            def work(writer: CollectionWriter, chunk: list[_Rename] = chunk) -> None:
                for rename in chunk:
                    if self._commit_rename(writer, filesystem, rename):
                        report.moved[rename.dst] = rename.src
                    else:
                        refused.append(rename)

            self._run(work)
        return refused

    def _commit_rename(
        self, writer: CollectionWriter, filesystem: held_fs.HeldFilesystem, rename: _Rename
    ) -> bool:
        source = projector.read_facts(self._root, rename.src)
        stored = source.manifest if rename.page else source.binary
        if stored is None or not self._same_files(filesystem, rename):
            return False
        if rename.page:
            change = write.StoreChange(
                write.Origin.OUTSIDE_EDITOR,
                "an outside rename",
                moves=(write.PageMove(rename.src, rename.dst, on_disk=True),),
                expect={
                    rename.src: write.Expect(revision=stored.revision, projected=True),
                    rename.dst: write.Expect(absent=True),
                },
            )
        else:
            # The store has no binary move: the new record and the old one's removal are one commit.
            change = write.StoreChange(
                write.Origin.OUTSIDE_EDITOR,
                "an outside rename",
                binaries=(write.BinaryRecord(rename.dst, rename.disk.sha256, rename.disk.size),),
                binary_deletes=(write.BinaryDelete(rename.src, on_disk=True),),
                expect={
                    rename.src: write.Expect(revision=stored.revision, sha256=source.stored_sha256),
                    rename.dst: write.Expect(absent=True),
                },
            )
        try:
            write.commit(writer, change)
        except write.StoreConflict:
            return False
        if not rename.page:
            self._clear_binary_holds(writer, rename.src, rename.dst)
        return True

    @staticmethod
    def _same_files(filesystem: held_fs.HeldFilesystem, rename: _Rename) -> bool:
        """Whether the old file is still gone and the new file still the one that was read."""
        try:
            return (
                projector.witness(filesystem, rename.src) is None
                and projector.witness(filesystem, rename.dst) == rename.disk.version
            )
        except (held_fs.HeldFsError, OSError):
            return False

    def _apply(
        self,
        filesystem: held_fs.HeldFilesystem,
        units: list[tuple[str, OnDisk | None]],
        report: ImportReport,
    ) -> None:
        def work(writer: CollectionWriter) -> None:
            for rel, disk in units:
                self._settle_one(writer, filesystem, rel, disk, report)

        if units:
            self._run(work)

    def _settle_one(
        self,
        writer: CollectionWriter,
        filesystem: held_fs.HeldFilesystem,
        rel: str,
        disk: OnDisk | None,
        report: ImportReport,
    ) -> None:
        """Decide one path inside the store thread's job, from facts read now: commit or hold."""
        facts = projector.read_facts(self._root, rel)
        verdict = judge(facts, disk)
        if verdict is Verdict.OWN:
            report.unchanged += 1
            return
        if not self._unchanged_since_read(filesystem, rel, disk, report):
            return
        page = ownership.classify(rel) is ownership.Ownership.PAGE
        if verdict is Verdict.CLEAR:
            self._clear_held_delete(writer, rel)
            report.cleared.append(rel)
        elif verdict is Verdict.GONE:
            if page:
                recorded = facts.manifest.sha256 if facts.manifest else facts.stored_sha256
                assert recorded is not None
                self._hold(writer, rel, HELD_DELETE, facts, recorded, None, report)
            else:
                self._remove_binary(writer, rel, facts, report)
        else:
            assert disk is not None
            if page:
                self._import_page(writer, rel, facts, disk, report)
            else:
                self._import_binary(writer, rel, facts, disk, report)

    @staticmethod
    def _unchanged_since_read(
        filesystem: held_fs.HeldFilesystem, rel: str, disk: OnDisk | None, report: ImportReport
    ) -> bool:
        """Whether the file is still the one that the earlier read saw: the last look before commit.

        A commit of bytes read earlier could replace a newer change, so a file whose identity, size
        or times moved is left for the next event or sweep, which reads it afresh.
        """
        try:
            now = projector.witness(filesystem, rel)
        except (held_fs.HeldFsError, OSError) as refusal:
            report.unjudged[rel] = getattr(refusal, "code", type(refusal).__name__)
            return False
        if now == (None if disk is None else disk.version):
            return True
        report.unjudged[rel] = CHANGED_WHILE_READ
        return False

    def _import_page(
        self,
        writer: CollectionWriter,
        rel: str,
        facts: Facts,
        disk: OnDisk,
        report: ImportReport,
    ) -> None:
        # A held path stays held, and a file too large to copy has no bytes to import.
        if facts.held or disk.body is None:
            self._hold(writer, rel, HELD_EDIT, facts, disk.sha256, disk.body, report)
            return
        base = facts.manifest or facts.page
        # One test says whether the store has moved since the file was projected: the commit's own.
        expect = (
            write.Expect(absent=True)
            if base is None
            else write.Expect(revision=base.revision, projected=True)
        )
        put = write.PagePut(rel, disk.body, on_disk_mtime_ns=disk.mtime_ns)
        change = write.StoreChange(
            write.Origin.OUTSIDE_EDITOR, "an outside change", puts=(put,), expect={rel: expect}
        )
        try:
            write.commit(writer, change)
        except write.StoreConflict:
            self._hold(writer, rel, HELD_EDIT, facts, disk.sha256, disk.body, report)
        except OpError as refusal:
            self._not_owned(rel, refusal, report)
        else:
            report.imported.append(rel)

    def _import_binary(
        self,
        writer: CollectionWriter,
        rel: str,
        facts: Facts,
        disk: OnDisk,
        report: ImportReport,
    ) -> None:
        if facts.intent is not None:
            # An Exomem removal is queued and the file is not the one it names: keep the race.
            self._hold(writer, rel, HELD_EDIT, facts, disk.sha256, None, report)
            return
        expect = (
            write.Expect(absent=True)
            if facts.binary is None
            else write.Expect(revision=facts.binary.revision, sha256=facts.binary.sha256)
        )
        change = write.StoreChange(
            write.Origin.OUTSIDE_EDITOR,
            "an outside change",
            binaries=(write.BinaryRecord(rel, disk.sha256, disk.size),),
            expect={rel: expect},
        )
        try:
            write.commit(writer, change)
        except write.StoreConflict:
            self._hold(writer, rel, HELD_EDIT, facts, disk.sha256, None, report)
        except OpError as refusal:
            self._not_owned(rel, refusal, report)
        else:
            self._clear_binary_holds(writer, rel)
            report.imported.append(rel)

    def _remove_binary(
        self, writer: CollectionWriter, rel: str, facts: Facts, report: ImportReport
    ) -> None:
        binary = facts.binary
        assert binary is not None
        change = write.StoreChange(
            write.Origin.OUTSIDE_EDITOR,
            "an outside delete",
            binary_deletes=(write.BinaryDelete(rel, on_disk=True),),
            expect={rel: write.Expect(revision=binary.revision, sha256=binary.sha256)},
        )
        try:
            write.commit(writer, change)
        except write.StoreConflict:
            self._hold(writer, rel, HELD_DELETE, facts, binary.sha256, None, report)
        except OpError as refusal:
            self._not_owned(rel, refusal, report)
        else:
            self._clear_binary_holds(writer, rel)
            report.removed.append(rel)

    @staticmethod
    def _not_owned(rel: str, refusal: OpError, report: ImportReport) -> None:
        if refusal.code != write.NOT_OWNED:
            raise refusal
        report.unjudged[rel] = refusal.code

    @staticmethod
    def _clear_binary_holds(writer: CollectionWriter, *paths: str) -> None:
        """Drop the holds on binaries that a commit has just settled.

        A hold on a binary keeps no bytes, so an import that settles the path ends it.
        """
        with writer.handle.transaction() as tx:
            tx.executemany("DELETE FROM store_held WHERE path = ?", [(path,) for path in paths])

    @staticmethod
    def _clear_held_delete(writer: CollectionWriter, rel: str) -> None:
        with writer.handle.transaction() as tx:
            tx.execute("DELETE FROM store_held WHERE path = ? AND kind = ?", (rel, HELD_DELETE))

    def _hold(
        self,
        writer: CollectionWriter,
        rel: str,
        kind: str,
        facts: Facts,
        sha256: str,
        body: bytes | None,
        report: ImportReport,
    ) -> None:
        stored = facts.page or facts.binary
        with writer.handle.transaction() as tx:
            projector.record_held(
                tx,
                rel,
                kind,
                None if facts.manifest is None else facts.manifest.revision,
                None if stored is None else stored.revision,
                sha256,
                body,
            )
        report.held.append(rel)
        log.warning(
            "the vault importer held %s: a person's %s met a change in the store", rel, kind
        )

    # -- Markdown that the store does not own ----------------------------------------------------

    def _refresh_noted(self, report: ImportReport) -> None:
        """Rederive, on the store thread, the rows of each unowned Markdown file that was noted.

        The store holds no bytes for such a file, so nothing is imported: ``StoreChange.refresh``
        derives its lexical, graph and identity rows from the file at the commit's revision. A
        failure that is not a refusal puts the paths back for the next settle and is raised.
        """
        noted = self._take_refresh()
        pending = list(noted)
        try:
            for chunk in _chunks(list(noted), APPLY_BATCH):
                self._refresh_rows(chunk, noted, report)
                pending = pending[len(chunk) :]
        except Exception:
            for rel in pending:
                self._note_refresh(rel, noted[rel])
            raise

    def _refresh_rows(
        self, rels: list[str], noted: Mapping[str, str | None], report: ImportReport
    ) -> None:
        change = write.StoreChange(
            write.Origin.OUTSIDE_EDITOR,
            "an outside change to a file that the store does not own",
            refresh=tuple(rels),
        )

        def work(writer: CollectionWriter) -> None:
            write.commit(writer, change)

        try:
            self._run(work)
        except OpError as refusal:
            if len(rels) > 1:
                # A refusal names no path: ask for each, so one bad path holds back no other.
                for rel in rels:
                    self._refresh_rows([rel], noted, report)
                return
            # Nothing is remembered, so the next settle notes the path and asks again.
            report.unjudged[rels[0]] = refusal.code
        else:
            report.refreshed.extend(rels)
            self._remember_rowless(rels, noted)

    def _remember_rowless(self, rels: list[str], noted: Mapping[str, str | None]) -> None:
        """Remember the bytes of each refreshed file that still has no row, and forget the rest.

        A file with no row after its refresh is one that recall policy withholds: its bytes are
        refreshed once, not at each sweep. A file that has rows is current by its hash alone.
        """
        marks = ",".join("?" * len(rels))
        with read.snapshot(self._root) as view:
            rowed = {
                rel
                for (rel,) in view.connection.execute(
                    f"SELECT path FROM pages WHERE content_hash IS NOT NULL AND path IN ({marks})",
                    rels,
                )
            }
        for rel in rels:
            sha256 = noted[rel]
            if rel in rowed or sha256 is None:
                self._rowless.pop(rel, None)
            else:
                self._rowless[rel] = sha256

    def _judge_unowned(
        self, filesystem: held_fs.HeldFilesystem, rel: str, report: ImportReport
    ) -> None:
        """Note a Markdown file outside the store's ownership whose derived rows are stale.

        The store does not hold such a file's bytes, so nothing is imported: ``_refresh_noted``
        rederives the page's rows from the file. A file with no row is noted only when the corpus
        walk reaches it.
        """
        if not ownership.is_markdown(rel):
            return
        disk = self._observe(filesystem, rel, report)
        if rel in report.unjudged:
            return
        with read.snapshot(self._root) as view:
            row = view.connection.execute(
                "SELECT content_hash FROM pages WHERE path = ?", (rel,)
            ).fetchone()
        if row is None and not read.walk_member(rel):
            return
        self._check_unowned(rel, disk, None if row is None else row[0], report)

    def _sweep_unowned(self, filesystem: held_fs.HeldFilesystem, report: ImportReport) -> None:
        """Compare each unowned Markdown file with its rows, and find the files that have none.

        A file with a row is judged by its hash. A file that the lexical corpus holds and no row
        describes is new, or its rows are withheld by recall policy. The corpus walk that file mode
        makes finds it. That walk reads the vault's whole Markdown tree, so the report carries
        what it cost (``corpus_files``, ``corpus_seconds``).
        """
        with read.snapshot(self._root) as view:
            rows = view.connection.execute(
                "SELECT path, content_hash FROM pages WHERE content_hash IS NOT NULL"
            ).fetchall()
        rowed: set[str] = set()
        for rel, content_hash in rows:
            if self._owned_or_not_markdown(rel):
                continue
            rowed.add(rel)
            disk = self._observe(filesystem, rel, report)
            if rel in report.unjudged:
                continue
            self._check_unowned(rel, disk, content_hash, report)
        started = time.monotonic()
        corpus = lexstore.corpus_paths(self._root)
        report.corpus_seconds = time.monotonic() - started
        report.corpus_files = len(corpus)
        for path in corpus:
            rel = vault.relative_posix(path, self._root)
            if rel in rowed or self._owned_or_not_markdown(rel):
                continue
            disk = self._observe(filesystem, rel, report)
            if rel in report.unjudged:
                continue
            self._check_unowned(rel, disk, None, report)

    @staticmethod
    def _owned_or_not_markdown(rel: str) -> bool:
        return ownership.classify(rel) is not ownership.Ownership.NONE or not ownership.is_markdown(
            rel
        )

    def _check_unowned(
        self, rel: str, disk: OnDisk | None, content_hash: str | None, report: ImportReport
    ) -> None:
        """Note ``rel`` for a refresh when its row differs from the file; else it is unchanged."""
        rowless_sha256 = None
        if disk is None:
            stale = content_hash is not None  # the rows of a file that is gone
        elif content_hash is not None:
            stale = disk.sha256 != content_hash
        else:
            # No row for these bytes: a new file, or one whose rows the recall policy withholds.
            # The second kind stays rowless after a refresh: refresh its bytes once, not each sweep.
            rowless_sha256 = disk.sha256
            stale = self._rowless.get(rel) != rowless_sha256
        if stale:
            self._note_refresh(rel, rowless_sha256)
        else:
            report.unchanged += 1

    def _note_refresh(self, rel: str, rowless_sha256: str | None = None) -> None:
        with self._refresh_lock:
            self._refresh[rel] = rowless_sha256


def _below_any(rel: str, skipped: list[ownership.Skipped]) -> bool:
    """Whether a walk left ``rel`` unread: skipped, or under a folder that it could not read."""
    return any(rel == found.rel or rel.startswith(found.rel + "/") for found in skipped)


def _chunks(items: list[_Item], size: int) -> Iterator[list[_Item]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _pair_renames(looks: Mapping[str, _Look], hints: Mapping[str, str]) -> list[_Rename]:
    """The renames among a batch: an old file that is gone and a new file with its exact bytes.

    A move event names its pair. Without one, a gone file and a new file pair only when each is the
    only one in the batch with those bytes, so that two copies of one file cannot swap identities.
    A page pairs with a page and a binary with a binary.
    """
    gone = {
        rel: look
        for rel, look in looks.items()
        if look.verdict is Verdict.GONE and look.facts.stored_sha256 is not None
    }
    created = {
        rel: look
        for rel, look in looks.items()
        if look.verdict is Verdict.ADOPT
        and look.facts.stored_sha256 is None
        and look.facts.manifest is None
        and not look.facts.held
    }

    def same_bytes(src: str, dst: str) -> bool:
        disk = created[dst].disk
        return (
            disk is not None
            and gone[src].facts.stored_sha256 == disk.sha256
            and ownership.classify(src) is ownership.classify(dst)
        )

    pairs: dict[str, str] = {}
    for dst, src in hints.items():
        if dst in created and src in gone and src not in pairs.values() and same_bytes(src, dst):
            pairs[dst] = src
    by_stored: dict[tuple[ownership.Ownership, str], list[str]] = defaultdict(list)
    for rel, look in gone.items():
        if rel not in pairs.values() and look.facts.stored_sha256 is not None:
            by_stored[(ownership.classify(rel), look.facts.stored_sha256)].append(rel)
    by_disk: dict[tuple[ownership.Ownership, str], list[str]] = defaultdict(list)
    for rel, look in created.items():
        if rel not in pairs and look.disk is not None:
            by_disk[(ownership.classify(rel), look.disk.sha256)].append(rel)
    for key, sources in by_stored.items():
        targets = by_disk.get(key, [])
        if len(sources) == 1 and len(targets) == 1:
            pairs[targets[0]] = sources[0]
    return [
        _Rename(src, dst, disk, ownership.classify(dst) is ownership.Ownership.PAGE)
        for dst, src in sorted(pairs.items())
        if (disk := created[dst].disk) is not None
    ]
