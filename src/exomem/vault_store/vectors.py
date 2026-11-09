"""Vectors for a store-mode vault: computed after commit from the store's page bytes.

OpenSpec make-vault-store-canonical, task 6.1 (design D2, D7). In store mode the Markdown on disk is
a projection that may lag the store, so nothing here reads it: the drain takes each changed page's
exact bytes from a store snapshot, and the vector sidecar records which bytes (and which store
revision) each page's rows were computed from, in the transaction that writes the rows. A reader
compares that stamp with ``store_pages.sha256``: a page whose vectors do not match is left out of
the vector lane for that query, and the lexical and graph lanes still serve it. The drain runs after
the commit and off the store transaction, so a kill between the two leaves the sidecar behind the
store, never ahead of it.

Change detection reads the store's audit rows past the sidecar's cursor, never a file event or a
file time. The cursor is ``(revision, audit seq)``: the audit trail has no index by revision, and
its ``seq`` is the key that a range scan follows. A migration or a rollback writes one audit row
with no path, which says that every page's life restarts. A sidecar with no cursor has never caught
up, and one whose cursor lies past the end of the audit trail belongs to another store: all three
make the drain compare every stored page with the sidecar. That census judges only the pages that
the store owns; the sidecar's rows for other files (collection layers, dot-folders, the vault
outside the Knowledge Base) are not the store's to keep or to delete.

The cursor moves only after every page that its audit rows name is stamped, purged or owed. A page
whose attempt failed is owed: its stamp says so, readers withhold it, and a turn with nothing newer
retries it.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .. import index_paths, kbdir, recall_policy, reserved_paths, runtime_resources, vault
from ..embedding_index import EmbeddingIndex, StoreStamp
from ..find_corpus import walk_name_admits
from . import mode, ownership, read

if TYPE_CHECKING:
    from ..embeddings import ParentRows

log = logging.getLogger(__name__)

#: What one turn does at most: audit rows read, pages embedded, pages published together, owed pages
#: retried, and paths purged together. They bound a turn, not what the drain reaches: the next turn
#: continues from what the sidecar already holds. A retry is small because a page that keeps failing
#: costs a full attempt each time.
AUDIT_ROWS_PER_TURN = 256
PAGES_PER_TURN = 32
PAGES_PER_PUBLICATION = 8
OWED_RETRIES_PER_TURN = 4
PURGES_PER_CALL = 512


@dataclass(frozen=True, slots=True)
class Withheld:
    """The pages whose vector rows a reader must leave out, or all if the store cannot say."""

    paths: frozenset[str] = frozenset()
    everything: bool = False

    def __contains__(self, path: object) -> bool:
        return self.everything or path in self.paths

    def __bool__(self) -> bool:
        return self.everything or bool(self.paths)


NOTHING_WITHHELD = Withheld()

#: Per vault, why `withheld` last found the mode or the store unreadable: it logs when this changes.
_UNREADABLE: dict[str, str] = {}


def in_store_mode(vault_root: Path) -> bool:
    """Whether this vault's vectors follow the store, as ``mode.store_mode_for_reads`` says.

    A mode record that cannot be read does not stop a vault that the store itself says is not in
    store mode (its mode row is absent or says ``file``): it runs as a file-mode vault. Where the
    store says ``store`` or cannot be read either, the mode is unknown and the first error stands.
    """
    try:
        return mode.store_mode_for_reads(vault_root)
    except Exception:  # noqa: BLE001 - re-raised unless the store's own row rules store mode out
        try:
            recorded = mode._stored_mode(vault_root)
        except Exception:  # noqa: BLE001 - an unreadable store leaves the mode unknown
            recorded = mode.STORE_MODE
        if recorded == mode.STORE_MODE:
            raise
        return False


def withheld(vault_root: Path, index: EmbeddingIndex) -> Withheld:
    """The pages whose vectors the store does not vouch for; nothing when the vault is in file mode.

    A page is proven when its stamp, written with its rows, names the bytes that the store holds
    now. The cursor says which pages can have changed since the sidecar last caught up, so only
    those, and the owed ones, are compared; where the cursor cannot be followed (`_followed_seq`)
    every owned page that the sidecar holds rows for is. A mode or a store that cannot be read
    withholds everything: an unknown state is never proof.
    """
    key = os.fspath(vault_root)
    try:
        found = _withheld(vault_root, index) if in_store_mode(vault_root) else NOTHING_WITHHELD
    except Exception as error:  # noqa: BLE001 - an unreadable store withholds, not breaks, a query
        reason = f"{type(error).__name__}: {error}"
        if _UNREADABLE.get(key) != reason:  # a query per second must not log per second
            _UNREADABLE[key] = reason
            log.warning(
                "store-mode vectors withheld: the mode or the store was unreadable (%s)", reason
            )
        return Withheld(everything=True)
    _UNREADABLE.pop(key, None)
    return found


def _withheld(vault_root: Path, index: EmbeddingIndex) -> Withheld:
    if not index.path.exists():
        return NOTHING_WITHHELD  # no sidecar, no rows to serve
    with index.store_record() as record, read.snapshot(vault_root) as view:
        cursor = record.cursor()
        seq = _followed_seq(view, cursor)
        if seq is None:
            stored = {meta.path: meta.sha256 for meta in view.pages()}
            candidates = {path for path in record.row_paths() if owned(path)}
            stamps = record.stamps()
        else:
            candidates = {
                path
                for (path,) in view.connection.execute(
                    "SELECT DISTINCT path FROM store_audit WHERE seq > ? AND path IS NOT NULL",
                    (seq,),
                )
            }
            stored = {
                path: meta.sha256
                for path in candidates
                if (meta := view.page_meta(path)) is not None
            }
            stamps = record.stamps(candidates, with_owed=True)
            candidates |= {path for path, stamp in stamps.items() if stamp.owed}
    return Withheld(
        frozenset(path for path in candidates if not _proves(stamps.get(path), stored.get(path)))
    )


def _proves(stamp: StoreStamp | None, sha256: str | None) -> bool:
    """Whether the stamp shows that the page's rows were computed from the bytes with this hash."""
    return stamp is not None and not stamp.owed and sha256 is not None and stamp.sha256 == sha256


def owned(path: str) -> bool:
    """Whether the store owns the page at `path`; its rows for other files are not the drain's."""
    return ownership.classify(path) is ownership.Ownership.PAGE


def _audit_head(view: read.StoreSnapshot) -> int:
    return view.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM store_audit").fetchone()[0]


def _followed_seq(view: read.StoreSnapshot, cursor: tuple[int, int] | None) -> int | None:
    """The audit seq to read past, or None when the trail cannot say what changed (a census).

    The trail cannot say with no cursor, with a cursor beyond the end of the trail (one that
    another or an older store left), and after a migration or a rollback, whose audit row has no
    path. Every page is then compared with the sidecar.
    """
    if cursor is None or cursor[1] > _audit_head(view):
        return None
    restarted = view.connection.execute(
        "SELECT 1 FROM store_audit WHERE seq > ? AND path IS NULL LIMIT 1", (cursor[1],)
    ).fetchone()
    return None if restarted is not None else cursor[1]


def embeddable(rel: str) -> bool:
    """Whether the embedding walk would reach the stored page `rel`, judged by its path alone.

    The walk (`index_paths.iter_index_markdown`) also looks at each file on disk, and a stored
    page may have no file yet, so this keeps the walk's path tests: the Markdown suffix, the pages
    it skips by name, the excluded folders of the index scope and the reserved registry. Content
    admission (the access policy, a Records manifest) is judged on the page's bytes by the drain.
    """
    if not index_paths.is_embeddable_path(Path(rel)) or vault.in_excluded_scan_dir(rel):
        return False
    if index_paths.index_scope() == "vault":
        inside = rel
    else:
        prefix = kbdir.kb_prefix()
        if not rel.startswith(prefix):
            return False
        inside = rel[len(prefix) :]
        *folders, name = inside.split("/")
        if not walk_name_admits(name, directory=False) or not all(
            walk_name_admits(folder, directory=True) for folder in folders
        ):
            return False
    parts = inside.split("/")
    # The walk prunes a blocked folder, so a page below one is unreachable whatever its own name.
    return not any(
        reserved_paths.classify_logical("/".join(parts[: count + 1])).blocked
        for count in range(len(parts))
    )


@dataclass(frozen=True, slots=True)
class Drained:
    """What one `drain` call did. ``more`` says that the next call has work waiting."""

    embedded: int = 0
    purged: int = 0
    owed: int = 0
    more: bool = False
    #: Why nothing ran: the vector backend is off or cannot load. The work stays where it was.
    blocked: str | None = None

    @property
    def progressed(self) -> bool:
        return bool(self.embedded or self.purged)


@dataclass(slots=True)
class _Plan:
    """One turn's pages, read from one store snapshot and the sidecar's cursor."""

    #: Pages to (re)embed, in order.
    work: list[str] = field(default_factory=list)
    #: Pages the store no longer holds, or that no embedding walk reaches: their rows go.
    drop: list[str] = field(default_factory=list)
    #: A moved page's old paths. Their rows seed the new page's, so a move costs no encode.
    reuse: dict[str, list[str]] = field(default_factory=dict)
    #: Where the cursor goes once every page above is handled; None for a retry of owed pages.
    cursor: tuple[int, int] | None = None
    #: The audit read stopped at its limit, so more rows may follow the cursor.
    more_audit: bool = False
    #: A retry took as many owed pages as one turn may, so more may be owed.
    retry_full: bool = False


def needs_vectors(stamp: StoreStamp | None, sha256: str) -> bool:
    """Whether a page whose store bytes hash to `sha256` is owed new rows, given its stamp.

    An owed stamp on these very bytes waits for the retry turn: its attempt just failed, and a plan
    that took it again would never end.
    """
    return stamp is None or stamp.sha256 != sha256


def drain(
    vault_root: Path,
    *,
    stop: threading.Event | None = None,
    pages: int = PAGES_PER_TURN,
    publication: int = PAGES_PER_PUBLICATION,
    audit_rows: int = AUDIT_ROWS_PER_TURN,
) -> Drained:
    """Bring the vector sidecar of a store-mode vault one bounded step toward the store.

    Run it from the semantic drain, after the commit and never inside it. Each call plans from the
    sidecar's cursor, so a call that a kill or a stop cut short is resumed, not repeated: a page
    whose rows carry the stamp of the store's bytes is skipped, and a batch's rows commit with their
    stamps. Nothing here reads the projected Markdown: each page's bytes come from a store snapshot.
    """
    from .. import embeddings, foreground_priority

    with (
        foreground_priority.cancellable(stop),
        runtime_resources.model_work("bulk", cancel_event=stop),
    ):
        blocked = embeddings.backend_unavailable()
        if blocked is not None:
            return Drained(blocked=blocked)
        index = embeddings.get_embedding_index(vault_root)
        plan = _plan(vault_root, index, audit_rows)
        return _carry_out(vault_root, index, plan, stop, pages, publication)


def _plan(vault_root: Path, index: EmbeddingIndex, audit_rows: int) -> _Plan:
    cursor = index.store_cursor()
    with read.snapshot(vault_root) as view:
        seq = _followed_seq(view, cursor)
        if seq is None:
            return _census_plan(index, view)
        rows = view.connection.execute(
            "SELECT seq, revision, path, from_path FROM store_audit WHERE seq > ? "
            "ORDER BY seq LIMIT ?",
            (seq, audit_rows),
        ).fetchall()
        if not rows:
            return _retry_plan(index, view)
        plan = _Plan(cursor=(rows[-1][1], rows[-1][0]), more_audit=len(rows) == audit_rows)
        named: dict[str, None] = {}
        for _seq, _revision, path, from_path in rows:
            named[path] = None
            if from_path is not None:
                # The new path inherits what the old one had, itself moved or not.
                plan.reuse[path] = [from_path, *plan.reuse.pop(from_path, [])]
        stamps = index.store_stamps(named)
        _file_pages(plan, view, named, stamps)
        return plan


def _census_plan(index: EmbeddingIndex, view: read.StoreSnapshot) -> _Plan:
    """Every page against every stamp: the first pass, and the one after a migration or rollback."""
    plan = _Plan(cursor=(view.revision, _audit_head(view)))
    stored = {meta.path: meta for meta in view.pages()}
    _file_pages(plan, view, sorted(stored), index.store_stamps(), metas=stored)
    # A path that only the sidecar knows, and that the store would own (a page deleted before the
    # sidecar caught up), has no meta. The sidecar's rows for files the store does not own stay.
    plan.drop += sorted(path for path in index.row_paths() if path not in stored and owned(path))
    return plan


def _retry_plan(index: EmbeddingIndex, view: read.StoreSnapshot) -> _Plan:
    """A few of the owed pages that were tried least, when the sidecar is level with the store."""
    plan = _Plan()
    owed = index.store_owed_paths(OWED_RETRIES_PER_TURN)
    plan.retry_full = len(owed) == OWED_RETRIES_PER_TURN
    for path in owed:
        meta = view.page_meta(path)
        if meta is None or not embeddable(path):
            plan.drop.append(path)
        else:
            plan.work.append(path)
    return plan


def _file_pages(
    plan: _Plan,
    view: read.StoreSnapshot,
    paths: Iterable[str],
    stamps: dict[str, StoreStamp],
    *,
    metas: dict[str, read.PageMeta] | None = None,
) -> None:
    """File each path under work (owed vectors), drop (nothing to keep) or neither (current)."""
    for path in paths:
        meta = metas.get(path) if metas is not None else view.page_meta(path)
        if meta is None or not embeddable(path):
            plan.drop.append(path)
        elif needs_vectors(stamps.get(path), meta.sha256):
            plan.work.append(path)


def _carry_out(
    vault_root: Path,
    index: EmbeddingIndex,
    plan: _Plan,
    stop: threading.Event | None,
    pages: int,
    publication: int,
) -> Drained:
    embedded = owed = 0
    batch: list[tuple[str, StoreStamp, ParentRows]] = []
    handled = 0

    def flush() -> None:
        nonlocal embedded, owed
        if not batch:
            return
        try:
            index.upsert_batch(
                [rows.replacement for _rel, _stamp, rows in batch],
                [rows.unit for _rel, _stamp, rows in batch],
                identity=batch[0][2].producer,
                store_stamps={rel: stamp for rel, stamp, _rows in batch},
            )
            embedded += len(batch)
        except Exception as error:  # noqa: BLE001 - a failed batch is owed, not lost
            log.warning("store-mode vector batch failed: %s", error)
            index.record_store_stamps(
                {rel: stamp._replace(owed=True) for rel, stamp, _rows in batch}
            )
            owed += len(batch)
        batch.clear()

    gone: list[str] = []
    for rel in plan.work:
        if handled >= pages or (stop is not None and stop.is_set()):
            break
        handled += 1
        result = _prepare(vault_root, index, rel, plan.reuse.get(rel, ()))
        if result is None:
            gone.append(rel)
            continue
        stamp, rows = result
        if rows is None and stamp.owed:
            index.record_store_stamps({rel: stamp})
            owed += 1
        elif rows is None:
            # Not admitted to recall: no rows, and a stamp so that the page is not examined again.
            index.purge_paths_if_present([rel])
            index.record_store_stamps({rel: stamp})
        else:
            if batch and batch[0][2].producer != rows.producer:
                flush()
            batch.append((rel, stamp, rows))
            if len(batch) >= publication:
                flush()
    flush()
    finished = handled == len(plan.work)
    purged = 0
    if finished:
        # After the work, so that a moved page's new rows could take what the old path's rows held.
        drop = plan.drop + gone
        for start in range(0, len(drop), PURGES_PER_CALL):
            purged += index.purge_paths_if_present(drop[start : start + PURGES_PER_CALL])
        if plan.cursor is not None:
            index.set_store_cursor(*plan.cursor)
    return Drained(
        embedded=embedded,
        purged=purged,
        owed=owed,
        more=not finished or plan.more_audit or (plan.retry_full and bool(embedded or purged)),
    )


def _prepare(
    vault_root: Path, index: EmbeddingIndex, rel: str, reuse_from: Sequence[str]
) -> tuple[StoreStamp, ParentRows | None] | None:
    """One page's rows from its stored bytes, and the stamp that goes with them.

    None means that the store no longer holds the page. Rows None with an owed stamp means that the
    attempt failed on these bytes; with a plain stamp, that recall policy does not admit them.
    """
    from .. import embeddings

    with read.snapshot(vault_root) as view:
        meta = view.page_meta(rel)
        revision = view.revision
    if meta is None:
        return None
    stamp = StoreStamp(meta.sha256, revision)
    try:
        with runtime_resources.parser_preparation(meta.size) as preparation:
            preparation.reserve_source(meta.size)
            with read.snapshot(vault_root) as view:
                page = view.page(rel)
                revision = view.revision
            if page is None:
                return None
            if len(page.body) > meta.size:
                preparation.reserve_source(len(page.body))
            stamp = StoreStamp(page.meta.sha256, revision)
            if not recall_policy.admits_markdown_content(vault_root, rel, page.body):
                return stamp, None
            preparation.hold(page.body)
            rows = embeddings.parent_rows_from_source(
                vault_root,
                index,
                rel,
                page.body,
                mtime=page.meta.mtime_ns / 1e9,
                preparation=preparation,
                reuse_from=reuse_from,
            )
    except Exception as error:  # noqa: BLE001 - a page that fails is owed, not skipped
        log.warning("store-mode vectors for %s are owed: %s", rel, error)
        return stamp._replace(owed=True), None
    return stamp, rows
