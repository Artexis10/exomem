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
  bytes. One row stands for each path and keeps its first detection time.

The importer never writes, replaces or removes a file. A path that it cannot judge stays as it is.
It reads the disk on the caller's thread and runs each decision on the store thread, one commit
for each path, so a kill between two paths leaves the earlier ones settled and the later ones to
the next sweep, which repeats nothing: every decision is made again from the hashes.
"""

from __future__ import annotations

import enum
import logging
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .. import held_fs
from ..cli_ops import OpError
from . import ownership, projector, read, write
from .projector import Facts, OnDisk, Runner

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

_Item = TypeVar("_Item")

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


def judge(facts: Facts, disk: OnDisk | None) -> Verdict:
    """Whether the file at a path is a person's change, from the store's facts and one disk read.

    A held path is already settled for a person: bytes that the hold already keeps are nothing new,
    a later change replaces the held bytes, and a later delete leaves the held row as it is.
    """
    if disk is None:
        # A removal that Exomem queued or made, whose manifest write a kill lost, is not an outside
        # delete: ``removed`` is the audit trail's own word for it. A page that the store dropped
        # is the same, and a path that was never projected has no file to lose.
        if facts.held or facts.removed or facts.stored_sha256 is None:
            return Verdict.OWN
        return Verdict.GONE if _was_projected(facts) else Verdict.OWN
    if disk.sha256 in facts.own_hashes or disk.sha256 in (facts.stored_sha256, facts.held_sha256):
        return Verdict.OWN
    return Verdict.ADOPT


def _was_projected(facts: Facts) -> bool:
    # A binary's file is never written by the store, so a recorded binary is on disk by definition.
    return facts.manifest is not None or (facts.binary is not None and facts.intent is None)


@dataclass(slots=True)
class ImportReport:
    """What one import or sweep settled, by path; ``unjudged`` names paths it could not judge."""

    #: Paths whose bytes or record the store now holds from the disk, a rename's new path included.
    imported: list[str] = field(default_factory=list)
    #: Each renamed page as ``new path -> old path``.
    moved: dict[str, str] = field(default_factory=dict)
    #: Paths held for a person to resolve: a racing edit, a delete, or a file too large to copy.
    held: list[str] = field(default_factory=list)
    #: Paths that carried no change.
    unchanged: int = 0
    #: A path the importer left alone, with the reason: a refusal of the file system or the store.
    unjudged: dict[str, str] = field(default_factory=dict)

    @property
    def settled(self) -> int:
        return len(self.imported) + len(self.moved) + len(self.held)


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


class Importer:
    """Settles a store-mode vault's outside changes: ``import_paths`` and ``sweep`` are the job."""

    def __init__(self, vault_root: Path, *, run: Runner | None = None) -> None:
        self._root = Path(vault_root)
        self._run = run or projector.store_thread_runner(self._root)
        self._refresh_lock = threading.Lock()
        self._refresh: set[str] = set()

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
        a rename only when the old file is gone and the new file holds the old page's exact bytes.
        """
        report = ImportReport()
        with self._filesystem() as filesystem:
            self._settle(filesystem, sorted(set(rels)), dict(moves or {}), report)
        return report

    def sweep(self) -> ImportReport:
        """Hash every file that the store owns and settle what differs from the store's records.

        The hash is the only test, so an edit that keeps a file's size and modification time is
        caught as any other. A stored path with no file is judged missing only when the walk that
        looked for it was whole: a folder or file that the walk could not read leaves its paths as
        they are.
        """
        report = ImportReport()
        skipped: list[ownership.Skipped] = []
        with self._filesystem() as filesystem:
            known = self._known_hashes()
            seen: set[str] = set()
            differing: list[str] = []
            for owned in ownership.iter_owned(self._root, on_skip=skipped.append):
                seen.add(owned.rel)
                disk = self._observe(filesystem, owned.rel, report)
                if disk is not None and disk.sha256 not in known.get(owned.rel, ()):
                    differing.append(owned.rel)
            missing = [rel for rel in known if rel not in seen and not _below_any(rel, skipped)]
            self._settle(filesystem, sorted({*differing, *missing}), {}, report)
            self._sweep_unowned(filesystem, report)
        return report

    def take_refresh(self) -> list[str]:
        """The unowned Markdown paths whose lexical rows are stale, in order; clears the list."""
        with self._refresh_lock:
            taken, self._refresh = sorted(self._refresh), set()
        return taken

    # -- reading the disk -------------------------------------------------------------------------

    @contextmanager
    def _filesystem(self) -> Iterator[held_fs.HeldFilesystem]:
        with held_fs.acquire(projector.anchor(self._root)).require() as filesystem:
            yield filesystem

    def _observe(
        self,
        filesystem: held_fs.HeldFilesystem,
        rel: str,
        report: ImportReport,
        *,
        keep_body: bool = False,
    ) -> OnDisk | None:
        """The file at ``rel``, or None when it is missing or the file system will not show it.

        A refusal (a symlink, an unreadable folder) is recorded and reads as no change, never as a
        deleted file: ``unjudged`` keeps it visible, and the path is looked at again next time.
        """
        try:
            return projector.observe(filesystem, rel, keep_body=keep_body)
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
        for rename in self._apply_renames(renames, report):
            singles.extend((rename.src, rename.dst))
        for chunk in _chunks(sorted(singles), APPLY_BATCH):
            units: list[tuple[str, OnDisk | None]] = []
            for rel in chunk:
                # The bytes are read only for a change, which an import or a hold then keeps.
                disk = self._observe(filesystem, rel, report, keep_body=True)
                if rel not in report.unjudged:
                    units.append((rel, disk))
            self._apply(units, report)

    def _apply_renames(self, renames: list[_Rename], report: ImportReport) -> list[_Rename]:
        """Commit each rename; return the ones that the store refused."""
        refused: list[_Rename] = []
        for chunk in _chunks(renames, APPLY_BATCH):

            def work(writer: CollectionWriter, chunk: list[_Rename] = chunk) -> None:
                for rename in chunk:
                    if self._commit_rename(writer, rename):
                        report.moved[rename.dst] = rename.src
                    else:
                        refused.append(rename)

            self._run(work)
        return refused

    def _commit_rename(self, writer: CollectionWriter, rename: _Rename) -> bool:
        source = projector.read_facts(self._root, rename.src)
        if source.manifest is None:
            return False
        change = write.StoreChange(
            write.Origin.OUTSIDE_EDITOR,
            "an outside rename",
            moves=(write.PageMove(rename.src, rename.dst, on_disk=True),),
            expect={
                rename.src: write.Expect(revision=source.manifest.revision, projected=True),
                rename.dst: write.Expect(absent=True),
            },
        )
        try:
            write.commit(writer, change)
        except write.StoreConflict:
            return False
        return True

    def _apply(self, units: list[tuple[str, OnDisk | None]], report: ImportReport) -> None:
        def work(writer: CollectionWriter) -> None:
            for rel, disk in units:
                self._settle_one(writer, rel, disk, report)

        if units:
            self._run(work)

    def _settle_one(
        self, writer: CollectionWriter, rel: str, disk: OnDisk | None, report: ImportReport
    ) -> None:
        """Decide one path inside the store thread's job, from facts read now: commit or hold."""
        facts = projector.read_facts(self._root, rel)
        verdict = judge(facts, disk)
        if verdict is Verdict.OWN:
            report.unchanged += 1
            return
        page = ownership.classify(rel) is ownership.Ownership.PAGE
        if verdict is Verdict.GONE:
            recorded = facts.manifest.sha256 if facts.manifest else facts.stored_sha256
            assert recorded is not None
            self._hold(writer, rel, "delete", facts, recorded, None, report)
            return
        assert disk is not None
        keepable = disk.body is not None or not page
        if facts.held or not keepable or (not page and facts.intent is not None):
            # A held path stays held, and a file too large to copy has no bytes to import.
            self._hold(writer, rel, "edit", facts, disk.sha256, disk.body, report)
            return
        change = self._import_change(rel, page, facts, disk)
        try:
            write.commit(writer, change)
        except write.StoreConflict:
            self._hold(writer, rel, "edit", facts, disk.sha256, disk.body, report)
        except OpError as refusal:
            if refusal.code != write.NOT_OWNED:
                raise
            report.unjudged[rel] = refusal.code
        else:
            report.imported.append(rel)

    @staticmethod
    def _import_change(rel: str, page: bool, facts: Facts, disk: OnDisk) -> write.StoreChange:
        reason = "an outside change"
        if not page:
            expect = (
                write.Expect(absent=True)
                if facts.binary is None
                else write.Expect(revision=facts.binary.revision, sha256=facts.binary.sha256)
            )
            record = write.BinaryRecord(rel, disk.sha256, disk.size)
            return write.StoreChange(
                write.Origin.OUTSIDE_EDITOR, reason, binaries=(record,), expect={rel: expect}
            )
        assert disk.body is not None
        base = facts.manifest or facts.page
        # One test says whether the store has moved since the file was projected: the commit's own.
        expect = (
            write.Expect(absent=True)
            if base is None
            else write.Expect(revision=base.revision, projected=True)
        )
        put = write.PagePut(rel, disk.body, on_disk_mtime_ns=disk.mtime_ns)
        return write.StoreChange(
            write.Origin.OUTSIDE_EDITOR, reason, puts=(put,), expect={rel: expect}
        )

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

    def _judge_unowned(
        self, filesystem: held_fs.HeldFilesystem, rel: str, report: ImportReport
    ) -> None:
        """Note a Markdown file outside the store's ownership whose lexical rows are stale.

        The store does not hold such a file's bytes, so nothing is imported: the page's derived rows
        are refreshed from the file. A file with no row is noted only when the corpus walk reaches
        it.
        """
        # A file extension is a closed set that the format fixes: it names the format, not meaning.
        if not rel.lower().endswith(".md"):
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
        if disk is None or row is None or disk.sha256 != row[0]:
            self._note_refresh(rel)

    def _sweep_unowned(self, filesystem: held_fs.HeldFilesystem, report: ImportReport) -> None:
        """Compare each lexical row of an unowned Markdown file with the file's hash."""
        with read.snapshot(self._root) as view:
            rows = view.connection.execute(
                "SELECT path, content_hash FROM pages WHERE content_hash IS NOT NULL"
            ).fetchall()
        for rel, content_hash in rows:
            owned = ownership.classify(rel) is not ownership.Ownership.NONE
            # A file extension is a closed set that the format fixes; it names no meaning.
            if owned or not rel.lower().endswith(".md"):
                continue
            disk = self._observe(filesystem, rel, report)
            if rel not in report.unjudged and (disk is None or disk.sha256 != content_hash):
                self._note_refresh(rel)

    def _note_refresh(self, rel: str) -> None:
        with self._refresh_lock:
            self._refresh.add(rel)


def _below_any(rel: str, skipped: list[ownership.Skipped]) -> bool:
    """Whether a walk left ``rel`` unread: skipped, or under a folder that it could not read."""
    return any(rel == found.rel or rel.startswith(found.rel + "/") for found in skipped)


def _chunks(items: list[_Item], size: int) -> Iterator[list[_Item]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _pair_renames(looks: Mapping[str, _Look], hints: Mapping[str, str]) -> list[_Rename]:
    """The renames among a batch: an old page that is gone and a new page with its exact bytes.

    A move event names its pair. Without one, a gone page and a new file pair only when each is the
    only one in the batch with those bytes, so that two copies of one page cannot swap identities.
    """
    gone = {
        rel: look
        for rel, look in looks.items()
        if look.verdict is Verdict.GONE and look.facts.page is not None
    }
    created = {
        rel: look
        for rel, look in looks.items()
        if look.verdict is Verdict.ADOPT
        and look.facts.page is None
        and look.facts.manifest is None
        and not look.facts.held
        and ownership.classify(rel) is ownership.Ownership.PAGE
    }

    def same_bytes(src: str, dst: str) -> bool:
        stored = gone[src].facts.page
        disk = created[dst].disk
        return stored is not None and disk is not None and stored.sha256 == disk.sha256

    pairs: dict[str, str] = {}
    for dst, src in hints.items():
        if dst in created and src in gone and src not in pairs.values() and same_bytes(src, dst):
            pairs[dst] = src
    by_stored: dict[str, list[str]] = defaultdict(list)
    for rel, look in gone.items():
        if rel not in pairs.values() and look.facts.page is not None:
            by_stored[look.facts.page.sha256].append(rel)
    by_disk: dict[str, list[str]] = defaultdict(list)
    for rel, look in created.items():
        if rel not in pairs and look.disk is not None:
            by_disk[look.disk.sha256].append(rel)
    for sha256, sources in by_stored.items():
        targets = by_disk.get(sha256, [])
        if len(sources) == 1 and len(targets) == 1:
            pairs[targets[0]] = sources[0]
    return [
        _Rename(src, dst, disk)
        for dst, src in sorted(pairs.items())
        if (disk := created[dst].disk) is not None
    ]
