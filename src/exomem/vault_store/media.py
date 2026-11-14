"""The store-mode media writer (OpenSpec make-vault-store-canonical, tasks 6.4 and 6.5).

A media binary stays a file. Its derived rows are the sidecar page that ``media_processing`` writes
beside it, with that page's lexical and graph rows. A store-mode vault commits a page only on the
store thread, so the sidecar commit hops there (``on_store_thread``) and the reconciliation around
it, including the hash of the whole binary, stays on the importer's thread, as the importer's own
reads do.

``derive`` reconciles the media binaries that an outside change just recorded. A reconciliation that
fails is named in the report, and ``recover`` is the sweep's bounded pass that finds what a failure
or a kill left without its rows, as the file-mode watcher's periodic pass does.

The media store evicts an original's bytes and restores them under its own mutation guard. Those two
changes of the file are not outside edits, so ``managed`` asks ``media_store.managed_event``, the
answer that the file-mode watcher already acts on, before the importer reads a missing file as a
delete.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .. import held_fs, media_processing, media_store, reserved_paths
from ..collection_store import preview, runtime
from . import mode, ownership, projector

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

_Result = TypeVar("_Result")

#: What ``managed`` answers for a missing file when the media store cannot be read: the importer
#: cannot tell an eviction from a delete, so it settles nothing and the next sweep looks again.
UNREADABLE = "unreadable"

#: How long a media commit waits, on its own thread, for a busy vault boundary before it gives up
#: with ``projector.BoundaryBusy``. Another background holder keeps the boundary for one unit of
#: work, and the store thread itself never waits for it (``projector._holding_boundary``).
BUSY_WAIT_SECONDS = 5.0
BUSY_POLL_SECONDS = 0.05

#: What ``derive`` reports for a binary whose commit met a busy boundary; ``recover`` retries it.
BOUNDARY_BUSY = "BOUNDARY_BUSY"


def governed(rel: str) -> bool:
    """Whether media processing derives rows for ``rel``: a media binary that the store owns.

    ``media_processing.classify_media`` is the one list of media kinds. The store owns only the
    Knowledge Base, which is also all that the file-mode watcher reconciles.
    """
    return (
        ownership.classify(rel) is ownership.Ownership.BINARY
        and media_processing.classify_media(rel) is not None
    )


def off_store_thread(root: Path) -> bool:
    """Whether a store-mode vault's media sidecar commit starts on a thread that must hop.

    The store's own server thread never hops: a caller there that has dropped its writer binding
    would queue behind itself, so it keeps the refusal that a commit gives it.
    """
    root = Path(root).resolve()
    if not mode.store_mode_active(root) or preview.bound_writer(root) is not None:
        return False
    server = runtime._SERVERS.get(root)
    return server is None or server.thread is not threading.current_thread()


def on_store_thread(
    root: Path, work: Callable[[], _Result], *, operation: str = "vault_store_media_commit"
) -> _Result:
    """Run ``work`` on the store thread, inside the vault mutation guard, and return its result.

    The media worker's result arrives on its own thread, and the importer reconciles on its own.
    Their sidecar commit takes this door instead of committing from there, which the store refuses.
    A caller that would hold the boundary around the commit puts its whole guarded unit in ``work``:
    the store thread holds the boundary for it, and a hop from a thread that holds one raises
    ``projector.HopUnderBoundary``. A busy boundary is waited out here, on the caller's thread, for
    up to ``BUSY_WAIT_SECONDS``; nothing of ``work`` has run when it raises ``BoundaryBusy``.
    """
    outcome: list[_Result] = []

    def job(_writer: CollectionWriter) -> None:
        outcome.append(work())

    run = projector.store_thread_runner(root, operation=operation)
    deadline = time.monotonic() + BUSY_WAIT_SECONDS
    while True:
        try:
            run(job)
        except projector.BoundaryBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(BUSY_POLL_SECONDS)
            continue
        return outcome[0]


def holding_boundary(
    root: Path,
    operation: str,
    unit: Callable[[], _Result],
    guard: Callable[[], AbstractContextManager[object]],
) -> _Result:
    """Run ``unit`` holding the vault mutation boundary and return what it returns.

    For a background writer whose unit commits a sidecar. In a store-mode vault that commit runs
    on the store thread, which must hold the boundary itself (``projector.HopUnderBoundary``), so
    off that thread the whole unit runs there (``on_store_thread``); a boundary that stays busy
    raises ``projector.BoundaryBusy`` with nothing of the unit run. Otherwise this thread takes
    ``guard()``, the writer's own mutation guard, as file mode does.
    """
    if off_store_thread(root):
        return on_store_thread(root, unit, operation=operation)
    with guard():
        return unit()


def derive(root: Path, rels: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """Reconcile each governed media binary: the paths done, and the failures with their codes.

    A failure never stops the next path. It is returned and the sweep's ``recover`` pass tries the
    path again. The sidecar commit hops to the store thread, one commit per path, so a governed
    write runs between two of them.
    """
    derived: list[str] = []
    failed: dict[str, str] = {}
    for rel in dict.fromkeys(rels):
        if not governed(rel):
            continue
        try:
            outcome = media_processing.reconcile_media(root, rel, explicit=False)
        except projector.BoundaryBusy:
            failed[rel] = BOUNDARY_BUSY  # ordinary contention: the sweep's recover pass retries it
        except Exception as error:  # noqa: BLE001 - one unreadable binary must not stop the others
            failed[rel] = getattr(error, "code", type(error).__name__)
            log.warning("media derivation failed for %s", rel, exc_info=True)
        else:
            if outcome is not None:
                derived.append(rel)
    return derived, failed


def after_removal(root: Path, rels: Iterable[str]) -> None:
    """Drop what lives outside the store for binaries that left ``rels`` (design D6).

    A store commit carries a page's lexical, graph and identity rows. An image's or a video's CLIP
    rows, a recording's speech progress and a video's scene frames live outside it, so a writer
    that moved or trashed a binary drops them after its commit, as file mode's fan-out does. Each
    step is best-effort: the commit is durable, and reconcile finds what a failure leaves.
    """
    from .. import embeddings, index_sync

    removed = list(dict.fromkeys(rels))
    try:
        embeddings.delete_clip_after_remove(root, removed)
    except Exception:  # noqa: BLE001 - the derived rows follow a durable commit
        log.warning("CLIP cleanup failed for %s", removed, exc_info=True)
    index_sync.drop_media_residue(root, removed)


def recover(root: Path) -> int:
    """One bounded, rotating pass that reconciles the media binaries still without their rows.

    It is ``media_processing.reconcile_all_media`` with the limit of the file-mode watcher's pass.
    The number it returns is the binaries it attempted: it logs each failure and goes on.
    """
    return media_processing.reconcile_all_media(
        root, limit=media_processing.DEFAULT_RECONCILE_LIMIT
    )


def managed(root: Path, rel: str) -> str | None:
    """``DEFER`` or ``SUPPRESS`` when the media store itself is changing or has changed ``rel``.

    ``SUPPRESS`` holds only for a missing file: an eviction removed it. A file that is there and
    differs from its record is a change that the importer records, whatever the media store's cache
    holds, so a copy that took an evicted original's record never keeps the restored original from
    getting a record of its own. A restore that matches the record is judged unchanged before this
    is asked.

    ``UNREADABLE`` answers a missing file when the media store cannot be read: the importer cannot
    tell an eviction from a delete. A present file answers None then, so the importer reads it
    itself and records it or reports what refused it, as it does without a media store.
    """
    if ownership.classify(rel) is not ownership.Ownership.BINARY:
        return None
    path = root / rel
    try:
        answer = media_store.managed_event(root, path)
    except (
        sqlite3.Error,
        OSError,
        ValueError,
        held_fs.HeldFsError,
        reserved_paths.ReservedPathLeafError,
    ):
        log.warning("media store state unreadable for %s", rel, exc_info=True)
        return None if os.path.lexists(path) else UNREADABLE
    if answer == media_store.SUPPRESS and os.path.lexists(path):
        return None
    return answer
